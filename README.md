# hermes-claude-cli-mode

Gateway hook that adds Claude CLI mode for `provider: anthropic`.

This lets Hermes keep the normal Anthropic provider entrypoint while the model
request is executed by `claude -p`. Select this mode with `api_mode: cli`.

## Compatibility

This hook is implemented and tested against the following baseline:

| Component | Baseline |
|-----------|----------|
| Hook | `0.2.1` |
| Claude Code CLI | `2.1.123` (`claude --version`) |
| `hermes-agent` | `0.11.0`, `main` commit `d9bf09372` |
| Verification date | `2026-05-07` |

Newer versions may work, but revalidate this hook when Claude CLI changes its
`stream-json` protocol, permission prompt behavior, session flags, MCP flags, or
Claude Code settings format. Also revalidate when Hermes changes provider setup,
gateway approval internals, gateway sessions, or tool invocation behavior.

## Status

Initial support:

- patches `agent.anthropic_adapter.build_anthropic_client()`
- patches `agent.auxiliary_client.resolve_provider_client()` so auxiliary `main`, `auto`, and Anthropic tasks follow Claude CLI mode
- activates only when `provider: anthropic` is paired with `api_mode: cli`
- bypasses Hermes' native Anthropic credential resolver for CLI-mode runtime setup
- keeps a long-lived `claude -p` process for safe append-only continuations
- passes Hermes `system` through Claude CLI's system prompt file argument
- sends the request body through Claude CLI's `--input-format stream-json`
- supports Claude CLI streaming output for Hermes `messages.stream()`
- applies `timeout_seconds` to both non-streaming and streaming CLI calls
- terminates CLI calls that produce no output for the built-in fresh/resume watchdog window
- caps live Claude CLI processes and closes the oldest idle live process when capacity is reached
- supports opt-in Claude CLI session continuation with append-only transcript checks
- exposes common Claude CLI control flags through hook config
- runs Claude CLI with `--output-format stream-json --input-format stream-json --verbose`
- disables Claude CLI built-in tools by default with `--tools ""`
- exposes Hermes tools to Claude CLI through a temporary MCP config
- clears inherited Claude/Anthropic/OTEL provider environment overrides before launching Claude CLI
- converts Claude CLI output into an Anthropic Message-like object
- exposes Hermes tools to Claude CLI through a loopback MCP bridge
- routes Claude CLI permission prompts through Hermes gateway approval when `permission_prompt_tool` is left empty
- serializes Claude CLI invocations per Hermes client so one Claude session is not written by overlapping subprocesses
- invalidates Claude CLI session state when Hermes rewrites, compresses, or resets the transcript
- follows Hermes busy-input handling by aborting active Claude CLI processes on `interrupt`, preserving queued turns on `queue`, and letting Hermes deliver `steer` guidance at its normal injection point
- patches known-problematic Hermes session_search/skills guidance wording at runtime to avoid a Claude Code CLI false positive
- sends single-turn or latest-turn image input as native stream-json image blocks when possible
- materializes image content into stable Claude Code `@file` references for flattened history prompts
- does not send Hermes assistant/tool history as native Claude CLI message records on fresh full-prompt starts; live continuations rely on Claude CLI's own session state

## Source Layout

`handler.py` is only the Hermes hook entrypoint. Runtime implementation lives
directly under the hook root:

| Layer / Module | Responsibility |
|----------------|----------------|
| `domain/config.py` | Runtime config loading and validation. |
| `domain/constants.py` / `domain/state.py` | Shared constants and process-local state. |
| `domain/invocation.py` | Per-call Claude CLI invocation data. |
| `infrastructure/claude_cli.py` | CLI env, settings, session helpers, argument normalization, temp files, and output parsing. |
| `infrastructure/content.py` | Hermes message, system prompt, and image input conversion. |
| `infrastructure/live_session.py` | Long-lived Claude CLI subprocess lifecycle. |
| `infrastructure/streaming.py` | Claude CLI stream-json output to Anthropic-compatible stream events. |
| `infrastructure/mcp_bridge.py` | Loopback MCP bridge and permission prompt bridge. |
| `features/anthropic_client.py` | Anthropic-compatible Claude CLI client facade and CLI argument assembly. |
| `hermes_adapter/exports.py` | Re-export surface used by `handler.py`. |
| `hermes_adapter/auxiliary.py` | Auxiliary LLM routing patch for `main`, `auto`, and Anthropic tasks in CLI mode. |
| `hermes_adapter/runtime_selection.py` | Hermes provider/runtime selection helpers. |
| `hermes_adapter/hook.py` | Hermes module patch installation and hook lifecycle. |

