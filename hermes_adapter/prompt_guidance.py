"""Claude CLI prompt-guidance workarounds."""

from __future__ import annotations

from typing import Any


CLAUDE_CLI_SESSION_SEARCH_GUIDANCE_PATCHES = (
    (
        "When the user references something from a past conversation or you suspect "
        "relevant cross-session context exists, use session_search to recall it before "
        "asking them to repeat themselves.",
        "When the user refers to earlier work or you think useful context may exist "
        "in prior Hermes sessions, use session_search before asking them to repeat details.",
    ),
)
CLAUDE_CLI_SKILLS_GUIDANCE_PATCHES = (
    (
        "save the approach as a skill with skill_manage so you can reuse it next time.",
        "use skill_manage to save the approach so it can be reused later.",
    ),
    (
        "When using a skill and finding it outdated, incomplete, or wrong, "
        "patch it immediately with skill_manage(action='patch') — don't wait to be asked. "
        "Skills that aren't maintained become liabilities.",
        "When a loaded skill is outdated, incomplete, or wrong, update it with "
        "skill_manage(action='patch') after confirming the needed correction. "
        "Keep skills maintained so future runs stay accurate.",
    ),
)


def _patch_guidance_text(value: Any, patches: tuple[tuple[str, str], ...]) -> tuple[Any, bool]:
    if not isinstance(value, str):
        return value, False
    patched = value
    for needle, replacement in patches:
        patched = patched.replace(needle, replacement)
    return patched, patched != value


def _patch_cli_safe_prompt_guidance(module: Any) -> bool:
    # Temporary workaround for a Claude Code CLI 2.1.123 false positive observed
    # when Hermes exposes both session_search and skills guidance. The CLI can
    # reject the turn with "out of extra usage" even though apiKeySource is none.
    # Patch only the known-problematic core wording so future Hermes or Claude
    # Code updates can make this a no-op instead of being overwritten wholesale.
    patched = False
    session_guidance, session_patched = _patch_guidance_text(
        getattr(module, "SESSION_SEARCH_GUIDANCE", None),
        CLAUDE_CLI_SESSION_SEARCH_GUIDANCE_PATCHES,
    )
    if session_patched:
        setattr(module, "SESSION_SEARCH_GUIDANCE", session_guidance)
        patched = True
    skills_guidance, skills_patched = _patch_guidance_text(
        getattr(module, "SKILLS_GUIDANCE", None),
        CLAUDE_CLI_SKILLS_GUIDANCE_PATCHES,
    )
    if skills_patched:
        setattr(module, "SKILLS_GUIDANCE", skills_guidance)
        patched = True
    return patched
