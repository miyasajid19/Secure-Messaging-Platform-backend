# Signal Clone — Backend

FastAPI + SQLAlchemy 2.x + SQLite (WAL) backend for the Signal Messenger clone assignment. Serves a REST API for auth, conversations, contacts, and message CRUD, plus a single WebSocket endpoint for real-time messaging, presence, typing, and reactions. Single uvicorn worker (locked decision — see below).

The frontend lives at `../frontend/` (Next.js 16 App Router, Vercel-deployed). The full architecture is documented in `../PLAN.md`.

## Architecture

```
┌────────────────────┐    REST  +  WS (?token=<jwt>)    ┌────────────────────┐
│ Browser            │ ───────────────────────────────▶ │ FastAPI (uvicorn)  │
│ (Next.js on Vercel)│                                  │  + ConnectionMgr  │
│  localStorage JWT  │                                  │  + Sweeper (30s)   │
└────────────────────┘                                  └─────────┬──────────┘
                                                                  │
                                                          ┌───────▼────────┐
                                                          │ SQLite + WAL    │
                                                          │ (Render Disk    │
                                                          │  /data/app.db)  │
                                                          └────────────────┘
```

Three layers:
1. **Presentation** — FastAPI route handlers in `app/{auth,conversations,contacts,users,realtime}/router.py`.
2. **Domain / persistence** — SQLAlchemy 2.x typed-declarative models in `app/models/`. Pydantic response models in `app/schemas/`.
3. **Real-time state** — in-process singleton `ConnectionManager` in `app/realtime/manager.py` (users, typing, presence). Lost on restart; fine for the demo.

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Framework | FastAPI 0.115+ | Async; built-in WebSocket support |
| ORM | SQLAlchemy 2.x | Typed declarative (`Mapped[...]`, `mapped_column`) |
| DB | SQLite 3 (WAL mode) | Single file on a Render disk mounted at `/data` |
| Auth | PyJWT (HS256) | Stateless; expiry 7 days |
| Realtime | FastAPI WebSocket + asyncio | In-memory pub/sub by conversation |
| Python | 3.11+ | `.python-version` pinned |
| Deploy | Render | Single instance and worker; persistent disk at `/data` |

## Quick start (local dev)

```bash
cd backend

# Python 3.11+ required
python -m venv .venv
source .venv/bin/activate   # bash/zsh
# .venv\Scripts\activate.ps1   # Windows PowerShell

pip install -r requirements.txt
cp .env.example .env       # provides dev defaults (JWT_SECRET, CORS_ORIGINS, etc.)

# Seed the demo DB
python -m app.seed

# Run (single worker — DO NOT add --workers N>1)
uvicorn app.main:app --reload
# Server: http://localhost:8000  ·  Docs: http://localhost:8000/docs
```

On startup the app calls `Base.metadata.create_all` (idempotent) and runs a `[startup] DB OK (sqlite:///./app.db)` log line. `/health` returns the row counts per table.

**Verify:**

```bash
curl http://localhost:8000/health
# -> {"status":"ok","db":"reachable","counts":{"users":7,"contacts":6,"conversations":12,...}}

sqlite3 app.db "PRAGMA journal_mode;"
# -> wal
```

## Deployment (Render)

Create a Render **Web Service** from this GitHub repository and select the Docker runtime. Render builds the root `Dockerfile` and runs its `CMD` by default. Leave Render's Docker Command blank so the seed step is preserved.

1. Attach a persistent disk with mount path `/data` (available on paid web services). Set `DATABASE_URL=sqlite:////data/app.db`. Without a disk, the database file is ephemeral and will be reseeded from scratch after a restart or deploy. A disk-backed service runs one instance and has a brief interruption during deploys.
2. Keep one service instance and one Uvicorn worker. The connection manager and disappearing-message sweeper keep state in process memory. Render disks are limited to one service instance.
3. Add the production environment variables in the Render service's Environment tab. Do not copy the local `.env` file or use the development JWT secret.
4. Set the health check path to `/health`. The Dockerfile binds to `0.0.0.0` and defaults to Render's `PORT` value of `10000`.
5. Enable auto-deploy for the connected Git branch if you want each push to redeploy. After pushing, check the deploy logs and open `https://<your-service>.onrender.com/health` to confirm the database is reachable.

