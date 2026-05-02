"""Anthropic-compatible client backed by the Claude CLI subprocess."""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

from domain.config import TransportConfig
from domain.constants import (
    CLI_INPUT_FORMAT,
    CLI_OUTPUT_FORMAT,
    HOOK_NAME,
    MCP_PERMISSION_PROMPT_ALLOWED_TOOL,
)
from infrastructure.content import (
    _build_prompt,
    _build_stream_json_input,
    _image_cache_root,
    _stream_json_content,
    _system_prompt,
)
from domain.invocation import _ClaudeCliInvocation
from infrastructure.live_session import _ClaudeCliLiveSession
from infrastructure.mcp_bridge import _MCP_BRIDGE_MANAGER
from infrastructure.streaming import _MessagesAPI
from infrastructure.claude_cli import (
    _build_claude_cli_env,
    _claude_session_transcript_exists,
    _effective_max_turns,
    _ensure_live_session_capacity,
    _hash_json,
    _hash_text,
    _live_session_args,
    _live_session_fingerprint,
    _make_message,
    _normalize_claude_cli_model,
    _parse_cli_output,
    _preflight_cli,
    _redact_error,
    _register_client_parent_session,
    _remove_temp_file,
    _resolve_command,
    _resolve_no_output_timeout_seconds,
    _settings_arg_for_invocation,
    _unregister_client_parent_session,
    _upsert_arg_value,
    _uses_internal_permission_prompt,
    _write_temp_file,
    _write_temp_json,
)

logger = logging.getLogger(__name__)