Structural tests keep the dependency graph explicit and prevent the entrypoint
from growing runtime implementation again.

## Configuration

Apply the YAML snippets in this section to the target Hermes agent's
active config file, at the same level as existing top-level keys such as
`model`, `auxiliary`, `hooks`, and `plugins`. Use the same configuration file
that already defines the agent's `model` block. Do not put these settings in
this hook's `HOOK.yaml` or in Claude Code's `.claude/settings.json`.

No `claude_cli` block is required for normal use. The hook is active when it is
loaded and the main model explicitly selects Claude CLI mode:

```yaml
model:
  provider: anthropic
  default: claude-opus-4-7
  api_mode: cli
```

`api_mode: cli` is the recommended readable marker for this hook. `base_url` is
not needed for native Claude CLI use; the CLI decides its own endpoint from
Claude Code configuration. Internally the hook still rides Hermes'
`anthropic_messages` code path after it has observed `api_mode: cli`, so request
construction, streaming, retry integration, and context compression stay
compatible with the existing agent. Plain `api_mode: anthropic_messages`
continues to use Hermes' native Anthropic Messages client.

Auxiliary tasks such as title generation, compression, memory side calls, MCP
side calls, and vision also follow Claude CLI mode when their provider is
`main`, `auto`, `anthropic`, `claude`, or `claude-code`. Explicit auxiliary
`base_url` or `api_key` settings remain a hard override and are delegated to
Hermes' native auxiliary router.

The default `claude_cli` profile is the recommended profile for normal Hermes
gateway use. Most empty values mean "let Claude CLI use its own default" or
"do not enable a second Claude Code feature that overlaps with Hermes". The
hook keeps Hermes in charge of provider selection, gateway sessions, approvals,
tools, memory, and the Hermes MCP bridge, while Claude CLI is used as the
Anthropic transport. When `mcp_enabled` is true, the hook launches Claude CLI
with a temporary MCP config that contains the Hermes loopback MCP server. Claude
Code's own MCP settings are also allowed by default. Set
`claude_code_mcp_enabled: false` only when you need strict isolation to the
Hermes bridge.

For most agents, do not add a `claude_cli` block at all. Add settings only when
you have a concrete reason:

| Goal | Setting to consider | Why it is not default |
|------|---------------------|------------------------|
| Tune reasoning strength | `effort` | Higher effort can increase latency and cost; lower effort can reduce quality. |
| Force or disable Claude Code thinking mode | `thinking` | Claude Code already enables thinking by default. Override only when you need a specific mode, fixed budget, or display preference for this transport. |
| Enable Claude Code fast mode | `fast_mode` | Fast mode changes Claude Code's own behavior and may trade off response depth, so the default is disabled. |
| Debug Claude CLI hook or MCP behavior | `include_hook_events`, `debug_filter`, `debug_file` | They add noisy output and may expose environment-specific hook errors. |
| Debug Claude CLI process startup or session reuse | `live_session: false` | Long-lived sessions are faster for normal use, but disabling them makes every turn start a fresh subprocess. |
| Let Claude Code auto-delegate to native subagents | `agents`, `.claude/agents`, or `plugin_dirs` | Subagents add a second context/tool system and may increase total model work even when they save the main context. |
| Let Claude CLI use native file/edit/bash tools | `tools: default` or a tool allowlist | Hermes already owns tool routing and approvals; enabling both creates two tool systems. |
| Disable Claude Code MCP settings | `claude_code_mcp_enabled: false` | Claude Code MCP settings are useful when you intentionally configured them in Claude Code. Disable them only when the extra MCP surface causes a concrete problem. |
| Load project/local Claude Code settings | `setting_sources: user,project,local` or `settings` | User settings are loaded by default. Project/local settings may contain environment-specific hooks or permissions that do not work in the Hermes runtime. |
| Load Claude Code plugins | `plugin_dirs` | Plugins can overlap with Hermes plugins/hooks and may add startup cost. |
| Cap cost or turns | `max_budget_usd`, `max_turns` | They can turn otherwise valid responses into CLI errors. `max_turns: 0` means no CLI turn limit. |
| Require structured JSON output | `json_schema` | Intended for internal machine-readable calls, not normal chat. It changes every response into schema-constrained output and may require more than one Claude turn. |
| Limit large MCP outputs | `max_mcp_output_tokens`, `mcp_tool_result_char_limit` | Limits are useful for runaway output, but can silently remove tool result content. |
| Improve prompt-cache reuse | `exclude_dynamic_system_prompt_sections` | It changes where Claude Code places dynamic environment context. Test before enabling globally. |

