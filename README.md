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

## Auth (mocked OTP)

The OTP is the constant `123456`. There is no SMS or email — the
`/auth/request-otp` response includes a `debug_otp` field so the dev
frontend can autofill it. Gate or remove that field before deploying.

### Test auth with curl

```bash
# 1. Request an OTP (upserts the user; response includes debug_otp)
curl -s -X POST http://localhost:8000/auth/request-otp \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+15550000099"}'
# -> {"sent":true,"debug_otp":"123456"}

# 2. Verify the OTP — issues a JWT
TOKEN=$(curl -s -X POST http://localhost:8000/auth/verify-otp \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+15550000099","otp":"123456"}' \
  | python -c 'import sys,json; print(json.load(sys.stdin)["token"])')

# 3. /auth/me with the token
curl -s http://localhost:8000/auth/me -H "Authorization: Bearer $TOKEN"

# 4. PATCH /auth/profile
curl -s -X PATCH http://localhost:8000/auth/profile \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"Test User","username":"testuser99"}'

# 5. /auth/me without a token — expect 401
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/auth/me
# -> 401

# 6. /auth/verify-otp with the wrong OTP — expect 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/auth/verify-otp \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+15550000099","otp":"000000"}'
# -> 401
```

The token payload includes `sub` (user id), `phone`, `iat`, `exp`, and
`iss="signal-clone"`. Decode locally to inspect:

```bash
python -c "import sys, jwt as j; print(j.decode(sys.argv[1], options={'verify_signature':False}))" "$TOKEN"
```

### Auth smoke test

A scripted end-to-end check (no pytest needed). Boots nothing extra —
it talks to whatever's already serving on port 8000.

```bash
.venv/bin/python tests/test_auth.py
```

Exits 0 on success, non-zero on the first failing assertion. Safe to
run repeatedly; uses a fresh phone (`+1555000<random>`) so the seed
data isn't disturbed.

## Read API

All routes below require `Authorization: Bearer <token>` (the JWT from
`/auth/verify-otp`). `/`, `/health`, and `/auth/*` are the only public
routes. The smoke script `tests/test_conversations.py` exercises each of
these against Alice's token.

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/verify-otp \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+15550000001","otp":"123456"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['token'])")
H="Authorization: Bearer $TOKEN"
```

### `GET /conversations`

```bash
curl -s http://localhost:8000/conversations -H "$H" | python -m json.tool
```

Returns the current user's conversations, most-recent-activity first.
Each entry includes participants, last message preview, unread count,
and a computed `avatar_url` (other user's avatar for direct chats;
DiceBear initials URL for groups).

### `GET /conversations/{id}/messages?limit=50&before=<message_id>`

```bash
curl -s "http://localhost:8000/conversations/1/messages?limit=5" -H "$H" | python -m json.tool
```

Paginated timeline, ASC by time. `before` is a message id; omit it for
the latest `limit` messages. 403 if the caller isn't a participant.

### `GET /contacts`

```bash
curl -s http://localhost:8000/contacts -H "$H" | python -m json.tool
```

Current user's address book, newest first.

### `POST /contacts`

```bash
curl -s -X POST http://localhost:8000/contacts \
  -H "$H" -H 'Content-Type: application/json' \
  -d '{"phone":"+15550000002"}' | python -m json.tool
```

Add a contact by phone. Returns the (new or existing) `direct`
conversation as a `ConversationOut`. Status codes:
- `404` if the phone doesn't match a user
- `409` if already a contact
- `200` on success

### `GET /users/search?q=<query>`

```bash
curl -s "http://localhost:8000/users/search?q=ali" -H "$H" | python -m json.tool
```

Case-insensitive search across phone prefix, display_name substring,
and username substring. Excludes the caller. `already_contact` is true
if a `contacts` row already exists. Empty `q` returns `[]`. Capped at
20 results.

## Environment variables

All settings live in `app/config.py` and are read from the environment or
`.env`. Defaults are dev-friendly so the app boots out of the box.

| Var                  | Default                          | Notes |
|----------------------|----------------------------------|-------|
| `DATABASE_URL`       | `sqlite:///./app.db`             | SQLAlchemy URL. In prod, point at a Railway Volume mount. |
| `JWT_SECRET`         | `change-me-in-prod`              | **Override in prod.** HMAC secret for signing JWTs. |
| `JWT_ALGORITHM`      | `HS256`                          | JWT signing algorithm. |
| `JWT_EXPIRES_MINUTES`| `10080` (7 days)                 | Token TTL. |
| `JWT_ISSUER`         | `signal-clone`                   | `iss` claim. Tokens with a different `iss` are rejected. |
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
│   ├── auth/              # Phase 2: mocked-OTP login + JWT
│   │   ├── __init__.py
│   │   ├── jwt.py         # encode/decode (HS256, iss, exp)
│   │   ├── otp.py         # constant 123456
│   │   ├── schemas.py     # RequestOtp, VerifyOtp, ProfileUpdate, UserOut
│   │   ├── deps.py        # get_current_user → User
│   │   └── router.py      # /auth/request-otp, /verify-otp, /me, /profile
│   ├── schemas/           # Phase 4: Pydantic response models
│   │   └── __init__.py    # ConversationOut, MessageOut, ContactOut, UserSearchResult, …
│   ├── conversations/     # Phase 4: list + message timeline
│   │   ├── __init__.py
│   │   ├── router.py      # /conversations, /conversations/{id}/messages
│   │   └── service.py     # last_message, unread_count, avatar_url, assembler
│   ├── contacts/          # Phase 4: list + add
│   │   ├── __init__.py
│   │   └── router.py      # /contacts (GET, POST)
│   ├── users/             # Phase 4: search
│   │   ├── __init__.py
│   │   └── router.py      # /users/search
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
│   ├── __init__.py
│   ├── test_auth.py           # auth smoke (Phase 2)
│   └── test_conversations.py  # read-API smoke (Phase 4)
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
