# Signal Clone — Backend (Phase 0)

FastAPI + SQLAlchemy + SQLite. Phase 0 is a minimal scaffold: the app boots
and `GET /health` returns `{"status": "ok", "db": "reachable"}`. Auth, models,
and WebSockets land in later phases — see the master plan.

## Prerequisites

- **Python 3.11+** (the repo pins `3.11` in `.python-version`)
- `git`

## Setup

```bash
cd backend
python -m venv .venv

# Activate (pick the one for your shell)
# bash / zsh:
source .venv/bin/activate
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Windows cmd:
.venv\Scripts\activate.bat

pip install -r requirements.txt
```

Copy the example env file before first run so the app has a `JWT_SECRET` to
read (Phase 2 will use it; Phase 0 ignores it but the config still loads it):

```bash
cp .env.example .env       # bash
# or on Windows PowerShell:
Copy-Item .env.example .env
```

## Run

> **Single worker only.** SQLite has a single writer; running more than one
> uvicorn worker would cause `database is locked` errors under any real
> load. This is a locked decision — see `PLAN.md` ("SQLite production config").
> Do **not** add `--workers N` for N > 1.

```bash
# bash / zsh
.venv/bin/uvicorn app.main:app --reload

# Windows PowerShell
.venv\Scripts\uvicorn.exe app.main:app --reload
```

The server listens on `http://localhost:8000`. Interactive API docs are at
`http://localhost:8000/docs`.

## Verify

```bash
curl http://localhost:8000/health
# -> {"status":"ok","db":"reachable"}
```

The SQLite file is created on first boot at `./app.db` (relative to the
`backend/` directory — see `DATABASE_URL` in `.env`).

Confirm WAL mode:

```bash
sqlite3 app.db "PRAGMA journal_mode;"
# -> wal
```

## Environment variables

All settings live in `app/config.py` and are read from the environment or
`.env`. Defaults are dev-friendly so the app boots out of the box.

| Var                  | Default                          | Notes |
|----------------------|----------------------------------|-------|
| `DATABASE_URL`       | `sqlite:///./app.db`             | SQLAlchemy URL. In prod, point at a Railway Volume mount. |
| `JWT_SECRET`         | `change-me-in-prod`              | **Override in prod.** Used in Phase 2. |
| `JWT_ALGORITHM`      | `HS256`                          | Used in Phase 2. |
| `JWT_EXPIRES_MINUTES`| `10080` (7 days)                 | Used in Phase 2. |
| `CORS_ORIGINS`       | `http://localhost:3000`          | Comma-separated. Add the prod frontend host when deploying. |

## Project layout

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI app, /health endpoint
│   ├── config.py         # pydantic-settings BaseSettings, loads .env
│   └── database.py       # SQLAlchemy engine, SessionLocal, get_db, WAL pragmas
├── tests/
│   └── __init__.py
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## What lands in later phases

- Phase 1: SQLAlchemy models for users, conversations, messages, etc.
- Phase 2: JWT auth (mocked OTP → real token).
- Phase 5: WebSocket endpoint at `/ws`.
- Phase 9: Deploy to Railway with a persistent Volume for `app.db`.