```yaml
claude_cli:
  command: claude
  config_dir: /opt/data/.claude
  timeout_seconds: 600
  live_session: true
  live_idle_timeout_seconds: 600
  effort: ""
  thinking: ""
  fast_mode: false
  include_partial_messages: true
  include_hook_events: false
  replay_user_messages: false
  system_prompt_mode: append
  setting_sources: user
  settings: ""
  agent: ""
  agents: ""
  tools: ""
  disallowed_tools: ""
  permission_mode: ""
  permission_prompt_tool: ""
  max_turns: 0
  max_budget_usd: 0
  fallback_model: ""
  json_schema: ""
  session_mode: session-id
  session_name: ""
  no_session_persistence: false
  mcp_enabled: true
  claude_code_mcp_enabled: true
  max_mcp_output_tokens: 0
  mcp_tool_result_char_limit: 0
  mcp_execute_code_timeout_seconds: 45
  add_dirs: []
  plugin_dirs: []
  debug_filter: ""
  debug_file: ""
  disable_slash_commands: false
  exclude_dynamic_system_prompt_sections: false
  extra_args: []
```

| Key | Type / Values | Default | Example | Behavior |
|-----|---------------|---------|---------|----------|
| `command` | string | `claude` | `/usr/local/bin/claude` | Claude CLI executable name or path. Omit unless `claude` is not on `PATH`. |
| `config_dir` | string path | `/opt/data/.claude` | `/opt/data/.claude` | `CLAUDE_CONFIG_DIR` for the subprocess. The hook clears inherited Claude provider overrides first, then sets this value. |
| `timeout_seconds` | positive number | `600` | `900` | Maximum wall-clock time for one Claude CLI turn, including streaming turns. The built-in no-output watchdog is derived from this value. |
| `live_session` | boolean | `true` | `true` | Keeps one Claude CLI process open for safe append-only continuations. Disable only when debugging Claude CLI process startup or session persistence. At most 16 live processes are kept across the Python process; the oldest idle one is closed when capacity is reached. |
| `live_idle_timeout_seconds` | positive number | `600` | `300` | Idle lifetime for an unused live Claude CLI process. The process is closed sooner when Hermes rewrites or resets the session, request settings change, capacity is reached, or the subprocess exits. |
| `include_partial_messages` | boolean | `true` | `true` | Adds `--include-partial-messages` for richer streaming JSON output. |
| `include_hook_events` | boolean | `false` | `false` | Adds `--include-hook-events` when enabled. Usually noisy. |
| `replay_user_messages` | boolean | `false` | `false` | Adds `--replay-user-messages` with stream-json output for short-lived calls. Live sessions force this flag because Claude CLI needs it for repeated stdin user messages. |
| `system_prompt_mode` | one of `append`, `replace` | `append` | `append` | Uses `--append-system-prompt-file`; `replace` uses `--system-prompt-file`. |
| `setting_sources` | string | `user` | `user,local` | Passed to Claude CLI as `--setting-sources`. The default follows Claude Code user settings while avoiding project/local settings that may be specific to another runtime environment. |
| `settings` | string path / JSON string / object | `""` | `"/opt/data/.claude/settings.json"` | Raw Claude CLI `--settings` value. Objects are serialized as JSON. Empty means omit the flag. |
| `agent` | string | `""` | `researcher` | Raw Claude CLI `--agent` value. This makes the whole Claude Code session run as one named subagent. Empty means omit the flag. |
| `agents` | JSON string or object | `""` | `{"researcher":{"description":"Investigates broad questions","prompt":"Research and summarize findings."}}` | Raw Claude CLI `--agents` value. Objects are serialized as JSON. Names and purposes are arbitrary; define as many specialized subagents as needed. These subagents are loaded for the session and Claude Code can automatically delegate to them based on their descriptions. |
| `tools` | string or list of strings | `""` | `default` | Raw Claude CLI `--tools` value for built-in Claude Code tools. The default empty string disables built-in tools so Hermes tools remain centralized. Set `default` only if you intentionally want Claude CLI native tools. |
| `disallowed_tools` | string or list of strings | `""` | `["Bash"]` | Raw Claude CLI `--disallowedTools` value. |
| `permission_mode` | one of `""`, `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions` | `""` | `acceptEdits` | Raw Claude CLI `--permission-mode` value. Empty means omit the flag. |
| `permission_prompt_tool` | string | `""` | `mcp__auth__prompt` | Raw Claude CLI `--permission-prompt-tool` override. Empty uses the hook's built-in Hermes approval bridge, exposed as `mcp__hermes__claude_cli_permission_prompt` when `mcp_enabled` is true. |
| `effort` | one of `""`, `low`, `medium`, `high`, `xhigh`, `max` | `""` | `low` | Raw Claude CLI `--effort` value. Empty means omit the flag. |
| `thinking` | `""`, one of `adaptive`, `enabled`, `disabled`, or object with `type`, `display`, `budget_tokens`, `disable_adaptive` | `""` | `{"type":"adaptive","display":"summarized"}` | Claude Code thinking controls. `type` may be `adaptive`, `enabled`, or `disabled`. `display` may be `summarized` or `omitted` and maps to Claude Code's `showThinkingSummaries` setting. `budget_tokens` sets `MAX_THINKING_TOKENS`. `disable_adaptive` maps to `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING`. |
| `fast_mode` | boolean | `false` | `true` | Sets Claude Code's `fastMode` setting for this invocation. |
| `max_turns` | positive integer or `0` | `0` | `20` | Raw Claude CLI `--max-turns`; `0` omits the flag. |
| `max_budget_usd` | positive number or `0` | `0` | `1.50` | Raw Claude CLI `--max-budget-usd`; `0` omits the flag. |
| `fallback_model` | string | `""` | `sonnet` | Raw Claude CLI `--fallback-model` value. Empty means omit the flag. Claude CLI rejects this when it resolves to the same model as the active `model`. |
| `json_schema` | JSON string or object | `""` | `{"type":"object"}` | Raw Claude CLI `--json-schema` value for internal machine-readable calls, such as classification, extraction, validation, or hook-driven self-calls. Objects are serialized as JSON. Empty means omit the flag. This is usually not useful for normal gateway chat because it forces every response into schema-constrained JSON. Structured output may take more than one Claude turn; when this is set with `max_turns: 1`, the hook omits `--max-turns` because Claude CLI cannot satisfy strict schema output within one turn. |
| `session_mode` | one of `off`, `resume`, `session-id` | `session-id` | `session-id` | Controls Claude CLI session reuse; see the table below. |
| `session_name` | string | `""` | `hermes` | Raw Claude CLI `--name` value. Empty means omit the flag. |
| `no_session_persistence` | boolean | `false` | `false` | Adds `--no-session-persistence` only when no session flag is being used. |
| `mcp_enabled` | boolean | `true` | `true` | Enables the loopback MCP bridge used for Hermes tools and the built-in Claude CLI permission prompt bridge. This does not disable Claude Code's own MCP settings. |
| `claude_code_mcp_enabled` | boolean | `true` | `false` | Allows Claude Code CLI to also load MCP servers from its own settings. When false, the hook adds `--strict-mcp-config` so only the Hermes bridge is visible, or no MCP servers are visible when `mcp_enabled` is also false. |
| `max_mcp_output_tokens` | positive integer or `0` | `0` | `12000` | Sets `MAX_MCP_OUTPUT_TOKENS` for the subprocess when greater than `0`. |
| `mcp_tool_result_char_limit` | positive integer or `0` | `0` | `60000` | Trims Hermes MCP tool results before returning them to Claude CLI; `0` disables trimming. |
| `mcp_execute_code_timeout_seconds` | positive number | `45` | `30` | Overrides Hermes `execute_code` timeout only while Claude CLI calls it through the Hermes MCP bridge. Normal Hermes `execute_code` calls keep the core `code_execution.timeout` setting. |
| `add_dirs` | list of strings or string | `[]` | `["/opt/shared"]` | Raw Claude CLI `--add-dir` directories. Use only when Claude CLI native context discovery needs extra directories. |
| `plugin_dirs` | list of strings or string | `[]` | `["/opt/data/.claude/plugins/local"]` | Raw Claude CLI `--plugin-dir` values. |
| `debug_filter` | string | `""` | `api,hooks` | Raw Claude CLI `--debug` filter. Empty means omit the flag. |
| `debug_file` | string path | `""` | `/opt/data/logs/claude-debug.log` | Raw Claude CLI `--debug-file` path. Empty means omit the flag. |
| `disable_slash_commands` | boolean | `false` | `true` | Adds `--disable-slash-commands`. Claude CLI treats this as disabling skills and slash commands for the session. |
| `exclude_dynamic_system_prompt_sections` | boolean | `false` | `true` | Adds `--exclude-dynamic-system-prompt-sections` to improve prompt-cache reuse for Claude Code's default prompt. |
| `extra_args` | list of strings | `[]` | `["--brief"]` | Extra Claude CLI arguments appended last after safety normalization. Use only for flags this hook does not model yet. Flags owned by the hook, such as model, stream formats, MCP, permissions, settings, tools, sessions, system prompt, and auth/endpoint flags, are ignored here; use the typed setting instead. |

