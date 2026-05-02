"""Runtime patching for Hermes auxiliary LLM calls in Claude CLI mode."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import domain.config as _config
from domain.constants import ANTHROPIC_API_BASE_URL, HOOK_NAME, ORIGINAL_ATTR, PATCH_ATTR
from features.anthropic_client import ClaudeCliAnthropicClient
from .runtime_selection import _config_requests_claude_cli_mode, _is_cli_api_mode

logger = logging.getLogger(__name__)

_AUXILIARY_CLI_PROVIDERS = {"", "auto", "main", "anthropic", "claude", "claude-code"}


def _runtime_requests_claude_cli(main_runtime: Any) -> bool:
    if not isinstance(main_runtime, dict):
        return False
    if main_runtime.get("claude_cli_mode_requested") is True:
        return True
    provider = str(main_runtime.get("provider") or "").strip().lower()
    api_mode = main_runtime.get("api_mode")
    return provider == "anthropic" and _is_cli_api_mode(api_mode)


def _auxiliary_request_uses_claude_cli(
    *,
    provider: Any,
    api_mode: Any,
    explicit_base_url: Any,
    explicit_api_key: Any,
    raw_codex: bool,
    main_runtime: Any,
) -> bool:
    if raw_codex:
        return False
    if explicit_base_url or explicit_api_key:
        return False
    provider_text = str(provider or "auto").strip().lower()
    if provider_text.startswith("custom:"):
        return False
    if provider_text not in _AUXILIARY_CLI_PROVIDERS:
        return False
    if _is_cli_api_mode(api_mode):
        return True
    if api_mode and str(api_mode).strip().lower() not in {"", "anthropic_messages"}:
        return False
    return _runtime_requests_claude_cli(main_runtime) or _config_requests_claude_cli_mode(
        "anthropic" if provider_text in {"anthropic", "claude", "claude-code"} else None
    )


def _auxiliary_parent(model: str) -> Any:
    return SimpleNamespace(
        provider="anthropic",
        api_mode="anthropic_messages",
        model=model,
        session_id="claude-cli-auxiliary",
        valid_tool_names=set(),
        _claude_cli_mode_requested=True,
        _primary_runtime={
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "claude_cli_mode_requested": True,
        },
    )


def _configured_model() -> str:
    try:
        from hermes_cli.config import load_config

        config = load_config()
        model_cfg = config.get("model", {}) if isinstance(config, dict) else {}
        if isinstance(model_cfg, dict):
            return str(model_cfg.get("default") or "").strip()
    except Exception:
        pass
    return ""


def _build_auxiliary_cli_client(module: Any, model: Any, *, async_mode: bool) -> tuple[Any, str]:
    resolved_model = str(model or "").strip() or _configured_model() or "claude"
    cli_client = ClaudeCliAnthropicClient(
        parent_agent=_auxiliary_parent(resolved_model),
        config=_config._CONFIG,
    )
    sync_client = module.AnthropicAuxiliaryClient(
        cli_client,
        resolved_model,
        "claude-cli",
        ANTHROPIC_API_BASE_URL,
        is_oauth=False,
    )
    if async_mode:
        return module.AsyncAnthropicAuxiliaryClient(sync_client), resolved_model
    return sync_client, resolved_model


def _patch_auxiliary_client_module(module: Any) -> bool:
    original = getattr(module, "resolve_provider_client", None)
    if original is None:
        return False
    if getattr(original, PATCH_ATTR, False):
        return True
    if not hasattr(module, "AnthropicAuxiliaryClient") or not hasattr(module, "AsyncAnthropicAuxiliaryClient"):
        logger.warning("%s: auxiliary client patch skipped; Anthropic auxiliary wrappers not found", HOOK_NAME)
        return False

    def wrapped_resolve_provider_client(
        provider: str,
        model: str = None,
        async_mode: bool = False,
        raw_codex: bool = False,
        explicit_base_url: str = None,
        explicit_api_key: str = None,
        api_mode: str = None,
        main_runtime: Any = None,
        is_vision: bool = False,
    ) -> tuple[Any, str]:
        if _auxiliary_request_uses_claude_cli(
            provider=provider,
            api_mode=api_mode,
            explicit_base_url=explicit_base_url,
            explicit_api_key=explicit_api_key,
            raw_codex=raw_codex,
            main_runtime=main_runtime,
        ):
            client, resolved_model = _build_auxiliary_cli_client(module, model, async_mode=async_mode)
            logger.info(
                "%s: routing auxiliary provider %r model %r through Claude CLI",
                HOOK_NAME,
                provider,
                resolved_model,
            )
            return client, resolved_model
        return original(
            provider,
            model=model,
            async_mode=async_mode,
            raw_codex=raw_codex,
            explicit_base_url=explicit_base_url,
            explicit_api_key=explicit_api_key,
            api_mode=api_mode,
            main_runtime=main_runtime,
            is_vision=is_vision,
        )

    setattr(wrapped_resolve_provider_client, PATCH_ATTR, True)
    setattr(wrapped_resolve_provider_client, ORIGINAL_ATTR, original)
    module.resolve_provider_client = wrapped_resolve_provider_client
    logger.info("%s: patched %s.resolve_provider_client", HOOK_NAME, getattr(module, "__name__", "agent.auxiliary_client"))
    return True
