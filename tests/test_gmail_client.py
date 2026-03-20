from __future__ import annotations

import httplib2

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

from app.services.gmail import GmailMailboxClient, GmailServiceError
from tests.conftest import make_envelope


def make_http_error(status: int, reason: str) -> HttpError:
    response = httplib2.Response({"status": str(status)})
    content = (
        '{"error":{"errors":[{"message":"Problem","domain":"global","reason":"'
        + reason
        + '"}]}}'
    ).encode("utf-8")
    return HttpError(response, content)


def test_execute_with_retry_retries_transient_http_error(monkeypatch) -> None:
    attempts = {"count": 0}

    def flaky_operation():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise make_http_error(500, "backendError")
        return {"ok": True}

    monkeypatch.setattr("app.services.gmail.time.sleep", lambda *_args, **_kwargs: None)

    result = GmailMailboxClient._execute_with_retry(flaky_operation, "Loading Gmail data")

    assert result == {"ok": True}
    assert attempts["count"] == 3


def test_fetch_recent_messages_skips_single_failed_message(monkeypatch) -> None:
    monkeypatch.setattr("app.services.gmail.build", lambda *args, **kwargs: object())
    credentials = Credentials(token="token")
    client = GmailMailboxClient(credentials)

    class FakeRequest:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class FakeMessagesResource:
        def list(self, **_kwargs):
            return FakeRequest({"messages": [{"id": "good-msg"}, {"id": "bad-msg"}]})

    class FakeUsersResource:
        def messages(self):
            return FakeMessagesResource()

    class FakeService:
        def users(self):
            return FakeUsersResource()

    client._service = FakeService()

    def fake_load_full_message(message_id: str, max_body_chars: int):
        assert max_body_chars == 8192
        if message_id == "bad-msg":
            raise GmailServiceError("Loading Gmail message bad-msg failed: backend error")
        return make_envelope("good-msg", subject="Hello", history_id="123")

    monkeypatch.setattr(client, "_load_full_message", fake_load_full_message)

    batch = client.fetch_recent_messages(limit=10, max_body_chars=8192)

    assert [message.gmail_message_id for message in batch.messages] == ["good-msg"]
    assert batch.latest_history_id == "123"
    assert batch.notes == ["Skipped Gmail message bad-msg: Loading Gmail message bad-msg failed: backend error"]
