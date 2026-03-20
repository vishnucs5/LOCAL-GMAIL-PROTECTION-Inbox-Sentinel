from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol, TypeVar

import httplib2
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import Settings
from app.models import ConnectedAccount
from app.security import TokenCipher
from app.services.email_parser import build_message_envelope
from app.services.types import MessageEnvelope, SyncBatch

TransientResult = TypeVar("TransientResult")


class GmailServiceError(RuntimeError):
    pass


class InvalidHistoryCursorError(GmailServiceError):
    pass


@dataclass(slots=True)
class OAuthResult:
    email: str
    refresh_token: str
    access_token: str | None
    access_token_expires_at: datetime | None


class GmailMailboxClientProtocol(Protocol):
    def ensure_label(self, label_name: str) -> str:
        raise NotImplementedError

    def fetch_recent_messages(self, limit: int, max_body_chars: int) -> SyncBatch:
        raise NotImplementedError

    def fetch_messages_since(self, history_id: str, max_body_chars: int) -> SyncBatch:
        raise NotImplementedError

    def apply_label(self, gmail_message_id: str, label_id: str) -> None:
        raise NotImplementedError

    def get_profile_email(self) -> str:
        raise NotImplementedError

    def persisted_token_state(self) -> tuple[str | None, datetime | None]:
        raise NotImplementedError


class GmailMailboxClient:
    def __init__(self, credentials: Credentials, *, timeout_seconds: float = 20.0) -> None:
        self._credentials = credentials
        self._http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout_seconds))
        self._service = build("gmail", "v1", http=self._http, cache_discovery=False)

    def get_profile_email(self) -> str:
        try:
            profile = self._execute_with_retry(
                lambda: self._service.users().getProfile(userId="me").execute(),
                "Loading the Gmail profile",
            )
        except HttpError as exc:
            raise GmailServiceError(_describe_http_error(exc, "Loading the Gmail profile")) from exc
        return profile.get("emailAddress", "")

    def persisted_token_state(self) -> tuple[str | None, datetime | None]:
        return self._credentials.token, self._credentials.expiry

    def ensure_label(self, label_name: str) -> str:
        try:
            labels = self._execute_with_retry(
                lambda: self._service.users().labels().list(userId="me").execute(),
                "Listing Gmail labels",
            ).get("labels", [])
        except HttpError as exc:
            raise GmailServiceError(_describe_http_error(exc, "Listing Gmail labels")) from exc
        for label in labels:
            if label.get("name") == label_name:
                return label["id"]

        try:
            created = self._execute_with_retry(
                lambda: self._service.users().labels().create(
                    userId="me",
                    body={
                        "name": label_name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                ).execute(),
                "Creating the Gmail review label",
            )
        except HttpError as exc:
            raise GmailServiceError(_describe_http_error(exc, "Creating the Gmail review label")) from exc
        return created["id"]

    def fetch_recent_messages(self, limit: int, max_body_chars: int) -> SyncBatch:
        try:
            response = self._execute_with_retry(
                lambda: self._service.users().messages().list(userId="me", maxResults=limit).execute(),
                "Listing recent Gmail messages",
            )
        except HttpError as exc:
            raise GmailServiceError(_describe_http_error(exc, "Listing recent Gmail messages")) from exc
        message_refs = response.get("messages", [])
        messages, notes = self._load_message_batch([item["id"] for item in message_refs], max_body_chars)
        latest_history_id = None
        for message in messages:
            if message.history_id:
                latest_history_id = max(latest_history_id or "0", message.history_id, key=int)
        return SyncBatch(messages=messages, latest_history_id=latest_history_id, source="backfill", notes=notes)

    def fetch_messages_since(self, history_id: str, max_body_chars: int) -> SyncBatch:
        message_ids: list[str] = []
        next_page_token: str | None = None
        latest_history_id = history_id

        try:
            while True:
                response = self._service.users().history().list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    pageToken=next_page_token,
                )
                response = self._execute_with_retry(
                    response.execute,
                    "Loading Gmail history updates",
                )
                latest_history_id = response.get("historyId", latest_history_id)

                for history_entry in response.get("history", []):
                    for added in history_entry.get("messagesAdded", []):
                        message = added.get("message", {})
                        message_id = message.get("id")
                        if message_id and message_id not in message_ids:
                            message_ids.append(message_id)

                next_page_token = response.get("nextPageToken")
                if not next_page_token:
                    break
        except HttpError as exc:
            if getattr(exc, "status_code", None) == 404 or getattr(exc.resp, "status", None) == 404:
                raise InvalidHistoryCursorError("Stored Gmail history cursor expired.")
            raise GmailServiceError(_describe_http_error(exc, "Loading Gmail history updates")) from exc

        messages, notes = self._load_message_batch(message_ids, max_body_chars)
        return SyncBatch(messages=messages, latest_history_id=latest_history_id, source="incremental", notes=notes)

    def apply_label(self, gmail_message_id: str, label_id: str) -> None:
        try:
            self._execute_with_retry(
                lambda: self._service.users().messages().modify(
                    userId="me",
                    id=gmail_message_id,
                    body={"addLabelIds": [label_id]},
                ).execute(),
                f"Applying Gmail label to message {gmail_message_id}",
            )
        except HttpError as exc:
            raise GmailServiceError(
                _describe_http_error(exc, f"Applying Gmail label to message {gmail_message_id}")
            ) from exc

    def _load_full_message(self, gmail_message_id: str, max_body_chars: int) -> MessageEnvelope:
        try:
            full_message = self._execute_with_retry(
                lambda: self._service.users().messages().get(
                    userId="me",
                    id=gmail_message_id,
                    format="full",
                ).execute(),
                f"Loading Gmail message {gmail_message_id}",
            )
        except HttpError as exc:
            raise GmailServiceError(_describe_http_error(exc, f"Loading Gmail message {gmail_message_id}")) from exc
        return build_message_envelope(full_message, max_chars=max_body_chars)

    def _load_message_batch(self, message_ids: list[str], max_body_chars: int) -> tuple[list[MessageEnvelope], list[str]]:
        messages: list[MessageEnvelope] = []
        notes: list[str] = []

        for message_id in message_ids:
            try:
                messages.append(self._load_full_message(message_id, max_body_chars))
            except GmailServiceError as exc:
                notes.append(f"Skipped Gmail message {message_id}: {exc}")

        return messages, notes

    @staticmethod
    def _execute_with_retry(
        operation: Callable[[], TransientResult],
        action: str,
        *,
        max_attempts: int = 3,
        initial_delay_seconds: float = 1.0,
    ) -> TransientResult:
        delay = initial_delay_seconds

        for attempt in range(1, max_attempts + 1):
            try:
                return operation()
            except HttpError as exc:
                if attempt >= max_attempts or not _is_transient_http_error(exc):
                    raise
                time.sleep(delay)
                delay *= 2

        raise GmailServiceError(f"{action} failed after retries.")


