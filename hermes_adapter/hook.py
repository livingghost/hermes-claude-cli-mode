"""Hermes module patching entrypoints for Claude CLI mode."""

from __future__ import annotations

import importlib.abc
import logging
import os
import sys
import threading
from typing import Any

import domain.config as _config
from features.anthropic_client import ClaudeCliAnthropicClient
from domain.config import _coerce_bool
from domain.constants import (
    HOOK_NAME,
    IMPORT_FINDER_ATTR,
    ORIGINAL_ATTR,
    PATCH_ATTR,
    SKIP_IMPORT_BOOTSTRAP_ENV,
)
from .auxiliary import _patch_auxiliary_client_module
from .runtime_selection import (
    _agent_requests_claude_cli_mode,
    _agent_uses_claude_cli,
    _build_claude_cli_runtime,
    _coerce_cli_mode_to_anthropic_messages,
    _config_requests_claude_cli_mode,
    _init_requests_claude_cli_mode,
    _mark_claude_cli_client_state,
    _primary_runtime_requests_claude_cli,
    _run_with_claude_cli_agent_context,
    _switch_requests_claude_cli_mode,
)
from .prompt_guidance import _patch_cli_safe_prompt_guidance
from infrastructure.claude_cli import _invalidate_clients_for_parent_session
from domain.state import _CURRENT_AGENT

logger = logging.getLogger(__name__)

_PATCH_LOCK = threading.RLock()
_RUN_AGENT_IMPORT_MODULES = {
    "run_agent",
    "hermes.agent.run",
    "hermes.agent.run_agent",
}
_GATEWAY_RUN_MODULES = {
    "gateway.run",
    "hermes.gateway.run",
}
_GATEWAY_SESSION_MODULES = {
    "gateway.session",
    "hermes.gateway.session",
}
_RUNTIME_PROVIDER_MODULES = {
    "hermes_cli.runtime_provider",
}
_AUXILIARY_CLIENT_MODULES = {
    "agent.auxiliary_client",
}
_PROMPT_GUIDANCE_MODULES = {
    "agent.prompt_builder",
    "hermes.agent.prompt_builder",
}
_IMPORT_PATCH_MODULES = (
    _RUN_AGENT_IMPORT_MODULES
    | _GATEWAY_RUN_MODULES
    | _GATEWAY_SESSION_MODULES
    | _RUNTIME_PROVIDER_MODULES
    | _AUXILIARY_CLIENT_MODULES
    | _PROMPT_GUIDANCE_MODULES
)
_PATCH_PENDING = False


def _patch_prompt_guidance_modules(*modules: Any) -> bool:
    patched = False
    seen: set[int] = set()
    candidates = list(modules)
    candidates.extend(
        module
        for module_name in _PROMPT_GUIDANCE_MODULES
        if (module := sys.modules.get(module_name)) is not None
    )
    for module in candidates:
        if module is None:
            continue
        identity = id(module)
        if identity in seen:
            continue
        seen.add(identity)
        patched = _patch_cli_safe_prompt_guidance(module) or patched
    return patched


def _patch_anthropic_adapter() -> bool:
    try:
        import agent.anthropic_adapter as anthropic_adapter
    except Exception as exc:
        logger.warning("%s: failed to import anthropic_adapter: %s", HOOK_NAME, exc)
        return False

    original = getattr(anthropic_adapter, "build_anthropic_client", None)
    if original is None:
        logger.warning("%s: anthropic_adapter.build_anthropic_client not found", HOOK_NAME)
        return False
    if getattr(original, PATCH_ATTR, False):
        return True

    def wrapped_build_anthropic_client(*args: Any, **kwargs: Any) -> Any:
        agent = _CURRENT_AGENT.get()
        if _agent_uses_claude_cli(agent):
            return ClaudeCliAnthropicClient(parent_agent=agent, config=_config._CONFIG)
        return original(*args, **kwargs)

    setattr(wrapped_build_anthropic_client, PATCH_ATTR, True)
    setattr(wrapped_build_anthropic_client, ORIGINAL_ATTR, original)
    anthropic_adapter.build_anthropic_client = wrapped_build_anthropic_client
    logger.info("%s: patched agent.anthropic_adapter.build_anthropic_client", HOOK_NAME)
    return True


