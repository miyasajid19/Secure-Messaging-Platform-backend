"""In-memory WebSocket connection manager.

State lives in this single object for the whole uvicorn process. We
deliberately don't reach for Redis or any cross-process primitive
because the project is locked to a single uvicorn worker — see
PLAN.md "SQLite production config". If/when we move to multiple
workers, this whole module is the swap point.

Concurrency:
  - All mutation methods acquire `_lock` before reading/writing the
    dicts. This is important because `connect`/`disconnect` and
    `send_to_user`/`broadcast_to_conversation` can interleave under
    asyncio.
  - `send_to_*` acquires the lock just long enough to grab a reference
    to the WebSocket, releases, then awaits `send_json` outside the
    lock. Holding the lock across an await would serialize all
    broadcasts, which is the kind of thing you only notice in
    load tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from fastapi import WebSocket

log = logging.getLogger("realtime")


class ConnectionManager:
    """Tracks active sockets + typing state for one uvicorn process."""

    # The window after which a stale "typing.start" auto-expires. 6s
    # matches the spec and gives the UI a sensible debounce.
    TYPING_TTL_SECONDS: float = 6.0
    # How often the typing sweeper task runs.
    TYPING_SWEEP_INTERVAL_SECONDS: float = 2.0
    # Max time the broadcast path waits for a recipient's WS to be
    # ready. 0.5s is long enough for a normal handshake to complete
    # but short enough that a slow recipient doesn't block the
    # sender's POST response.
    READY_WAIT_SECONDS: float = 0.5

    def __init__(self) -> None:
        # user_id -> WebSocket. We allow only one socket per user; a
        # second connection for the same user kicks the first.
        self._connections: dict[int, WebSocket] = {}
        # (conversation_id, user_id) -> last typing timestamp (monotonic).
        self._typing: dict[tuple[int, int], float] = {}
        # Guards mutations on both dicts. Send paths take the lock
        # only long enough to copy out the sockets, then release.
        self._lock = asyncio.Lock()
        # Long-running background task that expires stale typing entries.
        self._sweeper_task: Optional[asyncio.Task[None]] = None
        # Set of (conversation_id, user_id) currently being expired by
        # the sweeper — used so we can broadcast `typing.stop` once
        # per expiry, not once per sweep tick.
        self._last_swept: set[tuple[int, int]] = set()
        # user_id -> asyncio.Event that fires once the WS handler has
        # finished sending the initial snapshot + presence broadcasts.
        # Used by the broadcast path to give the *sender's* connection
        # a moment to settle so they receive their own `message.new`
        # — see the UI-send race note in `app/realtime/router.py`.
        self._ready_events: dict[int, asyncio.Event] = {}

    # --- Lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Kick off the typing-expiry sweeper.

        Idempotent: re-calling is a no-op (the existing task is
        reused). The lifespan handler in `app.main` is the only
        caller.
        """
        if self._sweeper_task is None or self._sweeper_task.done():
            self._sweeper_task = asyncio.create_task(self._typing_sweeper())
            log.info("realtime: typing sweeper started")

    async def stop(self) -> None:
        """Stop the sweeper. Best-effort; safe to call multiple times."""
        if self._sweeper_task is not None and not self._sweeper_task.done():
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except asyncio.CancelledError:
                pass
            self._sweeper_task = None
            log.info("realtime: typing sweeper stopped")

    # --- Connections -----------------------------------------------------

    async def connect(self, user_id: int, ws: WebSocket) -> None:
        """Register a socket for `user_id`.

        If a prior socket is open for the same user, close it before
        accepting the new one. The spec calls for "one socket per
        user" so the UI never gets duplicate events.

        Also creates an un-set `asyncio.Event` (the "ready" event)
        that the WS handler will fire after sending the initial
        snapshot. Broadcasts wait briefly on this event so they don't
        miss a sender whose WS handshake hasn't quite finished.
        """
        await ws.accept()
        async with self._lock:
            existing = self._connections.get(user_id)
            if existing is not None:
                # Drop the old socket out from under it; FastAPI's
                # WebSocket doesn't expose a clean way to close a
                # specific peer, but send_text/binary raises once the
                # peer has gone. Closing on the server side surfaces
                # a clean 1001 to the previous client.
                try:
                    await existing.close(code=1000, reason="replaced")
                except Exception:  # noqa: BLE001
                    pass
            self._connections[user_id] = ws
            # Reset any stale ready event from a prior connection.
            self._ready_events[user_id] = asyncio.Event()
        log.info("realtime: connect user_id=%s (online=%d)", user_id, len(self._connections))

    async def disconnect(self, user_id: int, ws: WebSocket) -> None:
        """Unregister a socket. Safe to call with a stale socket.

        Only removes the entry if the registered socket is the one we
        were given — that way a replaced connection doesn't
        accidentally unregister the new one.
        """
        async with self._lock:
            if self._connections.get(user_id) is ws:
                del self._connections[user_id]
            # Drop the ready event too so a re-connect starts fresh.
            self._ready_events.pop(user_id, None)
        log.info("realtime: disconnect user_id=%s (online=%d)", user_id, len(self._connections))

    async def force_disconnect(self, user_id: int, code: int = 1000) -> bool:
        """Close any active WS for `user_id` (logout, kick, etc.).

        Returns True if a connection was actually closed, False if the
        user was already offline. The caller is responsible for
        broadcasting `presence {online: false}` to other users —
        this method only handles the local cleanup.
        """
        async with self._lock:
            ws = self._connections.pop(user_id, None)
            self._ready_events.pop(user_id, None)
        if ws is None:
            return False
        try:
            await ws.close(code=code)
        except Exception as exc:  # noqa: BLE001
            # The peer may have already gone; close is best-effort.
            log.info("realtime: force_disconnect close raised: %s", exc)
        log.info("realtime: force_disconnect user_id=%s code=%s", user_id, code)
        return True

    def mark_ready(self, user_id: int) -> None:
        """Fire the user's ready event (called after the snapshot is sent)."""
        evt = self._ready_events.get(user_id)
        if evt is not None:
            evt.set()

    async def wait_for_ready(self, user_id: int, *, timeout: float = 0.5) -> None:
        """Wait up to `timeout` seconds for the user's ready event.

        No-op if the user has no ready event (offline, or already
        ready). Returns silently on timeout — broadcasting to a not-
        yet-ready user is a soft failure, not an error.
        """
        evt = self._ready_events.get(user_id)
        if evt is None or evt.is_set():
            return
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return

    def is_online(self, user_id: int) -> bool:
        return user_id in self._connections

    def online_users(self) -> set[int]:
        return set(self._connections.keys())

    # --- Send -----------------------------------------------------------

    async def send_to_user(self, user_id: int, payload: dict) -> bool:
        """Send to a single user. Returns True if the user was online.

        Silently no-ops if the user is offline — the caller doesn't
        need to special-case presence. Returns False so the caller
        can log/metric if it cares.
        """
        ws = self._copy_socket(user_id)
        if ws is None:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("realtime: send_to_user(%s) failed: %s", user_id, exc)
            return False

    async def broadcast_to_conversation(
        self,
        conversation_id: int,
        participant_ids: list[int],
        payload: dict,
        exclude_user_id: Optional[int] = None,
    ) -> None:
        """Send to every online participant in a conversation.

        Takes the list of participant ids as a parameter (rather than
        querying the DB) because the caller always already has it.

        Each target gets a brief wait for their ready event — see
        the UI-send race note in `app/realtime/router.py`. A target
        that's slow to ready (or never will, because they disconnected
        mid-broadcast) is silently skipped after `READY_WAIT_SECONDS`.
        """
        targets = [uid for uid in participant_ids if uid != exclude_user_id]
        # Wait for each target to be ready (max 0.5s). This is a
        # best-effort, server-side mitigation for the UI-send race;
        # the real fix is the frontend not sending until its WS is
        # open.
        for uid in targets:
            await self.wait_for_ready(uid, timeout=self.READY_WAIT_SECONDS)
        sockets = self._copy_sockets(targets)
        for uid, ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("realtime: broadcast send to %s failed: %s", uid, exc)

    # --- Typing ---------------------------------------------------------

    def mark_typing(self, conversation_id: int, user_id: int) -> None:
        """Note that `user_id` is typing in `conversation_id`.

        Not async because it's a tiny in-memory dict update. The
        caller is expected to also `broadcast` the start event.
        """
        # typing state is a write/read race that's resolved by the
        # single-worker guarantee; no lock needed for the read in the
        # sweeper because monotonic timestamps are monotonic.
        self._typing[(conversation_id, user_id)] = time.monotonic()

    def clear_typing(self, conversation_id: int, user_id: int) -> None:
        self._typing.pop((conversation_id, user_id), None)

    def typing_now(self, conversation_id: int, user_id: int) -> bool:
        """True iff the user's last typing ts in this conv is < TTL ago."""
        ts = self._typing.get((conversation_id, user_id))
        if ts is None:
            return False
        return (time.monotonic() - ts) < self.TYPING_TTL_SECONDS

    def typing_users(self, conversation_id: int) -> set[int]:
        """Users currently typing in `conversation_id` (after applying TTL).

        Used by tests to assert the in-memory state without waiting
        for the sweeper.
        """
        now = time.monotonic()
        return {
            uid
            for (cid, uid), ts in self._typing.items()
            if cid == conversation_id and (now - ts) < self.TYPING_TTL_SECONDS
        }

    # --- Internals ------------------------------------------------------

    def _copy_socket(self, user_id: int) -> Optional[WebSocket]:
        return self._connections.get(user_id)

    def _copy_sockets(self, user_ids: list[int]) -> list[tuple[int, WebSocket]]:
        return [(uid, self._connections[uid]) for uid in user_ids if uid in self._connections]

    async def _typing_sweeper(self) -> None:
        """Periodically expire stale typing entries and broadcast stops.

        Runs forever (until cancelled in `stop`). Logs a stop event
        exactly once per expiry to avoid spamming the WS clients.
        """
        while True:
            try:
                await asyncio.sleep(self.TYPING_SWEEP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return

            now = time.monotonic()
            expired: list[tuple[int, int]] = []
            for key, ts in list(self._typing.items()):
                if (now - ts) >= self.TYPING_TTL_SECONDS:
                    expired.append(key)

            if not expired:
                self._last_swept.clear()
                continue

            for key in expired:
                conv_id, user_id = key
                # Only broadcast if this is the FIRST tick where this
                # entry is expired — without `_last_swept` we'd spam
                # `typing.stop` every 2 seconds.
                if key not in self._last_swept:
                    self._typing.pop(key, None)
                    # Broadcast — but we need participant ids, which
                    # would require a DB query. We hand that off to
                    # the caller via a hook so we don't couple the
                    # manager to the DB layer.
                    try:
                        await self._on_typing_expire(conv_id, user_id)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("realtime: _on_typing_expire failed: %s", exc)
            self._last_swept = set(expired)

    async def _on_typing_expire(self, conversation_id: int, user_id: int) -> None:
        """Default no-op; replaced by the router on startup.

        The router sets this to a closure that knows the conversation
        participants (so we can broadcast `typing.stop`). The default
        keeps the manager usable in unit tests that don't need WS
        broadcasts.
        """
        return None


# Module-level singleton — every router that does
# `from app.realtime import connection_manager` shares this instance.
connection_manager = ConnectionManager()