The Docker start command runs `python -m app.seed` before Uvicorn on every container start. The seed script checks for its demo-user sentinel and does nothing when the database is already seeded, so restarts and redeploys do not duplicate the demo data. Render pre-deploy commands run on a separate instance without the persistent disk, so the startup command is used for seeding.

### Env vars (production)

| Var | Required | Example | Notes |
|---|---|---|---|
| `DATABASE_URL` | yes | `sqlite:////data/app.db` | Absolute path to the Volume file. |
| `JWT_SECRET` | yes | `<random 64-char hex>` | Used to sign JWTs. **Never ship the dev default in prod.** |
| `JWT_ALGORITHM` | no | `HS256` | |
| `JWT_EXPIRES_MINUTES` | no | `10080` | 7 days. |
| `JWT_ISSUER` | no | `signal-clone` | `iss` claim; mismatched tokens are rejected. |
| `CORS_ORIGINS` | yes | `https://your-app.vercel.app` | Comma-separated. The Vercel domain only — don't allow `localhost` in prod. |
| `IMAGEKIT_PRIVATE_KEY` | only for attachments | `<key>` | ImageKit server-side upload key for chat attachments. Optional — composer hides the paperclip if missing. |

### Verify a live deploy

```bash
# 1. Health
curl https://your-service.onrender.com/health
# -> {"status":"ok","db":"reachable","counts":{...}}

# 2. Auth (Sajid Miya is a seeded user; OTP is always 123456)
TOKEN=$(curl -s -X POST https://your-service.onrender.com/auth/verify-otp \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+919000000001","otp":"123456"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['token'])")

# 3. Conversations
curl -s https://your-service.onrender.com/conversations \
  -H "Authorization: Bearer $TOKEN"

# 4. WS smoke: open the app in two browser windows and send a message between users
```

Open the deployed URL in two browser windows; log in as Sajid Miya and Aasif Miya (OTP `123456`); send a message from Sajid → it should appear in Aasif's window in under 1 s.

## Database schema

8 tables; all FK columns indexed; `ON DELETE CASCADE` on the dependent rows so a delete of a `conversation` or `user` cleans up cleanly.

| Table | Key columns | Notes |
|---|---|---|
| `users` | `id, phone (unique), username, display_name, avatar_url, password_hash (unused), last_seen, created_at` | Created by `request-otp` even before login (registration). |
| `contacts` | `owner_id, contact_id, nickname, added_at` | UNIQUE on `(owner_id, contact_id)`. |
| `conversations` | `id, type ('direct'\|'group'), name, disappear_after_seconds, last_message_at, created_at` | `last_message_at` indexed for the list query. |
| `conversation_participants` | `conversation_id, user_id, joined_at, role ('admin'\|'member')` | UNIQUE on `(conversation_id, user_id)`. |
| `messages` | `id, conversation_id, sender_id, content, type ('text'\|'image'\|'system'), parent_id (FK→messages, ON DELETE SET NULL), disappear_after_seconds, created_at` | `parent_id` powers Phase 8.1 reply/quoted; `disappear_after_seconds` is Phase 8.4. |
| `message_status` | `message_id, user_id, status ('sending'\|'sent'\|'delivered'\|'read'), updated_at` | UNIQUE on `(message_id, user_id)`. Composite index `(user_id, message_id)` for unread-count queries. |
| `message_reactions` | `message_id, user_id, emoji (VARCHAR 16), created_at` | UNIQUE on `(message_id, user_id, emoji)`. UTF-8 round-trip safe. |
| `attachments` | `message_id, url, mime, size_bytes` | Populated by the image upload flow. |

