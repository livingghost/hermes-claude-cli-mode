import base64
import builtins
import importlib
import importlib.machinery
import importlib.util
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path

import pytest


HOOK_DIR = Path(__file__).resolve().parents[1]
HANDLER_PATH = HOOK_DIR / "handler.py"
IMPORT_FINDER_ATTR = "_hermes_claude_cli_import_finder"
IMPORT_WRAPPER_ATTR = "_hermes_claude_cli_import_wrapper"


@pytest.fixture(autouse=True)
def restore_import_hooks():
    original_import = builtins.__import__
    original_meta_path = list(sys.meta_path)
    yield
    builtins.__import__ = original_import
    sys.meta_path[:] = original_meta_path
    for attr in (IMPORT_FINDER_ATTR, IMPORT_WRAPPER_ATTR):
        if hasattr(sys, attr):
            delattr(sys, attr)
    sys.modules.pop("run_agent", None)


def load_handler(monkeypatch):
    monkeypatch.setenv("HERMES_HOOK_SKIP_IMPORT_BOOTSTRAP", "1")
    for module_name in list(sys.modules):
        if module_name in {"domain", "features", "hermes_adapter", "infrastructure"} or module_name.startswith(
            ("domain.", "features.", "hermes_adapter.", "infrastructure.")
        ):
            sys.modules.pop(module_name, None)
    module_name = f"_claude_cli_mode_handler_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_preflight_cli", lambda config: None)
    monkeypatch.setattr(module.anthropic_client, "_preflight_cli", lambda config: None)
    return module


def install_config(monkeypatch, model_config):
    hermes_cli_pkg = types.ModuleType("hermes_cli")
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.load_config = lambda: {"model": model_config}
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)


def install_anthropic_adapter(monkeypatch, native_client):
    agent_pkg = types.ModuleType("agent")
    adapter_mod = types.ModuleType("agent.anthropic_adapter")
    adapter_mod.build_anthropic_client = lambda *args, **kwargs: native_client
    agent_pkg.anthropic_adapter = adapter_mod
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.anthropic_adapter", adapter_mod)
    return adapter_mod


def install_auxiliary_client(monkeypatch):
    agent_pkg = sys.modules.get("agent") or types.ModuleType("agent")
    auxiliary_mod = types.ModuleType("agent.auxiliary_client")
    calls = []

    class AnthropicAuxiliaryClient:
        def __init__(self, real_client, model, api_key, base_url, is_oauth=False):
            self._real_client = real_client
            self.model = model
            self.api_key = api_key
            self.base_url = base_url
            self.is_oauth = is_oauth

        def close(self):
            close = getattr(self._real_client, "close", None)
            if callable(close):
                close()

    class AsyncAnthropicAuxiliaryClient:
        def __init__(self, sync_client):
            self._sync_client = sync_client
            self.model = sync_client.model
            self.api_key = sync_client.api_key
            self.base_url = sync_client.base_url

    def resolve_provider_client(provider, model=None, async_mode=False, raw_codex=False, explicit_base_url=None, explicit_api_key=None, api_mode=None, main_runtime=None, is_vision=False):
        calls.append(
            {
                "provider": provider,
                "model": model,
                "async_mode": async_mode,
                "raw_codex": raw_codex,
                "explicit_base_url": explicit_base_url,
                "explicit_api_key": explicit_api_key,
                "api_mode": api_mode,
                "main_runtime": main_runtime,
                "is_vision": is_vision,
            }
        )
        return types.SimpleNamespace(provider=provider, model=model), model or "native-model"

    auxiliary_mod.AnthropicAuxiliaryClient = AnthropicAuxiliaryClient
    auxiliary_mod.AsyncAnthropicAuxiliaryClient = AsyncAnthropicAuxiliaryClient
    auxiliary_mod.resolve_provider_client = resolve_provider_client
    agent_pkg.auxiliary_client = auxiliary_mod
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary_mod)
    return types.SimpleNamespace(module=auxiliary_mod, calls=calls)


def install_runtime_provider(monkeypatch, original):
    runtime_provider_mod = types.ModuleType("hermes_cli.runtime_provider")
    runtime_provider_mod.resolve_runtime_provider = original
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime_provider_mod)
    return runtime_provider_mod


def make_fake_run_agent_module():
    module = types.SimpleNamespace(__name__="run_agent")

    class AIAgent:
        def __init__(
            self,
            base_url=None,
            api_key=None,
            provider=None,
            api_mode=None,
            model="",
            **kwargs,
        ):
            self.provider = (provider or "").strip().lower()
            self.model = model
            self.session_id = kwargs.get("session_id") or "parent-session"
            if api_mode in {"chat_completions", "codex_responses", "anthropic_messages"}:
                self.api_mode = api_mode
            elif self.provider == "anthropic":
                self.api_mode = "anthropic_messages"
            else:
                self.api_mode = "chat_completions"
            self._client_kwargs = {}
            self._primary_runtime = {
                "provider": self.provider,
                "api_mode": self.api_mode,
            }
            if self.api_mode == "anthropic_messages":
                from agent.anthropic_adapter import build_anthropic_client

                self._anthropic_client = build_anthropic_client(api_key or "key", base_url)
            else:
                self._anthropic_client = None

        def _try_refresh_anthropic_client_credentials(self):
            return True

        def reset_session_state(self):
            return "reset"

        def interrupt(self, message=None):
            self._interrupt_requested = True
            self._interrupt_message = message

        def _rebuild_anthropic_client(self):
            from agent.anthropic_adapter import build_anthropic_client

            self._anthropic_client = build_anthropic_client("key", None)

        def _swap_credential(self, entry):
            from agent.anthropic_adapter import build_anthropic_client

            self._anthropic_client = build_anthropic_client("key", None)

        def _restore_primary_runtime(self):
            from agent.anthropic_adapter import build_anthropic_client

            self.provider = "anthropic"
            self.api_mode = "anthropic_messages"
            self._anthropic_client = build_anthropic_client("key", None)
            return True

        def switch_model(self, new_model, new_provider, api_key="", base_url="", api_mode=""):
            from agent.anthropic_adapter import build_anthropic_client

            self.model = new_model
            self.provider = new_provider
            self.api_mode = api_mode or ("anthropic_messages" if new_provider == "anthropic" else "chat_completions")
            self._primary_runtime = {
                "provider": self.provider,
                "api_mode": self.api_mode,
            }
            if self.api_mode == "anthropic_messages":
                self._anthropic_client = build_anthropic_client(api_key or "key", base_url)
            else:
                self._anthropic_client = None

        def close(self):
            return "closed"

    module.AIAgent = AIAgent
    return module


def write_fake_run_agent_file(tmp_path):
    run_agent_path = tmp_path / "run_agent.py"
    run_agent_path.write_text(
        "\n".join(
            [
                "class AIAgent:",
                "    def __init__(self, base_url=None, api_key=None, provider=None, api_mode=None, model='', **kwargs):",
                "        self.provider = provider or ''",
                "        self.api_mode = api_mode or ''",
                "        self.model = model",
                "        self.session_id = kwargs.get('session_id') or 'session'",
                "        from agent.anthropic_adapter import build_anthropic_client",
                "        self._anthropic_client = build_anthropic_client(api_key or 'key', base_url)",
                "    def _try_refresh_anthropic_client_credentials(self):",
                "        return True",
                "    def reset_session_state(self):",
                "        return 'reset'",
                "    def _rebuild_anthropic_client(self):",
                "        return None",
                "    def _swap_credential(self, entry):",
                "        return None",
                "    def _restore_primary_runtime(self):",
                "        return True",
                "    def switch_model(self, new_model, new_provider, api_key='', base_url='', api_mode=''):",
                "        return None",
                "    def close(self):",
                "        return None",
            ]
        ),
        encoding="utf-8",
    )
    return run_agent_path


def install_fake_approval_module(monkeypatch, *, choice="once", config=None):
    tools_pkg = types.ModuleType("tools")
    approval_mod = types.ModuleType("tools.approval")
    approvals = set()
    hook_events = []
    notifications = []
    current_session = {"value": ""}

    class ApprovalEntry:
        def __init__(self, data):
            self.event = threading.Event()
            self.data = data
            self.result = None

    approval_mod._lock = threading.Lock()
    approval_mod._gateway_queues = {}
    approval_mod._gateway_notify_cbs = {}
    approval_mod._ApprovalEntry = ApprovalEntry
    approval_mod._get_approval_mode = lambda: "manual"
    approval_mod._get_approval_config = lambda: config or {"gateway_timeout": 10}
    approval_mod.is_session_yolo_enabled = lambda _session_key: False
    approval_mod.is_approved = lambda _session_key, pattern_key: pattern_key in approvals
    approval_mod.approve_session = lambda _session_key, pattern_key: approvals.add(pattern_key)
    approval_mod.get_current_session_key = lambda default="": current_session["value"] or default

    def set_current_session_key(session_key):
        previous = current_session["value"]
        current_session["value"] = session_key
        return previous

    def reset_current_session_key(previous):
        current_session["value"] = previous

    def fire_hook(hook_name, **kwargs):
        hook_events.append((hook_name, kwargs))

    def notify(approval_data):
        notifications.append(approval_data)
        queue_items = approval_mod._gateway_queues[approval_data["session_key"] if "session_key" in approval_data else "hermes-session"]
        entry = queue_items[0]
        entry.result = choice
        entry.event.set()

    approval_mod.set_current_session_key = set_current_session_key
    approval_mod.reset_current_session_key = reset_current_session_key
    approval_mod._fire_approval_hook = fire_hook
    approval_mod._gateway_notify_cbs["hermes-session"] = notify
    tools_pkg.approval = approval_mod
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.approval", approval_mod)
    return types.SimpleNamespace(
        approvals=approvals,
        hook_events=hook_events,
        notifications=notifications,
        module=approval_mod,
    )


