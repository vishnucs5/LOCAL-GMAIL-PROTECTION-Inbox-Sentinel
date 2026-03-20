from __future__ import annotations

from datetime import datetime, timezone
import logging
from threading import Lock, Thread

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings, get_settings
from app.database import create_database_bundle, ensure_app_settings, init_database
from app.models import ClassificationResult, ConnectedAccount, StoredMessage
from app.security import TokenCipher
from app.services.classifier import ClassifierError, OpenAICompatibleClassifier
from app.services.gmail import GmailAuthManager, GmailServiceError
from app.services.sync import MailSyncService
from app.services.types import SyncReport

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_app(
    settings: Settings | None = None,
    *,
    gmail_auth_manager: GmailAuthManager | None = None,
    classifier: OpenAICompatibleClassifier | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    database = create_database_bundle(settings.database_url)
    token_cipher = TokenCipher(settings.app_encryption_key)
    gmail_auth_manager = gmail_auth_manager or GmailAuthManager(settings, token_cipher)
    classifier = classifier or OpenAICompatibleClassifier(settings)
    sync_service = MailSyncService(settings, gmail_auth_manager, classifier)
    templates = Jinja2Templates(directory=str(settings.template_dir))
    templates.env.filters["datetimefmt"] = format_datetime
    templates.env.filters["percentfmt"] = format_percent

    app = FastAPI(title=settings.app_name)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

    app.state.settings = settings
    app.state.database = database
    app.state.session_factory = database.session_factory
    app.state.templates = templates
    app.state.token_cipher = token_cipher
    app.state.gmail_auth_manager = gmail_auth_manager
    app.state.classifier = classifier
    app.state.sync_service = sync_service
    app.state.sync_lock = Lock()
    app.state.last_sync_report = None
    app.state.scheduler = None
    app.state.sync_thread = None

    @app.on_event("startup")
    def startup() -> None:
        init_database(database, settings)
        if settings.enable_scheduler:
            scheduler = BackgroundScheduler(timezone="UTC")
            scheduler.start()
            app.state.scheduler = scheduler
            schedule_sync_job(app)

    @app.on_event("shutdown")
    def shutdown() -> None:
        scheduler = app.state.scheduler
        if scheduler:
            scheduler.shutdown(wait=False)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        with app.state.session_factory() as db:
            context = build_dashboard_context(request, db)
            return templates.TemplateResponse("index.html", context)

    @app.get("/messages", response_class=HTMLResponse)
    def messages(request: Request) -> HTMLResponse:
        with app.state.session_factory() as db:
            rows = db.scalars(
                select(StoredMessage)
                .options(joinedload(StoredMessage.classification))
                .order_by(StoredMessage.received_at.desc(), StoredMessage.id.desc())
            ).unique().all()
            return templates.TemplateResponse(
                "messages.html",
                {
                    "request": request,
                    "messages": rows,
                    "runtime_settings": ensure_app_settings(db, settings),
                    "account": get_connected_account(db),
                    "last_sync_report": app.state.last_sync_report,
                },
            )

    @app.get("/messages/{gmail_id}", response_class=HTMLResponse)
    def message_detail(request: Request, gmail_id: str) -> HTMLResponse:
        with app.state.session_factory() as db:
            message = db.scalar(
                select(StoredMessage)
                .where(StoredMessage.gmail_message_id == gmail_id)
                .options(joinedload(StoredMessage.classification))
            )
            if message is None:
                raise HTTPException(status_code=404, detail="Message not found.")

            return templates.TemplateResponse(
                "message_detail.html",
                {
                    "request": request,
                    "message": message,
                    "runtime_settings": ensure_app_settings(db, settings),
                    "account": get_connected_account(db),
                    "last_sync_report": app.state.last_sync_report,
                },
            )

    @app.post("/sync/run")
    def run_sync(request: Request) -> Response:
        report = start_sync(app, source="manual")
        if request.headers.get("HX-Request") == "true":
            response = templates.TemplateResponse(
                "_sync_status.html",
                {"request": request, "report": report},
            )
            return response
        return RedirectResponse(url="/", status_code=303)

    @app.get("/sync/status", response_class=HTMLResponse)
    def sync_status(request: Request) -> HTMLResponse:
        report = request.app.state.last_sync_report
        response = templates.TemplateResponse(
            "_sync_status.html",
            {"request": request, "report": report},
        )
        if report and report.status != "busy":
            response.headers["HX-Refresh"] = "true"
        return response

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        with app.state.session_factory() as db:
            return templates.TemplateResponse(
                "settings.html",
                {
                    "request": request,
                    "runtime_settings": ensure_app_settings(db, settings),
                    "account": get_connected_account(db),
                    "google_ready": settings.google_is_configured,
                    "ai_ready": settings.ai_is_configured,
                    "last_sync_report": app.state.last_sync_report,
                },
            )

    @app.post("/settings")
    async def update_settings(request: Request) -> RedirectResponse:
        form = await request.form()
        try:
            poll_interval_seconds = max(15, min(3600, int(str(form.get("poll_interval_seconds", "60")))))
            spam_threshold = max(0.0, min(1.0, float(str(form.get("spam_threshold", "0.85")))))
            max_body_chars = max(500, min(20000, int(str(form.get("max_body_chars", "8192")))))
            gmail_label_name = str(form.get("gmail_label_name", "AI_SPAM_REVIEW")).strip() or "AI_SPAM_REVIEW"
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid settings values.") from exc

        with app.state.session_factory() as db:
            runtime_settings = ensure_app_settings(db, settings)
            runtime_settings.poll_interval_seconds = poll_interval_seconds
            runtime_settings.spam_threshold = spam_threshold
            runtime_settings.max_body_chars = max_body_chars
            runtime_settings.gmail_label_name = gmail_label_name
            db.add(runtime_settings)
            db.commit()

        schedule_sync_job(app)
        return RedirectResponse(url="/settings", status_code=303)

    @app.get("/auth/google/start")
    def google_auth_start(request: Request) -> RedirectResponse:
        try:
            authorization_url = app.state.gmail_auth_manager.build_authorization_url(request.session)
        except GmailServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=authorization_url, status_code=302)

    @app.get("/auth/google/callback")
    def google_auth_callback(request: Request, code: str, state: str) -> RedirectResponse:
        try:
            result = app.state.gmail_auth_manager.complete_authorization(
                code=code,
                state=state,
                expected_state=request.session.pop("oauth_state", None),
                code_verifier=request.session.pop("oauth_code_verifier", None),
            )
        except GmailServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Unexpected Google OAuth callback failure")
            raise HTTPException(status_code=400, detail=f"Unexpected Google OAuth error: {exc}") from exc

        with app.state.session_factory() as db:
            try:
                replace_connected_account(
                    app,
                    db,
                    result.email,
                    result.refresh_token,
                    result.access_token,
                    result.access_token_expires_at,
                )
            except Exception as exc:
                logger.exception("Failed to save the connected Gmail account")
                raise HTTPException(status_code=500, detail=f"Failed to save the connected Gmail account: {exc}") from exc
        return RedirectResponse(url="/", status_code=303)

    @app.post("/auth/google/disconnect")
    def google_disconnect() -> RedirectResponse:
        with app.state.session_factory() as db:
            account = get_connected_account(db)
            if account is not None:
                db.delete(account)
                db.commit()
        return RedirectResponse(url="/", status_code=303)

    return app


