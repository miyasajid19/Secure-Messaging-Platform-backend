# Signal Clone — Backend

FastAPI + SQLAlchemy 2.x + SQLite. Phase 0/1: the app boots, `GET /health`
returns row counts for every table, and a `python -m app.seed` script gives
the UI realistic demo data. Auth, REST endpoints, and WebSockets land in
later phases — see the master plan in `../PLAN.md`.

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
read (Phase 2 will use it; later phases still need it):

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

On startup the app calls `Base.metadata.create_all(engine)`, which is
idempotent: a fresh `./app.db` gets every table; a warm restart is a no-op.
Alembic migrations are intentionally out of scope for this demo (see
[Schema management](#schema-management) below).

## Seed

A small but realistic dataset — 5 users, 3 groups, 4 direct conversations,
35 messages spread over the last 7 days, plus an attachment, a reaction,
and a reply. Re-running is a no-op: the script keys on the first seed
user's phone and short-circuits if the DB is already populated.

```bash
# from anywhere, as long as cwd is the project root containing app/
.venv/bin/python -m app.seed
# or on Windows:
.venv\Scripts\python.exe -m app.seed
```

`/health` then reports counts for every table. Use this to confirm the
seed (and any later data work) without opening a SQLite client.

## Verify

```bash
curl http://localhost:8000/health
# -> {"status":"ok","db":"reachable","counts":{"users":5,"contacts":0,"conversations":7,...}}
```

The SQLite file is created on first boot at `./app.db` (relative to the
`backend/` directory — see `DATABASE_URL` in `.env`).

Confirm WAL mode and the schema:

```bash
sqlite3 app.db "PRAGMA journal_mode;"
# -> wal

sqlite3 app.db ".tables"
# -> attachments  contacts  conversation_participants  conversations
#    message_reactions  message_status  messages  users

sqlite3 app.db ".schema messages"
# -> ... parent_id INTEGER, disappear_after_seconds INTEGER, ...
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
│   ├── main.py            # FastAPI app, /health, create_all on startup
│   ├── config.py          # pydantic-settings BaseSettings, loads .env
│   ├── database.py        # SQLAlchemy engine, SessionLocal, get_db, WAL pragmas
│   ├── seed.py            # `python -m app.seed` — idempotent demo data
│   └── models/            # SQLAlchemy 2.x typed declarative models
│       ├── __init__.py    # re-exports so Base.metadata sees everything
│       ├── enums.py       # ConversationType, MessageType, MessageStatusState, …
│       ├── user.py
│       ├── contact.py
│       ├── conversation.py    # Conversation + ConversationParticipant
│       ├── message.py         # Message + MessageStatus
│       ├── attachment.py
│       └── reaction.py        # MessageReaction
├── tests/
│   └── __init__.py
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Schema management

`Base.metadata.create_all` is run on app startup and from the seed
script. It's idempotent — tables that exist are left alone, missing ones
are created. We deliberately do **not** use Alembic for this demo:

- The schema is small and only changes in clearly-bounded phases.
- `create_all` is enough to keep dev and prod in lockstep while we're the
  only writer to the DB.
- When the schema stops being trivially mutable (Phase 3+), revisit.

If you ever need to wipe state: `rm app.db app.db-wal app.db-shm` and
re-run `python -m app.seed`.

## What lands in later phases

- Phase 2: JWT auth (mocked OTP → real token) + Pydantic schemas.
- Phase 4: Conversation list and contact REST endpoints.
- Phase 5: WebSocket endpoint at `/ws`.
- Phase 8: Bonus features (reply/quoted, reactions, disappearing, attachments).
- Phase 9: Deploy to Railway with a persistent Volume for `app.db`.
