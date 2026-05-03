"""Constants for the Hermes Claude CLI mode hook."""

from __future__ import annotations


HOOK_NAME = "hermes-claude-cli-mode"
HOOK_VERSION = "0.2.0"
CONFIG_KEY = "claude_cli"
ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"
PATCH_ATTR = "_hermes_claude_cli_wrapped"
ORIGINAL_ATTR = "_hermes_claude_cli_original"
IMPORT_FINDER_ATTR = "_hermes_claude_cli_import_finder"
SKIP_IMPORT_BOOTSTRAP_ENV = "HERMES_HOOK_SKIP_IMPORT_BOOTSTRAP"
CLI_OUTPUT_FORMAT = "stream-json"
CLI_INPUT_FORMAT = "stream-json"
MCP_SERVER_NAME = "hermes"
MCP_ALLOWED_TOOLS = "mcp__hermes__*"
MCP_PERMISSION_PROMPT_TOOL = "claude_cli_permission_prompt"
MCP_PERMISSION_PROMPT_ALLOWED_TOOL = f"mcp__{MCP_SERVER_NAME}__{MCP_PERMISSION_PROMPT_TOOL}"
CLAUDE_CLI_MODEL_ALIASES = {
    "opus": "opus",
    "opus-4.7": "opus",
    "claude-opus-4-7": "opus",
    "sonnet": "sonnet",
    "sonnet-4.6": "sonnet",
    "claude-sonnet-4-6": "sonnet",
    "haiku": "haiku",
    "haiku-4.5": "haiku",
    "claude-haiku-4-5": "haiku",
}
IMAGE_MEDIA_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
FRESH_NO_OUTPUT_TIMEOUT = {"ratio": 0.8, "min": 180.0, "max": 600.0}
RESUME_NO_OUTPUT_TIMEOUT = {"ratio": 0.3, "min": 60.0, "max": 180.0}
LIVE_SESSION_IDLE_TIMEOUT_SECONDS = 600.0
LIVE_MAX_SESSIONS = 16
LIVE_MAX_STDERR_CHARS = 64 * 1024
LIVE_MAX_TURN_OUTPUT_CHARS = 2 * 1024 * 1024
LIVE_NORMALIZED_VALUE_FLAGS = frozenset(
    {
        "--mcp-config",
        "--session-id",
    }
)
LIVE_PROCESS_OMITTED_VALUE_FLAGS = frozenset(
    {
        "--resume",
        "--session-id",
        "-r",
    }
)
LIVE_OMITTED_VALUE_FLAGS = frozenset(
    {
        "--append-system-prompt-file",
        "--resume",
        "--system-prompt-file",
        "-r",
    }
)
EXTRA_ARG_CONTROLLED_VALUE_FLAGS = frozenset(
    {
        "--add-dir",
        "--agent",
        "--agents",
        "--allowedTools",
        "--append-system-prompt",
        "--append-system-prompt-file",
        "--api-key",
        "--api-key-helper",
        "--base-url",
        "--config-dir",
        "--cwd",
        "--debug",
        "--debug-file",
        "--disallowedTools",
        "--effort",
        "--fallback-model",
        "--fast-mode",
        "--fastMode",
        "--input-format",
        "--json-schema",
        "--max-budget-usd",
        "--max-turns",
        "--mcp-config",
        "--model",
        "--name",
        "--oauth-token",
        "--output-format",
        "--permission-mode",
        "--permission-prompt-tool",
        "--plugin-dir",
        "--resume",
        "--session-id",
        "--setting-sources",
        "--settings",
        "--system-prompt",
        "--system-prompt-file",
        "--thinking",
        "--tools",
        "-r",
    }
)
EXTRA_ARG_CONTROLLED_SWITCH_FLAGS = frozenset(
    {
        "--bare",
        "--continue",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
        "--fork-session",
        "--from-pr",
        "--include-hook-events",
        "--include-partial-messages",
        "--no-chrome",
        "--no-session-persistence",
        "--print",
        "--replay-user-messages",
        "--strict-mcp-config",
        "--verbose",
        "-c",
        "-p",
    }
)
CLAUDE_CLI_CLEAR_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY_OLD",
        "ANTHROPIC_API_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_UNIX_SOCKET",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_PLUGIN_CACHE_DIR",
        "CLAUDE_CODE_PLUGIN_SEED_DIR",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
        "CLAUDE_CODE_REMOTE",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_COWORK_PLUGINS",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        "OTEL_LOGS_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_SDK_DISABLED",
        "OTEL_TRACES_EXPORTER",
    }
)