The following Claude CLI flags are intentionally fixed by this hook instead of configurable:

| Flag | Fixed value | Reason |
|------|-------------|--------|
| `--print` | always on | The hook is a non-interactive transport. |
| `--output-format` | `stream-json` | Required for streaming and compatible with structured stdin. |
| `--input-format` | `stream-json` | Keeps the user message on Claude CLI's structured stdin path. |
| `--verbose` | always on | Required by Claude CLI when `--output-format stream-json` is used. |
| `--no-chrome` | always on | Browser integration is outside the Hermes gateway transport boundary. |
| `--replay-user-messages` | always on for live sessions | Required so subsequent stdin user messages are replayed correctly inside the live process. Short-lived calls use `claude_cli.replay_user_messages`. |

MCP discovery uses a temporary config owned by this hook for the Hermes bridge.
Set `claude_code_mcp_enabled: false` to add `--strict-mcp-config`. The same
fixed or typed flags are also filtered out of `extra_args`, so a raw argument
cannot silently override the transport contract.

Claude CLI model selection follows Claude Code family aliases for common Claude
refs. For example, `anthropic/claude-opus-4.7` and `claude-opus-4-7` become
`opus`; `sonnet-4.6` becomes `sonnet`; `haiku-4.5` becomes `haiku`.

