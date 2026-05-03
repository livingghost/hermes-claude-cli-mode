"""Loopback MCP bridge used by Claude CLI mode."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from domain.config import TransportConfig
from domain.constants import (
    HOOK_NAME,
    HOOK_VERSION,
    MCP_ALLOWED_TOOLS,
    MCP_PERMISSION_PROMPT_TOOL,
    MCP_SERVER_NAME,
)
from .claude_cli import (
    _hash_json,
    _uses_internal_permission_prompt,
    _write_temp_json,
)

logger = logging.getLogger(__name__)

def _json_rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id if request_id is not None else None, "result": result}


def _json_rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id if request_id is not None else None,
        "error": {"code": code, "message": message},
    }


def _normalize_tool_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    normalized = dict(schema)
    if normalized.get("type") != "object":
        normalized["type"] = "object"
    if not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    return normalized


def _strip_mcp_prefix(name: str, valid_tool_names: set[str]) -> tuple[str, str]:
    if name in valid_tool_names:
        return name, name
    if name.startswith("mcp_"):
        stripped = name[len("mcp_") :]
        if stripped in valid_tool_names:
            return stripped, stripped
        return stripped, stripped
    return name, name


def _trim_mcp_tool_result(text: str, char_limit: int) -> str:
    if char_limit <= 0 or len(text) <= char_limit:
        return text
    omitted = len(text) - char_limit
    return f"{text[:char_limit]}\n\n[truncated by {HOOK_NAME}: {omitted} chars omitted]"


def _permission_prompt_tool_entry() -> dict[str, Any]:
    return {
        "name": MCP_PERMISSION_PROMPT_TOOL,
        "description": "Routes Claude CLI permission prompts through Hermes approval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "The Claude CLI tool requesting permission.",
                },
                "input": {
                    "type": "object",
                    "description": "The original input for the requested tool.",
                    "additionalProperties": True,
                },
            },
            "required": ["tool_name", "input"],
            "additionalProperties": True,
        },
    }


def _build_mcp_tool_entries(
    parent_agent: Any,
    api_kwargs: dict[str, Any],
    config: TransportConfig,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    raw_tools = api_kwargs.get("tools")
    valid_tool_names = set(getattr(parent_agent, "valid_tool_names", set()) or set())
    entries: list[dict[str, Any]] = []
    dispatch_by_exposed: dict[str, str] = {}
    seen: set[str] = set()

    if _uses_internal_permission_prompt(config):
        entries.append(_permission_prompt_tool_entry())
        seen.add(MCP_PERMISSION_PROMPT_TOOL)

    if isinstance(raw_tools, list):
        for tool in raw_tools:
            if not isinstance(tool, dict):
                continue
            raw_name = str(tool.get("name") or "").strip()
            if not raw_name:
                continue
            exposed_name, dispatch_name = _strip_mcp_prefix(raw_name, valid_tool_names)
            if not exposed_name or exposed_name in seen:
                continue
            seen.add(exposed_name)
            schema = tool.get("inputSchema") or tool.get("input_schema") or tool.get("parameters")
            entries.append(
                {
                    "name": exposed_name,
                    "description": str(tool.get("description") or ""),
                    "inputSchema": _normalize_tool_schema(schema),
                }
            )
            dispatch_by_exposed[exposed_name] = dispatch_name

    return entries, dispatch_by_exposed


def _permission_prompt_input(arguments: dict[str, Any]) -> Any:
    tool_input = arguments.get("input")
    if isinstance(tool_input, dict):
        return tool_input
    if tool_input is None:
        return {}
    return tool_input


def _format_permission_prompt_request(
    arguments: dict[str, Any],
) -> tuple[str, Any, str, str, str]:
    tool_name = (
        str(arguments.get("tool_name") or arguments.get("name") or "unknown").strip()
        or "unknown"
    )
    tool_input = _permission_prompt_input(arguments)
    command = ""
    if tool_name.lower() == "bash" and isinstance(tool_input, dict):
        command = str(tool_input.get("command") or "").strip()
    if not command:
        try:
            rendered_input = json.dumps(
                tool_input,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                default=str,
            )
        except Exception:
            rendered_input = str(tool_input)
        command = f"Claude CLI tool: {tool_name}\n{rendered_input}"
    description = f"Claude CLI requested permission for {tool_name}"
    pattern_key = f"claude-cli:{tool_name.lower()}:{_hash_json(tool_input)}"
    return tool_name, tool_input, command, description, pattern_key


class _HermesGatewayApprovalBridge:
    """Adapter around Hermes gateway approval internals used by Claude CLI permissions."""

    _REQUIRED_CAPABILITIES = (
        "_lock",
        "_gateway_queues",
        "_gateway_notify_cbs",
        "_ApprovalEntry",
    )

    def __init__(self, approval_module: Any) -> None:
        self._approval = approval_module

    @classmethod
    def load(cls) -> tuple[Any, str]:
        try:
            from tools import approval as approval_module
        except Exception as exc:
            return None, f"Hermes approval is unavailable: {exc}"
        return cls(approval_module), ""

    def request(
        self,
        *,
        session_key: str,
        command: str,
        description: str,
        pattern_key: str,
    ) -> dict[str, Any]:
        session_key = self._session_key(session_key)
        missing = self._missing_capabilities()
        if missing:
            return {
                "approved": False,
                "choice": "deny",
                "message": f"Hermes gateway approval is missing required capability: {missing}.",
            }

        if self._bypass_enabled(session_key):
            return {"approved": True, "choice": "bypass"}

        if session_key and self._is_approved(session_key, pattern_key):
            return {"approved": True, "choice": "approved"}

        notify_cb = self._notify_callback(session_key)
        if notify_cb is None:
            return {
                "approved": False,
                "choice": "deny",
                "message": "Hermes gateway approval is not available for this session.",
            }

        approval_data = {
            "command": command,
            "pattern_key": pattern_key,
            "pattern_keys": [pattern_key],
            "description": description,
        }
        try:
            entry = self._approval._ApprovalEntry(approval_data)
        except Exception as exc:
            return {
                "approved": False,
                "choice": "deny",
                "message": f"Hermes gateway approval entry could not be created: {exc}",
            }
        self._append_entry(session_key, entry)
        self._fire_hook(
            "pre_approval_request",
            command=command,
            description=description,
            pattern_key=pattern_key,
            pattern_keys=[pattern_key],
            session_key=session_key,
            surface="claude_cli_permission",
        )

        try:
            notify_cb(approval_data)
        except Exception as exc:
            logger.warning("%s: gateway approval notify failed: %s", HOOK_NAME, exc)
            self._remove_entry(session_key, entry)
            return {
                "approved": False,
                "choice": "deny",
                "message": "Failed to send approval request to Hermes gateway.",
            }

        resolved = self._wait_for_entry(entry)
        self._remove_entry(session_key, entry)
        choice = getattr(entry, "result", None)
        outcome = "timeout" if not resolved else (choice if choice else "timeout")

        self._fire_hook(
            "post_approval_response",
            command=command,
            description=description,
            pattern_key=pattern_key,
            pattern_keys=[pattern_key],
            session_key=session_key,
            surface="claude_cli_permission",
            choice=outcome,
        )

        if not resolved or choice is None or choice == "deny":
            reason = "timed out" if not resolved else "denied by user"
            return {
                "approved": False,
                "choice": outcome,
                "message": f"Claude CLI permission {reason}.",
            }

        if choice in {"session", "always"}:
            self._approve_session(session_key, pattern_key)

        return {"approved": True, "choice": choice}

    def _missing_capabilities(self) -> str:
        missing = [
            name
            for name in self._REQUIRED_CAPABILITIES
            if not hasattr(self._approval, name)
        ]
        return ", ".join(missing)

    def _session_key(self, session_key: str) -> str:
        if session_key:
            return session_key
        get_current_session_key = getattr(self._approval, "get_current_session_key", None)
        if not callable(get_current_session_key):
            return ""
        try:
            return get_current_session_key(default="") or ""
        except Exception:
            return ""

    def _bypass_enabled(self, session_key: str) -> bool:
        if os.getenv("HERMES_YOLO_MODE"):
            return True
        try:
            approval_mode = self._approval._get_approval_mode()
        except Exception:
            approval_mode = "manual"
        is_session_yolo_enabled = getattr(self._approval, "is_session_yolo_enabled", None)
        try:
            yolo_enabled = (
                bool(is_session_yolo_enabled(session_key))
                if callable(is_session_yolo_enabled)
                else False
            )
        except Exception:
            yolo_enabled = False
        return yolo_enabled or approval_mode == "off"

    def _is_approved(self, session_key: str, pattern_key: str) -> bool:
        is_approved = getattr(self._approval, "is_approved", None)
        if not callable(is_approved):
            return False
        try:
            return bool(is_approved(session_key, pattern_key))
        except Exception:
            return False

    def _notify_callback(self, session_key: str) -> Any:
        try:
            with self._approval._lock:
                return self._approval._gateway_notify_cbs.get(session_key)
        except Exception:
            return None

    def _append_entry(self, session_key: str, entry: Any) -> None:
        with self._approval._lock:
            self._approval._gateway_queues.setdefault(session_key, []).append(entry)

    def _remove_entry(self, session_key: str, entry: Any) -> None:
        try:
            with self._approval._lock:
                queue_items = self._approval._gateway_queues.get(session_key, [])
                if entry in queue_items:
                    queue_items.remove(entry)
                if not queue_items:
                    self._approval._gateway_queues.pop(session_key, None)
        except Exception:
            pass

    def _timeout_seconds(self) -> int:
        get_approval_config = getattr(self._approval, "_get_approval_config", None)
        if not callable(get_approval_config):
            return 300
        try:
            timeout = get_approval_config().get("gateway_timeout", 300)
            return int(timeout)
        except Exception:
            return 300

    def _wait_for_entry(self, entry: Any) -> bool:
        try:
            from tools.environments.base import touch_activity_if_due
        except Exception:
            touch_activity_if_due = None

        event = getattr(entry, "event", None)
        wait = getattr(event, "wait", None)
        if not callable(wait):
            return False

        now = time.monotonic()
        deadline = now + max(self._timeout_seconds(), 0)
        activity_state = {"last_touch": now, "start": now}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if wait(timeout=min(1.0, remaining)):
                return True
            if touch_activity_if_due is not None:
                touch_activity_if_due(activity_state, "waiting for user approval")

    def _fire_hook(self, hook_name: str, **kwargs: Any) -> None:
        fire_approval_hook = getattr(self._approval, "_fire_approval_hook", None)
        if not callable(fire_approval_hook):
            return
        try:
            fire_approval_hook(hook_name, **kwargs)
        except Exception:
            pass

    def _approve_session(self, session_key: str, pattern_key: str) -> None:
        approve_session = getattr(self._approval, "approve_session", None)
        if not callable(approve_session):
            return
        try:
            approve_session(session_key, pattern_key)
        except Exception:
            pass


def _request_gateway_permission(
    *,
    session_key: str,
    command: str,
    description: str,
    pattern_key: str,
) -> dict[str, Any]:
    bridge, error = _HermesGatewayApprovalBridge.load()
    if bridge is None:
        return {
            "approved": False,
            "choice": "deny",
            "message": error,
        }
    return bridge.request(
        session_key=session_key,
        command=command,
        description=description,
        pattern_key=pattern_key,
    )


def _permission_prompt_tool_response(arguments: Any, parent_agent: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        arguments = {}
    _tool_name, tool_input, command, description, pattern_key = _format_permission_prompt_request(arguments)
    session_key = (
        getattr(parent_agent, "_gateway_session_key", None)
        or getattr(parent_agent, "session_id", None)
        or ""
    )
    approval_token = None
    activity_callback_set = False
    try:
        try:
            from tools.approval import set_current_session_key

            approval_token = set_current_session_key(session_key)
        except Exception:
            approval_token = None

        try:
            from tools.environments.base import set_activity_callback

            touch = getattr(parent_agent, "_touch_activity", None)
            if callable(touch):
                set_activity_callback(touch)
                activity_callback_set = True
        except Exception:
            activity_callback_set = False

        try:
            approval = _request_gateway_permission(
                session_key=session_key,
                command=command,
                description=description,
                pattern_key=pattern_key,
            )
        except Exception as exc:
            approval = {"approved": False, "message": f"Hermes approval failed: {exc}"}
    finally:
        if activity_callback_set:
            try:
                from tools.environments.base import set_activity_callback

                set_activity_callback(None)
            except Exception:
                pass
        if approval_token is not None:
            try:
                from tools.approval import reset_current_session_key

                reset_current_session_key(approval_token)
            except Exception:
                pass
    if approval.get("approved"):
        payload = {
            "behavior": "allow",
            "updatedInput": tool_input if isinstance(tool_input, dict) else {},
        }
    else:
        payload = {
            "behavior": "deny",
            "message": str(approval.get("message") or "Permission denied by Hermes."),
        }
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=True)}], "isError": False}


class _McpBridgeContext:
    def __init__(
        self,
        *,
        parent_agent: Any,
        tools: list[dict[str, Any]],
        dispatch_by_exposed: dict[str, str],
        config: TransportConfig,
    ) -> None:
        self.parent_agent = parent_agent
        self.tools = tools
        self.dispatch_by_exposed = dispatch_by_exposed
        self.config = config

    def call_tool(self, tool_name: str, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            arguments = {}
        if tool_name == MCP_PERMISSION_PROMPT_TOOL:
            return _permission_prompt_tool_response(arguments, self.parent_agent)

        dispatch_name = self.dispatch_by_exposed.get(tool_name)
        if not dispatch_name:
            return {
                "content": [{"type": "text", "text": f"Tool not available: {tool_name}"}],
                "isError": True,
            }

        parent_agent = self.parent_agent
        task_id = (
            getattr(parent_agent, "_current_task_id", None)
            or getattr(parent_agent, "session_id", None)
            or "claude-cli-mode"
        )
        tool_call_id = f"mcp-{uuid.uuid4()}"

        approval_token = None
        activity_callback_set = False
        try:
            try:
                from tools.approval import set_current_session_key

                session_key = (
                    getattr(parent_agent, "_gateway_session_key", None)
                    or getattr(parent_agent, "session_id", None)
                    or ""
                )
                approval_token = set_current_session_key(session_key)
            except Exception:
                approval_token = None

            try:
                from tools.environments.base import set_activity_callback

                set_activity_callback(parent_agent._touch_activity)
                activity_callback_set = True
            except Exception:
                activity_callback_set = False

            result = parent_agent._invoke_tool(dispatch_name, arguments, task_id, tool_call_id=tool_call_id)
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=True)
            text = _trim_mcp_tool_result(text, self.config.mcp_tool_result_char_limit)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as exc:
            return {"content": [{"type": "text", "text": str(exc) or "tool execution failed"}], "isError": True}
        finally:
            if activity_callback_set:
                try:
                    from tools.environments.base import set_activity_callback

                    set_activity_callback(None)
                except Exception:
                    pass
            if approval_token is not None:
                try:
                    from tools.approval import reset_current_session_key

                    reset_current_session_key(approval_token)
                except Exception:
                    pass


class _McpBridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], manager: "_McpBridgeManager"):
        super().__init__(server_address, _McpBridgeRequestHandler)
        self.manager = manager


class _McpBridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = f"HermesClaudeCliMCP/{HOOK_VERSION}"

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s: MCP HTTP: " + format, HOOK_NAME, *args)

    def _write_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/mcp":
            self._write_json(404, _json_rpc_error(None, -32000, "Not found"))
            return

        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            self._write_json(401, _json_rpc_error(None, -32001, "Unauthorized"))
            return
        token = auth_header[len("Bearer ") :].strip()
        context = self.server.manager.get_context(token)  # type: ignore[attr-defined]
        if context is None:
            self._write_json(401, _json_rpc_error(None, -32001, "Unauthorized"))
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""

        try:
            parsed = json.loads(raw)
        except Exception:
            self._write_json(400, _json_rpc_error(None, -32700, "Parse error"))
            return

        messages = parsed if isinstance(parsed, list) else [parsed]
        responses = []
        for message in messages:
            if not isinstance(message, dict):
                responses.append(_json_rpc_error(None, -32600, "Invalid Request"))
                continue
            response = self.server.manager.handle_message(context, message)  # type: ignore[attr-defined]
            if response is not None:
                responses.append(response)

        if not responses:
            self.send_response(202)
            self.end_headers()
            return
        self._write_json(200, responses if isinstance(parsed, list) else responses[0])


class _McpBridgeRegistration:
    def __init__(
        self,
        *,
        manager: "_McpBridgeManager",
        token: str,
        url: str,
        config_path: str,
        allowed_tools: str,
    ) -> None:
        self.manager = manager
        self.token = token
        self.url = url
        self.config_path = config_path
        self.allowed_tools = allowed_tools

    def close(self) -> None:
        self.manager.unregister(self.token)
        try:
            Path(self.config_path).unlink(missing_ok=True)
        except Exception:
            pass


class _McpBridgeManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._server: _McpBridgeHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._contexts: dict[str, _McpBridgeContext] = {}

    def ensure_started(self) -> int:
        with self._lock:
            if self._server is None:
                server = _McpBridgeHTTPServer(("127.0.0.1", 0), self)
                thread = threading.Thread(
                    target=server.serve_forever,
                    name="hermes-claude-cli-mcp",
                    daemon=True,
                )
                thread.start()
                self._server = server
                self._thread = thread
                logger.info("%s: MCP bridge listening on 127.0.0.1:%s", HOOK_NAME, server.server_port)
            return int(self._server.server_port)

    def get_context(self, token: str) -> _McpBridgeContext | None:
        with self._lock:
            return self._contexts.get(token)

    def register(
        self,
        *,
        parent_agent: Any,
        api_kwargs: dict[str, Any],
        config: TransportConfig,
    ) -> _McpBridgeRegistration | None:
        tools, dispatch_by_exposed = _build_mcp_tool_entries(parent_agent, api_kwargs, config)
        if not config.mcp_enabled or not tools:
            return None

        port = self.ensure_started()
        token = secrets.token_urlsafe(32)
        context = _McpBridgeContext(
            parent_agent=parent_agent,
            tools=tools,
            dispatch_by_exposed=dispatch_by_exposed,
            config=config,
        )
        with self._lock:
            self._contexts[token] = context

        url = f"http://127.0.0.1:{port}/mcp"
        hermes_server = {
            "type": "http",
            "url": url,
            "headers": {"Authorization": f"Bearer {token}"},
        }
        mcp_config = {
            "mcpServers": {
                MCP_SERVER_NAME: hermes_server,
            }
        }
        config_path = _write_temp_json(prefix="hermes-claude-cli-mcp-", value=mcp_config)
        return _McpBridgeRegistration(
            manager=self,
            token=token,
            url=url,
            config_path=config_path,
            allowed_tools=MCP_ALLOWED_TOOLS,
        )

    def unregister(self, token: str) -> None:
        with self._lock:
            self._contexts.pop(token, None)

    def handle_message(self, context: _McpBridgeContext, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}

        if method == "initialize":
            client_version = params.get("protocolVersion")
            protocol_version = client_version if client_version in {"2025-03-26", "2024-11-05"} else "2025-03-26"
            return _json_rpc_result(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "hermes", "version": HOOK_VERSION},
                },
            )
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "tools/list":
            return _json_rpc_result(request_id, {"tools": context.tools})
        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(tool_name, str):
                return _json_rpc_error(request_id, -32602, "Invalid tool name")
            return _json_rpc_result(request_id, context.call_tool(tool_name, arguments))
        return _json_rpc_error(request_id, -32601, f"Method not found: {method}")


_MCP_BRIDGE_MANAGER = _McpBridgeManager()