def test_agent_usage_requires_explicit_cli_mode(monkeypatch):
    handler = load_handler(monkeypatch)
    native_anthropic = types.SimpleNamespace(provider="anthropic", api_mode="anthropic_messages")
    cli_anthropic = types.SimpleNamespace(
        provider="anthropic",
        api_mode="anthropic_messages",
        _claude_cli_mode_requested=True,
    )
    cli_openrouter = types.SimpleNamespace(
        provider="openrouter",
        api_mode="anthropic_messages",
        _claude_cli_mode_requested=True,
    )

    assert handler._agent_uses_claude_cli(native_anthropic) is False
    assert handler._agent_uses_claude_cli(cli_anthropic) is True
    assert handler._agent_uses_claude_cli(cli_openrouter) is False


def test_config_api_mode_cli_selects_cli_client_without_native_anthropic_leak(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    native_client = object()
    install_anthropic_adapter(monkeypatch, native_client)
    handler = load_handler(monkeypatch)
    assert handler._patch_anthropic_adapter() is True
    run_agent = make_fake_run_agent_module()
    assert handler._patch_aiagent_module(run_agent) is True

    agent = run_agent.AIAgent(
        provider="anthropic",
        api_mode="anthropic_messages",
        model="claude-opus-4-7",
    )

    assert agent.api_mode == "anthropic_messages"
    assert isinstance(agent._anthropic_client, handler.ClaudeCliAnthropicClient)
    assert agent._claude_cli_enabled is True
    assert agent._claude_cli_mode_requested is True
    assert agent._primary_runtime["claude_cli_mode_requested"] is True
    agent._anthropic_client.close()


def test_cli_mode_interrupt_aborts_active_cli_invocation_after_core_interrupt(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    install_anthropic_adapter(monkeypatch, object())
    handler = load_handler(monkeypatch)
    assert handler._patch_anthropic_adapter() is True
    run_agent = make_fake_run_agent_module()
    assert handler._patch_aiagent_module(run_agent) is True

    agent = run_agent.AIAgent(
        provider="anthropic",
        api_mode="anthropic_messages",
        model="claude-opus-4-7",
    )
    calls = []

    def abort_active_invocations(*, reason):
        calls.append((reason, agent._interrupt_requested, agent._interrupt_message))

    agent._anthropic_client.abort_active_invocations = abort_active_invocations

    agent.interrupt("new instruction")

    assert calls == [("agent-interrupt", True, "new instruction")]
    agent._anthropic_client.close()


def test_cli_mode_patches_known_claude_cli_prompt_guidance_false_positive(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    native_client = object()
    install_anthropic_adapter(monkeypatch, native_client)
    handler = load_handler(monkeypatch)
    assert handler._patch_anthropic_adapter() is True
    run_agent = make_fake_run_agent_module()
    run_agent.SESSION_SEARCH_GUIDANCE = (
        "When the user references something from a past conversation or you suspect "
        "relevant cross-session context exists, use session_search to recall it before "
        "asking them to repeat themselves."
    )
    run_agent.SKILLS_GUIDANCE = (
        "After completing a complex task (5+ tool calls), fixing a tricky error, "
        "or discovering a non-trivial workflow, save the approach as a "
        "skill with skill_manage so you can reuse it next time.\n"
        "When using a skill and finding it outdated, incomplete, or wrong, "
        "patch it immediately with skill_manage(action='patch') — don't wait to be asked. "
        "Skills that aren't maintained become liabilities."
    )
    expected_session_guidance = handler.CLAUDE_CLI_SESSION_SEARCH_GUIDANCE_PATCHES[0][1]
    expected_skills_guidance = run_agent.SKILLS_GUIDANCE
    for needle, replacement in handler.CLAUDE_CLI_SKILLS_GUIDANCE_PATCHES:
        expected_skills_guidance = expected_skills_guidance.replace(needle, replacement)

    assert handler._patch_aiagent_module(run_agent) is True
    assert run_agent.SESSION_SEARCH_GUIDANCE != expected_session_guidance

    agent = run_agent.AIAgent(
        provider="anthropic",
        api_mode="anthropic_messages",
        model="claude-opus-4-7",
    )

    assert run_agent.SESSION_SEARCH_GUIDANCE == expected_session_guidance
    assert run_agent.SKILLS_GUIDANCE == expected_skills_guidance
    agent._anthropic_client.close()


def test_cli_mode_patches_prompt_builder_guidance_at_import_boundary(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    handler = load_handler(monkeypatch)
    prompt_builder = types.ModuleType("agent.prompt_builder")
    prompt_builder.SESSION_SEARCH_GUIDANCE = (
        "When the user references something from a past conversation or you suspect "
        "relevant cross-session context exists, use session_search to recall it before "
        "asking them to repeat themselves."
    )
    prompt_builder.SKILLS_GUIDANCE = (
        "After completing a complex task (5+ tool calls), fixing a tricky error, "
        "or discovering a non-trivial workflow, save the approach as a "
        "skill with skill_manage so you can reuse it next time.\n"
        "When using a skill and finding it outdated, incomplete, or wrong, "
        "patch it immediately with skill_manage(action='patch') — don't wait to be asked. "
        "Skills that aren't maintained become liabilities."
    )

    assert handler._patch_imported_module(prompt_builder) is True

    assert "relevant cross-session context exists" not in prompt_builder.SESSION_SEARCH_GUIDANCE
    assert "prior Hermes sessions" in prompt_builder.SESSION_SEARCH_GUIDANCE
    assert "patch it immediately" not in prompt_builder.SKILLS_GUIDANCE
    assert "don't wait" not in prompt_builder.SKILLS_GUIDANCE
    assert "after confirming the needed correction" in prompt_builder.SKILLS_GUIDANCE


def test_native_mode_keeps_prompt_builder_guidance_unchanged(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "anthropic_messages"},
    )
    handler = load_handler(monkeypatch)
    prompt_builder = types.ModuleType("agent.prompt_builder")
    prompt_builder.SESSION_SEARCH_GUIDANCE = "Use session_search with the native provider."
    prompt_builder.SKILLS_GUIDANCE = "Use skill_manage with the native provider."

    assert handler._patch_imported_module(prompt_builder) is False

    assert prompt_builder.SESSION_SEARCH_GUIDANCE == "Use session_search with the native provider."
    assert prompt_builder.SKILLS_GUIDANCE == "Use skill_manage with the native provider."


def test_cli_mode_leaves_unmatched_prompt_guidance_unchanged(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    native_client = object()
    install_anthropic_adapter(monkeypatch, native_client)
    handler = load_handler(monkeypatch)
    assert handler._patch_anthropic_adapter() is True
    run_agent = make_fake_run_agent_module()
    run_agent.SESSION_SEARCH_GUIDANCE = "Core already revised session_search guidance."
    run_agent.SKILLS_GUIDANCE = "Core already revised skill_manage guidance."

    assert handler._patch_aiagent_module(run_agent) is True
    agent = run_agent.AIAgent(
        provider="anthropic",
        api_mode="anthropic_messages",
        model="claude-opus-4-7",
    )

    assert run_agent.SESSION_SEARCH_GUIDANCE == "Core already revised session_search guidance."
    assert run_agent.SKILLS_GUIDANCE == "Core already revised skill_manage guidance."
    agent._anthropic_client.close()


def test_native_mode_keeps_original_prompt_guidance(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "anthropic_messages"},
    )
    native_client = object()
    install_anthropic_adapter(monkeypatch, native_client)
    handler = load_handler(monkeypatch)
    assert handler._patch_anthropic_adapter() is True
    run_agent = make_fake_run_agent_module()
    run_agent.SESSION_SEARCH_GUIDANCE = "Use session_search to recall past conversations."
    run_agent.SKILLS_GUIDANCE = "Use skill_manage to save reusable skills."

    assert handler._patch_aiagent_module(run_agent) is True
    agent = run_agent.AIAgent(
        provider="anthropic",
        api_mode="anthropic_messages",
        model="claude-opus-4-7",
    )

    assert run_agent.SESSION_SEARCH_GUIDANCE == "Use session_search to recall past conversations."
    assert run_agent.SKILLS_GUIDANCE == "Use skill_manage to save reusable skills."
    assert agent._anthropic_client is native_client


def test_run_agent_import_hook_chains_after_delegating_gateway_event_filter_finder(monkeypatch, tmp_path):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    install_anthropic_adapter(monkeypatch, object())
    handler = load_handler(monkeypatch)
    write_fake_run_agent_file(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    class GatewayEventFilterFinder:
        _gateway_event_filter_import_finder = True

        def find_spec(self, fullname, path=None, target=None):
            if fullname != "run_agent":
                return None
            for finder in sys.meta_path:
                if finder is self:
                    continue
                find_spec = getattr(finder, "find_spec", None)
                if find_spec is None:
                    continue
                spec = find_spec(fullname, path, target)
                if spec is None or spec.loader is None:
                    continue
                spec.loader = GatewayEventFilterLoader(spec.loader)
                return spec
            return None

    class GatewayEventFilterLoader:
        def __init__(self, wrapped_loader):
            self._wrapped_loader = wrapped_loader

        def create_module(self, spec):
            create_module = getattr(self._wrapped_loader, "create_module", None)
            return create_module(spec) if create_module is not None else None

        def exec_module(self, module):
            self._wrapped_loader.exec_module(module)
            module.gateway_event_filter_loader_ran = True

    assert handler._patch_anthropic_adapter() is True
    assert handler._patch_aiagent() is True
    sys.meta_path.insert(0, GatewayEventFilterFinder())

    imported = importlib.import_module("run_agent")
    agent = imported.AIAgent(provider="anthropic", api_mode="cli", model="claude-opus-4-7")

    assert imported.gateway_event_filter_loader_ran is True
    assert isinstance(agent._anthropic_client, handler.ClaudeCliAnthropicClient)
    assert agent._claude_cli_enabled is True
    agent._anthropic_client.close()


def test_module_import_hook_uses_pathfinder_without_delegating_to_other_finders(monkeypatch, tmp_path):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    install_anthropic_adapter(monkeypatch, object())
    handler = load_handler(monkeypatch)
    write_fake_run_agent_file(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    class SentinelFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "run_agent":
                raise AssertionError("Claude CLI mode finder must not delegate to unrelated meta path finders")
            return None

    assert handler._patch_anthropic_adapter() is True
    assert handler._patch_aiagent() is True
    sys.meta_path.insert(1, SentinelFinder())

    imported = importlib.import_module("run_agent")
    agent = imported.AIAgent(provider="anthropic", api_mode="cli", model="claude-opus-4-7")

    assert isinstance(agent._anthropic_client, handler.ClaudeCliAnthropicClient)
    assert agent._claude_cli_enabled is True
    agent._anthropic_client.close()


def test_builtin_import_hook_patches_after_pathfinder_based_hook_loads_run_agent(monkeypatch, tmp_path):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    install_anthropic_adapter(monkeypatch, object())
    handler = load_handler(monkeypatch)
    write_fake_run_agent_file(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    class PathFinderBasedGatewayFinder:
        _gateway_event_filter_import_finder = True

        def find_spec(self, fullname, path=None, target=None):
            if fullname != "run_agent":
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec is None or spec.loader is None:
                return spec
            spec.loader = GatewayEventFilterLoader(spec.loader)
            return spec

    class GatewayEventFilterLoader:
        def __init__(self, wrapped_loader):
            self._wrapped_loader = wrapped_loader

        def create_module(self, spec):
            create_module = getattr(self._wrapped_loader, "create_module", None)
            return create_module(spec) if create_module is not None else None

        def exec_module(self, module):
            self._wrapped_loader.exec_module(module)
            module.gateway_event_filter_loader_ran = True

    assert handler._patch_anthropic_adapter() is True
    assert handler._patch_aiagent() is True
    assert handler.hook._PATCH_PENDING is True
    sys.meta_path.insert(0, PathFinderBasedGatewayFinder())

    imported = __import__("run_agent", fromlist=["AIAgent"])
    agent = imported.AIAgent(provider="anthropic", api_mode="cli", model="claude-opus-4-7")

    assert imported.gateway_event_filter_loader_ran is True
    assert handler.hook._PATCH_PENDING is False
    assert isinstance(agent._anthropic_client, handler.ClaudeCliAnthropicClient)
    assert agent._claude_cli_enabled is True
    agent._anthropic_client.close()


def test_builtin_import_hook_ignores_unrelated_imports(monkeypatch):
    handler = load_handler(monkeypatch)
    calls = []
    monkeypatch.setattr(
        handler.hook,
        "_patch_imported_modules_for_import",
        lambda name, fromlist=(): calls.append((name, tuple(fromlist or ()))),
    )

    assert handler._install_builtin_import_hook() is True
    __import__("json")

    assert calls == []


def test_plain_anthropic_messages_uses_native_client(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "anthropic_messages"},
    )
    native_client = object()
    install_anthropic_adapter(monkeypatch, native_client)
    handler = load_handler(monkeypatch)
    assert handler._patch_anthropic_adapter() is True
    run_agent = make_fake_run_agent_module()
    assert handler._patch_aiagent_module(run_agent) is True

    agent = run_agent.AIAgent(
        provider="anthropic",
        api_mode="anthropic_messages",
        model="claude-opus-4-7",
    )

    assert agent._anthropic_client is native_client
    assert agent._claude_cli_enabled is False
    assert agent._claude_cli_mode_requested is False


def test_runtime_provider_cli_mode_bypasses_native_anthropic_auth(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-haiku-4-5", "api_mode": "cli"},
    )

    def native_resolver(**kwargs):
        raise AssertionError("native Anthropic resolver should not run")

    runtime_provider = install_runtime_provider(monkeypatch, native_resolver)
    handler = load_handler(monkeypatch)

    assert handler._patch_runtime_provider_module(runtime_provider) is True

    runtime = runtime_provider.resolve_runtime_provider(requested=None, target_model="haiku")

    assert runtime == {
        "provider": "anthropic",
        "api_mode": "anthropic_messages",
        "base_url": "https://api.anthropic.com",
        "api_key": "claude-cli",
        "source": "claude-cli",
        "requested_provider": "anthropic",
        "target_model": "haiku",
        "claude_cli_mode_requested": True,
    }


def test_runtime_provider_delegates_without_cli_mode(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "anthropic_messages"},
    )
    calls = []

    def native_resolver(**kwargs):
        calls.append(kwargs)
        return {"provider": "anthropic", "api_mode": "anthropic_messages", "api_key": "native"}

    runtime_provider = install_runtime_provider(monkeypatch, native_resolver)
    handler = load_handler(monkeypatch)

    assert handler._patch_runtime_provider_module(runtime_provider) is True

    runtime = runtime_provider.resolve_runtime_provider(requested=None, target_model="claude-opus-4-7")

    assert runtime["api_key"] == "native"
    assert calls == [{"requested": None, "target_model": "claude-opus-4-7"}]


def test_auxiliary_main_provider_uses_claude_cli_when_main_runtime_requests_cli(monkeypatch, caplog):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    auxiliary = install_auxiliary_client(monkeypatch)
    handler = load_handler(monkeypatch)
    caplog.set_level(logging.INFO)

    assert handler._patch_auxiliary_client_module(auxiliary.module) is True

    client, model = auxiliary.module.resolve_provider_client(
        "main",
        model="claude-haiku-4-5",
        main_runtime={
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "claude_cli_mode_requested": True,
        },
    )

    assert auxiliary.calls == []
    assert model == "claude-haiku-4-5"
    assert isinstance(client, auxiliary.module.AnthropicAuxiliaryClient)
    assert isinstance(client._real_client, handler.ClaudeCliAnthropicClient)
    assert client.api_key == "claude-cli"
    assert client.base_url == "https://api.anthropic.com"
    assert "routing auxiliary provider 'main' model 'claude-haiku-4-5' through Claude CLI" in caplog.text
    client.close()


def test_auxiliary_anthropic_delegates_without_cli_mode(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "anthropic_messages"},
    )
    auxiliary = install_auxiliary_client(monkeypatch)
    handler = load_handler(monkeypatch)

    assert handler._patch_auxiliary_client_module(auxiliary.module) is True

    client, model = auxiliary.module.resolve_provider_client(
        "anthropic",
        model="claude-haiku-4-5",
        api_mode="anthropic_messages",
    )

    assert auxiliary.calls and auxiliary.calls[0]["provider"] == "anthropic"
    assert client.provider == "anthropic"
    assert model == "claude-haiku-4-5"


def test_auxiliary_explicit_base_url_delegates_even_when_main_uses_cli(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    auxiliary = install_auxiliary_client(monkeypatch)
    handler = load_handler(monkeypatch)

    assert handler._patch_auxiliary_client_module(auxiliary.module) is True

    client, model = auxiliary.module.resolve_provider_client(
        "anthropic",
        model="claude-haiku-4-5",
        explicit_base_url="https://api.anthropic.com",
        explicit_api_key="sk-test",
    )

    assert auxiliary.calls and auxiliary.calls[0]["explicit_base_url"] == "https://api.anthropic.com"
    assert client.provider == "anthropic"
    assert model == "claude-haiku-4-5"


def test_auxiliary_api_mode_cli_can_be_requested_directly(monkeypatch):
    install_config(monkeypatch, {})
    auxiliary = install_auxiliary_client(monkeypatch)
    handler = load_handler(monkeypatch)

    assert handler._patch_auxiliary_client_module(auxiliary.module) is True

    client, model = auxiliary.module.resolve_provider_client(
        "anthropic",
        model="claude-sonnet-4-6",
        api_mode="cli",
        async_mode=True,
    )

    assert auxiliary.calls == []
    assert isinstance(client, auxiliary.module.AsyncAnthropicAuxiliaryClient)
    assert isinstance(client._sync_client._real_client, handler.ClaudeCliAnthropicClient)
    assert model == "claude-sonnet-4-6"
    client._sync_client.close()


def test_primary_runtime_restore_keeps_cli_client(monkeypatch):
    install_config(monkeypatch, {})
    native_client = object()
    install_anthropic_adapter(monkeypatch, native_client)
    handler = load_handler(monkeypatch)
    assert handler._patch_anthropic_adapter() is True
    run_agent = make_fake_run_agent_module()
    assert handler._patch_aiagent_module(run_agent) is True

    agent = run_agent.AIAgent(
        provider="anthropic",
        api_mode="cli",
        model="claude-opus-4-7",
    )
    first_client = agent._anthropic_client
    assert isinstance(first_client, handler.ClaudeCliAnthropicClient)
    agent._anthropic_client = native_client
    agent._claude_cli_enabled = False

    assert agent._restore_primary_runtime() is True

    assert isinstance(agent._anthropic_client, handler.ClaudeCliAnthropicClient)
    assert agent._claude_cli_enabled is True
    first_client.close()
    agent._anthropic_client.close()


def test_switch_model_preserves_cli_mode_when_core_returns_anthropic_messages(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    native_client = object()
    install_anthropic_adapter(monkeypatch, native_client)
    handler = load_handler(monkeypatch)
    assert handler._patch_anthropic_adapter() is True
    run_agent = make_fake_run_agent_module()
    assert handler._patch_aiagent_module(run_agent) is True

    agent = run_agent.AIAgent(
        provider="anthropic",
        api_mode="cli",
        model="claude-opus-4-7",
    )
    first_client = agent._anthropic_client

    agent.switch_model(
        new_model="claude-sonnet-4-6",
        new_provider="anthropic",
        api_key="runtime-key",
        base_url="https://api.anthropic.com",
        api_mode="anthropic_messages",
    )

    assert agent.api_mode == "anthropic_messages"
    assert isinstance(agent._anthropic_client, handler.ClaudeCliAnthropicClient)
    assert agent._claude_cli_enabled is True
    assert agent._claude_cli_mode_requested is True
    first_client.close()
    agent._anthropic_client.close()


def test_switch_model_can_leave_cli_mode_when_provider_changes(monkeypatch):
    install_config(
        monkeypatch,
        {"provider": "anthropic", "default": "claude-opus-4-7", "api_mode": "cli"},
    )
    native_client = object()
    install_anthropic_adapter(monkeypatch, native_client)
    handler = load_handler(monkeypatch)
    assert handler._patch_anthropic_adapter() is True
    run_agent = make_fake_run_agent_module()
    assert handler._patch_aiagent_module(run_agent) is True

    agent = run_agent.AIAgent(
        provider="anthropic",
        api_mode="cli",
        model="claude-opus-4-7",
    )
    first_client = agent._anthropic_client

    agent.switch_model(
        new_model="openai/gpt-5.2",
        new_provider="openrouter",
        api_key="runtime-key",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
    )

    assert agent.api_mode == "chat_completions"
    assert agent._anthropic_client is None
    assert agent._claude_cli_enabled is False
    assert agent._claude_cli_mode_requested is False
    first_client.close()


def test_stream_json_input_shape_matches_claude_cli_user_message(monkeypatch):
    handler = load_handler(monkeypatch)

    payload = json.loads(handler._build_stream_json_input("hello"))

    assert payload == {
        "type": "user",
        "session_id": "",
        "parent_tool_use_id": None,
        "message": {"role": "user", "content": "hello"},
    }


def test_structured_output_is_returned_as_json_text(monkeypatch):
    handler = load_handler(monkeypatch)

    text, usage, session_id, model_id = handler._parse_cli_output(
        json.dumps(
            {
                "type": "result",
                "result": "Done",
                "structured_output": {"ok": True},
                "session_id": "session-1",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 3, "output_tokens": 4},
            }
        )
    )

    assert json.loads(text) == {"ok": True}
    assert usage["input_tokens"] == 3
    assert usage["output_tokens"] == 4
    assert session_id == "session-1"
    assert model_id == "claude-opus-4-7"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"type": "result", "result": "Done", "conversation_id": "conversation-1"}, "conversation-1"),
        (
            {
                "type": "result",
                "result": "Done",
                "message": {"role": "assistant", "content": [], "conversationId": "conversation-2"},
            },
            "conversation-2",
        ),
    ],
)
def test_parse_cli_output_reads_session_id_aliases(monkeypatch, payload, expected):
    handler = load_handler(monkeypatch)

    _, _, session_id, _ = handler._parse_cli_output(json.dumps(payload))

    assert session_id == expected


