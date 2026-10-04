"""Helpers for storing rich chat messages in the existing text column."""

from __future__ import annotations

import json
import re


MEDIA_PREFIX = "__MM_MEDIA_V1__"
# The model occasionally drops one or all of the expected pipe delimiters.
# Any TASK_* token is internal-only, so remove the entire tail starting there.
TASK_PROTOCOL_TAIL_RE = re.compile(
    r"(?:\|{0,3}\s*)TASK_[A-Z_]+(?:\s*:)?[\s\S]*$",
    re.IGNORECASE,
)


def encode_message_content(
    text: str,
    image_url: str = "",
    image_cloud_id: str = "",
) -> str:
    """Encode an image message without requiring a database migration."""
    text = (text or "").strip()
    if not image_url and not image_cloud_id:
        return text
    payload = {
        "type": "image",
        "text": text,
        "image_url": image_url,
        "image_cloud_id": image_cloud_id,
    }
    return MEDIA_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decode_message_content(value: str, strip_protocol: bool = True) -> dict:
    """Return a stable public representation for plain and image messages."""
    value = value or ""
    if not value.startswith(MEDIA_PREFIX):
        return {
            "type": "text",
            "text": strip_task_protocol(value) if strip_protocol else value,
            "image_url": "",
            "image_cloud_id": "",
        }
    try:
        payload = json.loads(value[len(MEDIA_PREFIX):])
    except (TypeError, json.JSONDecodeError):
        return {"type": "text", "text": "[图片]", "image_url": "", "image_cloud_id": ""}
    return {
        "type": "image",
        "text": (
            strip_task_protocol(str(payload.get("text") or ""))
            if strip_protocol
            else str(payload.get("text") or "")
        ),
        "image_url": str(payload.get("image_url") or ""),
        "image_cloud_id": str(payload.get("image_cloud_id") or ""),
    }


def strip_task_protocol(text: str) -> str:
    """Remove complete and malformed internal task directives from user-visible text."""
    cleaned = TASK_PROTOCOL_TAIL_RE.sub("", text or "")
    return cleaned.strip()
