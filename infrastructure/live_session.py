"""Long-lived Claude CLI subprocess session management."""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from typing import Any, Iterable

from domain.constants import HOOK_NAME, LIVE_MAX_STDERR_CHARS, LIVE_MAX_TURN_OUTPUT_CHARS
from domain.invocation import _ClaudeCliInvocation
from .claude_cli import (
    _append_cli_diagnostics,
    _read_session_id,
    _redact_error,
    _register_live_session,
    _unregister_live_session,
    _update_cli_diagnostics,
    _unwrap_nested_result,
)

logger = logging.getLogger(__name__)


class _ClaudeCliLiveSession:
    def __init__(
        self,
        *,
        client: "ClaudeCliAnthropicClient",
        invocation: _ClaudeCliInvocation,
        args: list[str],
        fingerprint: str,
    ) -> None:
        self._client = client
        self.args = args
        self.fingerprint = fingerprint
        self._output_queue: queue.Queue[Any] = queue.Queue()
        self._stdout_done = object()
        self._stderr_done = object()
        self._stderr_parts: list[str] = []
        self._diagnostics: dict[str, Any] = {}
        self._cleanup_callbacks: list[Any] = []
        self._idle_timer: threading.Timer | None = None
        self._closed = False
        self._turn_active = False
        self._last_used_at = time.monotonic()
        self._lock = threading.RLock()

        try:
            self._process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=invocation.env,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Claude CLI executable was not found. Install @anthropic-ai/claude-code "
                "inside the Hermes runtime or set claude_cli.command."
            ) from exc

        self._cleanup_callbacks = invocation.detach_cleanup_callbacks()
        self._client.register_process(self._process)
        self._stdout_thread = self._start_collector(
            self._process.stdout,
            "stdout",
            self._stdout_done,
            "hermes-claude-cli-live-stdout",
        )
        self._stderr_thread = self._start_collector(
            self._process.stderr,
            "stderr",
            self._stderr_done,
            "hermes-claude-cli-live-stderr",
        )
        _register_live_session(self)
        logger.info("%s: started Claude CLI live session", HOOK_NAME)

    @property
    def turn_active(self) -> bool:
        with self._lock:
            return self._turn_active

    def is_running(self) -> bool:
        with self._lock:
            return not self._closed and self._process.poll() is None

    @property
    def last_used_at(self) -> float:
        with self._lock:
            return self._last_used_at

    def close(self, reason: str = "close") -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._turn_active = False
            timer = self._idle_timer
            self._idle_timer = None
            process = self._process
        if timer is not None:
            timer.cancel()
        logger.info("%s: closing Claude CLI live session: %s", HOOK_NAME, reason)
        _unregister_live_session(self)
        try:
            if getattr(self._client, "_live_session", None) is self:
                self._client._live_session = None
        except Exception:
            pass
        self._client.unregister_process(process)
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            pass
        try:
            if process.poll() is None:
                process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass
        callbacks = list(reversed(self._cleanup_callbacks))
        self._cleanup_callbacks = []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def iter_turn(
        self,
        invocation: _ClaudeCliInvocation,
        line_handler: Any,
    ) -> Iterable[Any]:
        with self._lock:
            if self._closed or self._process.poll() is not None:
                self._closed = True
                raise RuntimeError("Claude CLI live session is not running")
            if self._turn_active:
                raise RuntimeError("Claude CLI live session is already handling a turn")
            self._turn_active = True
            self._last_used_at = time.monotonic()
            timer = self._idle_timer
            self._idle_timer = None
        if timer is not None:
            timer.cancel()

        stdout_lines: list[str] = []
        stderr_start = len(self._stderr_parts)
        session_id = ""
        success = False
        try:
            def abort_if_invalidated() -> None:
                if not self._client.invocation_was_invalidated(invocation):
                    return
                self.close("invocation-invalidated")
                raise InterruptedError("Claude CLI invocation was interrupted or invalidated")

            abort_if_invalidated()
            self._write_input(invocation.stdin_text)
            deadline = (
                time.monotonic() + self._client.config.timeout_seconds
                if self._client.config.timeout_seconds > 0
                else None
            )
            no_output_timeout = invocation.no_output_timeout_seconds
            last_output_at = time.monotonic()
            while True:
                abort_if_invalidated()
                wait_seconds = 0.25
                now = time.monotonic()
                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        self.close("timeout")
                        raise RuntimeError(
                            f"Claude CLI live session timed out after "
                            f"{self._client.config.timeout_seconds:.0f}s"
                        )
                    wait_seconds = min(wait_seconds, max(remaining, 0.001))
                if no_output_timeout > 0:
                    remaining_output = no_output_timeout - (now - last_output_at)
                    if remaining_output <= 0:
                        self.close("no-output-timeout")
                        raise RuntimeError(
                            f"Claude CLI live session produced no output for {no_output_timeout:.0f}s"
                        )
                    wait_seconds = min(wait_seconds, max(remaining_output, 0.001))

                try:
                    item = self._output_queue.get(timeout=wait_seconds)
                except queue.Empty:
                    abort_if_invalidated()
                    if self._process.poll() is not None:
                        self.close("exited")
                        detail = _redact_error("".join(self._stderr_parts[stderr_start:]).strip())
                        raise RuntimeError(
                            "Claude CLI live session exited before completing the turn"
                            + (f": {detail}" if detail else "")
                        )
                    continue

                if item is self._stdout_done or item is self._stderr_done:
                    continue
                if isinstance(item, Exception):
                    self.close("reader-error")
                    raise RuntimeError(f"Claude CLI live session output reader failed: {item}") from item

                stream_name, chunk = item
                last_output_at = time.monotonic()
                if stream_name == "stderr":
                    self._stderr_parts.append(str(chunk))
                    if sum(len(part) for part in self._stderr_parts) > LIVE_MAX_STDERR_CHARS:
                        self.close("stderr-limit")
                        raise RuntimeError("Claude CLI live session stderr exceeded output limit")
                    continue

                raw_line = str(chunk).strip()
                if not raw_line:
                    continue
                stdout_lines.append(raw_line)
                if sum(len(line) + 1 for line in stdout_lines) > LIVE_MAX_TURN_OUTPUT_CHARS:
                    self.close("stdout-limit")
                    raise RuntimeError("Claude CLI live session turn output exceeded output limit")

                parsed: dict[str, Any] | None = None
                try:
                    loaded = json.loads(raw_line)
                    if isinstance(loaded, dict):
                        parsed = loaded
                        _update_cli_diagnostics(parsed, self._diagnostics)
                        session_id = _read_session_id(loaded) or session_id
                except Exception:
                    parsed = None

                event = line_handler(raw_line)
                if event is not None:
                    yield event

                if parsed is not None and parsed.get("type") == "result":
                    if parsed.get("is_error") is True:
                        detail = _redact_error(_unwrap_nested_result(str(parsed.get("result") or "")).strip())
                        detail = _append_cli_diagnostics(detail or "unknown error", self._diagnostics)
                        self.close("result-error")
                        raise RuntimeError(f"Claude CLI live session failed: {detail}")
                    success = True
                    break

            abort_if_invalidated()
            self._client.record_invocation_success(session_id, invocation)
            if not stdout_lines:
                stderr = "".join(self._stderr_parts[stderr_start:]).strip()
                if stderr:
                    logger.debug("%s: Claude CLI live stderr: %s", HOOK_NAME, _redact_error(stderr))
        except GeneratorExit:
            self.close("stream-closed")
            raise
        except Exception:
            if self.is_running():
                self.close("turn-error")
            raise
        finally:
            with self._lock:
                self._turn_active = False
                self._last_used_at = time.monotonic()
            if success and self.is_running():
                self._schedule_idle_close()

    def _write_input(self, stdin_text: str) -> None:
        process_stdin = self._process.stdin
        if process_stdin is None:
            raise RuntimeError("Claude CLI live session stdin is unavailable")
        try:
            process_stdin.write(stdin_text)
            process_stdin.flush()
        except BrokenPipeError as exc:
            self.close("broken-pipe")
            raise RuntimeError("Claude CLI live session stdin pipe closed") from exc

    def _schedule_idle_close(self) -> None:
        idle_seconds = self._client.config.live_idle_timeout_seconds
        if idle_seconds <= 0:
            return

        def close_idle() -> None:
            with self._lock:
                active = self._turn_active
            if not active:
                self.close("idle")

        timer = threading.Timer(idle_seconds, close_idle)
        timer.daemon = True
        with self._lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            self._idle_timer = timer
        timer.start()

    def _start_collector(
        self,
        pipe: Any,
        stream_name: str,
        done_marker: object,
        thread_name: str,
    ) -> threading.Thread:
        def collect() -> None:
            try:
                if pipe is not None:
                    for chunk in pipe:
                        self._output_queue.put((stream_name, chunk))
            except Exception as exc:
                self._output_queue.put(exc)
            finally:
                self._output_queue.put(done_marker)

        thread = threading.Thread(target=collect, name=thread_name, daemon=True)
        thread.start()
        return thread
