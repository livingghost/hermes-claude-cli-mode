"""Prompt and image conversion for Claude CLI stream-json input."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlparse

from domain.constants import IMAGE_MEDIA_EXTENSIONS
from .claude_cli import _hash_text

def _image_suffix(media_type: Any) -> str:
    normalized = str(media_type or "").split(";", 1)[0].strip().lower()
    return IMAGE_MEDIA_EXTENSIONS.get(normalized, ".img")


def _image_media_type_from_path(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    normalized = str(guessed or "").split(";", 1)[0].strip().lower()
    return normalized if normalized in IMAGE_MEDIA_EXTENSIONS else "image/png"


def _image_cache_root(config_dir: str) -> Path:
    try:
        parent = Path(config_dir or "").expanduser().parent
        if str(parent):
            return parent / ".hermes-claude-cli-images"
    except Exception:
        pass
    return Path(tempfile.gettempdir()) / "hermes-claude-cli-images"


def _write_cached_image(*, image_root: Path, media_type: str, data: bytes) -> str:
    image_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    digest.update(media_type.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(data)
    path = image_root / f"{digest.hexdigest()}{_image_suffix(media_type)}"
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        pass
    return str(path)


def _decode_data_url(value: str) -> tuple[str, bytes] | None:
    header, separator, payload = value.partition(",")
    if separator != "," or not header.lower().startswith("data:"):
        return None
    media_type = header[5:].split(";", 1)[0] or "application/octet-stream"
    try:
        if ";base64" in header.lower():
            return media_type, base64.b64decode(payload, validate=True)
        return media_type, unquote_to_bytes(payload)
    except (binascii.Error, ValueError):
        return None


def _local_path_from_image_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https", "data"}:
        return ""
    if scheme == "file":
        path = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            path = f"//{parsed.netloc}{path}"
        if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return path if path else ""
    if scheme:
        return ""
    try:
        path = Path(raw).expanduser()
        if path.exists():
            return str(path)
    except Exception:
        return ""
    return ""


def _image_fields_from_block(value: dict[str, Any]) -> tuple[str, str, str]:
    block_type = str(value.get("type") or "")
    media_type = ""
    base64_data = ""
    image_url = ""

    if block_type == "image":
        source = value.get("source")
        if isinstance(source, dict):
            media_type = str(source.get("media_type") or source.get("mediaType") or "")
            source_type = str(source.get("type") or "").lower()
            if source_type == "base64" and isinstance(source.get("data"), str):
                base64_data = source["data"]
            elif isinstance(source.get("url"), str):
                image_url = source["url"]
            elif isinstance(source.get("path"), str):
                image_url = source["path"]
        elif isinstance(source, str):
            image_url = source
    else:
        raw_image = value.get("image_url") or value.get("input_image")
        if isinstance(raw_image, dict):
            image_url = str(raw_image.get("url") or raw_image.get("path") or "")
        elif isinstance(raw_image, str):
            image_url = raw_image
    return media_type, base64_data, image_url


def _image_reference_from_block(value: dict[str, Any], image_root: Path | None) -> str:
    media_type, base64_data, image_url = _image_fields_from_block(value)

    if base64_data:
        try:
            image_bytes = base64.b64decode(base64_data, validate=True)
        except (binascii.Error, ValueError):
            return "[image omitted: invalid base64 data]"
        path = _write_cached_image(
            image_root=image_root or _image_cache_root(""),
            media_type=media_type or "image/png",
            data=image_bytes,
        )
        return f"@{path}"

    image_url = image_url.strip()
    if image_url.startswith("data:"):
        decoded = _decode_data_url(image_url)
        if decoded is None:
            return "[image omitted: invalid data URL]"
        decoded_media_type, image_bytes = decoded
        path = _write_cached_image(
            image_root=image_root or _image_cache_root(""),
            media_type=decoded_media_type,
            data=image_bytes,
        )
        return f"@{path}"

    local_path = _local_path_from_image_url(image_url)
    if local_path:
        return f"@{local_path}"
    if image_url:
        return f"[image_url]\n{image_url}"
    return "[image]"


def _stream_image_block_from_block(value: dict[str, Any]) -> dict[str, Any] | None:
    media_type, base64_data, image_url = _image_fields_from_block(value)
    media_type = media_type or "image/png"
    if base64_data:
        try:
            base64.b64decode(base64_data, validate=True)
        except (binascii.Error, ValueError):
            return None
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64_data,
            },
        }

    image_url = image_url.strip()
    if image_url.startswith("data:"):
        decoded = _decode_data_url(image_url)
        if decoded is None:
            return None
        decoded_media_type, image_bytes = decoded
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": decoded_media_type,
                "data": base64.b64encode(image_bytes).decode("ascii"),
            },
        }

    local_path = _local_path_from_image_url(image_url)
    if local_path:
        try:
            image_bytes = Path(local_path).read_bytes()
        except Exception:
            return None
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _image_media_type_from_path(local_path),
                "data": base64.b64encode(image_bytes).decode("ascii"),
            },
        }
    if image_url:
        return {"type": "image", "source": {"type": "url", "url": image_url}}
    return None


def _is_image_block(value: dict[str, Any]) -> bool:
    block_type = str(value.get("type") or "")
    return block_type in {"image", "image_url", "input_image"} or "image_url" in value


def _content_has_image(value: Any) -> bool:
    if isinstance(value, dict):
        if _is_image_block(value):
            return True
        return _content_has_image(value.get("content"))
    if isinstance(value, list):
        return any(_content_has_image(item) for item in value)
    return False


def _stream_content_block(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        return {"type": "text", "text": value}
    if not isinstance(value, dict):
        text = getattr(value, "text", None)
        return {"type": "text", "text": text} if isinstance(text, str) else None
    if _is_image_block(value):
        return _stream_image_block_from_block(value)
    block_type = value.get("type")
    if block_type == "text" and isinstance(value.get("text"), str):
        return {"type": "text", "text": value["text"]}
    if "text" in value and isinstance(value.get("text"), str):
        return {"type": "text", "text": value["text"]}
    return None


def _stream_content_from_user_content(value: Any) -> str | list[dict[str, Any]] | None:
    if not _content_has_image(value):
        return None
    if isinstance(value, str):
        return value
    raw_items = value if isinstance(value, list) else [value]
    blocks: list[dict[str, Any]] = []
    for item in raw_items:
        block = _stream_content_block(item)
        if block is None:
            return None
        if block.get("type") == "text" and not str(block.get("text") or ""):
            continue
        blocks.append(block)
    return blocks or None


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or "user")
    return str(getattr(message, "role", "user") or "user")


def _stream_json_content(
    api_kwargs: dict[str, Any],
    *,
    latest_user_only: bool,
    fallback_prompt: str,
) -> str | list[dict[str, Any]]:
    messages = api_kwargs.get("messages")
    if not isinstance(messages, list):
        return fallback_prompt
    if not latest_user_only:
        if len(messages) != 1 or _message_role(messages[0]).lower() != "user":
            return fallback_prompt
        content = _stream_content_from_user_content(_message_content(messages[0]))
        return content if content is not None else fallback_prompt
    for message in reversed(messages):
        if _message_role(message).lower() != "user":
            continue
        content = _stream_content_from_user_content(_message_content(message))
        if content is not None:
            return content
        return fallback_prompt
    return fallback_prompt


def _stringify_block(value: Any, *, image_root: Path | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_stringify_block(item, image_root=image_root) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if _is_image_block(value):
            return _image_reference_from_block(value, image_root)
        block_type = value.get("type")
        if block_type == "text":
            return _stringify_block(value.get("text"), image_root=image_root)
        if block_type == "thinking":
            return _stringify_block(value.get("thinking"), image_root=image_root)
        if block_type == "tool_result":
            content = _stringify_block(value.get("content"), image_root=image_root)
            return f"[tool_result]\n{content}" if content else "[tool_result]"
        if block_type == "tool_use":
            name = value.get("name", "tool")
            payload = value.get("input", {})
            return f"[tool_use:{name}]\n{json.dumps(payload, ensure_ascii=True)}"
        if "text" in value:
            return _stringify_block(value.get("text"), image_root=image_root)
        if "content" in value:
            return _stringify_block(value.get("content"), image_root=image_root)
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(value, "content", None)
    if content is not None:
        return _stringify_block(content, image_root=image_root)
    return str(value)


def _system_prompt(api_kwargs: dict[str, Any], *, image_root: Path | None = None) -> str:
    return _stringify_block(api_kwargs.get("system"), image_root=image_root).strip()


def _message_role_and_content(message: Any, *, image_root: Path | None = None) -> tuple[str, str]:
    if isinstance(message, dict):
        role = str(message.get("role") or "user")
        content = _stringify_block(message.get("content"), image_root=image_root).strip()
        return role, content
    role = str(getattr(message, "role", "user") or "user")
    content = _stringify_block(getattr(message, "content", ""), image_root=image_root).strip()
    return role, content


def _latest_user_prompt(api_kwargs: dict[str, Any], *, image_root: Path | None = None) -> str:
    messages = api_kwargs.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            role, content = _message_role_and_content(message, image_root=image_root)
            if role.lower() == "user" and content:
                return content
    return ""


def _build_prompt(
    api_kwargs: dict[str, Any],
    *,
    include_system: bool = True,
    latest_user_only: bool = False,
    image_root: Path | None = None,
) -> str:
    if latest_user_only:
        latest = _latest_user_prompt(api_kwargs, image_root=image_root)
        if latest:
            return latest

    parts: list[str] = []
    system = _system_prompt(api_kwargs, image_root=image_root)
    if system:
        if include_system:
            parts.append(f"System:\n{system}")

    messages = api_kwargs.get("messages")
    if isinstance(messages, list):
        for message in messages:
            role, content = _message_role_and_content(message, image_root=image_root)
            if content:
                parts.append(f"{role.capitalize()}:\n{content}")

    if not parts:
        parts.append(str(api_kwargs.get("prompt") or ""))

    return "\n\n".join(parts).strip()


def _build_stream_json_input(content: str | list[dict[str, Any]]) -> str:
    payload = {
        "type": "user",
        "session_id": "",
        "parent_tool_use_id": None,
        "message": {
            "role": "user",
            "content": content,
        },
    }
    return json.dumps(payload, ensure_ascii=True) + "\n"
