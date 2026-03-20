from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from email.header import decode_header

from bs4 import BeautifulSoup

from app.services.types import MessageEnvelope


def decode_rfc2047(value: str | None) -> str:
    if not value:
        return ""

    decoded_parts: list[str] = []
    for chunk, encoding in decode_header(value):
        if isinstance(chunk, bytes):
            decoded_parts.append(chunk.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded_parts.append(chunk)
    return "".join(decoded_parts).strip()


def decode_base64_urlsafe(data: str | None) -> str:
    if not data:
        return ""

    padding = "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(data + padding)
    except (ValueError, binascii.Error):
        return ""
    return raw.decode("utf-8", errors="replace")


def _gather_part_bodies(payload: dict, mime_type: str) -> list[str]:
    bodies: list[str] = []
    current_type = payload.get("mimeType", "")

    if current_type == mime_type:
        body = decode_base64_urlsafe(payload.get("body", {}).get("data"))
        if body.strip():
            bodies.append(body)

    for part in payload.get("parts", []) or []:
        bodies.extend(_gather_part_bodies(part, mime_type))

    return bodies


def html_to_text(html: str) -> str:
    if not html.strip():
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text("\n", strip=True).strip()


def normalize_whitespace(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.splitlines()]
    compact = "\n".join(line for line in lines if line)
    return compact.strip()


def normalize_email_payload(payload: dict | None, snippet: str = "", max_chars: int = 8192) -> str:
    payload = payload or {}
    text_parts = _gather_part_bodies(payload, "text/plain")
    html_parts = _gather_part_bodies(payload, "text/html")

    if text_parts:
        content = "\n\n".join(text_parts)
    elif html_parts:
        content = "\n\n".join(html_to_text(html) for html in html_parts if html.strip())
    else:
        content = snippet or ""

    normalized = normalize_whitespace(content or snippet or "")
    return normalized[:max_chars]


def parse_received_at(internal_date_ms: str | None) -> datetime | None:
    if not internal_date_ms:
        return None
    try:
        return datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def header_map(payload: dict | None) -> dict[str, str]:
    payload = payload or {}
    result: dict[str, str] = {}
    for header in payload.get("headers", []) or []:
        name = header.get("name")
        if name:
            result[name.lower()] = decode_rfc2047(header.get("value"))
    return result


def build_message_envelope(gmail_message: dict, max_chars: int) -> MessageEnvelope:
    payload = gmail_message.get("payload", {}) or {}
    headers = header_map(payload)
    snippet = gmail_message.get("snippet", "") or ""

    return MessageEnvelope(
        gmail_message_id=gmail_message.get("id", ""),
        thread_id=gmail_message.get("threadId"),
        history_id=gmail_message.get("historyId"),
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        snippet=snippet.strip(),
        normalized_text=normalize_email_payload(payload, snippet=snippet, max_chars=max_chars),
        received_at=parse_received_at(gmail_message.get("internalDate")),
    )
