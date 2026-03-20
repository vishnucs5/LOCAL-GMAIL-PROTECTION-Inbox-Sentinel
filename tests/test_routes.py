from __future__ import annotations

import time

from sqlalchemy import select

from app.models import AppSettingsModel, ConnectedAccount, StoredMessage
from app.security import TokenCipher
from app.services.types import ClassificationDecision, SyncBatch
from tests.conftest import make_envelope


def test_manual_sync_route_runs_scan_and_stores_message(client, app_bundle) -> None:
    app, settings, mailbox, classifier = app_bundle
    classifier.decisions["msg-100"] = ClassificationDecision(
        verdict="spam",
        confidence=0.97,
        reasons=["Credential harvesting language"],
        provider_request_id="req-100",
        provider_model="test-model",
    )
    mailbox.recent_batches.append(
        SyncBatch(
            messages=[make_envelope("msg-100", subject="Verify your account now")],
            latest_history_id="201",
            source="backfill",
        )
    )

    with app.state.session_factory() as db:
        db.add(
            ConnectedAccount(
                email="owner@example.com",
                refresh_token_encrypted=TokenCipher(settings.app_encryption_key).encrypt("refresh-token"),
                access_token="access-token",
            )
        )
        db.commit()

    response = client.post("/sync/run", follow_redirects=False)
    assert response.status_code == 303

    stored_message = None
    for _ in range(20):
        with app.state.session_factory() as db:
            stored_message = db.scalar(select(StoredMessage).where(StoredMessage.gmail_message_id == "msg-100"))
        if stored_message is not None:
            break
        time.sleep(0.05)

    assert stored_message is not None
    assert stored_message.label_applied is True


def test_settings_route_updates_runtime_values(client) -> None:
    response = client.post(
        "/settings",
        data={
            "poll_interval_seconds": "120",
            "spam_threshold": "0.91",
            "max_body_chars": "7000",
            "gmail_label_name": "CUSTOM_REVIEW",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with client.app.state.session_factory() as db:
        runtime_settings = db.scalar(select(AppSettingsModel))

    assert runtime_settings is not None
    assert runtime_settings.poll_interval_seconds == 120
    assert runtime_settings.spam_threshold == 0.91
    assert runtime_settings.max_body_chars == 7000
    assert runtime_settings.gmail_label_name == "CUSTOM_REVIEW"


def test_dashboard_renders_sync_target_before_first_scan(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert 'id="sync-status-shell"' in response.text
    assert "No sync has run yet." in response.text


def test_sync_status_partial_polls_while_busy(client) -> None:
    client.app.state.last_sync_report = None
    busy_response = client.post("/sync/run", headers={"HX-Request": "true"})

    assert busy_response.status_code == 200
    assert 'hx-get="/sync/status"' in busy_response.text
