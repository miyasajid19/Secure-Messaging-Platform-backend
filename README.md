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

### Logout

```bash
curl -X POST http://localhost:8000/auth/logout \
  -H "Authorization: Bearer $TOKEN"
# -> {"logged_out":true}
```

Server-side effect: bumps `users.last_seen`, force-closes the user's
WebSocket (so other tabs go offline), and broadcasts
`presence {online: false}` to other connected users.

**Caveat:** the JWT is stateless, so it remains valid until its `exp`
even after logout. The endpoint cleans up server-side state (WS
connection, presence broadcast, `last_seen`) but does not invalidate
the token. The frontend must drop the token from `localStorage` on
logout so subsequent requests re-authenticate. To revoke individual
tokens you'd need a `token_version` column and a check in
`get_current_user` — out of scope for this milestone.

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

Each `MessageOut` carries a `seen_by: list[UserOut]` field — the users
who have marked that message as 'read', ordered ASC by when they read
it (first reader at index 0, matches Messenger/WhatsApp). Empty `[]`
for a freshly-sent message. The field is batch-loaded: one JOIN query
across all messages in the response, not N+1.

Each `MessageOut` also carries a `parent: MessageParent | None` field
(Phase 8.1). For a reply, this is a preview of the quoted message
(`{id, sender_id, sender_name, content, type}`); for a non-reply it's
`null`. The `content` is truncated to 120 chars (`+ "…"` if cut);
for image-type parents it's the sentinel `"📷 Photo"` so the
frontend can render a photo icon without trying to fit a URL in the
quote bubble.

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
| `message.delivered`  | `{ type, conversation_id, message_id, delivered_to }` — sent when a recipient gets the message |
| `message.read`       | `{ type, message_id, read_by, read_at }` (single message via WS)              |
| `message.read.bulk`  | `{ type, conversation_id, reader_id, up_to_message_id }` (via REST `/read`)   |