def test_create_message_uses_cli_reported_model(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path), session_mode="off")
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    stdout = json.dumps(
        {
            "type": "result",
            "result": "Done",
            "session_id": "session-1",
            "model": "claude-opus-4-7",
        }
    )
    monkeypatch.setattr(
        client,
        "_run_invocation",
        lambda invocation: types.SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )

    try:
        message = client.messages.create(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "hello"}],
        )
        assert message.model == "claude-opus-4-7"
    finally:
        client.close()


def test_create_message_reports_invalidated_cli_invocation_as_interrupted(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path), session_mode="off")
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    def run_and_abort(invocation):
        client.abort_active_invocations(reason="agent-interrupt")
        return types.SimpleNamespace(returncode=-15, stdout="", stderr="terminated")

    monkeypatch.setattr(client, "_run_invocation", run_and_abort)

    try:
        with pytest.raises(InterruptedError, match="interrupted or invalidated"):
            client.messages.create(
                model="claude-opus-4-7",
                messages=[{"role": "user", "content": "hello"}],
            )
    finally:
        client.close()


def test_create_message_respects_parent_interrupt_even_before_state_generation_changes(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path), session_mode="off")
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
        _interrupt_requested=False,
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    def run_and_mark_interrupted(invocation):
        parent._interrupt_requested = True
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"type": "result", "result": "stale response"}),
            stderr="",
        )

    monkeypatch.setattr(client, "_run_invocation", run_and_mark_interrupted)

    try:
        with pytest.raises(InterruptedError, match="interrupted or invalidated"):
            client.messages.create(
                model="claude-opus-4-7",
                messages=[{"role": "user", "content": "hello"}],
            )
    finally:
        client.close()


