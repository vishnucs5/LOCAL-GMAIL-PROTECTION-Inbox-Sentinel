from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.models import AppSettingsModel, Base


@dataclass
class DatabaseBundle:
    engine: Engine
    session_factory: sessionmaker[Session]


def create_database_bundle(database_url: str) -> DatabaseBundle:
    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"future": True}

    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        engine_kwargs["connect_args"] = connect_args
        if ":memory:" in database_url:
            engine_kwargs["poolclass"] = StaticPool

    engine = create_engine(database_url, **engine_kwargs)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return DatabaseBundle(engine=engine, session_factory=session_factory)


def init_database(bundle: DatabaseBundle, settings: Settings) -> None:
    Base.metadata.create_all(bundle.engine)
    with bundle.session_factory() as session:
        ensure_app_settings(session, settings)
        session.commit()


def ensure_app_settings(session: Session, settings: Settings) -> AppSettingsModel:
    record = session.scalar(select(AppSettingsModel).where(AppSettingsModel.id == 1))
    if record:
        return record

    record = AppSettingsModel(
        id=1,
        poll_interval_seconds=settings.default_poll_interval_seconds,
        spam_threshold=settings.default_spam_threshold,
        gmail_label_name=settings.default_gmail_label_name,
        max_body_chars=settings.default_max_body_chars,
        backfill_limit=settings.initial_backfill_limit,
    )
    session.add(record)
    session.flush()
    return record
