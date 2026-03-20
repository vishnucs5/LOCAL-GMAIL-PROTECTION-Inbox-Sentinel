from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.gmail import OAuthResult
from app.services.types import ClassificationDecision, MessageEnvelope, SyncBatch


def make_settings(tmp_path, *, enable_scheduler: bool = False) -> Settings:
    return Settings(
        app_name="Inbox Sentinel Test",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        enable_scheduler=enable_scheduler,
        google_client_id="google-client",
        google_client_secret="google-secret",
        app_encryption_key="test-encryption-secret",
        session_secret="test-session-secret",
        ai_api_key="test-ai-key",
    )


def make_envelope(
    gmail_message_id: str,
    *,
    subject: str,
    sender: str = "alerts@example.com",
    snippet: str = "message snippet",
    normalized_text: str = "message body",
    history_id: str = "101",
) -> MessageEnvelope:
    return MessageEnvelope(
        gmail_message_id=gmail_message_id,
        thread_id=f"thread-{gmail_message_id}",
        history_id=history_id,
        sender=sender,
        subject=subject,
        snippet=snippet,
        normalized_text=normalized_text,
        received_at=datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc),
    )


class FakeMailboxClient:
    def __init__(self) -> None:
        self.recent_batches: list[SyncBatch] = []
        self.incremental_batches: list[SyncBatch | Exception] = []
        self.applied_labels: list[tuple[str, str]] = []
        self.requested_label_name: str | None = None
        self.last_history_id: str | None = None
        self.expiry = datetime(2026, 3, 20, 13, 0, tzinfo=timezone.utc)

    def ensure_label(self, label_name: str) -> str:
        self.requested_label_name = label_name
        return "Label_999"

    def fetch_recent_messages(self, limit: int, max_body_chars: int) -> SyncBatch:
        return self.recent_batches.pop(0)

    def fetch_messages_since(self, history_id: str, max_body_chars: int) -> SyncBatch:
        self.last_history_id = history_id
        batch = self.incremental_batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch

    def apply_label(self, gmail_message_id: str, label_id: str) -> None:
        self.applied_labels.append((gmail_message_id, label_id))

    def get_profile_email(self) -> str:
        return "owner@example.com"

    def persisted_token_state(self) -> tuple[str | None, datetime | None]:
        return "refreshed-access-token", self.expiry


class FakeAuthManager:
    def __init__(self, mailbox_client: FakeMailboxClient) -> None:
        self.mailbox_client = mailbox_client

    def build_authorization_url(self, session: dict) -> str:
        session["oauth_state"] = "test-state"
        session["oauth_code_verifier"] = "test-code-verifier"
        return "https://example.test/oauth"

    def complete_authorization(
        self,
        code: str,
        state: str,
        expected_state: str | None,
        code_verifier: str | None,
    ) -> OAuthResult:
        assert code == "demo-code"
        assert state == expected_state
        assert code_verifier == "test-code-verifier"
        return OAuthResult(
            email="owner@example.com",
            refresh_token="refresh-token",
            access_token="access-token",
            access_token_expires_at=datetime(2026, 3, 20, 13, 0, tzinfo=timezone.utc),
        )

    def client_from_account(self, account) -> FakeMailboxClient:
        return self.mailbox_client


class FakeClassifier:
    def __init__(self, decisions: dict[str, ClassificationDecision] | None = None) -> None:
        self.decisions = decisions or {}
        self.calls: list[tuple[str, int]] = []

    def classify(self, message: MessageEnvelope, max_body_chars: int) -> ClassificationDecision:
        self.calls.append((message.gmail_message_id, max_body_chars))
        return self.decisions.get(
            message.gmail_message_id,
            ClassificationDecision(
                verdict="not_spam",
                confidence=0.15,
                reasons=["Looks safe."],
                provider_request_id="req_default",
                provider_model="test-model",
            ),
        )


@pytest.fixture
def app_bundle(tmp_path):
    settings = make_settings(tmp_path)
    mailbox = FakeMailboxClient()
    classifier = FakeClassifier()
    auth = FakeAuthManager(mailbox)
    app = create_app(settings=settings, gmail_auth_manager=auth, classifier=classifier)
    return app, settings, mailbox, classifier


@pytest.fixture
def client(app_bundle):
    app, _, _, _ = app_bundle
    with TestClient(app) as test_client:
        yield test_client