def test_run_invocation_aborts_promptly_when_invocation_is_invalidated(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path), session_mode="off", timeout_seconds=10)
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    invocation = handler._ClaudeCliInvocation(
        args=[sys.executable, "-c", "import time; time.sleep(30)"],
        env=dict(os.environ),
        stdin_text="",
        cleanup_callbacks=[],
        full_prompt="waiting",
        latest_user_only=False,
        request_fingerprint="fingerprint",
        parent_session_key="hermes-session",
        cli_session_id="",
        state_generation=0,
        no_output_timeout_seconds=9,
    )
    started = threading.Event()

    def invalidate_after_process_registers(process):
        original_register_process(process)
        started.set()
        client.abort_active_invocations(reason="agent-interrupt")

    original_register_process = client.register_process
    monkeypatch.setattr(client, "register_process", invalidate_after_process_registers)

    try:
        started_at = time.monotonic()
        with pytest.raises(InterruptedError, match="interrupted or invalidated"):
            client._run_invocation(invocation)
        assert started.is_set()
        assert time.monotonic() - started_at < 3
    finally:
        invocation.close()
        client.close()


def test_json_schema_omits_one_max_turn(monkeypatch):
    handler = load_handler(monkeypatch)

    config = handler.TransportConfig(max_turns="1", json_schema={"type": "object"})

    assert handler._effective_max_turns(config) == 0
    assert config.json_schema == json.dumps({"type": "object"}, ensure_ascii=True)


def test_preflight_reports_claude_cli_credentials_state(monkeypatch, tmp_path, caplog):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path))

    caplog.set_level(logging.WARNING)
    handler._preflight_claude_cli_credentials(config)
    assert "Claude CLI credentials were not found" in caplog.text
    assert str(tmp_path / ".credentials.json") in caplog.text

    caplog.clear()
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "token"}}),
        encoding="utf-8",
    )
    caplog.set_level(logging.INFO)
    handler._preflight_claude_cli_credentials(config)
    assert "Claude CLI credentials found" in caplog.text