The hook does not expose `--bare` because it bypasses the Claude Code OAuth files
used by this runtime setup and requires API-key style auth instead. Session
flags such as `--continue`, `--from-pr`, `--fork-session`, and remote/worktree
flags are also not exposed because Hermes owns session identity and process
lifecycle.

## Prompt Guidance Compatibility

In CLI mode, the hook applies a narrow runtime patch to Hermes'
`SESSION_SEARCH_GUIDANCE` and `SKILLS_GUIDANCE` text. With Claude Code CLI
`2.1.123`, the original combined guidance for `session_search` and skills can
trigger an incorrect `out of extra usage` API error even when Claude CLI is
using OAuth credentials. The replacement keeps the same intent but changes only
the known-problematic phrases. If Hermes or Claude Code changes the upstream
wording, the patch becomes a no-op instead of overwriting the new text.

`session_mode` values:

| Value | Behavior |
|-------|----------|
| `off` | Never pass Claude CLI session flags. Every call sends the full Hermes prompt. |
| `resume` | Capture Claude CLI's returned `session_id` and use `--resume` on later append-only calls from the same Hermes agent instance. |
| `session-id` | Derive a UUID from Hermes `session_id` plus an internal epoch and pass it with `--session-id`. This is the default. |

When an existing Claude CLI session can be safely continued, the hook sends only
the latest Hermes user message to avoid duplicating the already-persisted Claude
CLI context. This optimization is used only when the current Hermes prompt is an
append-only extension of the previous prompt and the request fingerprint is
unchanged.