def format_datetime(value: object) -> str:
    if value is None:
        return "Never"
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def format_percent(value: object) -> str:
    if value is None:
        return "0%"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "0%"


def get_connected_account(db) -> ConnectedAccount | None:
    return db.scalar(select(ConnectedAccount).order_by(ConnectedAccount.id))


def build_dashboard_context(request: Request, db) -> dict[str, object]:
    runtime_settings = ensure_app_settings(db, request.app.state.settings)
    account = get_connected_account(db)
    recent_messages = db.scalars(
        select(StoredMessage)
        .options(joinedload(StoredMessage.classification))
        .order_by(StoredMessage.received_at.desc(), StoredMessage.id.desc())
        .limit(25)
    ).unique().all()

    total_messages = db.scalar(select(func.count(StoredMessage.id))) or 0
    spam_messages = db.scalar(select(func.count(ClassificationResult.id)).where(ClassificationResult.verdict == "spam")) or 0
    labeled_messages = db.scalar(select(func.count(StoredMessage.id)).where(StoredMessage.label_applied.is_(True))) or 0
    review_needed = (
        db.scalar(
            select(func.count(ClassificationResult.id)).where(
                (ClassificationResult.verdict == "spam") & (ClassificationResult.confidence < runtime_settings.spam_threshold)
            )
        )
        or 0
    )

    return {
        "request": request,
        "account": account,
        "runtime_settings": runtime_settings,
        "recent_messages": recent_messages,
        "stats": {
            "total_messages": total_messages,
            "spam_messages": spam_messages,
            "labeled_messages": labeled_messages,
            "review_needed": review_needed,
        },
        "google_ready": request.app.state.settings.google_is_configured,
        "ai_ready": request.app.state.settings.ai_is_configured,
        "last_sync_report": request.app.state.last_sync_report,
    }