`Base.metadata.create_all(engine)` runs on startup and from `python -m app.seed`. Idempotent. No Alembic; not needed at this scale.

## Environment variables (full)

All settings live in `app/config.py` (`pydantic-settings.BaseSettings`) and load from the environment or `.env`.

| Var | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./app.db` | SQLAlchemy URL. In prod, `sqlite:////data/app.db` (note the 4 slashes — `//` is the URL scheme separator, `//data/...` is the absolute path). |
| `JWT_SECRET` | `change-me-in-prod` | HMAC secret. **Override in prod.** |
| `JWT_ALGORITHM` | `HS256` | |
| `JWT_EXPIRES_MINUTES` | `10080` | 7 days. |
| `JWT_ISSUER` | `signal-clone` | Tokens with a different `iss` are rejected. |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated. Each value is added to CORS `allow_origins`. |
| `IMAGEKIT_PRIVATE_KEY` | (empty) | ImageKit server-side upload key for chat attachments. Optional. |

## API reference

All routes below require `Authorization: Bearer <token>` (the JWT issued by `/auth/verify-otp`). The only public routes are `/`, `/health`, `/auth/*`, and `/ws` (which takes the token as a `?token=` query param).

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | — | Row counts per table (smoke check). |
| `POST` | `/auth/request-otp` | — | Upsert user by phone. OTP is always `123456`. |
| `POST` | `/auth/verify-otp` | — | Verify OTP, issue JWT. |
| `GET` | `/auth/me` | ✅ | Current user. |
| `PATCH` | `/auth/profile` | ✅ | Update display_name / username / avatar_url. Bumps `last_seen`. |
| `POST` | `/auth/change-phone` | ✅ | Verify the new E.164 phone with OTP and return the updated user plus a replacement JWT. |
| `POST` | `/auth/change-phone/request-otp` | ✅ | Request a phone-change OTP without creating a separate account. |
| `POST` | `/auth/upload-image` | ✅ | Upload a profile/group photo through ImageKit; returns `{url}`. |
| `POST` | `/auth/logout` | ✅ | Bump `last_seen`, force-close the user's WS, broadcast presence offline. JWT remains valid until `exp` (stateless). |
| `GET` | `/conversations` | ✅ | Current user's conversations, sorted by `last_message_at DESC`. |
| `GET` | `/conversations/{id}/messages?limit=50&before=<message_id>` | ✅ | Paginated messages, ASC. 403 if not a participant. |
| `POST` | `/conversations/{id}/messages` | ✅ | Send a message. Body `{content, type?, parent_id?}`. 201 + `MessageOut`. |
| `POST` | `/conversations/{id}/read` | ✅ | Mark messages read up to `message_id`. Returns `{marked_read}`. |
| `GET` | `/conversations/{id}/message-status?message_ids=1,2` | ✅ | Map of message id → status for the current user. |
| `GET` | `/conversations/{id}/disappearing-timer` | ✅ | Current `{disappear_after_seconds}` for the conversation. |
| `PATCH` | `/conversations/{id}/disappearing-timer` | ✅ | Set the conversation timer (`3600`/`86400`/`604800`/`null`). |
| `POST` | `/conversations` | ✅ | Create a group. Body `{type:"group", name, member_ids}`. Caller becomes admin. |
| `PATCH` | `/conversations/{id}` | ✅ | Admin-only. Update group `name` and/or `avatar_url`; broadcasts the new conversation to members. |
| `PATCH` | `/conversations/{id}/avatar` | ✅ | Admin-only. Update or clear group photo; broadcasts the change. |
| `POST` | `/conversations/{id}/members` | ✅ | Admin-only. Add user. |
| `DELETE` | `/conversations/{id}/members/{user_id}` | ✅ | Admin-only. Remove user and notify their connected clients. |
| `POST` | `/conversations/{id}/leave` | ✅ | Current member leaves the group; a group cannot be left by its last admin. |
| `PATCH` | `/conversations/{id}/members/{user_id}` | ✅ | Admin-only. Change role. |
| `DELETE` | `/conversations/{id}` | ✅ | Admin-only. Cascade-delete the group. |
| `GET` | `/contacts` | ✅ | Current user's contacts, newest first. |
| `POST` | `/contacts` | ✅ | Add a contact by phone. Auto-creates a direct conversation. |
| `GET` | `/users/search?q=<query>` | ✅ | Search users by phone/display_name/username. Optional `?conversation_id=` to set `already_member`. |
| `GET` | `/users/online` | ✅ | List of currently-connected user ids. |
| `POST` | `/messages/{id}/reactions` | ✅ | Add an emoji reaction. Body `{emoji}`. 201 + `ReactionGroup`. |
| `DELETE` | `/messages/{id}/reactions/{emoji}` | ✅ | Remove a reaction. 204 on success. |
| `WS` | `/ws?token=<jwt>` | ✅ (query) | Single bidirectional channel for `message.new`, `message.read.bulk`, `reactions.update`, `typing`, `presence`, `presence.snapshot`, `conversation.updated`, `conversation.deleted`, `message.delete`. |

