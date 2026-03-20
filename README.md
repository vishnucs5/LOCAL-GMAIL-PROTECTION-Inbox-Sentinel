# Inbox Sentinel

Inbox Sentinel is a local FastAPI dashboard that connects to one Gmail inbox, scans recent mail, classifies likely spam with an AI API, and applies a reversible Gmail label named `AI_SPAM_REVIEW`.

## Features

- Gmail OAuth login for a single mailbox
- Local SQLite storage for messages and classifications
- AI-powered spam classification with confidence scores and reasons
- Automatic Gmail label application for high-confidence spam
- Manual scan button plus optional background polling
- Review dashboard for recent mail, flagged spam, and settings

## Stack

- FastAPI + Jinja2 + HTMX
- SQLAlchemy + SQLite
- APScheduler
- Gmail API
- OpenAI-compatible chat completion API

## Setup

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/).
   The current machine appears to have a blocked Microsoft Store Python alias, so a direct Python.org install is the safest path.
2. Create and activate a virtual environment.
3. Install the app:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

4. Copy `.env.example` to `.env` and fill in the Google OAuth and AI API credentials.
   For a smoother first run on a local laptop, keep `ENABLE_SCHEDULER=false` until the first manual scan succeeds.
5. Create a Google OAuth web application and add `http://localhost:8000/auth/google/callback` as an authorized redirect URI.
6. Run the app:

```powershell
uvicorn app.main:app --reload
```

7. Open [http://localhost:8000](http://localhost:8000).

## Gmail OAuth Scopes

The app requests:

- `openid`
- `https://www.googleapis.com/auth/userinfo.email`
- `https://www.googleapis.com/auth/gmail.modify`

`gmail.modify` is required to apply the `AI_SPAM_REVIEW` label.

## Notes

- Email data is stored locally in SQLite.
- Refresh tokens are encrypted at rest with `APP_ENCRYPTION_KEY`.
- If the AI API is not configured, sync attempts will fail with a clear dashboard error until the key is added.
- Gmail API requests use a bounded timeout so a transient Gmail backend issue does not leave the dashboard stuck in `busy`.
