from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.security import TokenCipher
from app.services.gmail import GmailAuthManager, GmailServiceError, OAuthResult


class StubCredentials:
    def __init__(self) -> None:
        self.refresh_token = "refresh-token"
        self.token = "access-token"
        self.expiry = datetime(2026, 3, 21, 0, 0, tzinfo=timezone.utc)


class StubFlow:
    code_verifier = "verifier-123"

    def __init__(self) -> None:
        self.credentials = StubCredentials()
        self.fetch_token_code: str | None = None

    def authorization_url(self, **kwargs):
        return "https://example.test/oauth", "state-123"

    def fetch_token(self, code: str):
        self.fetch_token_code = code


def test_build_authorization_url_stores_code_verifier(monkeypatch) -> None:
    settings = Settings(
        google_client_id="client-id",
        google_client_secret="client-secret",
        app_encryption_key="secret",
    )
    manager = GmailAuthManager(settings, TokenCipher("secret"))
    session: dict[str, str] = {}
    stub_flow = StubFlow()

    monkeypatch.setattr("app.services.gmail.Flow.from_client_config", lambda *args, **kwargs: stub_flow)

    url = manager.build_authorization_url(session)

    assert url == "https://example.test/oauth"
    assert session["oauth_state"] == "state-123"
    assert session["oauth_code_verifier"] == "verifier-123"


def test_complete_authorization_requires_code_verifier() -> None:
    settings = Settings(
        google_client_id="client-id",
        google_client_secret="client-secret",
        app_encryption_key="secret",
    )
    manager = GmailAuthManager(settings, TokenCipher("secret"))

    with pytest.raises(GmailServiceError, match="code verifier"):
        manager.complete_authorization(
            code="demo-code",
            state="state-123",
            expected_state="state-123",
            code_verifier=None,
        )