With `live_session: true`, the hook keeps the same Claude CLI process open for
those safe append-only continuations and writes the next stream-json user
message to the existing stdin pipe. The live process is restarted when Hermes
sends a full prompt, the system prompt/model/tools/MCP/config fingerprint
changes, the Hermes parent session changes, the subprocess exits, or the idle
timeout expires. Startup artifacts such as the temporary system prompt file and
temporary MCP config stay alive for the live process and are cleaned up when
that process closes.

Live process startup follows Claude CLI's stdin protocol: the hook strips
session flags such as `--session-id` and `--resume` from the long-lived process
argv, then relies on the live process state for later stdin turns. If Hermes is
already in latest-user-only continuation mode but no live process is available,
the hook falls back to a short-lived Claude CLI call with the normal session
flags instead of starting a contextless live process.

## Busy Input Modes

Hermes owns `display.busy_input_mode`; this hook has no separate busy-input
setting. CLI mode follows the same three Hermes modes:

| Mode | Hook behavior |
|------|---------------|
| `interrupt` | Hermes calls `AIAgent.interrupt()`. The hook lets Hermes mark the run interrupted, then closes the active Claude CLI live session, terminates any active `claude -p` subprocess, and invalidates Claude CLI session state so the pending user message is answered in a fresh continuation. |
| `queue` | Hermes leaves the active CLI turn running and queues the new message for the next Hermes turn. The next turn is routed through the same append-only/session checks as any other continuation. |
| `steer` | Hermes stores the guidance and injects it at its normal tool-boundary injection point. If Hermes cannot inject it inside the current run, Hermes returns it as the next user turn; the hook then treats it like a normal queued continuation. |

## Model Names

The hook normalizes common model references after `api_mode: cli` has selected
Claude CLI mode:

- `anthropic/claude-opus-4.7` becomes `opus`
- `claude-*` ids with dotted numeric versions are first normalized to dash-form
  ids, then common Claude Code family aliases are applied; for example
  `claude-sonnet-4.6` becomes `sonnet`
- `haiku-4.5` becomes `haiku`
- other model names and aliases are passed through to Claude CLI unchanged

Model names are not a mode switch. Use `api_mode: cli` to select this hook.
Claude CLI owns final model validation.

## Thinking

`effort` is the primary Claude Code-native control for adaptive reasoning depth.
`thinking` exists for explicit overrides that cannot be expressed by
`--effort` alone:

```yaml
claude_cli:
  effort: max
  thinking:
    type: adaptive
    display: summarized
```