def test_no_output_watchdog_uses_fresh_and_resume_profiles(monkeypatch):
    handler = load_handler(monkeypatch)

    assert handler._resolve_no_output_timeout_seconds(600, latest_user_only=False) == 480
    assert handler._resolve_no_output_timeout_seconds(600, latest_user_only=True) == 180
    assert handler._resolve_no_output_timeout_seconds(120, latest_user_only=False) == 119
    assert handler._resolve_no_output_timeout_seconds(120, latest_user_only=True) == 60


def test_live_session_args_preserve_permission_prompt_replay_and_normalize_unstable_values(monkeypatch):
    handler = load_handler(monkeypatch)

    live_args = handler._live_session_args(
        [
            "claude",
            "-p",
            "--input-format",
            "text",
            "--permission-prompt-tool",
            "mcp__auth__prompt",
            "--append-system-prompt-file",
            "X:/hermes-fixture/system.txt",
            "--mcp-config",
            "X:/hermes-fixture/mcp.json",
            "--session-id",
            "session-id-1",
            "--resume",
            "session-1",
        ]
    )

    assert live_args[live_args.index("--input-format") + 1] == "stream-json"
    assert live_args[live_args.index("--output-format") + 1] == "stream-json"
    assert live_args[live_args.index("--permission-prompt-tool") + 1] == "mcp__auth__prompt"
    assert live_args.count("--replay-user-messages") == 1
    assert "--session-id" not in live_args
    assert "--resume" not in live_args
    assert "X:/hermes-fixture/system.txt" in live_args

    normalized = handler._normalized_live_args(live_args)
    assert "X:/hermes-fixture/system.txt" not in normalized
    assert "session-1" not in normalized
    assert "session-id-1" not in normalized
    assert normalized[normalized.index("--mcp-config") + 1] == "<live-value>"

    fallback_args = handler._live_session_args(["claude", "-p"])
    assert fallback_args[fallback_args.index("--permission-prompt-tool") + 1] == "stdio"


def test_live_session_capacity_closes_oldest_idle_session(monkeypatch):
    handler = load_handler(monkeypatch)

    class FakeLiveSession:
        def __init__(self, last_used_at, *, active=False):
            self._last_used_at = last_used_at
            self.turn_active = active
            self.closed_reason = ""

        @property
        def last_used_at(self):
            return self._last_used_at

        def is_running(self):
            return not self.closed_reason

        def close(self, reason):
            self.closed_reason = reason
            handler._unregister_live_session(self)

    with handler._LIVE_SESSIONS_LOCK:
        handler._LIVE_SESSIONS.clear()
    sessions = [FakeLiveSession(index) for index in range(handler.LIVE_MAX_SESSIONS)]
    try:
        for session in sessions:
            handler._register_live_session(session)

        handler._ensure_live_session_capacity()

        assert sessions[0].closed_reason == "capacity"
        assert all(not session.closed_reason for session in sessions[1:])
    finally:
        for session in sessions:
            handler._unregister_live_session(session)


def test_live_session_capacity_rejects_when_all_sessions_are_active(monkeypatch):
    handler = load_handler(monkeypatch)

    class FakeLiveSession:
        turn_active = True

        def is_running(self):
            return True

    with handler._LIVE_SESSIONS_LOCK:
        handler._LIVE_SESSIONS.clear()
    sessions = [FakeLiveSession() for _ in range(handler.LIVE_MAX_SESSIONS)]
    try:
        for session in sessions:
            handler._register_live_session(session)

        with pytest.raises(RuntimeError, match="Too many Claude CLI live sessions"):
            handler._ensure_live_session_capacity()
    finally:
        for session in sessions:
            handler._unregister_live_session(session)


def test_prepare_invocation_uses_session_id_and_latest_user_resume(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path), session_mode="session-id")
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    first_kwargs = {
        "model": "claude-opus-4-7",
        "system": "system prompt",
        "messages": [{"role": "user", "content": "first"}],
    }

    first = client.prepare_invocation(first_kwargs)
    try:
        assert first.args[first.args.index("--output-format") + 1] == "stream-json"
        assert first.args[first.args.index("--input-format") + 1] == "stream-json"
        assert "--verbose" in first.args
        assert "--no-chrome" in first.args
        assert first.args[first.args.index("--tools") + 1] == ""
        assert first.args[first.args.index("--setting-sources") + 1] == "user"
        assert "--strict-mcp-config" not in first.args
        assert first.args[first.args.index("--allowedTools") + 1] == "mcp__hermes__*"
        assert "--mcp-config" in first.args
        assert first.args[first.args.index("--permission-prompt-tool") + 1] == (
            "mcp__hermes__claude_cli_permission_prompt"
        )
        assert "--input-format" in first.args
        assert "--session-id" in first.args
        session_id = first.args[first.args.index("--session-id") + 1]
        uuid.UUID(session_id)
        assert json.loads(first.stdin_text)["message"]["content"] == "User:\nfirst"
        client.record_invocation_success(session_id, first)
    finally:
        first.close()

    second_kwargs = {
        "model": "claude-opus-4-7",
        "system": "system prompt",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
        ],
    }
    second = client.prepare_invocation(second_kwargs)
    try:
        assert second.latest_user_only is True
        assert json.loads(second.stdin_text)["message"]["content"] == "second"
        assert second.args[second.args.index("--session-id") + 1] == session_id
    finally:
        second.close()