## Auth (mocked OTP)

The OTP is the constant `123456`. There is no SMS or email — `/auth/request-otp` returns a `debug_otp` field so the dev frontend can autofill. **Remove or gate that field via env before deploying to a real environment.**

```bash
# 1. Request an OTP (upserts the user)
curl -s -X POST http://localhost:8000/auth/request-otp \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+15550000099"}'
# -> {"sent":true,"debug_otp":"123456"}

# 2. Verify — issues a JWT
TOKEN=$(curl -s -X POST http://localhost:8000/auth/verify-otp \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+15550000099","otp":"123456"}' \
  | python -c 'import sys,json; print(json.load(sys.stdin)["token"])')

# 3. /auth/me with the token
curl -s http://localhost:8000/auth/me -H "Authorization: Bearer $TOKEN"

# 4. /auth/me without a token — expect 401
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/auth/me
# -> 401

# 5. PATCH profile
curl -s -X PATCH http://localhost:8000/auth/profile \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"Test User","username":"testuser99"}'

# 6. Logout (server-side cleanup; token remains valid until exp)
curl -s -X POST http://localhost:8000/auth/logout \
  -H "Authorization: Bearer $TOKEN"
# -> {"logged_out":true}
```

The JWT payload: `{sub: user_id, phone, iat, exp, iss:"signal-clone"}`, HS256-signed.

**JWT caveat (locked decision):** tokens are stateless — `/auth/logout` doesn't invalidate them, just bumps `last_seen` and force-closes the WS. The frontend must drop the token from `localStorage` on logout. For token revocation, add a `token_version` column on `users` and check it in `get_current_user` — out of scope for this milestone.

## Realtime (WebSocket)

`WS /ws?token=<jwt>` — auth via query param. The HTTP contract still uses `Authorization: Bearer`. They are parallel mechanisms, not interchangeable.

### Client → Server

```json
{ "type": "typing.start",  "conversation_id": 1 }
{ "type": "typing.stop",   "conversation_id": 1 }
{ "type": "message.read",  "conversation_id": 1, "message_id": 42 }
```

### Server → Client

| event | payload |
|---|---|
| `presence.snapshot` | `{ type, online_user_ids: [int, ...] }` — sent once on connect |
| `presence` | `{ type, user_id, online: bool }` — broadcast to others on join/leave |
| `typing` | `{ type, conversation_id, user_id, state: "start" \| "stop" }` |
| `message.new` | `{ type, message: MessageOut }` — broadcast to conversation participants |
| `message.read.bulk` | `{ type, conversation_id, reader_id, up_to_message_id }` |
| `reactions.update` | `{ type, conversation_id, message_id, reactions: ReactionGroup[] }` — full replacement list |
| `conversation.updated` | `{ type, conversation: ConversationOut }` |
| `conversation.deleted` | `{ type, conversation_id }` |
| `message.delete` | `{ type, conversation_id, message_id }` — fired by the disappearing-message sweep |

