from __future__ import annotations

from sqlalchemy import select

from app.database import create_database_bundle, init_database
from app.models import ClassificationResult, ConnectedAccount, StoredMessage
from app.security import TokenCipher
from app.services.classifier import LOCAL_FALLBACK_MODEL
from app.services.sync import MailSyncService
from app.services.types import ClassificationDecision, SyncBatch
from tests.conftest import FakeAuthManager, FakeClassifier, FakeMailboxClient, make_envelope, make_settings


def test_sync_deduplicates_and_updates_history_cursor(tmp_path) -> None:
    settings = make_settings(tmp_path)
    database = create_database_bundle(settings.database_url)
    init_database(database, settings)

    mailbox = FakeMailboxClient()
    classifier = FakeClassifier(
        {
            "msg-1": ClassificationDecision("spam", 0.95, ["Suspicious payment request"], "req-1", "test-model"),
            "msg-2": ClassificationDecision("not_spam", 0.10, ["Trusted sender"], "req-2", "test-model"),
            "msg-3": ClassificationDecision("spam", 0.91, ["Urgent credential reset"], "req-3", "test-model"),
        }
    )
    auth = FakeAuthManager(mailbox)
    service = MailSyncService(settings, auth, classifier)
    cipher = TokenCipher(settings.app_encryption_key)

    mailbox.recent_batches.append(
        SyncBatch(
            messages=[
                make_envelope("msg-1", subject="Urgent invoice"),
                make_envelope("msg-2", subject="Weekly update"),
            ],
            latest_history_id="101",
            source="backfill",
            notes=["Skipped Gmail message msg-ignored: backend error"],
        )
    )
    mailbox.incremental_batches.append(
        SyncBatch(
            messages=[
                make_envelope("msg-1", subject="Urgent invoice", history_id="102"),
                make_envelope("msg-3", subject="Reset your password", history_id="102"),
            ],
            latest_history_id="102",
            source="incremental",
        )
    )

    with database.session_factory() as db:
        db.add(
            ConnectedAccount(
                email="owner@example.com",
                refresh_token_encrypted=cipher.encrypt("refresh-token"),
                access_token="access-token",
            )
        )
        db.commit()

        first_report = service.sync(db)
        second_report = service.sync(db)

        account = db.scalar(select(ConnectedAccount))
        messages = db.scalars(select(StoredMessage).order_by(StoredMessage.gmail_message_id)).all()
        classifications = db.scalars(select(ClassificationResult).order_by(ClassificationResult.id)).all()

    assert first_report.fetched_count == 2
    assert first_report.labeled_count == 1
    assert second_report.fetched_count == 1
    assert second_report.skipped_count == 1
    assert account.last_history_id == "102"
    assert len(messages) == 3
    assert len(classifications) == 3
    assert "Skipped Gmail message msg-ignored: backend error" in first_report.notes
    assert mailbox.applied_labels == [("msg-1", "Label_999"), ("msg-3", "Label_999")]


def test_sync_reports_when_local_fallback_classification_is_used(tmp_path) -> None:
    settings = make_settings(tmp_path)
    database = create_database_bundle(settings.database_url)
    init_database(database, settings)

    mailbox = FakeMailboxClient()
    classifier = FakeClassifier(
        {
            "msg-1": ClassificationDecision(
                "spam",
                0.84,
                ["OpenAI rate-limited this scan, so a conservative local review was used."],
                None,
                LOCAL_FALLBACK_MODEL,
            )
        }
    )
    auth = FakeAuthManager(mailbox)
    service = MailSyncService(settings, auth, classifier)
    cipher = TokenCipher(settings.app_encryption_key)

    mailbox.recent_batches.append(
        SyncBatch(
            messages=[make_envelope("msg-1", subject="Verify your password immediately")],
            latest_history_id="101",
            source="backfill",
        )
    )

    with database.session_factory() as db:
        db.add(
            ConnectedAccount(
                email="owner@example.com",
                refresh_token_encrypted=cipher.encrypt("refresh-token"),
                access_token="access-token",
            )
        )
        db.commit()

        report = service.sync(db)
        message = db.scalar(select(StoredMessage).where(StoredMessage.gmail_message_id == "msg-1"))
        classification = db.scalar(select(ClassificationResult).where(ClassificationResult.message_id == message.id))

    assert report.classified_count == 1
    assert report.labeled_count == 0
    assert classification.provider_model == LOCAL_FALLBACK_MODEL
    assert any("conservative local review" in note.lower() for note in report.notes)
    assert mailbox.applied_labels == []
