"""Runtime configuration parsing for the Hermes Claude CLI mode hook."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    CONFIG_KEY,
    EXTRA_ARG_CONTROLLED_SWITCH_FLAGS,
    EXTRA_ARG_CONTROLLED_VALUE_FLAGS,
    LIVE_SESSION_IDLE_TIMEOUT_SECONDS,
)

class TransportConfig:
    __slots__ = (
        "command",
        "config_dir",
        "timeout_seconds",
        "live_session",
        "live_idle_timeout_seconds",
        "extra_args",
        "include_partial_messages",
        "include_hook_events",
        "replay_user_messages",
        "system_prompt_mode",
        "setting_sources",
        "settings",
        "agent",
        "agents",
        "tools",
        "disallowed_tools",
        "permission_mode",
        "permission_prompt_tool",
        "effort",
        "thinking",
        "fast_mode",
        "max_turns",
        "max_budget_usd",
        "fallback_model",
        "json_schema",
        "session_mode",
        "session_name",
        "no_session_persistence",
        "mcp_enabled",
        "claude_code_mcp_enabled",
        "max_mcp_output_tokens",
        "mcp_tool_result_char_limit",
        "mcp_execute_code_timeout_seconds",
        "add_dirs",
        "plugin_dirs",
        "debug_filter",
        "debug_file",
        "disable_slash_commands",
        "exclude_dynamic_system_prompt_sections",
    )

    def __init__(
        self,
        *,
        command: str = "claude",
        config_dir: str = "/opt/data/.claude",
        timeout_seconds: float = 600.0,
        live_session: bool = True,
        live_idle_timeout_seconds: float = LIVE_SESSION_IDLE_TIMEOUT_SECONDS,
        extra_args: tuple[str, ...] = (),
        include_partial_messages: bool = True,
        include_hook_events: bool = False,
        replay_user_messages: bool = False,
        system_prompt_mode: str = "append",
        setting_sources: str = "user",
        settings: str = "",
        agent: str = "",
        agents: str = "",
        tools: str = "",
        disallowed_tools: str = "",
        permission_mode: str = "",
        permission_prompt_tool: str = "",
        effort: str = "",
        thinking: str = "",
        fast_mode: bool = False,
        max_turns: int = 0,
        max_budget_usd: float = 0.0,
        fallback_model: str = "",
        json_schema: str = "",
        session_mode: str = "session-id",
        session_name: str = "",
        no_session_persistence: bool = False,
        mcp_enabled: bool = True,
        claude_code_mcp_enabled: bool = True,
        max_mcp_output_tokens: int = 0,
        mcp_tool_result_char_limit: int = 0,
        mcp_execute_code_timeout_seconds: float = 45.0,
        add_dirs: tuple[str, ...] = (),
        plugin_dirs: tuple[str, ...] = (),
        debug_filter: str = "",
        debug_file: str = "",
        disable_slash_commands: bool = False,
        exclude_dynamic_system_prompt_sections: bool = False,
    ) -> None:
        self.command = _coerce_str(command, "claude")
        self.config_dir = _coerce_str(config_dir, "/opt/data/.claude")
        self.timeout_seconds = _coerce_float(timeout_seconds, 600.0)
        self.live_session = _coerce_bool(live_session, default=True)
        self.live_idle_timeout_seconds = _coerce_float(
            live_idle_timeout_seconds,
            LIVE_SESSION_IDLE_TIMEOUT_SECONDS,
        )
        self.extra_args = _coerce_extra_args(extra_args)
        self.include_partial_messages = _coerce_bool(include_partial_messages, default=True)
        self.include_hook_events = _coerce_bool(include_hook_events, default=False)
        self.replay_user_messages = _coerce_bool(replay_user_messages, default=False)
        self.system_prompt_mode = _coerce_choice(
            system_prompt_mode,
            default="append",
            choices={"append", "replace"},
        )
        self.setting_sources = _coerce_optional_str(setting_sources) or "user"
        self.settings = _coerce_json_arg_value(settings)
        self.agent = _coerce_optional_str(agent)
        self.agents = _coerce_json_arg_value(agents)
        self.tools = _coerce_cli_list_arg_value(tools)
        self.disallowed_tools = _coerce_cli_list_arg_value(disallowed_tools)
        self.permission_mode = _coerce_choice(
            permission_mode,
            default="",
            choices={"", "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"},
        )
        self.permission_prompt_tool = _coerce_optional_str(permission_prompt_tool)
        self.effort = _coerce_choice(
            effort,
            default="",
            choices={"", "low", "medium", "high", "xhigh", "max"},
        )
        self.thinking = _coerce_thinking(thinking)
        self.fast_mode = _coerce_bool(fast_mode, default=False)
        self.max_turns = _coerce_int(max_turns, 0)
        self.max_budget_usd = _coerce_float(max_budget_usd, 0.0)
        self.fallback_model = _coerce_optional_str(fallback_model)
        self.json_schema = _coerce_json_arg_value(json_schema)
        self.session_mode = _coerce_choice(
            session_mode,
            default="session-id",
            choices={"off", "resume", "session-id"},
        )
        self.session_name = _coerce_optional_str(session_name)
        self.no_session_persistence = _coerce_bool(no_session_persistence, default=False)
        self.mcp_enabled = _coerce_bool(mcp_enabled, default=True)
        self.claude_code_mcp_enabled = _coerce_bool(claude_code_mcp_enabled, default=True)
        self.max_mcp_output_tokens = _coerce_int(max_mcp_output_tokens, 0)
        self.mcp_tool_result_char_limit = _coerce_int(mcp_tool_result_char_limit, 0)
        self.mcp_execute_code_timeout_seconds = _coerce_float(
            mcp_execute_code_timeout_seconds,
            45.0,
        )
        self.add_dirs = _coerce_str_tuple(add_dirs)
        self.plugin_dirs = _coerce_str_tuple(plugin_dirs)
        self.debug_filter = _coerce_optional_str(debug_filter)
        self.debug_file = _coerce_optional_str(debug_file)
        self.disable_slash_commands = _coerce_bool(disable_slash_commands, default=False)
        self.exclude_dynamic_system_prompt_sections = _coerce_bool(
            exclude_dynamic_system_prompt_sections,
            default=False,
        )


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        raw = os.environ.get("HERMES_HOME", "").strip()
        return Path(raw) if raw else Path.home() / ".hermes"


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def _coerce_str(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _coerce_optional_str(value: Any) -> str:
    return _coerce_str(value, "")


def _coerce_choice(value: Any, *, default: str, choices: set[str]) -> str:
    parsed = _coerce_str(value, default).strip()
    return parsed if parsed in choices else default


def _coerce_cli_list_arg_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list | tuple):
        items = [str(item).strip() for item in value if str(item).strip()]
        return ",".join(items)
    return ""


def _coerce_json_arg_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True)
    return ""


def _coerce_thinking(value: Any) -> str:
    if isinstance(value, str):
        raw = value.strip()
        if raw in {"adaptive", "enabled", "disabled"}:
            return raw
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except Exception:
                return ""
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=True)
        return ""
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True)
    return ""


def _coerce_extra_args(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    args: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            args.append(item.strip())
    return _normalize_extra_args(args)


def _matches_controlled_value_arg(arg: str) -> bool:
    return any(
        arg.startswith(f"{flag}=") or arg.startswith(f"{flag} ")
        for flag in EXTRA_ARG_CONTROLLED_VALUE_FLAGS
    )


def _matches_controlled_switch_arg(arg: str) -> bool:
    return any(
        arg == flag or arg.startswith(f"{flag}=") or arg.startswith(f"{flag} ")
        for flag in EXTRA_ARG_CONTROLLED_SWITCH_FLAGS
    )


def _normalize_extra_args(args: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    index = 0
    values = list(args)
    while index < len(values):
        arg = values[index]
        if arg in EXTRA_ARG_CONTROLLED_VALUE_FLAGS:
            index += 1
            while index < len(values) and not values[index].startswith("-"):
                index += 1
            continue
        if _matches_controlled_value_arg(arg) or _matches_controlled_switch_arg(arg):
            index += 1
            continue
        normalized.append(arg)
        index += 1
    return tuple(normalized)


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    if not isinstance(value, list | tuple):
        return ()
    items: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            items.append(item.strip())
    return tuple(items)


_CONFIG = TransportConfig()


def _load_runtime_config() -> TransportConfig:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
        config_path = _hermes_home() / "config.yaml"
        try:
            import yaml

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            cfg = {}

    raw = cfg.get(CONFIG_KEY) if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        raw = {}

    return TransportConfig(
        command=_coerce_str(raw.get("command"), "claude"),
        config_dir=_coerce_str(raw.get("config_dir"), "/opt/data/.claude"),
        timeout_seconds=_coerce_float(raw.get("timeout_seconds"), 600.0),
        live_session=_coerce_bool(raw.get("live_session"), default=True),
        live_idle_timeout_seconds=_coerce_float(
            raw.get("live_idle_timeout_seconds"),
            LIVE_SESSION_IDLE_TIMEOUT_SECONDS,
        ),
        extra_args=_coerce_extra_args(raw.get("extra_args")),
        include_partial_messages=_coerce_bool(raw.get("include_partial_messages"), default=True),
        include_hook_events=_coerce_bool(raw.get("include_hook_events"), default=False),
        replay_user_messages=_coerce_bool(raw.get("replay_user_messages"), default=False),
        system_prompt_mode=_coerce_choice(
            raw.get("system_prompt_mode"),
            default="append",
            choices={"append", "replace"},
        ),
        setting_sources=_coerce_optional_str(raw.get("setting_sources")) or "user",
        settings=_coerce_json_arg_value(raw.get("settings")),
        agent=_coerce_optional_str(raw.get("agent")),
        agents=_coerce_json_arg_value(raw.get("agents")),
        tools=_coerce_cli_list_arg_value(raw.get("tools")),
        disallowed_tools=_coerce_cli_list_arg_value(raw.get("disallowed_tools")),
        permission_mode=_coerce_choice(
            raw.get("permission_mode"),
            default="",
            choices={"", "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"},
        ),
        permission_prompt_tool=_coerce_optional_str(raw.get("permission_prompt_tool")),
        effort=_coerce_choice(
            raw.get("effort"),
            default="",
            choices={"", "low", "medium", "high", "xhigh", "max"},
        ),
        thinking=_coerce_thinking(raw.get("thinking")),
        fast_mode=raw.get("fast_mode"),
        max_turns=_coerce_int(raw.get("max_turns"), 0),
        max_budget_usd=_coerce_float(raw.get("max_budget_usd"), 0.0),
        fallback_model=_coerce_optional_str(raw.get("fallback_model")),
        json_schema=_coerce_json_arg_value(raw.get("json_schema")),
        session_mode=_coerce_choice(
            raw.get("session_mode"),
            default="session-id",
            choices={"off", "resume", "session-id"},
        ),
        session_name=_coerce_optional_str(raw.get("session_name")),
        no_session_persistence=_coerce_bool(raw.get("no_session_persistence"), default=False),
        mcp_enabled=_coerce_bool(raw.get("mcp_enabled"), default=True),
        claude_code_mcp_enabled=_coerce_bool(raw.get("claude_code_mcp_enabled"), default=True),
        max_mcp_output_tokens=_coerce_int(raw.get("max_mcp_output_tokens"), 0),
        mcp_tool_result_char_limit=_coerce_int(raw.get("mcp_tool_result_char_limit"), 0),
        mcp_execute_code_timeout_seconds=_coerce_float(
            raw.get("mcp_execute_code_timeout_seconds"),
            45.0,
        ),
        add_dirs=_coerce_str_tuple(raw.get("add_dirs")),
        plugin_dirs=_coerce_str_tuple(raw.get("plugin_dirs")),
        debug_filter=_coerce_optional_str(raw.get("debug_filter")),
        debug_file=_coerce_optional_str(raw.get("debug_file")),
        disable_slash_commands=_coerce_bool(raw.get("disable_slash_commands"), default=False),
        exclude_dynamic_system_prompt_sections=_coerce_bool(
            raw.get("exclude_dynamic_system_prompt_sections"),
            default=False,
        ),
    )


def _reload_config() -> bool:
    global _CONFIG
    _CONFIG = _load_runtime_config()
    return True