def test_prepare_invocation_normalizes_provider_prefix_and_version_dots(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path), session_mode="off")
    parent = types.SimpleNamespace(
        model="sonnet-4.6",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "anthropic/claude-opus-4.7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        assert invocation.args[invocation.args.index("--model") + 1] == "opus"
    finally:
        invocation.close()

    fallback_invocation = client.prepare_invocation(
        {
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        assert fallback_invocation.args[fallback_invocation.args.index("--model") + 1] == "sonnet"
    finally:
        fallback_invocation.close()

    haiku_invocation = client.prepare_invocation(
        {
            "model": "haiku-4.5",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        assert haiku_invocation.args[haiku_invocation.args.index("--model") + 1] == "haiku"
    finally:
        haiku_invocation.close()
        client.close()


def test_prepare_invocation_uses_hermes_mcp_config_by_default(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path))
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "first"}],
        }
    )
    try:
        assert "--strict-mcp-config" not in invocation.args
        assert invocation.args[invocation.args.index("--allowedTools") + 1] == "mcp__hermes__*"
        assert invocation.args[invocation.args.index("--permission-prompt-tool") + 1] == (
            "mcp__hermes__claude_cli_permission_prompt"
        )
        mcp_config_path = Path(invocation.args[invocation.args.index("--mcp-config") + 1])
        assert list(json.loads(mcp_config_path.read_text(encoding="utf-8"))["mcpServers"]) == ["hermes"]
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_can_use_strict_hermes_mcp_config(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path), claude_code_mcp_enabled=False)
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "first"}],
        }
    )
    try:
        assert "--strict-mcp-config" in invocation.args
        assert invocation.args[invocation.args.index("--allowedTools") + 1] == "mcp__hermes__*"
        mcp_config_path = Path(invocation.args[invocation.args.index("--mcp-config") + 1])
        assert list(json.loads(mcp_config_path.read_text(encoding="utf-8"))["mcpServers"]) == ["hermes"]
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_emits_optional_cli_controls(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        effort="low",
        add_dirs=("/opt/shared",),
        plugin_dirs=("/opt/data/plugin-a", "/opt/data/plugin-b"),
        debug_filter="api",
        debug_file="/opt/data/logs/claude-debug.log",
        disable_slash_commands=True,
        exclude_dynamic_system_prompt_sections=True,
        tools="default",
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        assert invocation.args[invocation.args.index("--tools") + 1] == "default"
        assert invocation.args[invocation.args.index("--effort") + 1] == "low"
        assert invocation.args[invocation.args.index("--add-dir") + 1] == "/opt/shared"
        assert "--exclude-dynamic-system-prompt-sections" in invocation.args
        assert "--disable-slash-commands" in invocation.args
        assert invocation.args.count("--plugin-dir") == 2
        assert invocation.args[invocation.args.index("--debug") + 1] == "api"
        assert invocation.args[invocation.args.index("--debug-file") + 1] == "/opt/data/logs/claude-debug.log"
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_emits_all_configured_cli_controls(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        extra_args=("--brief",),
        include_partial_messages=False,
        include_hook_events=True,
        replay_user_messages=True,
        system_prompt_mode="replace",
        setting_sources="user,local",
        settings={"hooks": {}},
        agent="code-reviewer",
        agents={
            "code-reviewer": {
                "description": "Reviews code changes when review is useful.",
                "prompt": "You are a code reviewer.",
                "model": "sonnet",
            }
        },
        tools=["Bash", "Read"],
        disallowed_tools=("Edit",),
        permission_mode="plan",
        permission_prompt_tool="mcp__auth__prompt",
        effort="medium",
        max_turns="3",
        max_budget_usd="0.5",
        fallback_model="sonnet",
        json_schema={"type": "object"},
        session_mode="off",
        session_name="named-session",
        no_session_persistence=True,
        mcp_enabled=False,
        max_mcp_output_tokens="12000",
        mcp_tool_result_char_limit="60000",
        add_dirs=["/opt/shared", "/opt/data"],
        plugin_dirs=["/opt/plugin"],
        debug_filter="api,hooks",
        debug_file="/tmp/claude-debug.log",
        disable_slash_commands=True,
        exclude_dynamic_system_prompt_sections=True,
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "system": "system prompt",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        assert "--include-partial-messages" not in invocation.args
        assert "--include-hook-events" in invocation.args
        assert "--replay-user-messages" in invocation.args
        assert "--system-prompt-file" in invocation.args
        assert invocation.args[invocation.args.index("--setting-sources") + 1] == "user,local"
        assert json.loads(invocation.args[invocation.args.index("--settings") + 1]) == {
            "hooks": {},
            "fastMode": False,
        }
        assert invocation.args[invocation.args.index("--agent") + 1] == "code-reviewer"
        assert json.loads(invocation.args[invocation.args.index("--agents") + 1]) == {
            "code-reviewer": {
                "description": "Reviews code changes when review is useful.",
                "prompt": "You are a code reviewer.",
                "model": "sonnet",
            }
        }
        assert invocation.args[invocation.args.index("--tools") + 1] == "Bash,Read"
        assert invocation.args[invocation.args.index("--disallowedTools") + 1] == "Edit"
        assert invocation.args[invocation.args.index("--permission-mode") + 1] == "plan"
        assert invocation.args[invocation.args.index("--permission-prompt-tool") + 1] == "mcp__auth__prompt"
        assert invocation.args[invocation.args.index("--effort") + 1] == "medium"
        assert invocation.args[invocation.args.index("--max-turns") + 1] == "3"
        assert invocation.args[invocation.args.index("--max-budget-usd") + 1] == "0.5"
        assert invocation.args[invocation.args.index("--fallback-model") + 1] == "sonnet"
        assert invocation.args[invocation.args.index("--json-schema") + 1] == json.dumps({"type": "object"}, ensure_ascii=True)
        assert invocation.args[invocation.args.index("--name") + 1] == "named-session"
        assert "--no-session-persistence" in invocation.args
        assert "--session-id" not in invocation.args
        assert invocation.args[invocation.args.index("--add-dir") + 1:invocation.args.index("--add-dir") + 3] == [
            "/opt/shared",
            "/opt/data",
        ]
        assert invocation.args[invocation.args.index("--plugin-dir") + 1] == "/opt/plugin"
        assert invocation.args[invocation.args.index("--debug") + 1] == "api,hooks"
        assert invocation.args[invocation.args.index("--debug-file") + 1] == "/tmp/claude-debug.log"
        assert "--disable-slash-commands" in invocation.args
        assert "--exclude-dynamic-system-prompt-sections" in invocation.args
        assert invocation.args[-1] == "--brief"
        assert invocation.env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
        assert invocation.env["MAX_MCP_OUTPUT_TOKENS"] == "12000"
    finally:
        invocation.close()
        client.close()


def test_extra_args_are_normalized_away_from_hook_owned_flags(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        extra_args=[
            "--brief",
            "--setting-sources",
            "project",
            "--permission-mode=bypassPermissions",
            "--output-format",
            "text",
            "--mcp-config=/tmp/foreign-mcp.json",
            "--allowedTools",
            "mcp__foreign__*",
            "--dangerously-skip-permissions",
            "--model claude-haiku-4-5",
        ],
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        assert config.extra_args == ("--brief",)
        assert invocation.args[invocation.args.index("--setting-sources") + 1] == "user"
        assert invocation.args[invocation.args.index("--output-format") + 1] == "stream-json"
        assert invocation.args[invocation.args.index("--model") + 1] == "opus"
        assert "bypassPermissions" not in invocation.args
        assert "/tmp/foreign-mcp.json" not in invocation.args
        assert "mcp__foreign__*" not in invocation.args
        assert "--dangerously-skip-permissions" not in invocation.args
        assert invocation.args[-1] == "--brief"
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_emits_adaptive_thinking_settings(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        settings={"permissions": {"defaultMode": "auto"}},
        thinking={"type": "adaptive", "display": "summarized"},
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        settings = json.loads(invocation.args[invocation.args.index("--settings") + 1])
        assert settings["permissions"]["defaultMode"] == "auto"
        assert settings["alwaysThinkingEnabled"] is True
        assert settings["showThinkingSummaries"] is True
        assert settings["env"]["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] == "0"
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_emits_fixed_budget_thinking_settings(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        thinking={"type": "enabled", "display": "omitted", "budget_tokens": 12000},
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        settings = json.loads(invocation.args[invocation.args.index("--settings") + 1])
        assert settings["alwaysThinkingEnabled"] is True
        assert settings["showThinkingSummaries"] is False
        assert settings["env"]["MAX_THINKING_TOKENS"] == "12000"
        assert settings["env"]["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] == "1"
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_emits_fast_mode_settings(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        settings={"permissions": {"defaultMode": "auto"}},
        fast_mode=True,
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        settings = json.loads(invocation.args[invocation.args.index("--settings") + 1])
        assert settings["permissions"]["defaultMode"] == "auto"
        assert settings["fastMode"] is True
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_emits_false_fast_mode_settings(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        settings={"permissions": {"defaultMode": "auto"}},
        fast_mode=False,
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        settings = json.loads(invocation.args[invocation.args.index("--settings") + 1])
        assert settings["permissions"]["defaultMode"] == "auto"
        assert settings["fastMode"] is False
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_emits_default_false_fast_mode_setting(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        settings={"permissions": {"defaultMode": "auto"}},
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        settings = json.loads(invocation.args[invocation.args.index("--settings") + 1])
        assert settings["permissions"]["defaultMode"] == "auto"
        assert settings["fastMode"] is False
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_merges_disabled_thinking_with_settings_file(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"permissions": {"defaultMode": "plan"}}, ensure_ascii=True),
        encoding="utf-8",
    )
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        settings=str(settings_path),
        thinking="disabled",
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    settings_arg = Path(invocation.args[invocation.args.index("--settings") + 1])
    try:
        assert settings_arg != settings_path
        settings = json.loads(settings_arg.read_text(encoding="utf-8"))
        assert settings["permissions"]["defaultMode"] == "plan"
        assert settings["alwaysThinkingEnabled"] is False
        assert settings["env"]["MAX_THINKING_TOKENS"] == "0"
    finally:
        invocation.close()
        client.close()
    assert not settings_arg.exists()


def test_prepare_invocation_clears_ambient_claude_provider_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-api-key")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/host/.claude")
    monkeypatch.setenv("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector")
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path))
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)

    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        assert "ANTHROPIC_API_KEY" not in invocation.env
        assert "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST" not in invocation.env
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in invocation.env
        assert invocation.env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    finally:
        invocation.close()
        client.close()


def test_run_invocation_kills_silent_process_after_no_output_watchdog(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path), timeout_seconds=10)
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    invocation = handler._ClaudeCliInvocation(
        args=[sys.executable, "-c", "import time; time.sleep(5)"],
        env=dict(os.environ),
        stdin_text="",
        cleanup_callbacks=[],
        full_prompt="silent",
        latest_user_only=False,
        request_fingerprint="fingerprint",
        parent_session_key="hermes-session",
        cli_session_id="",
        state_generation=0,
        no_output_timeout_seconds=1,
    )

    try:
        with pytest.raises(RuntimeError, match="produced no output"):
            client._run_invocation(invocation)
    finally:
        invocation.close()
        client.close()


def test_abort_active_invocations_invalidates_session_and_terminates_process(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path), session_mode="session-id")
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    client.register_process(process)
    with client._state_lock:
        client._claude_session_started = True
        client._claude_session_id = "stale-session"
        client._last_full_prompt = "previous prompt"
        client._last_request_fingerprint = "previous fingerprint"
        previous_epoch = client._session_epoch
        previous_generation = client._state_generation

    try:
        client.abort_active_invocations(reason="agent-interrupt")
        process.wait(timeout=5)

        assert process.poll() is not None
        assert client._claude_session_started is False
        assert client._claude_session_id == ""
        assert client._last_full_prompt == ""
        assert client._session_epoch == previous_epoch + 1
        assert client._state_generation == previous_generation + 1
    finally:
        try:
            if process.poll() is None:
                process.kill()
        finally:
            client.unregister_process(process)
            client.close()


def test_live_session_reuses_process_and_owns_startup_artifacts(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    script = tmp_path / "fake_live_cli.py"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "count = 0",
                "for raw in sys.stdin:",
                "    count += 1",
                "    payload = json.loads(raw)",
                "    content = payload['message']['content']",
                "    print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': f'{count}:{content}'}}}), flush=True)",
                "    print(json.dumps({'type': 'result', 'result': f'{count}:{content}', 'session_id': 'live-session', 'usage': {'input_tokens': count, 'output_tokens': count + 1}}), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        timeout_seconds=10,
        live_idle_timeout_seconds=60,
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    cleanup_calls = []
    first = handler._ClaudeCliInvocation(
        args=[sys.executable, "-u", str(script)],
        env=dict(os.environ),
        stdin_text=handler._build_stream_json_input("one"),
        cleanup_callbacks=[lambda: cleanup_calls.append("startup")],
        full_prompt="one",
        latest_user_only=False,
        request_fingerprint="fingerprint",
        parent_session_key="hermes-session",
        cli_session_id="cli-session",
        state_generation=0,
        no_output_timeout_seconds=3,
    )
    session = handler._ClaudeCliLiveSession(
        client=client,
        invocation=first,
        args=first.args,
        fingerprint="fingerprint",
    )
    try:
        first_lines = []
        for _ in session.iter_turn(first, lambda line: first_lines.append(line)):
            pass
        first.close()
        first_text, _, first_session_id, _ = handler._parse_cli_output("\n".join(first_lines))
        assert first_text == "1:one"
        assert first_session_id == "live-session"
        assert cleanup_calls == []

        second = handler._ClaudeCliInvocation(
            args=[sys.executable, "-u", str(script)],
            env=dict(os.environ),
            stdin_text=handler._build_stream_json_input("two"),
            cleanup_callbacks=[lambda: cleanup_calls.append("turn")],
            full_prompt="one\n\ntwo",
            latest_user_only=True,
            request_fingerprint="fingerprint",
            parent_session_key="hermes-session",
            cli_session_id="cli-session",
            state_generation=0,
            no_output_timeout_seconds=3,
        )
        try:
            second_lines = []
            for _ in session.iter_turn(second, lambda line: second_lines.append(line)):
                pass
            second_text, _, _, _ = handler._parse_cli_output("\n".join(second_lines))
            assert second_text == "2:two"
        finally:
            second.close()
        assert cleanup_calls == ["turn"]
    finally:
        session.close("test")
        client.close()

    assert cleanup_calls == ["turn", "startup"]


def test_live_session_reports_parent_interrupt_as_interrupted(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    script = tmp_path / "fake_live_wait_cli.py"
    script.write_text(
        "\n".join(
            [
                "import sys",
                "import time",
                "for _raw in sys.stdin:",
                "    time.sleep(30)",
            ]
        ),
        encoding="utf-8",
    )
    config = handler.TransportConfig(config_dir=str(tmp_path), timeout_seconds=10)
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
        _interrupt_requested=False,
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    cleanup_calls = []
    invocation = handler._ClaudeCliInvocation(
        args=[sys.executable, "-u", str(script)],
        env=dict(os.environ),
        stdin_text=handler._build_stream_json_input("hello"),
        cleanup_callbacks=[lambda: cleanup_calls.append("startup")],
        full_prompt="hello",
        latest_user_only=False,
        request_fingerprint="fingerprint",
        parent_session_key="hermes-session",
        cli_session_id="cli-session",
        state_generation=0,
        no_output_timeout_seconds=9,
    )
    session = handler._ClaudeCliLiveSession(
        client=client,
        invocation=invocation,
        args=invocation.args,
        fingerprint="fingerprint",
    )
    timer = threading.Timer(0.05, lambda: setattr(parent, "_interrupt_requested", True))

    try:
        timer.start()
        started_at = time.monotonic()
        with pytest.raises(InterruptedError, match="interrupted or invalidated"):
            for _ in session.iter_turn(invocation, lambda line: None):
                pass
        assert time.monotonic() - started_at < 3
        assert session.is_running() is False
        assert cleanup_calls == ["startup"]
    finally:
        timer.cancel()
        invocation.close()
        session.close("test")
        client.close()


def test_live_session_error_includes_cli_auth_and_rate_limit_diagnostics(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    script = tmp_path / "fake_live_error_cli.py"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'apiKeySource': 'none', 'model': 'opus', 'claude_code_version': '9.9.9'}), flush=True)",
                "for _raw in sys.stdin:",
                "    print(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'rateLimitType': 'five_hour', 'isUsingOverage': False, 'overageStatus': 'rejected', 'resetsAt': 1777668000}}), flush=True)",
                "    print(json.dumps({'type': 'result', 'is_error': True, 'result': 'API Error: 400 {\"error\":\"limit\"}'}), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    config = handler.TransportConfig(config_dir=str(tmp_path), timeout_seconds=10)
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    invocation = handler._ClaudeCliInvocation(
        args=[sys.executable, "-u", str(script)],
        env=dict(os.environ),
        stdin_text=handler._build_stream_json_input("hello"),
        cleanup_callbacks=[],
        full_prompt="hello",
        latest_user_only=False,
        request_fingerprint="fingerprint",
        parent_session_key="hermes-session",
        cli_session_id="cli-session",
        state_generation=0,
        no_output_timeout_seconds=3,
    )
    session = handler._ClaudeCliLiveSession(
        client=client,
        invocation=invocation,
        args=invocation.args,
        fingerprint="fingerprint",
    )
    try:
        with pytest.raises(RuntimeError) as exc_info:
            list(session.iter_turn(invocation, lambda line: None))
        message = str(exc_info.value)
        assert "Claude CLI live session failed" in message
        assert "apiKeySource=none" in message
        assert "model=opus" in message
        assert "isUsingOverage=False" in message
        assert "overageStatus=rejected" in message
        assert "resetsAt=2026-05-01T20:40:00Z" in message
    finally:
        session.close("test")
        invocation.close()
        client.close()


def test_parse_cli_output_raises_error_with_cli_diagnostics(monkeypatch):
    handler = load_handler(monkeypatch)
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "apiKeySource": "none",
                    "model": "opus",
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "result": "API Error: 400 limit",
                }
            ),
        ]
    )

    with pytest.raises(RuntimeError) as exc_info:
        handler._parse_cli_output(stdout)

    assert "Claude CLI failed" in str(exc_info.value)
    assert "apiKeySource=none" in str(exc_info.value)
    assert "model=opus" in str(exc_info.value)