The hook maps `thinking` to Claude Code settings and environment variables
because Claude CLI does not expose a raw `--thinking` flag. `type: adaptive`
enables thinking and requests adaptive reasoning where Claude Code supports it.
`type: enabled` enables thinking; when paired with `budget_tokens`, the hook
sets `MAX_THINKING_TOKENS` and disables adaptive reasoning for models where
Claude Code still allows fixed budgets. `type: disabled` disables thinking by
setting `alwaysThinkingEnabled: false` and `MAX_THINKING_TOKENS=0`.

`display: summarized` and `display: omitted` map to Claude Code's
`showThinkingSummaries` setting.

## Subagents

Claude Code subagents can be used through three Claude Code-native discovery
paths:

- define session-scoped subagents with `claude_cli.agents`
- place subagent markdown files under the configured Claude config directory's
  `agents/` folder
- load plugins that provide subagents with `claude_cli.plugin_dirs`

`claude_cli.agents` maps directly to Claude CLI's `--agents` JSON flag. Once
the definitions are loaded, Claude Code can decide when to delegate based on
each subagent's `description`; users do not need to mention the subagent in
every prompt. `claude_cli.agent` maps to `--agent` and is different: it makes
the whole session run as that one subagent, so it is best reserved for agents
that should always use a specialized persona.

Example with multiple automatic delegation candidates:

```yaml
claude_cli:
  agents:
    researcher:
      description: Investigates broad or ambiguous technical questions and returns concise findings.
      prompt: Research the requested topic, inspect relevant files, and return only the important findings.
      model: sonnet
    tester:
      description: Runs focused verification and explains failures.
      prompt: Validate the change with the smallest useful tests and summarize failures clearly.
      model: sonnet
    planner:
      description: Breaks complex implementation work into concrete steps.
      prompt: Produce a pragmatic implementation plan with risks and file ownership.
      model: sonnet
```

Subagents are a Claude Code-native feature. They may save the main conversation
context, but they can also add model work and bring Claude Code's own tool and
permission behavior into the session. With the default `tools: ""`, built-in
Claude Code tools remain disabled at the top level. If a subagent should use
Claude Code-native file, edit, or shell tools, configure its `tools` field and
test the resulting permission behavior in the same runtime environment as the
Hermes gateway.

If Hermes rewrites history through `/retry`, `/undo`, manual `/compress`,
automatic compression, `/new`, `/reset`, `/resume`, or a session id rotation,
the hook discards the previous Claude CLI continuation state. In `session-id`
mode it increments an internal epoch and derives a new UUID, so the next call
starts a new Claude CLI session with Hermes' current full prompt as the source
of truth. In-flight Claude CLI results from an invalidated generation are
ignored for future session reuse.

Hermes `/new` and `/reset` evict the cached `AIAgent` in gateway mode. The hook
also clears Claude CLI session state when `AIAgent.reset_session_state()` runs,
closes any active Claude CLI subprocesses when `AIAgent.close()` runs, patches
`SessionStore.rewrite_transcript()`, and evicts cached gateway agents after
`/undo` and `/compress`. A cleared or rewritten Hermes session therefore does
not intentionally resume the previous Claude CLI session.

## Credentials

The hook does not read, store, or refresh Claude credentials itself. It clears
inherited Claude/Anthropic/OTEL provider overrides for the subprocess, then sets
`CLAUDE_CONFIG_DIR` from `claude_cli.config_dir`.

Hermes' native Anthropic path can resolve Claude Code OAuth credentials, but
CLI mode delegates authentication to the `claude` process. Use the same
Claude Code config directory for the login/setup step and for this hook, so the
CLI can read the generated `.credentials.json`.

Hermes-managed Anthropic PKCE credentials, such as entries in the Hermes
credential pool or `.anthropic_oauth.json`, are for Hermes' native API path.
They are not copied into Claude Code's `.credentials.json` by this hook, and
Claude CLI does not read them directly.

Default config path used by this hook:

```text
/opt/data/.claude
```

Keep the Claude Code files generated by `claude /login` or
`claude setup-token` in the config directory visible to the Claude CLI
subprocess. Do not commit those files. The files must be readable by the user
running the Hermes gateway process. If Claude CLI reports `Not logged in` even though
`.credentials.json` exists, check ownership and permissions on the configured
`.claude` directory.
At startup, the hook logs a warning when `.credentials.json` is missing from
`claude_cli.config_dir`, because the Claude CLI subprocess will normally need
that file for this runtime setup.

