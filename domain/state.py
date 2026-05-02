"""Shared mutable state for the Hermes Claude CLI mode hook."""

from __future__ import annotations

import threading
import weakref
from contextvars import ContextVar
from typing import Any

_CURRENT_AGENT: ContextVar[Any | None] = ContextVar(
    "hermes_claude_cli_current_agent",
    default=None,
)
_CLAUDE_CLI_MODE_REQUESTED: ContextVar[bool] = ContextVar(
    "claude_cli_mode_requested",
    default=False,
)
_CLIENT_REGISTRY_LOCK = threading.RLock()
_CLIENTS_BY_PARENT_SESSION: dict[str, weakref.WeakSet[Any]] = {}
_LIVE_SESSIONS_LOCK = threading.RLock()
_LIVE_SESSIONS: list[Any] = []
