"""Hermes runtime selection helpers for Claude CLI mode."""

from __future__ import annotations

import inspect
from typing import Any

from features.anthropic_client import ClaudeCliAnthropicClient
from domain.constants import ANTHROPIC_API_BASE_URL
from domain.state import _CLAUDE_CLI_MODE_REQUESTED, _CURRENT_AGENT

def _is_cli_api_mode(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "cli"


def _call_argument_position(callable_obj: Any, name: str) -> int | None:
    try:
        parameters = list(inspect.signature(callable_obj).parameters)
    except Exception:
        return None
    try:
        index = parameters.index(name)
    except ValueError:
        return None
    # Wrappers receive *args without the leading self parameter.
    return index - 1


def _call_argument(callable_obj: Any, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if name in kwargs:
        return kwargs[name]
    position = _call_argument_position(callable_obj, name)
    if position is not None and 0 <= position < len(args):
        return args[position]
    return None


def _replace_call_argument(
    callable_obj: Any,
    name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    value: Any,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    next_args = list(args)
    next_kwargs = dict(kwargs)
    if name in next_kwargs:
        next_kwargs[name] = value
        return tuple(next_args), next_kwargs
    position = _call_argument_position(callable_obj, name)
    if position is not None and 0 <= position < len(next_args):
        next_args[position] = value
    else:
        next_kwargs[name] = value
    return tuple(next_args), next_kwargs


def _config_requests_claude_cli_mode(provider: Any) -> bool:
    provider_text = str(provider or "").strip().lower()
    if provider_text and provider_text != "anthropic":
        return False
    try:
        from hermes_cli.config import load_config

        model_cfg = load_config().get("model")
    except Exception:
        return False
    if not isinstance(model_cfg, dict):
        return False
    if not _is_cli_api_mode(model_cfg.get("api_mode")):
        return False
    configured_provider = str(model_cfg.get("provider") or "").strip().lower()
    return not configured_provider or configured_provider == "anthropic"


def _build_claude_cli_runtime(requested_provider: Any, target_model: Any = None) -> dict[str, Any]:
    return {
        "provider": "anthropic",
        "api_mode": "anthropic_messages",
        "base_url": ANTHROPIC_API_BASE_URL,
        "api_key": "claude-cli",
        "source": "claude-cli",
        "requested_provider": (str(requested_provider).strip().lower() if requested_provider else "anthropic"),
        "target_model": target_model,
        "claude_cli_mode_requested": True,
    }


def _agent_requests_claude_cli_mode(agent: Any) -> bool:
    return bool(
        _CLAUDE_CLI_MODE_REQUESTED.get()
        or getattr(agent, "_claude_cli_mode_requested", False)
    )


def _mark_claude_cli_client_state(agent: Any) -> None:
    enabled = isinstance(getattr(agent, "_anthropic_client", None), ClaudeCliAnthropicClient)
    setattr(agent, "_claude_cli_enabled", enabled)
    primary_runtime = getattr(agent, "_primary_runtime", None)
    if isinstance(primary_runtime, dict):
        if getattr(agent, "_claude_cli_mode_requested", False):
            primary_runtime["claude_cli_mode_requested"] = True
        else:
            primary_runtime.pop("claude_cli_mode_requested", None)


def _agent_uses_claude_cli(agent: Any) -> bool:
    if agent is None:
        return False
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    return provider == "anthropic" and _agent_requests_claude_cli_mode(agent)


def _run_with_claude_cli_agent_context(agent: Any, requested: bool, callback: Any, *args: Any, **kwargs: Any) -> Any:
    token_agent = _CURRENT_AGENT.set(agent)
    token_requested = _CLAUDE_CLI_MODE_REQUESTED.set(requested)
    try:
        return callback(*args, **kwargs)
    finally:
        _CLAUDE_CLI_MODE_REQUESTED.reset(token_requested)
        _CURRENT_AGENT.reset(token_agent)


def _primary_runtime_requests_claude_cli(agent: Any) -> bool:
    primary_runtime = getattr(agent, "_primary_runtime", None)
    return bool(
        getattr(agent, "_claude_cli_mode_requested", False)
        or (
            isinstance(primary_runtime, dict)
            and primary_runtime.get("claude_cli_mode_requested") is True
        )
    )


def _init_requests_claude_cli_mode(original_init: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    requested_api_mode = _call_argument(original_init, "api_mode", args, kwargs)
    if _is_cli_api_mode(requested_api_mode):
        return True
    requested_provider = _call_argument(original_init, "provider", args, kwargs)
    return _config_requests_claude_cli_mode(requested_provider)


def _switch_requests_claude_cli_mode(agent: Any, original_switch: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    requested_api_mode = _call_argument(original_switch, "api_mode", args, kwargs)
    if _is_cli_api_mode(requested_api_mode):
        return True
    requested_provider = str(_call_argument(original_switch, "new_provider", args, kwargs) or "").strip().lower()
    if requested_provider != "anthropic":
        return False
    if getattr(agent, "_claude_cli_mode_requested", False):
        return True
    return _config_requests_claude_cli_mode(requested_provider)


def _coerce_cli_mode_to_anthropic_messages(
    callable_obj: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if not _is_cli_api_mode(_call_argument(callable_obj, "api_mode", args, kwargs)):
        return args, kwargs
    return _replace_call_argument(callable_obj, "api_mode", args, kwargs, "anthropic_messages")