class GmailAuthManager:
    def __init__(self, settings: Settings, token_cipher: TokenCipher) -> None:
        self._settings = settings
        self._token_cipher = token_cipher

    def build_authorization_url(self, session: dict) -> str:
        if not self._settings.google_is_configured:
            raise GmailServiceError("Google OAuth credentials are not configured.")

        flow = Flow.from_client_config(
            self._client_config,
            scopes=self._settings.gmail_scopes,
            redirect_uri=self._settings.google_redirect_uri,
        )
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        session["oauth_state"] = state
        if flow.code_verifier:
            session["oauth_code_verifier"] = flow.code_verifier
        return authorization_url

    def complete_authorization(
        self,
        code: str,
        state: str,
        expected_state: str | None,
        code_verifier: str | None,
    ) -> OAuthResult:
        if not expected_state or state != expected_state:
            raise GmailServiceError("OAuth state validation failed.")
        if not code_verifier:
            raise GmailServiceError("OAuth code verifier missing. Start the Google sign-in again from http://localhost:8000.")

        flow = Flow.from_client_config(
            self._client_config,
            scopes=self._settings.gmail_scopes,
            redirect_uri=self._settings.google_redirect_uri,
            code_verifier=code_verifier,
            autogenerate_code_verifier=False,
        )
        try:
            flow.fetch_token(code=code)
            credentials = flow.credentials
        except Exception as exc:
            raise GmailServiceError(f"Google token exchange failed: {exc}") from exc

        if not credentials.refresh_token:
            raise GmailServiceError("Google did not return a refresh token. Re-consent is required.")

        try:
            mailbox = GmailMailboxClient(credentials, timeout_seconds=self._settings.gmail_timeout_seconds)
        except Exception as exc:
            raise GmailServiceError(f"Initializing the Gmail client failed: {exc}") from exc

        return OAuthResult(
            email=mailbox.get_profile_email(),
            refresh_token=credentials.refresh_token,
            access_token=credentials.token,
            access_token_expires_at=credentials.expiry,
        )

    def client_from_account(self, account: ConnectedAccount) -> GmailMailboxClient:
        refresh_token = self._token_cipher.decrypt(account.refresh_token_encrypted)
        credentials = Credentials(
            token=account.access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._settings.google_client_id,
            client_secret=self._settings.google_client_secret,
            scopes=self._settings.gmail_scopes,
        )

        if credentials.expired and credentials.refresh_token:
            credentials.refresh(GoogleRequest())

        return GmailMailboxClient(credentials, timeout_seconds=self._settings.gmail_timeout_seconds)

    @property
    def _client_config(self) -> dict:
        return {
            "web": {
                "client_id": self._settings.google_client_id,
                "client_secret": self._settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }


def _describe_http_error(exc: HttpError, action: str) -> str:
    status = getattr(exc.resp, "status", None)
    base = f"{action} failed"

    if status == 403:
        return (
            f"{base}: Google returned 403. Enable the Gmail API in this Google Cloud project and "
            "make sure the OAuth app is still in Testing with your Gmail account listed as a test user."
        )
    if status == 401:
        return f"{base}: Google rejected the credentials with 401."
    return f"{base}: {exc}"


def _is_transient_http_error(exc: HttpError) -> bool:
    status = getattr(exc.resp, "status", None)
    if status in {429, 500, 502, 503, 504}:
        return True

    for reason in _http_error_reasons(exc):
        if reason in {"backendError", "rateLimitExceeded", "userRateLimitExceeded"}:
            return True

    return False


def _http_error_reasons(exc: HttpError) -> list[str]:
    content = getattr(exc, "content", b"")
    if not content:
        return []

    try:
        payload = json.loads(content.decode("utf-8"))
    except (ValueError, AttributeError, UnicodeDecodeError):
        return []

    reasons: list[str] = []
    for item in payload.get("error", {}).get("errors", []):
        reason = item.get("reason")
        if reason:
            reasons.append(str(reason))
    return reasons
