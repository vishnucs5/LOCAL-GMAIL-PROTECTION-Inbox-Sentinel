from __future__ import annotations

from sqlalchemy import select

from app.models import ConnectedAccount


def test_google_oauth_callback_persists_connected_account(client) -> None:
    start_response = client.get("/auth/google/start", follow_redirects=False)
    assert start_response.status_code == 302

    callback_response = client.get(
        "/auth/google/callback",
        params={"code": "demo-code", "state": "test-state"},
        follow_redirects=False,
    )
    assert callback_response.status_code == 303

    with client.app.state.session_factory() as db:
        account = db.scalar(select(ConnectedAccount))

    assert account is not None
    assert account.email == "owner@example.com"
    assert account.refresh_token_encrypted != "refresh-token"
