from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    app_name: str = "Inbox Sentinel"
    app_environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    database_url: str = "sqlite:///./spam_detector.db"
    enable_scheduler: bool = True

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    app_encryption_key: str = "change-me"
    session_secret: str = "change-me-too"

    ai_api_key: str = ""
    ai_api_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4.1-mini"
    ai_timeout_seconds: float = 30.0
    gmail_timeout_seconds: float = 5.0

    default_poll_interval_seconds: int = 60
    default_spam_threshold: float = 0.85
    default_gmail_label_name: str = "AI_SPAM_REVIEW"
    default_max_body_chars: int = 8192
    initial_backfill_limit: int = 10

    project_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    @property
    def template_dir(self) -> Path:
        return self.project_root / "app" / "templates"

    @property
    def static_dir(self) -> Path:
        return self.project_root / "app" / "static"

    @property
    def gmail_scopes(self) -> list[str]:
        return [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/gmail.modify",
        ]

    @property
    def google_is_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def ai_is_configured(self) -> bool:
        return bool(self.ai_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