## Prompt Mapping

Hermes `system` content is written to a temporary file and passed with `--append-system-prompt-file`.

Hermes history is still collapsed into one text body when the request includes
assistant/tool history that Claude CLI cannot accept as native user-message
JSONL. The hook wraps that body as one JSONL user message compatible with
Claude CLI's stream-json stdin shape:

```json
{"type":"user","session_id":"","parent_tool_use_id":null,"message":{"role":"user","content":"..."}}
```

This keeps system instructions out of the user body and uses Claude CLI's
structured stdin path without causing Claude CLI to answer historical user turns
one by one.

When the current CLI turn is a single user message, or an append-only session
continuation where only the latest user message is sent, image content is passed
as native stream-json image blocks when it can be represented as Claude-style
base64 or URL image input. This covers Anthropic image blocks, OpenAI-style data
URL image blocks, local paths, and `file://` image URLs.

For flattened history prompts, image content is also materialized as Claude Code
`@file` references. Base64/data URL images are stored under a stable
content-addressed cache next to `claude_cli.config_dir`, for example
`/opt/data/.hermes-claude-cli-images`. Stable paths avoid changing the flattened
prompt on every turn and make append-only Claude CLI session reuse more
reliable. Remote `http://` or `https://` image URLs are kept as URL image blocks
for native current-turn input and as text URLs in flattened history prompts.

## Watchdog

The hook has two watchdogs. `timeout_seconds` is the overall wall-clock limit for
one turn. A no-output watchdog also terminates a CLI process that stops
producing stdout for too long, which usually means it is waiting for an
interactive prompt or a permission flow that is not connected to Hermes
approval. Fresh
full-prompt calls use a longer no-output window than append-only continuation
calls.

## MCP Bridge

When the request contains Hermes tools, or when the built-in Claude CLI
permission prompt bridge is active, the hook starts a local HTTP MCP server on
`127.0.0.1`, writes a temporary Claude MCP config file, and launches Claude CLI
with:

```text
--mcp-config <temp-file>
```

If `claude_code_mcp_enabled` is false, the hook also adds
`--strict-mcp-config`.

Tool calls are dispatched back through the active Hermes agent's existing `_invoke_tool()` path, so existing approval/session behavior stays centralized in Hermes.
The internal `claude_cli_permission_prompt` MCP tool is not dispatched as a
Hermes tool. It turns Claude CLI permission requests into Hermes
`approval.request` events and returns Claude CLI's expected JSON-stringified
`allow` or `deny` permission response.

The temporary MCP config contains only the `hermes` MCP server when Hermes
tools or the built-in permission prompt bridge are active. The hook also adds
`--allowedTools mcp__hermes__*` so those bridge tools can be used
non-interactively. If `mcp_enabled` is false, the hook does not create the
Hermes bridge. When `claude_code_mcp_enabled` is also false, it passes an empty
strict MCP config so Claude Code loads no MCP servers.

## Streaming

`messages.stream()` uses `claude -p` with `--output-format stream-json --verbose --include-partial-messages` and converts Claude CLI `stream_event` lines into Anthropic-style stream events. When `live_session` is enabled, streaming turns use the same live process reuse rules as non-streaming turns. Hermes can therefore receive `content_block_delta` text deltas through its existing Anthropic streaming path.

## Disclaimer

This hook is provided as-is, without warranty. You are responsible for how you
use it and for any consequences of that use, including Claude CLI usage, model
costs, tool execution, file changes, external service calls, authentication
files, and data handled by Hermes or Claude Code. Review your configuration,
credentials, and tool permissions before using it in a production or sensitive
environment.

## License

MIT. See [LICENSE](LICENSE).

## Support

If this hook is useful, you can support development here:

- [Sponsor on GitHub](https://github.com/sponsors/livingghost)
