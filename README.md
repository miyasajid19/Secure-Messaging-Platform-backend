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

## Realtime (WebSocket)

`WS /ws?token=<jwt>` is the single bidirectional channel. Auth is via
a query-param token (cookies don't survive cross-origin WS upgrades
reliably; the locked pattern is `?token=<jwt>`). The HTTP contract
still uses `Authorization: Bearer` — they're parallel mechanisms, not
interchangeable.

**Single-worker rule still applies.** The connection manager holds all
state in memory (`_connections: dict[int, WebSocket]`,
`_typing: dict[(conversation_id, user_id), float]`). Running more than
one uvicorn worker would split the state across processes and break
presence/typing. See `PLAN.md` "SQLite production config".

### Client → Server

```json
{ "type": "typing.start", "conversation_id": 1 }
{ "type": "typing.stop",  "conversation_id": 1 }
{ "type": "message.read", "message_id": 42 }
```

### Server → Client

| event                | payload                                                                         |
|----------------------|---------------------------------------------------------------------------------|
| `presence.snapshot`  | `{ type, online_user_ids: [int, ...] }` — sent once on connect                  |
| `presence`           | `{ type, user_id, online: bool }` — broadcast to other clients on join/leave   |
| `typing`             | `{ type, conversation_id, user_id, state: "start" \| "stop" }`                |
| `message.new`        | `{ type, message: MessageOut }`                                                |
| `message.read`       | `{ type, message_id, read_by, read_at }` (single message via WS)              |
| `message.read.bulk`  | `{ type, conversation_id, reader_id, up_to_message_id }` (via REST `/read`)   |

`typing` is automatically broadcast as `state: "stop"` after 6s of
silence (the manager's sweeper task).

### Realtime REST endpoints

| Method | Path                                                       | Notes |
|--------|------------------------------------------------------------|-------|
| `POST` | `/conversations/{id}/messages`                              | Send. Body `{content, type?, parent_id?}`. 201 + `MessageOut`. |
| `POST` | `/conversations/{id}/read`                                  | Mark read. Body `{message_id}`. Returns `{marked_read}`. |
| `GET`  | `/conversations/{id}/message-status?message_ids=1,2,3`      | `{message_id: status}` map. |
| `GET`  | `/users/online`                                            | `[user_id, ...]` of currently connected users. |

### Quick sanity check

```bash
# Get Alice's token
TOKEN=$(curl -s -X POST http://localhost:8000/auth/verify-otp \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+15550000001","otp":"123456"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Online users
curl -s http://localhost:8000/users/online -H "Authorization: Bearer $TOKEN"
# -> []

# Send a message (Alice → direct conversation with Bob, id=1 in the seed)
curl -s -X POST http://localhost:8000/conversations/1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"content":"hello from curl"}' | python -m json.tool

# Mark read
curl -s -X POST http://localhost:8000/conversations/1/read \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message_id":1}'
# -> {"marked_read":1}

# Per-message status
curl -s "http://localhost:8000/conversations/1/message-status?message_ids=1,2" \
  -H "Authorization: Bearer $TOKEN"
# -> {"1":"read"}
```

For a real WS exercise use the smoke test:

```bash
.venv/bin/python tests/test_realtime.py
```

It opens two clients (Alice + Bob), exchanges typing and a `message.new`
round-trip, and verifies the manager's in-memory state.

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
│   ├── users/             # Phase 4: search; Phase 5: online
│   │   ├── __init__.py
│   │   └── router.py      # /users/search, /users/online
│   ├── realtime/          # Phase 5: WebSocket + in-memory manager
│   │   ├── __init__.py    # exposes the singleton `connection_manager`
│   │   ├── manager.py     # ConnectionManager (state + typing sweeper)
│   │   ├── events.py      # TypedDicts for the WS event protocol
│   │   └── router.py      # /ws WebSocket + presence/typing/reads
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
│   ├── test_conversations.py  # read-API smoke (Phase 4)
│   └── test_realtime.py       # WS + send/read/status smoke (Phase 5)
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

- Phase 6: Group messaging.
- Phase 7: Polish & Signal feel (toasts, modals, keyboard shortcuts).
- Phase 8: Bonus features (reply/quoted, reactions, disappearing, attachments).
- Phase 9: Deploy to Railway with a persistent Volume for `app.db`.