`typing` automatically broadcasts `state: "stop"` after 6 s of silence (the manager's sweeper).

## Read API

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/verify-otp \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+919000000001","otp":"123456"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['token'])")
H="Authorization: Bearer $TOKEN"

# Conversations
curl -s http://localhost:8000/conversations -H "$H" | python -m json.tool

# Messages
curl -s "http://localhost:8000/conversations/1/messages?limit=5" -H "$H" | python -m json.tool

# Contacts
curl -s http://localhost:8000/contacts -H "$H" | python -m json.tool

# Search users
curl -s "http://localhost:8000/users/search?q=ali" -H "$H" | python -m json.tool
```

`MessageOut` carries three Phase-8 fields:
- `seen_by: list[UserOut]` — readers (ASC by when they read). Batched: one JOIN for the page, not N+1.
- `parent: MessageParent | None` — Phase 8.1 reply preview. `content` truncated to 120 chars; image-type → `"📷 Photo"`.
- `reactions: list[ReactionGroup]` — Phase 8.2. `users` is capped at 10 per group; `count` is always the full total.

## Disappearing messages (Phase 8.4)

Per-conversation timer that copies onto each new message at send time. Allowed values: `3600` (1h), `86400` (24h), `604800` (1w), `null` (off). `1` second is also accepted for smoke tests. A 30-second sweep hard-deletes expired rows and broadcasts `message.delete` to each participant.

```bash
# Enable one-hour
curl -s -X PATCH http://localhost:8000/conversations/1/disappearing-timer \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"disappear_after_seconds":3600}'

# Disable
curl -s -X PATCH http://localhost:8000/conversations/1/disappearing-timer \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"disappear_after_seconds":null}'
```

## Reactions (Phase 8.2)

`POST /messages/{id}/reactions` (body `{emoji}`) → 201 + `ReactionGroup`. `DELETE /messages/{id}/reactions/{emoji}` → 204. A `reactions.update` WS event fires on every change with the full new list. Status codes: 201 / 204 / 400 (empty/oversize emoji) / 404 (message or non-participant) / 409 (already reacted — treat as no-op).

**URL contract:**

| method | path | body | result |
|---|---|---|---|
| `POST` | `/messages/{id}/reactions` | `{"emoji":…}` | 201 / 400 / 404 / 409 |
| `POST` | `/messages/{id}/reactions/{emoji}` | — | 405 by design |
| `DELETE` | `/messages/{id}/reactions/{emoji}` | — | 204 / 404 |
| `GET` | `/messages/{id}/reactions` | — | 405 by design |

Emoji storage is UTF-8 round-trip safe. `sqlite3 app.db "SELECT hex(emoji) FROM message_reactions"` on a posted 👍 shows `F09F918D`, not `3F3F`.

## Groups

`POST /conversations` with `type:"group"` creates a group; the caller becomes the first admin. The caller's id is **silently deduped** from `member_ids` if present (UI flows naturally include the caller when picking members). Duplicate ids are dropped. Only ids that don't correspond to a real user still trigger a 403.

Member management is admin-only, except that any member may leave their own group membership:
- `POST /conversations/{id}/members` — add
- `DELETE /conversations/{id}/members/{uid}` — remove (400 if last admin leaving)
- `POST /conversations/{id}/leave` — leave as the authenticated member (400 if the caller is the last admin)
- `PATCH /conversations/{id}/members/{uid}` — change role
- `DELETE /conversations/{id}` — delete the group (cascades)

All four broadcast `message.new` (with a system message documenting the change) and `conversation.updated`. Delete broadcasts `conversation.deleted`.

## Chat attachments (Phase 8.5)

Set `IMAGEKIT_PRIVATE_KEY` in the backend `.env` to enable the paperclip in the composer. The upload endpoint is `POST /auth/upload-image`. Files go through the authenticated backend; the private key is never sent to the browser. Images appear inline in the chat; other file types become downloadable attachment rows.

## Project layout

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, /health, create_all, lifespan sweepers
│   ├── config.py            # pydantic-settings BaseSettings
│   ├── database.py          # SQLAlchemy engine, WAL pragmas, get_db
│   ├── seed.py              # python -m app.seed (idempotent)
│   ├── auth/                # mocked OTP + JWT
│   │   ├── jwt.py           # encode/decode (HS256, iss, exp)
│   │   ├── otp.py           # constant 123456
│   │   ├── schemas.py       # RequestOtp, VerifyOtp, ProfileUpdate, UserOut, Logout
│   │   ├── deps.py          # get_current_user
│   │   └── router.py        # /auth/* including /logout + /upload-image
│   ├── schemas/             # Pydantic response models
│   │   └── __init__.py      # ConversationOut, MessageOut, MessageParent, ReactionGroup, …
│   ├── conversations/       # list + timeline + send + reactions + group CRUD
│   │   ├── router.py        # /conversations, /conversations/{id}/messages, /reactions, etc.
│   │   └── service.py       # last_message, unread_count, avatar_url, parent preview
│   ├── contacts/            # /contacts (GET, POST)
│   ├── users/               # /users/search, /users/online
│   └── realtime/            # WS + in-memory manager + periodic sweepers
│       ├── manager.py       # ConnectionManager singleton
│       ├── events.py        # TypedDicts for the WS protocol
│       └── router.py        # /ws WebSocket
├── .env.example
├── requirements.txt
└── README.md
```