`typing` is automatically broadcast as `state: "stop"` after 6s of
silence (the manager's sweeper task).

### Realtime REST endpoints

| Method | Path                                                       | Notes |
|--------|------------------------------------------------------------|-------|
| `POST` | `/conversations/{id}/messages`                              | Send. Body `{content, type?, parent_id?}`. 201 + `MessageOut`. `parent_id` is Phase 8.1: must reference a message in the same conversation; 400 otherwise. The response (and the `message.new` WS payload) carries a `parent: MessageParent` preview for replies. |
| `POST` | `/conversations/{id}/read`                                  | Mark read. Body `{message_id}`. Returns `{marked_read}`. |
| `GET`  | `/conversations/{id}/message-status?message_ids=1,2,3`      | `{message_id: status}` map. |
| `GET`  | `/users/online`                                            | `[user_id, ...]` of currently connected users. |
| `POST` | `/messages/{id}/reactions`                                 | Add a reaction. Body `{emoji}`. 201 + `ReactionGroup`. 400 empty/oversize, 404 missing/non-participant, 409 already-reacted. Phase 8.2. |
| `DELETE` | `/messages/{id}/reactions/{emoji}`                        | Remove a reaction. 204 on success, 404 otherwise. |
| `PATCH` | `/conversations/{id}/disappearing-timer`                   | Set `{ "disappear_after_seconds": 3600 }`; `null` disables. |
| `GET` | `/conversations/{id}/disappearing-timer`                    | Read the current conversation timer. |

### Message delivery indicators

`GET /conversations/{id}/message-status?message_ids=...` returns the
status for the current user's incoming messages and an aggregate status
for messages they sent. Map the status values to the chat indicators:

- `sent`: one tick; the recipient is offline.
- `delivered`: double ticks; the recipient is online but has not read it.
- `read`: hollow seen indicator; the recipient has read the message.

When an online recipient gets a new message, or a recipient reconnects,
pending `sent` rows become `delivered` and the server emits
`message.delivered` to the conversation. In group chats,
the sender's aggregate advances only when every recipient reaches the
next state (for example, `read` means everyone has read it).

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

## Disappearing messages (Phase 8.4)

Set a conversation-wide timer for future messages. Allowed values are
`3600` (1 hour), `86400` (1 day), `604800` (1 week), and `null` to
disable it. `1` second is also accepted for demo and smoke-test use.
The timer value is copied onto each new message when sent, so changing
the conversation setting does not change existing messages. Expired
messages are hard-deleted by a process-local sweep every 30 seconds,
then each connected participant receives a `message.delete` event.

```bash
# Enable one-hour disappearing messages
curl -s -X PATCH http://localhost:8000/conversations/1/disappearing-timer \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"disappear_after_seconds":3600}'
# -> {"disappear_after_seconds":3600}

# Check the current setting
curl -s http://localhost:8000/conversations/1/disappearing-timer \
  -H "Authorization: Bearer $TOKEN"

# Disable the timer
curl -s -X PATCH http://localhost:8000/conversations/1/disappearing-timer \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"disappear_after_seconds":null}'
```

The sweeper and WebSocket connection state are in memory, so run a
single Uvicorn worker. Multiple workers would split sweeps and live
connections across processes.

## Reactions (Phase 8.2)

Users can react to a message with an emoji. Each `MessageOut` carries
a `reactions: list[ReactionGroup]` field, where each group is
`{emoji, count, users: [ReactionUser, ...]}` (`users` is capped at
10 — the `count` is always the full total so the UI can show "+N").
The `reactions` field is batch-loaded: one JOIN query across all
messages in the page, not N+1.

```bash
# Add a 👍 reaction to message 42
curl -s -X POST http://localhost:8000/messages/42/reactions \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"emoji":"👍"}'
# -> {"emoji":"👍","count":1,"users":[{"id":1,"display_name":"Alice Chen","avatar_url":"..."}]}

# Remove it
curl -s -X DELETE http://localhost:8000/messages/42/reactions/%F0%9F%91%8D \
  -H "Authorization: Bearer $TOKEN" \
  -o /dev/null -w '%{http_code}\n'
# -> 204
```

Status codes:
- 201 with `ReactionGroup` on first add
- 409 if you've already reacted with that emoji (idempotent for the UI)
- 404 if the message doesn't exist or you're not a participant
- 400 if the emoji is empty or > 16 chars

**URL contract (Phase 8.2 405-fix guard):** the emoji is in the
**body** for POST, in the **path** for DELETE. Putting the emoji in
the path on POST (or sending GET to the reactions path) returns 405
by design.

| method | path                              | body         | result   |
|--------|-----------------------------------|--------------|----------|
| POST   | `/messages/{id}/reactions`        | `{"emoji":..}`| 201 / 400 / 404 / 409 |
| POST   | `/messages/{id}/reactions/{emoji}`| —            | 405 by design |
| DELETE | `/messages/{id}/reactions/{emoji}`| —            | 204 / 404 |
| GET    | `/messages/{id}/reactions`        | —            | 405 by design |

A `reactions.update` WS event fires on every add/remove with the full
new list of reaction groups for that message — replace the local
cache, don't try to diff.

Emoji storage is UTF-8 round-trip safe (the `message_reactions.emoji`
column is plain `VARCHAR(16)`; `sqlite3 ... "SELECT hex(emoji)"` on a
posted 👍 shows `F09F918D`, not `3F3F`).

```json
{
  "type": "reactions.update",
  "conversation_id": 1,
  "message_id": 42,
  "reactions": [
    {"emoji": "👍", "count": 2, "users": [{"id": 1, "display_name": "Alice", "avatar_url": "..."}, {"id": 2, "display_name": "Bob", "avatar_url": "..."}]},
    {"emoji": "❤️", "count": 1, "users": [{"id": 3, "display_name": "Carol", "avatar_url": "..."}]}
  ]
}
```

## Groups

Group conversation CRUD lives under `/conversations/...` (no separate
prefix). The caller of `POST /conversations` becomes the first admin.
All member-management endpoints are admin-only; the spec table is in
`task.md` §7.

### Create a group

```bash
curl -s -X POST http://localhost:8000/conversations \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"type":"group","name":"Project Phoenix","member_ids":[2,3,4]}' \
  | python -m json.tool
```

Response is a `ConversationOut` (201) with 3 participants, `my_role: "admin"`,
`members_can_be_added: true`, and a system message already in
`last_message` so the group shows up at the top of the list.

The caller's id is **silently deduped** from `member_ids` if present
(UI flows naturally include the caller when picking members), and
duplicate ids in the list are dropped. So `member_ids: [1, 2, 2, 3]`
from user 1 is equivalent to `member_ids: [2, 3]` and produces a
3-person group. Only ids that don't correspond to a real user still
trigger a 403.

A `message.new` event with `type: "system"` is broadcast to every
member so their UIs pick up the new group without a refetch.

### Add / remove / promote members

```bash
# Add Dan (id=4) to conversation 8
curl -s -X POST http://localhost:8000/conversations/8/members \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id": 4}'

# Promote Bob (id=2) to admin
curl -s -X PATCH http://localhost:8000/conversations/8/members/2 \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"role": "admin"}'

# Remove Carol (id=3) from the group
curl -s -X DELETE http://localhost:8000/conversations/8/members/3 \
  -H "Authorization: Bearer $TOKEN" \
  -o /dev/null -w '%{http_code}\n'
# -> 204
```

All three broadcast `message.new` (with a system message documenting
the change) and `conversation.updated` so other tabs refresh their
participant lists.

### Delete a group

```bash
curl -s -X DELETE http://localhost:8000/conversations/8 \
  -H "Authorization: Bearer $TOKEN" \
  -o /dev/null -w '%{http_code}\n'
# -> 204
```

Admin only. Cascade via FK. Broadcasts `conversation.deleted` to all
participants so their UIs remove the row from the list.

### Updated event types

| event                 | payload                                                                                  |
|-----------------------|------------------------------------------------------------------------------------------|
| `message.new`         | now includes system messages (type=system) for create / add / remove / promote            |
| `conversation.updated`| `{type, conversation: ConversationOut}` — broadcast on every member-list change          |
| `conversation.deleted`| `{type, conversation_id}` — broadcast on group delete                                    |

### Admin rules

- `POST /conversations/{id}/members` — caller must be admin
- `DELETE /conversations/{id}/members/{uid}` — caller must be admin; **400** if it's the last admin leaving
- `PATCH /conversations/{id}/members/{uid}` — caller must be admin
- `DELETE /conversations/{id}` — caller must be admin

### User search with conversation scope

```bash
curl -s "http://localhost:8000/users/search?q=dan&conversation_id=8" \
  -H "Authorization: Bearer $TOKEN"
```

`already_member` is `True` for users already in conversation 8. Without
`conversation_id`, `already_member` is always `False`.

### Smoke test

```bash
.venv/bin/python tests/test_groups.py
```

35+ assertions covering create/add/remove/promote/delete, admin
enforcement, last-admin rule, group message broadcast to 3 clients,
and the `already_member` flag on `/users/search`.

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
│   │   └── router.py      # /auth/request-otp, /verify-otp, /me, /profile, /logout
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
│   ├── test_realtime.py       # WS + send/read/status smoke (Phase 5)
│   └── test_groups.py         # group CRUD + admin rules smoke (Phase 6)
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

- Phase 7: Polish & Signal feel (toasts, modals, keyboard shortcuts).
- Phase 8: Bonus features (reply/quoted, reactions, disappearing, attachments).
- Phase 9: Deploy to Railway with a persistent Volume for `app.db`.
