from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import ensure_app_settings
from app.models import ClassificationResult, ConnectedAccount, StoredMessage
from app.services.classifier import LOCAL_FALLBACK_MODEL, SpamClassifier, should_apply_spam_label
from app.services.gmail import GmailAuthManager, InvalidHistoryCursorError
from app.services.types import MessageEnvelope, SyncReport


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MailSyncService:
    def __init__(self, settings: Settings, gmail_auth_manager: GmailAuthManager, classifier: SpamClassifier) -> None:
        self._settings = settings
        self._gmail_auth_manager = gmail_auth_manager
        self._classifier = classifier

    def sync(self, db: Session) -> SyncReport:
        runtime_settings = ensure_app_settings(db, self._settings)
        account = db.scalar(select(ConnectedAccount).order_by(ConnectedAccount.id))
        if account is None:
            return SyncReport(status="no_account", source="none", error="Connect Gmail before running sync.", ran_at=utcnow())

        mailbox = self._gmail_auth_manager.client_from_account(account)
        label_id = mailbox.ensure_label(runtime_settings.gmail_label_name)

        try:
            batch = (
                mailbox.fetch_messages_since(account.last_history_id, runtime_settings.max_body_chars)
                if account.last_history_id
                else mailbox.fetch_recent_messages(runtime_settings.backfill_limit, runtime_settings.max_body_chars)
            )
            notes: list[str] = []
        except InvalidHistoryCursorError:
            batch = mailbox.fetch_recent_messages(runtime_settings.backfill_limit, runtime_settings.max_body_chars)
            notes = ["Stored Gmail history cursor expired, so a fresh backfill was used."]

        report = SyncReport(status="ok", source=batch.source, ran_at=utcnow(), notes=[*notes, *batch.notes])
        local_fallback_noted = False

        for envelope in batch.messages:
            if self._message_exists(db, envelope.gmail_message_id):
                report.skipped_count += 1
                continue

            stored_message = self._store_message(db, account, envelope)
            decision = self._classifier.classify(envelope, runtime_settings.max_body_chars)
            classification = ClassificationResult(
                message_id=stored_message.id,
                verdict=decision.verdict,
                confidence=decision.confidence,
                reasons=decision.reasons,
                provider_request_id=decision.provider_request_id,
                provider_model=decision.provider_model,
            )
            db.add(classification)
            stored_message.processed_at = utcnow()

            report.fetched_count += 1
            report.classified_count += 1

            if decision.provider_model == LOCAL_FALLBACK_MODEL and not local_fallback_noted:
                report.notes.append(
                    "OpenAI was temporarily unavailable for some messages, so conservative local review was used and those results were not auto-labeled."
                )
                local_fallback_noted = True

            if should_apply_spam_label(decision, runtime_settings.spam_threshold):
                mailbox.apply_label(envelope.gmail_message_id, label_id)
                stored_message.label_applied = True
                stored_message.gmail_label_id = label_id
                report.labeled_count += 1

            db.commit()

        account.last_history_id = batch.latest_history_id or account.last_history_id
        account.last_sync_at = utcnow()
        account.access_token, account.access_token_expires_at = mailbox.persisted_token_state()
        db.add(account)
        db.commit()
        return report

    @staticmethod
    def _message_exists(db: Session, gmail_message_id: str) -> bool:
        existing = db.scalar(select(StoredMessage.id).where(StoredMessage.gmail_message_id == gmail_message_id))
        return existing is not None

    @staticmethod
    def _store_message(db: Session, account: ConnectedAccount, envelope: MessageEnvelope) -> StoredMessage:
        stored_message = StoredMessage(
            account_id=account.id,
            gmail_message_id=envelope.gmail_message_id,
            thread_id=envelope.thread_id,
            history_id=envelope.history_id,
            sender=envelope.sender,
            subject=envelope.subject,
            snippet=envelope.snippet,
            normalized_text=envelope.normalized_text,
            received_at=envelope.received_at,
        )
        db.add(stored_message)
        db.flush()
        return stored_message