class ClaudeCliAnthropicClient:
    def __init__(self, *, parent_agent: Any, config: TransportConfig):
        self.parent_agent = parent_agent
        self.config = config
        _preflight_cli(config)
        self.messages = _MessagesAPI(self)
        self.closed = False
        self._invocation_lock = threading.RLock()
        self._process_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._active_processes: set[subprocess.Popen[str]] = set()
        self._claude_session_id = ""
        self._claude_session_started = False
        self._session_epoch = 0
        self._state_generation = 0
        self._parent_session_key = self._current_parent_session_key()
        self._last_full_prompt = ""
        self._last_request_fingerprint = ""
        self._live_session: _ClaudeCliLiveSession | None = None
        _register_client_parent_session(self, self._parent_session_key)

    def close(self) -> None:
        self.closed = True
        self._close_live_session("client-close")
        _unregister_client_parent_session(self, self._parent_session_key)
        with self._process_lock:
            processes = list(self._active_processes)
        for process in processes:
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass

    def register_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._active_processes.add(process)

    def unregister_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._active_processes.discard(process)

    def acquire_invocation_lock(self) -> None:
        self._invocation_lock.acquire()

    def release_invocation_lock(self) -> None:
        self._invocation_lock.release()

    def _close_live_session(self, reason: str) -> None:
        live_session = self._live_session
        self._live_session = None
        if live_session is not None:
            live_session.close(reason)

    def _uses_live_session(self) -> bool:
        return bool(
            self.config.live_session
            and self.config.session_mode in {"resume", "session-id"}
            and CLI_INPUT_FORMAT == "stream-json"
            and CLI_OUTPUT_FORMAT == "stream-json"
        )

    def live_session_for_invocation(
        self,
        invocation: _ClaudeCliInvocation,
    ) -> _ClaudeCliLiveSession | None:
        if not self._uses_live_session():
            return None

        live_args = _live_session_args(invocation.args)
        fingerprint = _live_session_fingerprint(invocation, live_args)
        live_session = self._live_session
        if live_session is not None and not live_session.is_running():
            self._close_live_session("not-running")
            live_session = None
        if live_session is not None and not invocation.latest_user_only:
            self._close_live_session("full-prompt")
            live_session = None
        if live_session is not None and live_session.fingerprint != fingerprint:
            self._close_live_session("fingerprint-changed")
            live_session = None
        if live_session is None:
            if invocation.latest_user_only:
                return None
            _ensure_live_session_capacity()
            live_session = _ClaudeCliLiveSession(
                client=self,
                invocation=invocation,
                args=live_args,
                fingerprint=fingerprint,
            )
            self._live_session = live_session
        return live_session

    def record_invocation_success(self, session_id: str, invocation: _ClaudeCliInvocation) -> None:
        with self._state_lock:
            current_parent_session_key = self._current_parent_session_key()
            if current_parent_session_key != invocation.parent_session_key:
                logger.info(
                    "%s: ignored Claude CLI session state from stale Hermes session",
                    HOOK_NAME,
                )
                return
            self._sync_parent_session_key(current_parent_session_key)
            if invocation.state_generation != self._state_generation:
                logger.info(
                    "%s: ignored Claude CLI session state from invalidated invocation",
                    HOOK_NAME,
                )
                return
            if self.config.session_mode == "resume":
                if session_id:
                    self._claude_session_id = session_id
                    self._claude_session_started = True
            elif self.config.session_mode == "session-id":
                self._claude_session_id = session_id or invocation.cli_session_id
                self._claude_session_started = True
            self._last_full_prompt = invocation.full_prompt
            self._last_request_fingerprint = invocation.request_fingerprint

    def reset_cli_session_state(self) -> None:
        self.invalidate_cli_session(reason="agent-reset", bump_epoch=True)

    def invalidate_cli_session(self, *, reason: str, bump_epoch: bool) -> None:
        self._close_live_session(reason)
        with self._state_lock:
            if bump_epoch and self.config.session_mode == "session-id":
                self._session_epoch += 1
            self._state_generation += 1
            self._claude_session_id = ""
            self._claude_session_started = False
            self._last_full_prompt = ""
            self._last_request_fingerprint = ""
        logger.debug("%s: invalidated Claude CLI session state: %s", HOOK_NAME, reason)

    def current_model(self, api_kwargs: dict[str, Any]) -> str:
        return _normalize_claude_cli_model(api_kwargs.get("model") or getattr(self.parent_agent, "model", ""))

    def _current_parent_session_key(self) -> str:
        raw_session_id = str(getattr(self.parent_agent, "session_id", "") or "").strip()
        if not raw_session_id:
            raw_session_id = str(getattr(self.parent_agent, "_current_task_id", "") or "").strip()
        if not raw_session_id:
            raw_session_id = "default"
        return raw_session_id

    def _sync_parent_session_key(self, explicit_key: str | None = None) -> str:
        current = (explicit_key or self._current_parent_session_key()).strip() or "default"
        if current == self._parent_session_key:
            return current
        self._close_live_session("parent-session-changed")
        _unregister_client_parent_session(self, self._parent_session_key)
        self._parent_session_key = current
        _register_client_parent_session(self, current)
        self._session_epoch = 0
        self._state_generation += 1
        self._claude_session_id = ""
        self._claude_session_started = False
        self._last_full_prompt = ""
        self._last_request_fingerprint = ""
        logger.debug("%s: parent Hermes session changed; reset Claude CLI session state", HOOK_NAME)
        return current

    def _session_id_for_cli(self) -> str:
        raw_session_id = self._parent_session_key or self._current_parent_session_key()
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{HOOK_NAME}:{raw_session_id}:{self._session_epoch}"))

    def _ensure_unused_session_id_for_full_prompt(self) -> str:
        session_id = self._session_id_for_cli()
        attempts = 0
        while (
            not self._claude_session_started
            and _claude_session_transcript_exists(self.config.config_dir, session_id)
            and attempts < 100
        ):
            self._session_epoch += 1
            attempts += 1
            session_id = self._session_id_for_cli()
        if attempts:
            logger.info(
                "%s: skipped %s existing Claude CLI session id(s) before full-prompt start",
                HOOK_NAME,
                attempts,
            )
        return session_id

    def _request_fingerprint(self, api_kwargs: dict[str, Any], *, system_prompt: str, model: str) -> str:
        return _hash_json(
            {
                "system": _hash_text(system_prompt),
                "model": model,
                "hermes_tools": api_kwargs.get("tools"),
                "mcp_enabled": self.config.mcp_enabled,
                "claude_code_mcp_enabled": self.config.claude_code_mcp_enabled,
                "setting_sources": self.config.setting_sources,
                "settings": self.config.settings,
                "agent": self.config.agent,
                "agents": self.config.agents,
                "tools": self.config.tools,
                "disallowed_tools": self.config.disallowed_tools,
                "permission_mode": self.config.permission_mode,
                "permission_prompt_tool": self.config.permission_prompt_tool,
                "effort": self.config.effort,
                "thinking": self.config.thinking,
                "fast_mode": self.config.fast_mode,
                "include_hook_events": self.config.include_hook_events,
                "include_partial_messages": self.config.include_partial_messages,
                "replay_user_messages": self.config.replay_user_messages,
                "system_prompt_mode": self.config.system_prompt_mode,
                "max_turns": self.config.max_turns,
                "max_budget_usd": self.config.max_budget_usd,
                "fallback_model": self.config.fallback_model,
                "json_schema": self.config.json_schema,
                "session_name": self.config.session_name,
                "no_session_persistence": self.config.no_session_persistence,
                "max_mcp_output_tokens": self.config.max_mcp_output_tokens,
                "mcp_tool_result_char_limit": self.config.mcp_tool_result_char_limit,
                "add_dirs": self.config.add_dirs,
                "plugin_dirs": self.config.plugin_dirs,
                "debug_filter": self.config.debug_filter,
                "debug_file": self.config.debug_file,
                "disable_slash_commands": self.config.disable_slash_commands,
                "exclude_dynamic_system_prompt_sections": self.config.exclude_dynamic_system_prompt_sections,
                "extra_args": self.config.extra_args,
            }
        )

    def _use_latest_user_prompt_only(self, *, full_prompt: str, request_fingerprint: str) -> bool:
        if self.config.session_mode not in {"resume", "session-id"}:
            return False
        if not self._claude_session_started or not self._last_full_prompt:
            return False
        append_only = (
            request_fingerprint == self._last_request_fingerprint
            and len(full_prompt) > len(self._last_full_prompt)
            and full_prompt.startswith(self._last_full_prompt)
        )
        if append_only:
            return True
        if self.config.session_mode == "session-id":
            self._session_epoch += 1
        self._state_generation += 1
        self._claude_session_id = ""
        self._claude_session_started = False
        self._last_full_prompt = ""
        self._last_request_fingerprint = ""
        logger.info("%s: Claude CLI session invalidated because Hermes transcript is not append-only", HOOK_NAME)
        return False

    def prepare_invocation(
        self,
        api_kwargs: dict[str, Any],
        *,
        force_streaming: bool = False,
    ) -> _ClaudeCliInvocation:
        cleanup_callbacks: list[Any] = []
        image_root = _image_cache_root(self.config.config_dir)
        system_prompt = _system_prompt(api_kwargs, image_root=image_root)
        model = self.current_model(api_kwargs)
        full_prompt = _build_prompt(
            api_kwargs,
            include_system=False,
            latest_user_only=False,
            image_root=image_root,
        )
        request_fingerprint = self._request_fingerprint(
            api_kwargs,
            system_prompt=system_prompt,
            model=model,
        )
        with self._state_lock:
            parent_session_key = self._sync_parent_session_key()
            latest_user_only = self._use_latest_user_prompt_only(
                full_prompt=full_prompt,
                request_fingerprint=request_fingerprint,
            )
            cli_session_id = ""
            if self.config.session_mode == "session-id":
                cli_session_id = (
                    self._session_id_for_cli()
                    if latest_user_only
                    else self._ensure_unused_session_id_for_full_prompt()
                )
            state_generation = self._state_generation
        if latest_user_only:
            prompt = _build_prompt(
                api_kwargs,
                include_system=False,
                latest_user_only=True,
                image_root=image_root,
            )
        else:
            prompt = full_prompt
        if not prompt:
            raise RuntimeError("Claude CLI mode received an empty prompt")
        if not model:
            raise RuntimeError("Claude CLI mode received no model")

        command = _resolve_command(self.config)
        args = [
            command,
            "-p",
            "--model",
            model,
            "--output-format",
            CLI_OUTPUT_FORMAT,
            "--input-format",
            CLI_INPUT_FORMAT,
            "--verbose",
            "--no-chrome",
            "--tools",
            self.config.tools,
        ]

        if system_prompt and not latest_user_only:
            system_prompt_path = _write_temp_file(
                prefix="hermes-claude-cli-system-",
                suffix=".txt",
                text=system_prompt,
            )
            cleanup_callbacks.append(lambda path=system_prompt_path: _remove_temp_file(path))
            system_flag = (
                "--system-prompt-file"
                if self.config.system_prompt_mode == "replace"
                else "--append-system-prompt-file"
            )
            args.extend([system_flag, system_prompt_path])

        if self.config.replay_user_messages:
            args.append("--replay-user-messages")
        if self.config.include_partial_messages or force_streaming:
            args.append("--include-partial-messages")
        if self.config.include_hook_events:
            args.append("--include-hook-events")

        if self.config.setting_sources:
            args.extend(["--setting-sources", self.config.setting_sources])
        settings_arg = _settings_arg_for_invocation(self.config, cleanup_callbacks)
        if settings_arg:
            args.extend(["--settings", settings_arg])
        if self.config.agent:
            args.extend(["--agent", self.config.agent])
        if self.config.agents:
            args.extend(["--agents", self.config.agents])
        if self.config.permission_mode:
            args.extend(["--permission-mode", self.config.permission_mode])
        if self.config.permission_prompt_tool:
            args.extend(["--permission-prompt-tool", self.config.permission_prompt_tool])
        if self.config.disallowed_tools:
            args.extend(["--disallowedTools", self.config.disallowed_tools])
        if self.config.effort:
            args.extend(["--effort", self.config.effort])
        if self.config.add_dirs:
            args.append("--add-dir")
            args.extend(self.config.add_dirs)
        if self.config.exclude_dynamic_system_prompt_sections:
            args.append("--exclude-dynamic-system-prompt-sections")
        if self.config.disable_slash_commands:
            args.append("--disable-slash-commands")
        for plugin_dir in self.config.plugin_dirs:
            args.extend(["--plugin-dir", plugin_dir])
        if self.config.debug_filter:
            args.extend(["--debug", self.config.debug_filter])
        if self.config.debug_file:
            args.extend(["--debug-file", self.config.debug_file])
        max_turns = _effective_max_turns(self.config)
        if max_turns > 0:
            args.extend(["--max-turns", str(max_turns)])
        if self.config.max_budget_usd > 0:
            args.extend(["--max-budget-usd", str(self.config.max_budget_usd)])
        if self.config.fallback_model:
            args.extend(["--fallback-model", self.config.fallback_model])
        if self.config.json_schema:
            args.extend(["--json-schema", self.config.json_schema])
        if self.config.session_name:
            args.extend(["--name", self.config.session_name])
        if self.config.session_mode == "resume" and latest_user_only and self._claude_session_id:
            args.extend(["--resume", self._claude_session_id])
        elif self.config.session_mode == "session-id":
            args.extend(["--session-id", cli_session_id])
        elif self.config.no_session_persistence:
            args.append("--no-session-persistence")

        mcp_registration = _MCP_BRIDGE_MANAGER.register(
            parent_agent=self.parent_agent,
            api_kwargs=api_kwargs,
            config=self.config,
        )
        if mcp_registration is not None:
            cleanup_callbacks.append(mcp_registration.close)
            args.extend(["--mcp-config", mcp_registration.config_path])
            if mcp_registration.allowed_tools:
                args.extend(["--allowedTools", mcp_registration.allowed_tools])
            if _uses_internal_permission_prompt(self.config):
                args = _upsert_arg_value(
                    args,
                    "--permission-prompt-tool",
                    MCP_PERMISSION_PROMPT_ALLOWED_TOOL,
                )
            if not self.config.claude_code_mcp_enabled:
                args.append("--strict-mcp-config")
        elif not self.config.claude_code_mcp_enabled:
            mcp_config_path = _write_temp_json(
                prefix="hermes-claude-cli-strict-mcp-",
                value={"mcpServers": {}},
            )
            cleanup_callbacks.append(lambda path=mcp_config_path: _remove_temp_file(path))
            args.extend(["--mcp-config", mcp_config_path, "--strict-mcp-config"])

        args.extend(self.config.extra_args)

        env = _build_claude_cli_env(self.config)
        if self.config.max_mcp_output_tokens > 0:
            env["MAX_MCP_OUTPUT_TOKENS"] = str(self.config.max_mcp_output_tokens)

        stream_content = _stream_json_content(
            api_kwargs,
            latest_user_only=latest_user_only,
            fallback_prompt=prompt,
        )
        stdin_text = _build_stream_json_input(stream_content) if CLI_INPUT_FORMAT == "stream-json" else prompt
        return _ClaudeCliInvocation(
            args=args,
            env=env,
            stdin_text=stdin_text,
            cleanup_callbacks=cleanup_callbacks,
            full_prompt=full_prompt,
            latest_user_only=latest_user_only,
            request_fingerprint=request_fingerprint,
            parent_session_key=parent_session_key,
            cli_session_id=cli_session_id,
            state_generation=state_generation,
            no_output_timeout_seconds=_resolve_no_output_timeout_seconds(
                self.config.timeout_seconds,
                latest_user_only=latest_user_only,
            ),
        )

    def _run_invocation(self, invocation: _ClaudeCliInvocation) -> Any:
        try:
            process = subprocess.Popen(
                invocation.args,
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

        self.register_process(process)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        output_queue: queue.Queue[Any] = queue.Queue()
        stdout_done = object()
        stderr_done = object()

        def collect(pipe: Any, stream_name: str, done_marker: object) -> None:
            try:
                if pipe is not None:
                    for chunk in pipe:
                        output_queue.put((stream_name, chunk))
            except Exception as exc:
                output_queue.put(exc)
            finally:
                output_queue.put(done_marker)

        stdout_thread = threading.Thread(
            target=collect,
            args=(process.stdout, "stdout", stdout_done),
            name="hermes-claude-cli-run-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=collect,
            args=(process.stderr, "stderr", stderr_done),
            name="hermes-claude-cli-run-stderr",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            try:
                if process.stdin is not None:
                    process.stdin.write(invocation.stdin_text)
                    process.stdin.close()
            except BrokenPipeError:
                pass

            deadline = (
                time.monotonic() + self.config.timeout_seconds
                if self.config.timeout_seconds > 0
                else None
            )
            no_output_timeout = invocation.no_output_timeout_seconds
            last_output_at = time.monotonic()
            done_markers: set[object] = set()
            while True:
                if len(done_markers) == 2 and process.poll() is not None:
                    break
                wait_seconds = 0.25
                now = time.monotonic()
                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        try:
                            if process.poll() is None:
                                process.kill()
                        except Exception:
                            pass
                        try:
                            process.wait(timeout=5)
                        except Exception:
                            pass
                        raise RuntimeError(f"Claude CLI timed out after {self.config.timeout_seconds:.0f}s")
                    wait_seconds = min(wait_seconds, max(remaining, 0.001))
                if no_output_timeout > 0:
                    remaining_output = no_output_timeout - (now - last_output_at)
                    if remaining_output <= 0:
                        try:
                            if process.poll() is None:
                                process.kill()
                        except Exception:
                            pass
                        try:
                            process.wait(timeout=5)
                        except Exception:
                            pass
                        raise RuntimeError(f"Claude CLI produced no output for {no_output_timeout:.0f}s")
                    wait_seconds = min(wait_seconds, max(remaining_output, 0.001))
                try:
                    item = output_queue.get(timeout=wait_seconds)
                except queue.Empty:
                    continue
                if item is stdout_done or item is stderr_done:
                    done_markers.add(item)
                    continue
                if isinstance(item, Exception):
                    raise RuntimeError(f"Claude CLI output reader failed: {item}") from item
                stream_name, chunk = item
                last_output_at = time.monotonic()
                if stream_name == "stdout":
                    stdout_parts.append(str(chunk))
                else:
                    stderr_parts.append(str(chunk))

            returncode = process.wait(timeout=5)
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            return SimpleNamespace(
                returncode=returncode,
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts),
            )
        finally:
            self.unregister_process(process)

    def create_message(self, api_kwargs: dict[str, Any]) -> Any:
        if self.closed:
            raise RuntimeError("Claude CLI mode is closed")

        with self._invocation_lock:
            invocation = self.prepare_invocation(api_kwargs)
            recorded_by_live = False
            try:
                live_session = self.live_session_for_invocation(invocation)
                if live_session is not None:
                    stdout_lines: list[str] = []

                    def collect_line(line: str) -> None:
                        stdout_lines.append(line)
                        return None

                    for _ in live_session.iter_turn(invocation, collect_line):
                        pass
                    recorded_by_live = True
                    completed = SimpleNamespace(
                        returncode=0,
                        stdout="\n".join(stdout_lines),
                        stderr="",
                    )
                else:
                    completed = self._run_invocation(invocation)
            finally:
                invocation.close()

            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            if completed.returncode != 0:
                detail = _redact_error(stderr.strip() or stdout.strip())
                raise RuntimeError(f"Claude CLI failed with exit code {completed.returncode}: {detail}")

            text, usage, session_id, response_model = _parse_cli_output(stdout)
            if not recorded_by_live:
                self.record_invocation_success(session_id, invocation)
            if not text and stderr.strip():
                logger.debug("%s: Claude CLI stderr: %s", HOOK_NAME, _redact_error(stderr.strip()))
            return _make_message(text=text, model=response_model or self.current_model(api_kwargs), usage=usage)
