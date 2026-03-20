from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ConnectedAccount(Base):
    __tablename__ = "connected_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_history_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    messages: Mapped[list["StoredMessage"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
    )


class StoredMessage(Base):
    __tablename__ = "stored_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("connected_accounts.id"), nullable=False, index=True)
    gmail_message_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    history_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    subject: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    label_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gmail_label_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    account: Mapped[ConnectedAccount] = relationship(back_populates="messages")
    classification: Mapped["ClassificationResult | None"] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        uselist=False,
    )


class ClassificationResult(Base):
    __tablename__ = "classification_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("stored_messages.id"), unique=True, nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    message: Mapped[StoredMessage] = relationship(back_populates="classification")


class AppSettingsModel(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    spam_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.85)
    gmail_label_name: Mapped[str] = mapped_column(String(255), nullable=False, default="AI_SPAM_REVIEW")
    max_body_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=8192)
    backfill_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
