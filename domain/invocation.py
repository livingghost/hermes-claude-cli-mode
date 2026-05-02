"""Invocation data passed to Claude CLI subprocess runners."""

from __future__ import annotations

from typing import Any


class _ClaudeCliInvocation:
    def __init__(
        self,
        *,
        args: list[str],
        env: dict[str, str],
        stdin_text: str,
        cleanup_callbacks: list[Any],
        full_prompt: str,
        latest_user_only: bool,
        request_fingerprint: str,
        parent_session_key: str,
        cli_session_id: str,
        state_generation: int,
        no_output_timeout_seconds: float,
    ) -> None:
        self.args = args
        self.env = env
        self.stdin_text = stdin_text
        self._cleanup_callbacks = cleanup_callbacks
        self.full_prompt = full_prompt
        self.latest_user_only = latest_user_only
        self.request_fingerprint = request_fingerprint
        self.parent_session_key = parent_session_key
        self.cli_session_id = cli_session_id
        self.state_generation = state_generation
        self.no_output_timeout_seconds = no_output_timeout_seconds

    def close(self) -> None:
        callbacks = list(reversed(self._cleanup_callbacks))
        self._cleanup_callbacks = []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def detach_cleanup_callbacks(self) -> list[Any]:
        callbacks = self._cleanup_callbacks
        self._cleanup_callbacks = []
        return callbacks