def execute_sync(app: FastAPI) -> SyncReport:
    if not app.state.sync_lock.acquire(blocking=False):
        report = SyncReport(status="busy", source="lock", error="A sync is already running.")
        app.state.last_sync_report = report
        return report

    return _run_sync_locked(app)


def start_sync(app: FastAPI, *, source: str) -> SyncReport:
    if not app.state.sync_lock.acquire(blocking=False):
        report = SyncReport(status="busy", source="lock", error="A sync is already running.", ran_at=utcnow())
        app.state.last_sync_report = report
        return report

    report = SyncReport(status="busy", source=source, error="Scanning inbox in the background...", ran_at=utcnow())
    app.state.last_sync_report = report
    sync_thread = Thread(target=_run_sync_locked, args=(app,), daemon=True, name=f"inbox-sync-{source}")
    app.state.sync_thread = sync_thread
    sync_thread.start()
    return report


def _run_sync_locked(app: FastAPI) -> SyncReport:
    try:
        with app.state.session_factory() as db:
            try:
                report = app.state.sync_service.sync(db)
            except (ClassifierError, GmailServiceError) as exc:
                logger.exception("Sync failed")
                report = SyncReport(status="error", source="runtime", error=str(exc))
            except Exception as exc:
                logger.exception("Unexpected sync failure")
                report = SyncReport(status="error", source="runtime", error=f"Unexpected sync error: {exc}")
    finally:
        app.state.sync_lock.release()

    app.state.last_sync_report = report
    app.state.sync_thread = None
    return report


def schedule_sync_job(app: FastAPI) -> None:
    scheduler = app.state.scheduler
    if scheduler is None:
        return

    with app.state.session_factory() as db:
        runtime_settings = ensure_app_settings(db, app.state.settings)
        seconds = runtime_settings.poll_interval_seconds

    scheduler.add_job(
        lambda: execute_sync(app),
        trigger="interval",
        seconds=seconds,
        id="gmail-sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def replace_connected_account(
    app: FastAPI,
    db,
    email: str,
    refresh_token: str,
    access_token: str | None,
    access_token_expires_at,
) -> None:
    encrypted_refresh_token = app.state.token_cipher.encrypt(refresh_token)
    existing = get_connected_account(db)
    if existing is None:
        existing = ConnectedAccount(
            email=email,
            refresh_token_encrypted=encrypted_refresh_token,
            access_token=access_token,
            access_token_expires_at=access_token_expires_at,
        )
        db.add(existing)
        db.commit()
        return

    if existing.email != email:
        db.delete(existing)
        db.commit()
        existing = ConnectedAccount(
            email=email,
            refresh_token_encrypted=encrypted_refresh_token,
            access_token=access_token,
            access_token_expires_at=access_token_expires_at,
        )
        db.add(existing)
        db.commit()
        return

    existing.refresh_token_encrypted = encrypted_refresh_token
    existing.access_token = access_token
    existing.access_token_expires_at = access_token_expires_at
    db.add(existing)
    db.commit()


app = create_app()
