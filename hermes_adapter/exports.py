"""Export surface used by the Hermes hook entrypoint."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

from domain import config, constants, invocation, state
from features import anthropic_client
from hermes_adapter import auxiliary, hook, prompt_guidance, runtime_selection
from infrastructure import claude_cli, content, live_session, mcp_bridge, streaming

from .hook import bootstrap, handle

_EXPORT_MODULES: tuple[ModuleType, ...] = (
    constants,
    state,
    config,
    invocation,
    claude_cli,
    content,
    live_session,
    mcp_bridge,
    streaming,
    anthropic_client,
    auxiliary,
    prompt_guidance,
    runtime_selection,
    hook,
)


def _export_to(namespace: dict[str, Any]) -> None:
    namespace["domain"] = importlib.import_module("domain")
    namespace["features"] = importlib.import_module("features")
    namespace["hermes_adapter"] = importlib.import_module("hermes_adapter")
    namespace["infrastructure"] = importlib.import_module("infrastructure")
    for module in _EXPORT_MODULES:
        namespace[module.__name__.rsplit(".", 1)[-1]] = module
        for name in dir(module):
            if name.startswith("__"):
                continue
            namespace[name] = getattr(module, name)


__all__ = ["bootstrap", "handle", "_export_to"]