def _patch_aiagent_module(module: Any) -> bool:
    cls = getattr(module, "AIAgent", None)
    if cls is None:
        return False

    original_init = getattr(cls, "__init__", None)
    if original_init is not None and not getattr(original_init, PATCH_ATTR, False):

        def wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
            requested_cli_mode = _init_requests_claude_cli_mode(original_init, args, kwargs)
            if requested_cli_mode:
                _patch_prompt_guidance_modules(module)
            setattr(self, "_claude_cli_mode_requested", requested_cli_mode)
            call_args, call_kwargs = _coerce_cli_mode_to_anthropic_messages(original_init, args, kwargs)
            try:
                _run_with_claude_cli_agent_context(
                    self,
                    requested_cli_mode,
                    original_init,
                    self,
                    *call_args,
                    **call_kwargs,
                )
            except Exception:
                setattr(self, "_claude_cli_mode_requested", False)
                raise
            _mark_claude_cli_client_state(self)

        setattr(wrapped_init, PATCH_ATTR, True)
        setattr(wrapped_init, ORIGINAL_ATTR, original_init)
        cls.__init__ = wrapped_init
        logger.info("%s: patched %s.AIAgent.__init__", HOOK_NAME, getattr(module, "__name__", "run_agent"))

    original_refresh = getattr(cls, "_try_refresh_anthropic_client_credentials", None)
    if original_refresh is not None and not getattr(original_refresh, PATCH_ATTR, False):

        def wrapped_refresh(self: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(self, "_claude_cli_enabled", False):
                return False
            return original_refresh(self, *args, **kwargs)

        setattr(wrapped_refresh, PATCH_ATTR, True)
        setattr(wrapped_refresh, ORIGINAL_ATTR, original_refresh)
        cls._try_refresh_anthropic_client_credentials = wrapped_refresh
        logger.info("%s: patched %s.AIAgent._try_refresh_anthropic_client_credentials", HOOK_NAME, getattr(module, "__name__", "run_agent"))

    original_reset = getattr(cls, "reset_session_state", None)
    if original_reset is not None and not getattr(original_reset, PATCH_ATTR, False):

        def wrapped_reset_session_state(self: Any, *args: Any, **kwargs: Any) -> Any:
            client = getattr(self, "_anthropic_client", None)
            if isinstance(client, ClaudeCliAnthropicClient):
                client.reset_cli_session_state()
            return original_reset(self, *args, **kwargs)

        setattr(wrapped_reset_session_state, PATCH_ATTR, True)
        setattr(wrapped_reset_session_state, ORIGINAL_ATTR, original_reset)
        cls.reset_session_state = wrapped_reset_session_state
        logger.info("%s: patched %s.AIAgent.reset_session_state", HOOK_NAME, getattr(module, "__name__", "run_agent"))

    original_rebuild = getattr(cls, "_rebuild_anthropic_client", None)
    if original_rebuild is not None and not getattr(original_rebuild, PATCH_ATTR, False):

        def wrapped_rebuild_anthropic_client(self: Any, *args: Any, **kwargs: Any) -> Any:
            requested_cli_mode = _agent_requests_claude_cli_mode(self)
            result = _run_with_claude_cli_agent_context(
                self,
                requested_cli_mode,
                original_rebuild,
                self,
                *args,
                **kwargs,
            )
            _mark_claude_cli_client_state(self)
            return result

        setattr(wrapped_rebuild_anthropic_client, PATCH_ATTR, True)
        setattr(wrapped_rebuild_anthropic_client, ORIGINAL_ATTR, original_rebuild)
        cls._rebuild_anthropic_client = wrapped_rebuild_anthropic_client
        logger.info("%s: patched %s.AIAgent._rebuild_anthropic_client", HOOK_NAME, getattr(module, "__name__", "run_agent"))

    original_swap_credential = getattr(cls, "_swap_credential", None)
    if original_swap_credential is not None and not getattr(original_swap_credential, PATCH_ATTR, False):

        def wrapped_swap_credential(self: Any, *args: Any, **kwargs: Any) -> Any:
            requested_cli_mode = _agent_requests_claude_cli_mode(self)
            result = _run_with_claude_cli_agent_context(
                self,
                requested_cli_mode,
                original_swap_credential,
                self,
                *args,
                **kwargs,
            )
            _mark_claude_cli_client_state(self)
            return result

        setattr(wrapped_swap_credential, PATCH_ATTR, True)
        setattr(wrapped_swap_credential, ORIGINAL_ATTR, original_swap_credential)
        cls._swap_credential = wrapped_swap_credential
        logger.info("%s: patched %s.AIAgent._swap_credential", HOOK_NAME, getattr(module, "__name__", "run_agent"))

    original_restore = getattr(cls, "_restore_primary_runtime", None)
    if original_restore is not None and not getattr(original_restore, PATCH_ATTR, False):

        def wrapped_restore_primary_runtime(self: Any, *args: Any, **kwargs: Any) -> Any:
            requested_cli_mode = _primary_runtime_requests_claude_cli(self)
            setattr(self, "_claude_cli_mode_requested", requested_cli_mode)
            result = _run_with_claude_cli_agent_context(
                self,
                requested_cli_mode,
                original_restore,
                self,
                *args,
                **kwargs,
            )
            _mark_claude_cli_client_state(self)
            return result

        setattr(wrapped_restore_primary_runtime, PATCH_ATTR, True)
        setattr(wrapped_restore_primary_runtime, ORIGINAL_ATTR, original_restore)
        cls._restore_primary_runtime = wrapped_restore_primary_runtime
        logger.info("%s: patched %s.AIAgent._restore_primary_runtime", HOOK_NAME, getattr(module, "__name__", "run_agent"))

    original_switch = getattr(cls, "switch_model", None)
    if original_switch is not None and not getattr(original_switch, PATCH_ATTR, False):

        def wrapped_switch_model(self: Any, *args: Any, **kwargs: Any) -> Any:
            requested_cli_mode = _switch_requests_claude_cli_mode(self, original_switch, args, kwargs)
            if requested_cli_mode:
                _patch_prompt_guidance_modules(module)
            call_args, call_kwargs = _coerce_cli_mode_to_anthropic_messages(original_switch, args, kwargs)
            previous_requested = getattr(self, "_claude_cli_mode_requested", False)
            setattr(self, "_claude_cli_mode_requested", requested_cli_mode)
            try:
                result = _run_with_claude_cli_agent_context(
                    self,
                    requested_cli_mode,
                    original_switch,
                    self,
                    *call_args,
                    **call_kwargs,
                )
            except Exception:
                setattr(self, "_claude_cli_mode_requested", previous_requested)
                raise
            _mark_claude_cli_client_state(self)
            return result

        setattr(wrapped_switch_model, PATCH_ATTR, True)
        setattr(wrapped_switch_model, ORIGINAL_ATTR, original_switch)
        cls.switch_model = wrapped_switch_model
        logger.info("%s: patched %s.AIAgent.switch_model", HOOK_NAME, getattr(module, "__name__", "run_agent"))

    original_close = getattr(cls, "close", None)
    if original_close is not None and not getattr(original_close, PATCH_ATTR, False):

        def wrapped_close(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                client = getattr(self, "_anthropic_client", None)
                if isinstance(client, ClaudeCliAnthropicClient):
                    client.close()
                    setattr(self, "_anthropic_client", None)
            except Exception:
                pass
            return original_close(self, *args, **kwargs)

        setattr(wrapped_close, PATCH_ATTR, True)
        setattr(wrapped_close, ORIGINAL_ATTR, original_close)
        cls.close = wrapped_close
        logger.info("%s: patched %s.AIAgent.close", HOOK_NAME, getattr(module, "__name__", "run_agent"))

    return True


def _patch_gateway_session_module(module: Any) -> bool:
    cls = getattr(module, "SessionStore", None)
    if cls is None:
        return False

    original_rewrite = getattr(cls, "rewrite_transcript", None)
    if original_rewrite is not None and not getattr(original_rewrite, PATCH_ATTR, False):

        def wrapped_rewrite_transcript(self: Any, session_id: str, messages: Any, *args: Any, **kwargs: Any) -> Any:
            result = original_rewrite(self, session_id, messages, *args, **kwargs)
            _invalidate_clients_for_parent_session(str(session_id or ""), reason="transcript-rewrite")
            return result

        setattr(wrapped_rewrite_transcript, PATCH_ATTR, True)
        setattr(wrapped_rewrite_transcript, ORIGINAL_ATTR, original_rewrite)
        cls.rewrite_transcript = wrapped_rewrite_transcript
        logger.info("%s: patched %s.SessionStore.rewrite_transcript", HOOK_NAME, getattr(module, "__name__", "gateway.session"))

    return True


def _evict_gateway_cached_agent_for_event(runner: Any, event: Any, *, reason: str) -> None:
    try:
        source = getattr(event, "source", None)
        if source is None:
            return
        session_key_for_source = getattr(runner, "_session_key_for_source", None)
        if not callable(session_key_for_source):
            return
        session_key = session_key_for_source(source)
        if not session_key:
            return
        release = getattr(runner, "_release_running_agent_state", None)
        if callable(release):
            try:
                release(session_key)
            except Exception:
                pass
        evict = getattr(runner, "_evict_cached_agent", None)
        if callable(evict):
            evict(session_key)
            logger.debug("%s: evicted cached gateway agent after %s", HOOK_NAME, reason)
    except Exception:
        logger.debug("%s: failed to evict cached gateway agent after %s", HOOK_NAME, reason, exc_info=True)


def _patch_gateway_run_module(module: Any) -> bool:
    cls = getattr(module, "GatewayRunner", None)
    if cls is None:
        return False

    original_undo = getattr(cls, "_handle_undo_command", None)
    if original_undo is not None and not getattr(original_undo, PATCH_ATTR, False):

        async def wrapped_handle_undo_command(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
            result = await original_undo(self, event, *args, **kwargs)
            _evict_gateway_cached_agent_for_event(self, event, reason="/undo")
            return result

        setattr(wrapped_handle_undo_command, PATCH_ATTR, True)
        setattr(wrapped_handle_undo_command, ORIGINAL_ATTR, original_undo)
        cls._handle_undo_command = wrapped_handle_undo_command
        logger.info("%s: patched %s.GatewayRunner._handle_undo_command", HOOK_NAME, getattr(module, "__name__", "gateway.run"))

    original_compress = getattr(cls, "_handle_compress_command", None)
    if original_compress is not None and not getattr(original_compress, PATCH_ATTR, False):

        async def wrapped_handle_compress_command(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
            result = await original_compress(self, event, *args, **kwargs)
            _evict_gateway_cached_agent_for_event(self, event, reason="/compress")
            return result

        setattr(wrapped_handle_compress_command, PATCH_ATTR, True)
        setattr(wrapped_handle_compress_command, ORIGINAL_ATTR, original_compress)
        cls._handle_compress_command = wrapped_handle_compress_command
        logger.info("%s: patched %s.GatewayRunner._handle_compress_command", HOOK_NAME, getattr(module, "__name__", "gateway.run"))

    return True


def _patch_runtime_provider_module(module: Any) -> bool:
    original = getattr(module, "resolve_runtime_provider", None)
    if original is None:
        return False
    if getattr(original, PATCH_ATTR, False):
        return True

    def wrapped_resolve_runtime_provider(*args: Any, **kwargs: Any) -> dict[str, Any]:
        requested = kwargs.get("requested")
        target_model = kwargs.get("target_model")
        if _config_requests_claude_cli_mode(requested):
            return _build_claude_cli_runtime(requested, target_model)
        return original(*args, **kwargs)

    setattr(wrapped_resolve_runtime_provider, PATCH_ATTR, True)
    setattr(wrapped_resolve_runtime_provider, ORIGINAL_ATTR, original)
    module.resolve_runtime_provider = wrapped_resolve_runtime_provider
    logger.info("%s: patched %s.resolve_runtime_provider", HOOK_NAME, getattr(module, "__name__", "hermes_cli.runtime_provider"))
    return True


def _patch_imported_module(module: Any) -> bool:
    name = getattr(module, "__name__", "")
    patched = False
    if name in _PROMPT_GUIDANCE_MODULES and _config_requests_claude_cli_mode("anthropic"):
        patched = _patch_prompt_guidance_modules(module) or patched
    if name in _RUNTIME_PROVIDER_MODULES:
        patched = _patch_runtime_provider_module(module) or patched
    if name in _AUXILIARY_CLIENT_MODULES:
        patched = _patch_auxiliary_client_module(module) or patched
    if name in _RUN_AGENT_IMPORT_MODULES:
        patched = _patch_aiagent_module(module) or patched
    if name in _GATEWAY_SESSION_MODULES:
        patched = _patch_gateway_session_module(module) or patched
    if name in _GATEWAY_RUN_MODULES:
        patched = _patch_gateway_run_module(module) or patched
    return patched


class _ModulePatchLoader(importlib.abc.Loader):
    def __init__(self, wrapped_loader: importlib.abc.Loader):
        self._wrapped_loader = wrapped_loader

    def create_module(self, spec: Any) -> Any:
        create_module = getattr(self._wrapped_loader, "create_module", None)
        if create_module is None:
            return None
        return create_module(spec)

    def exec_module(self, module: Any) -> None:
        self._wrapped_loader.exec_module(module)
        _patch_imported_module(module)


class _ModulePatchFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname not in _IMPORT_PATCH_MODULES:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            if getattr(finder, "_gateway_event_filter_import_finder", False):
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            spec = find_spec(fullname, path, target)
            if spec is None or spec.loader is None:
                continue
            if isinstance(spec.loader, _ModulePatchLoader):
                return spec
            spec.loader = _ModulePatchLoader(spec.loader)
            return spec
        return None


def _install_module_import_hook() -> bool:
    if getattr(sys, IMPORT_FINDER_ATTR, None) is not None:
        return True
    finder = _ModulePatchFinder()
    sys.meta_path.insert(0, finder)
    setattr(sys, IMPORT_FINDER_ATTR, finder)
    logger.info("%s: installed module import patch hook", HOOK_NAME)
    return True


def _patch_aiagent() -> bool:
    global _PATCH_PENDING
    patched = False
    for module_name in _RUN_AGENT_IMPORT_MODULES:
        module = sys.modules.get(module_name)
        if module is not None and _patch_imported_module(module):
            patched = True
    if not patched:
        _PATCH_PENDING = True
        _install_module_import_hook()
    else:
        _PATCH_PENDING = False
    return patched or _PATCH_PENDING


def _patch_gateway_modules() -> bool:
    patched = False
    for module_name in _GATEWAY_SESSION_MODULES | _GATEWAY_RUN_MODULES:
        module = sys.modules.get(module_name)
        if module is not None and _patch_imported_module(module):
            patched = True
    _install_module_import_hook()
    return patched


def _patch_runtime_provider() -> bool:
    patched = False
    for module_name in _RUNTIME_PROVIDER_MODULES:
        module = sys.modules.get(module_name)
        if module is not None and _patch_imported_module(module):
            patched = True
    _install_module_import_hook()
    return patched


def _patch_auxiliary_client() -> bool:
    patched = False
    for module_name in _AUXILIARY_CLIENT_MODULES:
        module = sys.modules.get(module_name)
        if module is not None and _patch_imported_module(module):
            patched = True
    _install_module_import_hook()
    return patched


def bootstrap(*, warn_incomplete: bool = True) -> bool:
    if not _config._reload_config():
        return False
    with _PATCH_LOCK:
        if _config_requests_claude_cli_mode("anthropic"):
            _patch_prompt_guidance_modules()
        patched_runtime = _patch_runtime_provider()
        patched_adapter = _patch_anthropic_adapter()
        patched_auxiliary = _patch_auxiliary_client()
        patched_agent = _patch_aiagent()
        patched_gateway = _patch_gateway_modules()
    if warn_incomplete and _PATCH_PENDING:
        logger.info("%s: AIAgent patch pending until run_agent is imported", HOOK_NAME)
    if warn_incomplete and not (patched_adapter and patched_agent):
        logger.warning(
            "%s: patch targets incomplete: runtime_provider=%s anthropic_adapter=%s agent=%s gateway=%s",
            HOOK_NAME,
            patched_runtime,
            patched_adapter,
            patched_agent,
            patched_gateway,
        )
    if warn_incomplete and not patched_auxiliary:
        logger.info("%s: auxiliary client patch pending until agent.auxiliary_client is imported", HOOK_NAME)
    return patched_adapter and patched_agent


async def handle(event_type: str, context: dict[str, Any] | None = None) -> None:
    if event_type != "gateway:startup":
        return
    bootstrap()


def _bootstrap_on_import() -> None:
    if _coerce_bool(os.environ.get(SKIP_IMPORT_BOOTSTRAP_ENV), default=False):
        return
    try:
        bootstrap(warn_incomplete=False)
    except Exception:
        logger.warning("%s: import-time bootstrap failed", HOOK_NAME, exc_info=True)


_bootstrap_on_import()
