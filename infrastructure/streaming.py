"""Claude CLI streaming output adapters for the Anthropic-compatible API."""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from types import SimpleNamespace
from typing import Any, Iterable

from domain.constants import HOOK_NAME
from domain.invocation import _ClaudeCliInvocation
from .live_session import _ClaudeCliLiveSession
from .claude_cli import (
    _append_cli_diagnostics,
    _make_message,
    _read_model_id,
    _read_session_id,
    _read_usage,
    _redact_error,
    _update_cli_diagnostics,
    _unwrap_nested_result,
)

logger = logging.getLogger(__name__)


def _to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _to_namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value

class _ClaudeCliStream:
    def __init__(self, client: "ClaudeCliAnthropicClient", api_kwargs: dict[str, Any]):
        self._client = client
        self._api_kwargs = api_kwargs
        self._message: Any = None
        self._invocation: _ClaudeCliInvocation | None = None
        self._live_session: _ClaudeCliLiveSession | None = None
        self._process: subprocess.Popen[str] | None = None
        self._stderr_parts: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._text_parts: list[str] = []
        self._usage: dict[str, int] = {}
        self._session_id = ""
        self._model_id = ""
        self._diagnostics: dict[str, Any] = {}
        self._error: BaseException | None = None
        self._invocation_lock_acquired = False

    def __enter__(self) -> "_ClaudeCliStream":
        try:
            self._client.acquire_invocation_lock()
            self._invocation_lock_acquired = True
            self._invocation = self._client.prepare_invocation(self._api_kwargs, force_streaming=True)
            live_session = self._client.live_session_for_invocation(self._invocation)
            if live_session is not None:
                self._live_session = live_session
                return self
            self._process = subprocess.Popen(
                self._invocation.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._invocation.env,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            self._cleanup()
            raise RuntimeError(
                "Claude CLI executable was not found. Install @anthropic-ai/claude-code "
                "inside the Hermes runtime or set claude_cli.command."
            ) from exc
        except Exception:
            self._cleanup()
            raise

        self._client.register_process(self._process)
        try:
            self._start_stderr_collector()
            assert self._process.stdin is not None
            self._process.stdin.write(self._invocation.stdin_text)
            self._process.stdin.close()
            return self
        except Exception:
            self._cleanup()
            raise

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._cleanup()
        return None

    def __iter__(self) -> Iterable[Any]:
        return self._iter_events()

    def get_final_message(self) -> Any:
        if (
            self._message is None
            and self._error is None
            and (self._process is not None or self._live_session is not None)
        ):
            for _ in self._iter_events():
                pass
        if self._error is not None:
            raise self._error
        return self._message

    def _start_stderr_collector(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return

        def collect() -> None:
            try:
                for chunk in process.stderr:
                    self._stderr_parts.append(chunk)
            except Exception:
                pass

        thread = threading.Thread(
            target=collect,
            name="hermes-claude-cli-stderr",
            daemon=True,
        )
        thread.start()
        self._stderr_thread = thread

    def _iter_events(self) -> Iterable[Any]:
        if self._live_session is not None:
            return self._live_event_generator()
        if self._process is None:
            return iter(())
        return self._event_generator()

    def _live_event_generator(self) -> Iterable[Any]:
        assert self._live_session is not None
        assert self._invocation is not None
        try:
            for event in self._live_session.iter_turn(self._invocation, self._handle_stdout_line):
                yield event
            if not self._message:
                self._message = _make_message(
                    text=_unwrap_nested_result("".join(self._text_parts)).strip(),
                    model=self._response_model(),
                    usage=self._usage,
                )
        except Exception as exc:
            self._error = self._interrupted_error_if_invalidated() or exc
            raise self._error
        finally:
            self._cleanup()

    def _event_generator(self) -> Iterable[Any]:
        process = self._process
        assert process is not None
        stdout_thread: threading.Thread | None = None
        try:
            if process.stdout is None:
                return
            stdout_queue: queue.Queue[Any] = queue.Queue()
            stdout_done = object()

            def collect_stdout() -> None:
                try:
                    assert process.stdout is not None
                    for raw_line in process.stdout:
                        stdout_queue.put(raw_line)
                except Exception as exc:
                    stdout_queue.put(exc)
                finally:
                    stdout_queue.put(stdout_done)

            stdout_thread = threading.Thread(
                target=collect_stdout,
                name="hermes-claude-cli-stdout",
                daemon=True,
            )
            stdout_thread.start()
            deadline = (
                time.monotonic() + self._client.config.timeout_seconds
                if self._client.config.timeout_seconds > 0
                else None
            )
            assert self._invocation is not None
            no_output_timeout = self._invocation.no_output_timeout_seconds
            last_output_at = time.monotonic()

            def abort_if_invalidated() -> None:
                interrupted = self._interrupted_error_if_invalidated()
                if interrupted is None:
                    return
                self._error = interrupted
                try:
                    if process.poll() is None:
                        process.kill()
                except Exception:
                    pass
                try:
                    process.wait(timeout=5)
                except Exception:
                    pass
                raise interrupted

            while True:
                abort_if_invalidated()
                wait_seconds = 0.25
                now = time.monotonic()
                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        try:
                            process.kill()
                        except Exception:
                            pass
                        try:
                            process.wait(timeout=5)
                        except Exception:
                            pass
                        if stdout_thread is not None:
                            stdout_thread.join(timeout=1)
                        self._error = RuntimeError(
                            f"Claude CLI streaming timed out after {self._client.config.timeout_seconds:.0f}s"
                        )
                        raise self._error
                    wait_seconds = min(wait_seconds, max(remaining, 0.001))
                if no_output_timeout > 0:
                    remaining_output = no_output_timeout - (now - last_output_at)
                    if remaining_output <= 0:
                        try:
                            process.kill()
                        except Exception:
                            pass
                        try:
                            process.wait(timeout=5)
                        except Exception:
                            pass
                        if stdout_thread is not None:
                            stdout_thread.join(timeout=1)
                        self._error = RuntimeError(
                            f"Claude CLI produced no output for {no_output_timeout:.0f}s"
                        )
                        raise self._error
                    wait_seconds = min(wait_seconds, max(remaining_output, 0.001))
                try:
                    queued = stdout_queue.get(timeout=wait_seconds)
                except queue.Empty:
                    abort_if_invalidated()
                    continue
                if queued is stdout_done:
                    break
                if isinstance(queued, Exception):
                    self._error = RuntimeError(f"Claude CLI stdout reader failed: {queued}")
                    raise self._error
                last_output_at = time.monotonic()
                raw_line = str(queued)
                line = raw_line.strip()
                if not line:
                    continue
                event = self._handle_stdout_line(line)
                if event is not None:
                    yield event

            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=5)
            if stdout_thread is not None:
                stdout_thread.join(timeout=1)
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=1)
            stderr = "".join(self._stderr_parts)
            abort_if_invalidated()
            if returncode != 0:
                detail = _redact_error(stderr.strip())
                self._error = RuntimeError(f"Claude CLI failed with exit code {returncode}: {detail}")
                raise self._error
            if not self._message:
                self._message = _make_message(
                    text=_unwrap_nested_result("".join(self._text_parts)).strip(),
                    model=self._response_model(),
                    usage=self._usage,
                )
            assert self._invocation is not None
            self._client.record_invocation_success(self._session_id, self._invocation)
            if not self._text_parts and stderr.strip():
                logger.debug("%s: Claude CLI stderr: %s", HOOK_NAME, _redact_error(stderr.strip()))
        finally:
            self._cleanup()

    def _handle_stdout_line(self, line: str) -> Any | None:
        try:
            parsed = json.loads(line)
        except Exception:
            self._text_parts.append(line)
            return SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text=line),
            )
        if not isinstance(parsed, dict):
            return None
        if parsed.get("type") == "user":
            return None
        _update_cli_diagnostics(parsed, self._diagnostics)

        self._usage = _read_usage(parsed) or self._usage
        self._session_id = _read_session_id(parsed) or self._session_id
        self._model_id = _read_model_id(parsed) or self._model_id

        if parsed.get("type") == "result" and isinstance(parsed.get("result"), str):
            final_text = _unwrap_nested_result(parsed["result"]).strip()
            if parsed.get("is_error") is True:
                detail = _append_cli_diagnostics(final_text or "unknown error", self._diagnostics)
                raise RuntimeError(f"Claude CLI failed: {_redact_error(detail)}")
            self._message = _make_message(
                text=final_text,
                model=self._response_model(),
                usage=self._usage,
            )
            return None

        if parsed.get("type") == "stream_event":
            event = parsed.get("event")
            if not isinstance(event, dict):
                return None
            if event.get("type") == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    delta_text = delta.get("text")
                    if isinstance(delta_text, str):
                        self._text_parts.append(delta_text)
            return _to_namespace(event)

        if parsed.get("type") in {
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        }:
            return _to_namespace(parsed)
        return None

    def _response_model(self) -> str:
        return self._model_id or self._client.current_model(self._api_kwargs)

    def _interrupted_error_if_invalidated(self) -> InterruptedError | None:
        if self._client.invocation_was_invalidated(self._invocation):
            return InterruptedError("Claude CLI invocation was interrupted or invalidated")
        return None

    def _cleanup(self) -> None:
        process = self._process
        if process is not None:
            self._client.unregister_process(process)
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass
        self._process = None
        if self._live_session is not None and self._message is None and self._error is None:
            self._live_session.close("stream-cleanup")
        self._live_session = None
        if self._invocation is not None:
            self._invocation.close()
            self._invocation = None
        if self._invocation_lock_acquired:
            self._invocation_lock_acquired = False
            self._client.release_invocation_lock()


class _MessagesAPI:
    def __init__(self, client: "ClaudeCliAnthropicClient"):
        self._client = client

    def create(self, **api_kwargs: Any) -> Any:
        return self._client.create_message(api_kwargs)

    def stream(self, **api_kwargs: Any) -> _ClaudeCliStream:
        return _ClaudeCliStream(self._client, api_kwargs)