def test_latest_only_invocation_uses_short_lived_fallback_when_no_live_session(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path))
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    invocation = handler._ClaudeCliInvocation(
        args=["claude", "-p", "--session-id", "cli-session"],
        env=dict(os.environ),
        stdin_text=handler._build_stream_json_input("next"),
        cleanup_callbacks=[],
        full_prompt="previous\n\nnext",
        latest_user_only=True,
        request_fingerprint="fingerprint",
        parent_session_key="hermes-session",
        cli_session_id="cli-session",
        state_generation=0,
        no_output_timeout_seconds=3,
    )
    try:
        assert client.live_session_for_invocation(invocation) is None
        assert client._live_session is None
    finally:
        invocation.close()
        client.close()


def test_stream_context_can_use_live_session(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    script = tmp_path / "fake_stream_live_cli.py"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "for raw in sys.stdin:",
                "    payload = json.loads(raw)",
                "    content = payload['message']['content']",
                "    print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': content}}}), flush=True)",
                "    print(json.dumps({'type': 'result', 'result': content, 'session_id': 'stream-live'}), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        timeout_seconds=10,
        live_idle_timeout_seconds=60,
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    invocation = handler._ClaudeCliInvocation(
        args=[sys.executable, "-u", str(script)],
        env=dict(os.environ),
        stdin_text=handler._build_stream_json_input("hello"),
        cleanup_callbacks=[],
        full_prompt="hello",
        latest_user_only=False,
        request_fingerprint="fingerprint",
        parent_session_key="hermes-session",
        cli_session_id="cli-session",
        state_generation=0,
        no_output_timeout_seconds=3,
    )
    monkeypatch.setattr(client, "prepare_invocation", lambda api_kwargs, force_streaming=False: invocation)

    try:
        with client.messages.stream(model="claude-opus-4-7", messages=[]) as stream:
            events = list(stream)
            message = stream.get_final_message()
        assert events[0].type == "content_block_delta"
        assert events[0].delta.text == "hello"
        assert message.content[0].text == "hello"
    finally:
        client.close()


def test_stream_context_reports_parent_interrupt_as_interrupted(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    script = tmp_path / "fake_stream_wait_cli.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    config = handler.TransportConfig(config_dir=str(tmp_path), session_mode="off", timeout_seconds=10)
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
        _interrupt_requested=False,
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    invocation = handler._ClaudeCliInvocation(
        args=[sys.executable, "-u", str(script)],
        env=dict(os.environ),
        stdin_text=handler._build_stream_json_input("hello"),
        cleanup_callbacks=[],
        full_prompt="hello",
        latest_user_only=False,
        request_fingerprint="fingerprint",
        parent_session_key="hermes-session",
        cli_session_id="",
        state_generation=0,
        no_output_timeout_seconds=9,
    )
    monkeypatch.setattr(client, "prepare_invocation", lambda api_kwargs, force_streaming=False: invocation)
    timer = threading.Timer(0.05, lambda: setattr(parent, "_interrupt_requested", True))

    try:
        timer.start()
        started_at = time.monotonic()
        with pytest.raises(InterruptedError, match="interrupted or invalidated"):
            with client.messages.stream(model="claude-opus-4-7", messages=[]) as stream:
                list(stream)
        assert time.monotonic() - started_at < 3
    finally:
        timer.cancel()
        client.close()


def test_unconsumed_stream_context_closes_unused_live_session(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    script = tmp_path / "fake_unconsumed_live_cli.py"
    script.write_text(
        "\n".join(
            [
                "import sys",
                "for _raw in sys.stdin:",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        timeout_seconds=10,
        live_idle_timeout_seconds=60,
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    cleanup_calls = []
    invocation = handler._ClaudeCliInvocation(
        args=[sys.executable, "-u", str(script)],
        env=dict(os.environ),
        stdin_text=handler._build_stream_json_input("hello"),
        cleanup_callbacks=[lambda: cleanup_calls.append("startup")],
        full_prompt="hello",
        latest_user_only=False,
        request_fingerprint="fingerprint",
        parent_session_key="hermes-session",
        cli_session_id="cli-session",
        state_generation=0,
        no_output_timeout_seconds=3,
    )
    monkeypatch.setattr(client, "prepare_invocation", lambda api_kwargs, force_streaming=False: invocation)

    try:
        with client.messages.stream(model="claude-opus-4-7", messages=[]):
            assert client._live_session is not None
        assert client._live_session is None
        assert cleanup_calls == ["startup"]
    finally:
        client.close()


def test_prepare_invocation_materializes_data_image_with_stable_path_and_native_stream_block(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    image_bytes = b"fake-png"
    image_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    config = handler.TransportConfig(config_dir=str(tmp_path))
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    image_path = None

    kwargs = {
        "model": "claude-opus-4-7",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
    }
    invocation = client.prepare_invocation(kwargs)
    try:
        content = json.loads(invocation.stdin_text)["message"]["content"]
        assert content == [
            {"type": "text", "text": "look"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                },
            },
        ]
        image_ref = next(line for line in invocation.full_prompt.splitlines() if line.startswith("@"))
        image_path = Path(image_ref[1:])
        assert image_path.suffix == ".png"
        assert image_path.parent.name == ".hermes-claude-cli-images"
        assert image_path.read_bytes() == image_bytes
    finally:
        invocation.close()

    assert image_path is not None
    assert image_path.exists()

    second = client.prepare_invocation(kwargs)
    try:
        second_ref = next(line for line in second.full_prompt.splitlines() if line.startswith("@"))
        assert second_ref == f"@{image_path}"
    finally:
        second.close()
        client.close()


def test_image_cache_root_falls_back_to_temp_dir_for_invalid_config_dir(monkeypatch):
    handler = load_handler(monkeypatch)

    root = handler._image_cache_root(object())

    assert root == Path(tempfile.gettempdir()) / "hermes-claude-cli-images"


def test_prepare_invocation_exposes_hermes_tools_through_mcp(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path))

    class Parent:
        model = "claude-opus-4-7"
        session_id = "hermes-session"
        valid_tool_names = {"echo_tool"}

        def _invoke_tool(self, name, arguments, task_id, tool_call_id=None):
            return "ok"

    client = handler.ClaudeCliAnthropicClient(parent_agent=Parent(), config=config)
    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "name": "echo_tool",
                    "description": "Echo test tool",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        }
    )
    try:
        mcp_config_path = Path(invocation.args[invocation.args.index("--mcp-config") + 1])
        mcp_config = json.loads(mcp_config_path.read_text(encoding="utf-8"))
        assert list(mcp_config["mcpServers"]) == ["hermes"]
        hermes_server = mcp_config["mcpServers"]["hermes"]
        assert hermes_server["type"] == "http"
        assert hermes_server["url"].startswith("http://127.0.0.1:")
        assert hermes_server["headers"]["Authorization"].startswith("Bearer ")
        assert invocation.args[invocation.args.index("--allowedTools") + 1] == "mcp__hermes__*"
        assert "--strict-mcp-config" not in invocation.args
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_mcp_disabled_allows_claude_code_mcp_by_default(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(config_dir=str(tmp_path), mcp_enabled=False)
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        assert "--mcp-config" not in invocation.args
        assert "--strict-mcp-config" not in invocation.args
        assert "--allowedTools" not in invocation.args
    finally:
        invocation.close()
        client.close()


def test_prepare_invocation_mcp_disabled_can_disable_claude_code_mcp(monkeypatch, tmp_path):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig(
        config_dir=str(tmp_path),
        mcp_enabled=False,
        claude_code_mcp_enabled=False,
    )
    parent = types.SimpleNamespace(
        model="claude-opus-4-7",
        session_id="hermes-session",
        valid_tool_names=set(),
    )
    client = handler.ClaudeCliAnthropicClient(parent_agent=parent, config=config)
    invocation = client.prepare_invocation(
        {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    try:
        mcp_config_path = Path(invocation.args[invocation.args.index("--mcp-config") + 1])
        mcp_config = json.loads(mcp_config_path.read_text(encoding="utf-8"))
        assert mcp_config == {"mcpServers": {}}
        assert "--strict-mcp-config" in invocation.args
        assert "--allowedTools" not in invocation.args
    finally:
        invocation.close()
        client.close()


def test_mcp_bridge_exposes_internal_permission_prompt_by_default(monkeypatch):
    handler = load_handler(monkeypatch)
    config = handler.TransportConfig()
    parent = types.SimpleNamespace(session_id="hermes-session", valid_tool_names=set())

    tools, dispatch = handler._build_mcp_tool_entries(parent, {}, config)

    assert [tool["name"] for tool in tools] == ["claude_cli_permission_prompt"]
    assert dispatch == {}


def test_mcp_bridge_dispatches_hermes_tool_call_with_generated_tool_call_id(monkeypatch):
    handler = load_handler(monkeypatch)
    calls = []

    class Parent:
        session_id = "synthetic-session"
        valid_tool_names = {"mirror_value"}

        def _touch_activity(self):
            calls.append(("touch",))

        def _invoke_tool(self, name, arguments, task_id, tool_call_id=None):
            calls.append((name, arguments, task_id, tool_call_id))
            return {"mirrored": arguments["value"]}

    context = handler._McpBridgeContext(
        parent_agent=Parent(),
        tools=[],
        dispatch_by_exposed={"mirror_value": "mirror_value"},
        config=handler.TransportConfig(mcp_tool_result_char_limit=0),
    )

    result = context.call_tool("mirror_value", {"value": "bridge-ok"})

    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"mirrored": "bridge-ok"}
    assert calls[-1][0] == "mirror_value"
    assert calls[-1][1] == {"value": "bridge-ok"}
    assert calls[-1][2] == "synthetic-session"
    assert calls[-1][3].startswith("mcp-")


def test_permission_prompt_tool_routes_approval_result_to_claude_cli(monkeypatch):
    handler = load_handler(monkeypatch)
    fake_approval = install_fake_approval_module(monkeypatch, choice="once")
    config = handler.TransportConfig()
    parent = types.SimpleNamespace(session_id="hermes-session", valid_tool_names=set())
    context = handler._McpBridgeContext(
        parent_agent=parent,
        tools=[handler._permission_prompt_tool_entry()],
        dispatch_by_exposed={},
        config=config,
    )

    result = context.call_tool(
        "claude_cli_permission_prompt",
        {"tool_name": "Bash", "input": {"command": "rm -rf /tmp/demo"}},
    )

    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"behavior": "allow", "updatedInput": {"command": "rm -rf /tmp/demo"}}
    assert fake_approval.notifications[0]["command"] == "rm -rf /tmp/demo"
    assert fake_approval.notifications[0]["description"] == "Claude CLI requested permission for Bash"
    assert fake_approval.hook_events[0][1]["surface"] == "claude_cli_permission"


def test_permission_prompt_tool_denies_when_user_denies(monkeypatch):
    handler = load_handler(monkeypatch)
    install_fake_approval_module(monkeypatch, choice="deny")
    parent = types.SimpleNamespace(session_id="hermes-session", valid_tool_names=set())
    context = handler._McpBridgeContext(
        parent_agent=parent,
        tools=[handler._permission_prompt_tool_entry()],
        dispatch_by_exposed={},
        config=handler.TransportConfig(),
    )

    result = context.call_tool(
        "claude_cli_permission_prompt",
        {"tool_name": "Write", "input": {"file_path": "/tmp/demo", "content": "x"}},
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload["behavior"] == "deny"
    assert "denied by user" in payload["message"]


def test_permission_prompt_tool_denies_when_gateway_adapter_is_unavailable(monkeypatch):
    handler = load_handler(monkeypatch)
    tools_pkg = types.ModuleType("tools")
    approval_mod = types.ModuleType("tools.approval")
    tools_pkg.approval = approval_mod
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.approval", approval_mod)
    parent = types.SimpleNamespace(session_id="hermes-session", valid_tool_names=set())
    context = handler._McpBridgeContext(
        parent_agent=parent,
        tools=[handler._permission_prompt_tool_entry()],
        dispatch_by_exposed={},
        config=handler.TransportConfig(),
    )

    result = context.call_tool(
        "claude_cli_permission_prompt",
        {"tool_name": "Bash", "input": {"command": "rm -rf /tmp/demo"}},
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload["behavior"] == "deny"
    assert "missing required capability" in payload["message"]