## Schema management

`Base.metadata.create_all` runs on app startup and from the seed script. It's idempotent — tables that exist are left alone, missing ones are created. We deliberately do **not** use Alembic for this demo:

- The schema is small and only changes in clearly-bounded phases.
- `create_all` keeps dev and prod in lockstep while we're the only writer to the DB.
- When the schema stops being trivially mutable, revisit.

To wipe state: `rm app.db app.db-wal app.db-shm && python -m app.seed`.

## Known limitations

These are intentional, locked decisions for this assignment's scope:

- **Single uvicorn worker.** The connection manager and the disappearing-message sweep are in-memory process state. Running `--workers N>1` would split them across processes and break presence/typing and sweep delivery.
- **SQLite single-writer.** Sufficient for the demo; horizontal scaling would need Postgres.
- **JWT is stateless.** `/auth/logout` doesn't invalidate the token (no `token_version` column); the frontend drops the token on logout to close the loop.
- **Mocked OTP.** No SMS/email; OTP is always `123456`. The `debug_otp` field in `/auth/request-otp` responses exists to let the dev frontend autofill — gate or strip in real deploys.
- **No WebSocket heartbeat.** A connection that drops without a clean close frame leaves the user marked "online" until they reconnect. The 6-second typing TTL partially mitigates. Acceptable for the demo.
- **In-memory typing / presence state.** Resets on every server restart.
- **WS auth via `?token=` query param.** Cookies don't survive cross-origin WS upgrades reliably; the locked pattern is `?token=<jwt>`. The HTTP REST contract uses `Authorization: Bearer` — they're parallel, not interchangeable.
- **No message-level features beyond Phase 8.** Reply, reactions, disappearing, and attachments are wired; quoted-reply tree depth, message edit/delete, voice/video calls, and E2E encryption are not.

## Locked decisions (full reference)

These are the decisions in `../PLAN.md` that constrain backend choices; flagged here so future contributors don't relitigate:

1. Backend framework: **FastAPI**
2. Auth storage: JWT in localStorage (frontend); HS256 (backend)
3. State management (frontend): Zustand (UI) + TanStack Query (server)
4. Styling (frontend): Tailwind v4 + `tokens.css`
5. Bonus features: all 5 shipped (reply, reactions, dark mode, disappearing, attachments)
6. Database: **SQLite everywhere** with WAL mode + single uvicorn worker + Render disk in prod
7. Hosting: **Render** (backend) + **Vercel** (frontend)
8. Visual theme (v1): **Light theme only** (dark mode deferred — frontend)
