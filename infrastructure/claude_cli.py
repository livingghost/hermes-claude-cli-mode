"""CLI, settings, session, and response helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from domain.config import TransportConfig, _coerce_bool, _coerce_optional_str, _hermes_home
from domain.constants import (
    CLAUDE_CLI_CLEAR_ENV,
    CLAUDE_CLI_MODEL_ALIASES,
    CLI_INPUT_FORMAT,
    CLI_OUTPUT_FORMAT,
    FRESH_NO_OUTPUT_TIMEOUT,
    HOOK_NAME,
    LIVE_MAX_SESSIONS,
    LIVE_NORMALIZED_VALUE_FLAGS,
    LIVE_OMITTED_VALUE_FLAGS,
    LIVE_PROCESS_OMITTED_VALUE_FLAGS,
    RESUME_NO_OUTPUT_TIMEOUT,
)
from domain.state import (
    _CLIENT_REGISTRY_LOCK,
    _CLIENTS_BY_PARENT_SESSION,
    _LIVE_SESSIONS,
    _LIVE_SESSIONS_LOCK,
)

logger = logging.getLogger(__name__)

def _resolve_command(config: TransportConfig) -> str:
    command = config.command
    if os.path.isabs(command) or os.sep in command:
        return command

    search_path = os.environ.get("PATH", "")
    hermes_home = _hermes_home()
    candidates = [
        str(hermes_home / ".local" / "bin"),
        "/opt/data/.local/bin",
        "/usr/local/bin",
    ]
    path = os.pathsep.join([p for p in candidates + [search_path] if p])
    resolved = shutil.which(command, path=path)
    return resolved or command


def _build_claude_cli_env(config: TransportConfig) -> dict[str, str]:
    env = dict(os.environ)
    for key in CLAUDE_CLI_CLEAR_ENV:
        env.pop(key, None)
    env["CLAUDE_CONFIG_DIR"] = config.config_dir
    return env


def _parse_json_object_arg(value: str) -> dict[str, Any] | None:
    value = value.strip()
    if not value.startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_settings_arg_object(settings_arg: str) -> tuple[dict[str, Any], str]:
    parsed = _parse_json_object_arg(settings_arg)
    if parsed is not None:
        return parsed, "json"
    path = Path(os.path.expanduser(settings_arg))
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "claude_cli settings overrides require claude_cli.settings to be empty, a JSON object, "
            "or a readable JSON settings file"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("claude_cli.settings must resolve to a JSON object when claude_cli.thinking is set")
    return parsed, "path"


def _deep_merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_settings(existing, value)
        else:
            merged[key] = value
    return merged


def _optional_nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _thinking_config(config: TransportConfig) -> dict[str, Any]:
    raw = config.thinking
    if not raw:
        return {}
    if raw in {"adaptive", "enabled", "disabled"}:
        return {"type": raw}
    parsed = _parse_json_object_arg(raw)
    if parsed is None:
        return {}

    thinking: dict[str, Any] = {}
    raw_type = _coerce_optional_str(parsed.get("type"))
    if raw_type in {"adaptive", "enabled", "disabled"}:
        thinking["type"] = raw_type
    raw_display = _coerce_optional_str(parsed.get("display"))
    if raw_display in {"summarized", "omitted"}:
        thinking["display"] = raw_display
    budget = _optional_nonnegative_int(
        parsed.get("budget_tokens", parsed.get("budgetTokens"))
    )
    if budget is not None:
        thinking["budget_tokens"] = budget
    if "disable_adaptive" in parsed:
        thinking["disable_adaptive"] = _coerce_bool(parsed.get("disable_adaptive"), default=False)
    elif "disableAdaptive" in parsed:
        thinking["disable_adaptive"] = _coerce_bool(parsed.get("disableAdaptive"), default=False)
    return thinking


def _thinking_settings_overrides(config: TransportConfig) -> dict[str, Any]:
    thinking = _thinking_config(config)
    if not thinking:
        return {}

    settings: dict[str, Any] = {}
    env: dict[str, str] = {}
    thinking_type = thinking.get("type")
    budget = thinking.get("budget_tokens")

    if thinking_type in {"adaptive", "enabled"}:
        settings["alwaysThinkingEnabled"] = True
    elif thinking_type == "disabled":
        settings["alwaysThinkingEnabled"] = False
        env["MAX_THINKING_TOKENS"] = "0"

    display = thinking.get("display")
    if thinking_type != "disabled":
        if display == "summarized":
            settings["showThinkingSummaries"] = True
        elif display == "omitted":
            settings["showThinkingSummaries"] = False

    if isinstance(budget, int):
        env["MAX_THINKING_TOKENS"] = str(budget)
        if thinking_type == "enabled" and budget > 0:
            env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1"

    if thinking_type == "adaptive":
        env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "0"

    if "disable_adaptive" in thinking:
        env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1" if thinking["disable_adaptive"] else "0"

    if env:
        settings["env"] = env
    return settings


def _claude_code_settings_overrides(config: TransportConfig) -> dict[str, Any]:
    settings: dict[str, Any] = {"fastMode": config.fast_mode}
    return _deep_merge_settings(settings, _thinking_settings_overrides(config))


def _settings_arg_for_invocation(config: TransportConfig, cleanup_callbacks: list[Any]) -> str:
    overrides = _claude_code_settings_overrides(config)
    if not overrides:
        return config.settings
    if not config.settings:
        return json.dumps(overrides, ensure_ascii=True)

    base, source = _read_settings_arg_object(config.settings)
    merged = _deep_merge_settings(base, overrides)
    if source == "path":
        settings_path = _write_temp_json(
            prefix="hermes-claude-cli-settings-",
            value=merged,
        )
        cleanup_callbacks.append(lambda path=settings_path: _remove_temp_file(path))
        return settings_path
    return json.dumps(merged, ensure_ascii=True)


def _preflight_cli(config: TransportConfig) -> None:
    command = _resolve_command(config)
    if os.path.isabs(command) and Path(command).exists():
        logger.info("%s: Claude CLI command resolved to %s", HOOK_NAME, command)
    elif shutil.which(command):
        logger.info("%s: Claude CLI command resolved to %s", HOOK_NAME, command)
    else:
        logger.warning(
            "%s: Claude CLI command is not currently resolvable: %s",
            HOOK_NAME,
            config.command,
        )
    _preflight_claude_cli_credentials(config)


def _claude_cli_credentials_path(config: TransportConfig) -> Path:
    return Path(config.config_dir).expanduser() / ".credentials.json"


def _preflight_claude_cli_credentials(config: TransportConfig) -> None:
    credentials_path = _claude_cli_credentials_path(config)
    try:
        if credentials_path.is_file():
            logger.info("%s: Claude CLI credentials found at %s", HOOK_NAME, credentials_path)
            return
    except Exception:
        pass
    logger.warning(
        "%s: Claude CLI credentials were not found at %s; run `claude /login` or "
        "`claude setup-token` with CLAUDE_CONFIG_DIR=%s",
        HOOK_NAME,
        credentials_path,
        Path(config.config_dir).expanduser(),
    )


def _redact_error(text: str) -> str:
    if not text:
        return ""
    redacted = text
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "Authorization",
    ):
        redacted = redacted.replace(key, f"{key[:4]}...")
    return redacted[-4000:]


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _hash_json(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except Exception:
        raw = str(value)
    return _hash_text(raw)


def _normalize_claude_cli_model(raw_model: Any) -> str:
    model = str(raw_model or "").strip()
    if not model:
        return ""
    lowered = model.lower()
    if lowered.startswith("anthropic/"):
        model = model[len("anthropic/") :].strip()
        lowered = model.lower()
    if lowered.startswith("claude-"):
        model = re.sub(r"(?<=\d)\.(?=\d)", "-", model)
        lowered = model.lower()
    return CLAUDE_CLI_MODEL_ALIASES.get(lowered, model)


def _claude_session_transcript_exists(config_dir: str, session_id: str) -> bool:
    session_id = (session_id or "").strip()
    if not session_id:
        return False
    projects_dir = Path(config_dir or "").expanduser() / "projects"
    try:
        project_dirs = [entry for entry in projects_dir.iterdir() if entry.is_dir()]
    except Exception:
        return False
    for project_dir in project_dirs:
        candidate = project_dir / f"{session_id}.jsonl"
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return True
        except Exception:
            continue
    return False


def _register_client_parent_session(client: Any, parent_session_key: str) -> None:
    if not parent_session_key:
        return
    with _CLIENT_REGISTRY_LOCK:
        clients = _CLIENTS_BY_PARENT_SESSION.get(parent_session_key)
        if clients is None:
            clients = weakref.WeakSet()
            _CLIENTS_BY_PARENT_SESSION[parent_session_key] = clients
        clients.add(client)


def _unregister_client_parent_session(client: Any, parent_session_key: str) -> None:
    if not parent_session_key:
        return
    with _CLIENT_REGISTRY_LOCK:
        clients = _CLIENTS_BY_PARENT_SESSION.get(parent_session_key)
        if clients is None:
            return
        try:
            clients.discard(client)
        except Exception:
            pass
        if not clients:
            _CLIENTS_BY_PARENT_SESSION.pop(parent_session_key, None)


def _invalidate_clients_for_parent_session(parent_session_key: str, *, reason: str) -> None:
    parent_session_key = (parent_session_key or "").strip()
    if not parent_session_key:
        return
    with _CLIENT_REGISTRY_LOCK:
        clients = list(_CLIENTS_BY_PARENT_SESSION.get(parent_session_key) or ())
    for client in clients:
        try:
            client.invalidate_cli_session(reason=reason, bump_epoch=True)
        except Exception:
            logger.debug("%s: failed to invalidate client for %s", HOOK_NAME, parent_session_key, exc_info=True)


def _effective_max_turns(config: TransportConfig) -> int:
    if config.json_schema and config.max_turns == 1:
        return 0
    return config.max_turns


def _uses_internal_permission_prompt(config: TransportConfig) -> bool:
    return config.mcp_enabled and not config.permission_prompt_tool


def _resolve_no_output_timeout_seconds(total_timeout_seconds: float, *, latest_user_only: bool) -> float:
    if total_timeout_seconds <= 0:
        return 0.0
    profile = RESUME_NO_OUTPUT_TIMEOUT if latest_user_only else FRESH_NO_OUTPUT_TIMEOUT
    computed = total_timeout_seconds * profile["ratio"]
    bounded = min(profile["max"], max(profile["min"], computed))
    return max(1.0, min(bounded, max(1.0, total_timeout_seconds - 1.0)))


def _upsert_arg_value(args: list[str], flag: str, value: str) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == flag:
            index += 2
            continue
        if arg.startswith(f"{flag}="):
            index += 1
            continue
        normalized.append(arg)
        index += 1
    normalized.extend([flag, value])
    return normalized


def _has_arg_value(args: list[str], flag: str) -> bool:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == flag:
            return index + 1 < len(args)
        if arg.startswith(f"{flag}="):
            return True
        index += 1
    return False


def _append_arg_once(args: list[str], flag: str) -> list[str]:
    return list(args) if flag in args else [*args, flag]


def _strip_value_flags(args: list[str], flags: frozenset[str]) -> list[str]:
    stripped: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in flags:
            index += 2
            continue
        if any(arg.startswith(f"{flag}=") for flag in flags):
            index += 1
            continue
        stripped.append(arg)
        index += 1
    return stripped


def _live_session_args(args: list[str]) -> list[str]:
    live_args = _strip_value_flags(list(args), LIVE_PROCESS_OMITTED_VALUE_FLAGS)
    live_args = _upsert_arg_value(live_args, "--input-format", CLI_INPUT_FORMAT)
    live_args = _upsert_arg_value(live_args, "--output-format", CLI_OUTPUT_FORMAT)
    if not _has_arg_value(live_args, "--permission-prompt-tool"):
        live_args = _upsert_arg_value(live_args, "--permission-prompt-tool", "stdio")
    return _append_arg_once(live_args, "--replay-user-messages")


def _normalized_live_args(args: list[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in LIVE_OMITTED_VALUE_FLAGS:
            index += 2
            continue
        if any(arg.startswith(f"{flag}=") for flag in LIVE_OMITTED_VALUE_FLAGS):
            index += 1
            continue
        if arg in LIVE_NORMALIZED_VALUE_FLAGS:
            normalized.extend([arg, "<live-value>"])
            index += 2
            continue
        normalized_flag = next(
            (flag for flag in LIVE_NORMALIZED_VALUE_FLAGS if arg.startswith(f"{flag}=")),
            None,
        )
        if normalized_flag is not None:
            normalized.append(f"{normalized_flag}=<live-value>")
            index += 1
            continue
        normalized.append(arg)
        index += 1
    return normalized


def _live_session_fingerprint(invocation: "_ClaudeCliInvocation", args: list[str]) -> str:
    env = {
        key: _hash_text(value)
        for key, value in sorted(invocation.env.items())
        if key in {"CLAUDE_CONFIG_DIR", "MAX_MCP_OUTPUT_TOKENS"}
    }
    return _hash_json(
        {
            "args": _normalized_live_args(args),
            "cli_session_id": invocation.cli_session_id,
            "env": env,
            "parent_session_key": invocation.parent_session_key,
            "request_fingerprint": invocation.request_fingerprint,
        }
    )


def _register_live_session(session: Any) -> None:
    with _LIVE_SESSIONS_LOCK:
        if session not in _LIVE_SESSIONS:
            _LIVE_SESSIONS.append(session)


def _unregister_live_session(session: Any) -> None:
    with _LIVE_SESSIONS_LOCK:
        try:
            _LIVE_SESSIONS.remove(session)
        except ValueError:
            pass


def _ensure_live_session_capacity() -> None:
    with _LIVE_SESSIONS_LOCK:
        running = [session for session in _LIVE_SESSIONS if _live_session_running(session)]
        _LIVE_SESSIONS[:] = running
        if len(running) < LIVE_MAX_SESSIONS:
            return
        idle = [session for session in running if not bool(getattr(session, "turn_active", False))]
        if not idle:
            raise RuntimeError("Too many Claude CLI live sessions are active")
        session_to_close = min(idle, key=lambda session: getattr(session, "last_used_at", 0.0))
    session_to_close.close("capacity")


def _live_session_running(session: Any) -> bool:
    try:
        return bool(session.is_running())
    except Exception:
        return False


def _write_temp_file(*, prefix: str, suffix: str, text: str) -> str:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _remove_temp_file(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def _write_temp_json(*, prefix: str, value: Any) -> str:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".json", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=True)
    return path


def _read_usage(parsed: dict[str, Any]) -> dict[str, int]:
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        usage = parsed.get("stats")
    if not isinstance(usage, dict):
        message = parsed.get("message")
        if isinstance(message, dict):
            usage = message.get("usage")
    if not isinstance(usage, dict):
        return {}

    def pick(*names: str) -> int:
        for name in names:
            raw = usage.get(name)
            if isinstance(raw, (int, float)) and raw > 0:
                return int(raw)
        return 0

    return {
        "input_tokens": pick("input_tokens", "inputTokens", "input"),
        "output_tokens": pick("output_tokens", "outputTokens", "output"),
        "cache_read_input_tokens": pick("cache_read_input_tokens", "cached_input_tokens", "cacheRead"),
        "cache_creation_input_tokens": pick("cache_creation_input_tokens", "cache_write_input_tokens", "cacheWrite"),
    }


def _read_session_id(parsed: dict[str, Any]) -> str:
    for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
        raw = parsed.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    message = parsed.get("message")
    if isinstance(message, dict):
        return _read_session_id(message)
    return ""


def _read_model_id(parsed: dict[str, Any]) -> str:
    raw = parsed.get("model")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    message = parsed.get("message")
    if isinstance(message, dict):
        return _read_model_id(message)
    return ""


def _update_cli_diagnostics(parsed: dict[str, Any], diagnostics: dict[str, Any]) -> None:
    event_type = parsed.get("type")
    if event_type == "system" and parsed.get("subtype") == "init":
        for key in ("apiKeySource", "model", "claude_code_version", "permissionMode"):
            value = parsed.get(key)
            if value is not None:
                diagnostics[key] = value
        return
    if event_type != "rate_limit_event":
        return

    rate_limit_info = parsed.get("rate_limit_info")
    if isinstance(rate_limit_info, dict):
        diagnostics["rate_limit_info"] = rate_limit_info


def _format_cli_diagnostics(diagnostics: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("apiKeySource", "model", "claude_code_version", "permissionMode"):
        value = diagnostics.get(key)
        if value is not None:
            parts.append(f"{key}={value}")

    rate_limit_info = diagnostics.get("rate_limit_info")
    if isinstance(rate_limit_info, dict):
        for key in (
            "status",
            "rateLimitType",
            "isUsingOverage",
            "overageStatus",
            "overageDisabledReason",
        ):
            value = rate_limit_info.get(key)
            if value is not None:
                parts.append(f"{key}={value}")
        resets_at = rate_limit_info.get("resetsAt")
        if isinstance(resets_at, (int, float)) and resets_at > 0:
            reset_time = datetime.fromtimestamp(resets_at, tz=timezone.utc)
            parts.append(f"resetsAt={reset_time.isoformat().replace('+00:00', 'Z')}")
    return ", ".join(parts)


def _append_cli_diagnostics(message: str, diagnostics: dict[str, Any]) -> str:
    detail = _format_cli_diagnostics(diagnostics)
    if not detail:
        return message
    return f"{message} | cli={detail}"


def _collect_text(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_collect_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    for key in ("response", "text", "result", "content"):
        if key in value:
            collected = _collect_text(value.get(key))
            if collected:
                return collected
    message = value.get("message")
    if isinstance(message, dict):
        return _collect_text(message)
    return ""


def _unwrap_nested_result(text: str) -> str:
    current = text
    for _ in range(8):
        stripped = current.strip()
        if not stripped.startswith("{"):
            return current
        try:
            parsed = json.loads(stripped)
        except Exception:
            return current
        if not isinstance(parsed, dict):
            return current
        if parsed.get("type") != "result" or not isinstance(parsed.get("result"), str):
            return current
        current = parsed["result"]
    return current


def _parse_cli_output(stdout: str) -> tuple[str, dict[str, int], str, str]:
    text = ""
    usage: dict[str, int] = {}
    session_id = ""
    model_id = ""
    diagnostics: dict[str, Any] = {}
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return stdout.strip(), usage, session_id, model_id

    for line in lines:
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") == "user":
            continue
        _update_cli_diagnostics(parsed, diagnostics)
        usage = _read_usage(parsed) or usage
        session_id = _read_session_id(parsed) or session_id
        model_id = _read_model_id(parsed) or model_id
        structured_output = parsed.get("structured_output")
        if isinstance(structured_output, (dict, list)):
            text = json.dumps(structured_output, ensure_ascii=True)
        if parsed.get("type") == "result" and isinstance(parsed.get("result"), str):
            if parsed.get("is_error") is True:
                detail = _unwrap_nested_result(parsed["result"]).strip() or "unknown error"
                raise RuntimeError(f"Claude CLI failed: {_redact_error(_append_cli_diagnostics(detail, diagnostics))}")
            if isinstance(structured_output, (dict, list)):
                return text.strip(), usage, session_id, model_id
            return _unwrap_nested_result(parsed["result"]).strip(), usage, session_id, model_id
        if parsed.get("type") == "stream_event":
            event = parsed.get("event")
            if isinstance(event, dict) and event.get("type") == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    delta_text = delta.get("text")
                    if isinstance(delta_text, str):
                        text += delta_text
            continue
        collected = (
            _collect_text(parsed.get("message"))
            or _collect_text(parsed.get("content"))
            or _collect_text(parsed.get("result"))
            or _collect_text(parsed)
        )
        if collected:
            text = collected

    return _unwrap_nested_result(text).strip(), usage, session_id, model_id


def _make_usage(usage: dict[str, int]) -> Any:
    return SimpleNamespace(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
    )


def _make_message(*, text: str, model: str, usage: dict[str, int]) -> Any:
    return SimpleNamespace(
        id=f"claude-cli-{int(time.time() * 1000)}",
        type="message",
        role="assistant",
        model=model,
        content=[SimpleNamespace(type="text", text=text or "")],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=_make_usage(usage),
    )
