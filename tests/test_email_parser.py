from __future__ import annotations

import base64

from app.services.email_parser import normalize_email_payload


def encode_body(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8").rstrip("=")


def test_normalize_plain_text_payload() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": encode_body("Hello there\nThis is plain text")}},
        ],
    }

    result = normalize_email_payload(payload, snippet="fallback")
    assert result == "Hello there\nThis is plain text"


def test_normalize_html_only_payload() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": encode_body("<p>Hello <strong>team</strong></p>")}},
        ],
    }

    result = normalize_email_payload(payload, snippet="fallback")
    assert result == "Hello\nteam"


def test_normalize_empty_payload_uses_snippet() -> None:
    result = normalize_email_payload(payload={}, snippet="Snippet only")
    assert result == "Snippet only"


def test_normalize_payload_truncates_large_body() -> None:
    large_text = "A" * 9000
    payload = {
        "mimeType": "text/plain",
        "body": {"data": encode_body(large_text)},
    }

    result = normalize_email_payload(payload, max_chars=8192)
    assert len(result) == 8192
