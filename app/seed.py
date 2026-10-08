"""Idempotent seed script.

Run from the project root with `python -m app.seed` (or directly via
`python -m app.seed` from `backend/`). Re-running is a no-op: we look up
existing rows by deterministic keys (phone, conversation shape, content)
and skip work that's already done.

Layout of the seeded DB:
  - 5 users (Alice, Bob, Carol, Dan, Eve)
  - 3 groups (Project Phoenix 3, Family Group 4, Squad Goals 3)
  - 4 direct conversations (one per representative pair)
  - 7 conversations total
  - 30+ messages spread over the last 7 days, including:
      * 1 reply (parent_id set)
      * 1 image-type message with a corresponding `attachments` row
      * 1 reaction on an existing message
  - `message_status` rows for every (message, non-sender participant) —
    recent ones are 'read', older ones are 'delivered'.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import (
    Attachment,
    Contact,
    Conversation,
    ConversationParticipant,
    ConversationType,
    Message,
    MessageReaction,
    MessageStatus,
    MessageStatusState,
    MessageType,
    ParticipantRole,
    User,
)

# --- Helpers ---------------------------------------------------------------

NOW = datetime.now(timezone.utc)


def _ago(days: float) -> datetime:
    """Return NOW - days, tz-aware UTC.

    Used to spread seeded messages across the last week so the
    conversation list ordering is visible from day one.
    """
    return NOW - timedelta(days=days)


def _get_or_create_user(
    db: Session, phone: str, *, username: str | None = None, display_name: str, avatar_url: str
) -> User:
    """Find a user by phone, creating one with the given profile if absent.

    Phone is the auth identifier, so it's the only field we key on; the
    rest can drift (display name corrections, etc.) without breaking
    idempotency.
    """
    user = db.query(User).filter(User.phone == phone).one_or_none()
    if user is not None:
        return user
    user = User(
        phone=phone,
        username=username,
        display_name=display_name,
        avatar_url=avatar_url,
    )
    db.add(user)
    db.flush()
    return user


def _get_or_create_direct(db: Session, user_a_id: int, user_b_id: int) -> Conversation:
    """Find or create the unique direct conversation between two users.

    "Unique" here means: a 1:1 conversation of type=direct that has
    exactly these two participants. We deliberately don't key on
    `conversations.id` — we key on the participant shape, so a re-seed
    after a partial wipe still finds the surviving conversation row.
    """
    pair = sorted([user_a_id, user_b_id])
    rows = (
        db.query(Conversation)
        .join(ConversationParticipant, ConversationParticipant.conversation_id == Conversation.id)
        .filter(Conversation.type == ConversationType.DIRECT)
        .group_by(Conversation.id)
        .having(func.count(ConversationParticipant.user_id) == 2)
        .all()
    )
    for conv in rows:
        member_ids = sorted(
            p.user_id
            for p in db.query(ConversationParticipant)
            .filter(ConversationParticipant.conversation_id == conv.id)
            .all()
        )
        if member_ids == pair:
            return conv

    conv = Conversation(type=ConversationType.DIRECT)
    db.add(conv)
    db.flush()
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=pair[0]))
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=pair[1]))
    db.flush()
    return conv


def _get_or_create_group(db: Session, name: str, user_ids: list[int]) -> Conversation:
    """Find or create a group conversation by name.

    Membership is re-synced on creation only — if a re-seed changes the
    member list for a group, the change is reflected on the next wipe +
    re-seed, not on a plain re-run.
    """
    conv = (
        db.query(Conversation)
        .filter(Conversation.name == name, Conversation.type == ConversationType.GROUP)
        .one_or_none()
    )
    if conv is not None:
        return conv

    conv = Conversation(type=ConversationType.GROUP, name=name)
    db.add(conv)
    db.flush()
    for idx, uid in enumerate(user_ids):
        role = ParticipantRole.ADMIN if idx == 0 else ParticipantRole.MEMBER
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=uid, role=role))
    db.flush()
    return conv


def _get_or_create_participant(
    db: Session, conversation_id: int, user_id: int, role: ParticipantRole = ParticipantRole.MEMBER
) -> ConversationParticipant:
    cp = (
        db.query(ConversationParticipant)
        .filter(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == user_id,
        )
        .one_or_none()
    )
    if cp is not None:
        return cp
    cp = ConversationParticipant(
        conversation_id=conversation_id, user_id=user_id, role=role
    )
    db.add(cp)
    db.flush()
    return cp


def _conversation_has_messages(db: Session, conversation_id: int) -> bool:
    """Skip-if-populated guard.

    We can't dedupe individual messages by content alone (a chatty
    conversation can repeat 'lol' multiple times). Instead, the seed is
    designed to drop at most N messages per conversation; if a
    conversation already has >= N messages, assume it was seeded and
    move on. Cheap and unambiguous.
    """
    return (
        db.query(func.count(Message.id))
        .filter(Message.conversation_id == conversation_id)
        .scalar()
        or 0
    ) > 0


def _add_message(
    db: Session,
    *,
    conversation_id: int,
    sender_id: int,
    content: str,
    created_at: datetime,
    type: MessageType = MessageType.TEXT,
    parent_id: int | None = None,
    disappear_after_seconds: int | None = None,
) -> Message:
    msg = Message(
        conversation_id=conversation_id,
        sender_id=sender_id,
        content=content,
        type=type,
        parent_id=parent_id,
        created_at=created_at,
        disappear_after_seconds=disappear_after_seconds,
    )
    db.add(msg)
    db.flush()
    return msg


def _add_status(
    db: Session, *, message_id: int, user_id: int, state: MessageStatusState
) -> None:
    existing = (
        db.query(MessageStatus)
        .filter(MessageStatus.message_id == message_id, MessageStatus.user_id == user_id)
        .one_or_none()
    )
    if existing is not None:
        return
    db.add(MessageStatus(message_id=message_id, user_id=user_id, status=state))


def _statuses_for_message(
    db: Session, msg: Message, sender_id: int, read: bool
) -> list[MessageStatus]:
    """Build per-recipient status rows for a freshly-created message.

    Every non-sender participant gets one row. `read=True` is used for the
    most recent messages (so the seed shows a believable mix of states).
    """
    recipient_ids = [
        p.user_id
        for p in db.query(ConversationParticipant)
        .filter(ConversationParticipant.conversation_id == msg.conversation_id)
        .all()
        if p.user_id != sender_id
    ]
    state = MessageStatusState.READ if read else MessageStatusState.DELIVERED
    rows: list[MessageStatus] = []
    for rid in recipient_ids:
        s = MessageStatus(message_id=msg.id, user_id=rid, status=state)
        db.add(s)
        rows.append(s)
    db.flush()
    return rows


# --- The seed --------------------------------------------------------------

def _seed(db: Session) -> None:
    # 1) Users --------------------------------------------------------------
    alice = _get_or_create_user(
        db,
        phone="+15550000001",
        username="alicechen",
        display_name="Alice Chen",
        avatar_url="https://i.pravatar.cc/150?u=alice",
    )
    bob = _get_or_create_user(
        db,
        phone="+15550000002",
        display_name="Bob Martinez",
        avatar_url="https://i.pravatar.cc/150?u=bob",
    )
    carol = _get_or_create_user(
        db,
        phone="+15550000003",
        display_name="Carol Singh",
        avatar_url="https://i.pravatar.cc/150?u=carol",
    )
    dan = _get_or_create_user(
        db,
        phone="+15550000004",
        display_name="Dan O'Brien",
        avatar_url="https://i.pravatar.cc/150?u=dan",
    )
    eve = _get_or_create_user(
        db,
        phone="+15550000005",
        display_name="Eve Tanaka",
        avatar_url="https://i.pravatar.cc/150?u=eve",
    )
    users_by_id = {u.id: u for u in (alice, bob, carol, dan, eve)}

    # 2) Conversations ------------------------------------------------------
    # 3 groups, 4 direct = 7 conversations total. Alice is added to the
    # Squad group too (spec: she should be a participant in enough
    # conversations that `GET /conversations` returns ≥5 for her).
    phoenix = _get_or_create_group(
        db, "Project Phoenix", [alice.id, bob.id, carol.id]
    )
    family = _get_or_create_group(
        db, "Family Group", [alice.id, bob.id, dan.id, eve.id]
    )
    squad = _get_or_create_group(
        db, "Squad Goals", [alice.id, carol.id, dan.id, eve.id]
    )

    d_ab = _get_or_create_direct(db, alice.id, bob.id)
    d_ac = _get_or_create_direct(db, alice.id, carol.id)
    d_bd = _get_or_create_direct(db, bob.id, dan.id)
    d_ce = _get_or_create_direct(db, carol.id, eve.id)

    # 2b) Contacts --------------------------------------------------------
    # Phase 4 needs ≥2 contacts for Alice so `GET /contacts` returns
    # data on a fresh DB. We also add a couple of cross-address-book
    # rows so the search endpoint can show a mix of `already_contact`
    # true/false. Notably, Alice does NOT have Bob as a contact yet —
    # the conversations smoke test asserts that POST /contacts for Bob
    # succeeds (i.e. creates a new contact row), so we leave that slot
    # empty.
    def _add_contact(owner: User, target: User) -> None:
        existing = (
            db.query(Contact)
            .filter(Contact.owner_id == owner.id, Contact.contact_id == target.id)
            .one_or_none()
        )
        if existing is not None:
            return
        db.add(Contact(owner_id=owner.id, contact_id=target.id))
        db.flush()

    _add_contact(alice, carol)
    _add_contact(alice, dan)
    _add_contact(bob, alice)
    _add_contact(dan, alice)

    # 3) Messages -----------------------------------------------------------
    # Direct conversations: 5 messages each, spread over 7 days.
    # Groups: 5 messages each.
    # We pick 1 reply in Phoenix, 1 image in Squad, 1 reaction in Phoenix.

    all_messages: list[Message] = []

    def _emit(conv_id: int, sender: User, content: str, days_ago: float, **kw):
        m = _add_message(
            db,
            conversation_id=conv_id,
            sender_id=sender.id,
            content=content,
            created_at=_ago(days_ago),
            **kw,
        )
        all_messages.append(m)
        return m

    if not _conversation_has_messages(db, d_ab.id):
        _emit(d_ab.id, alice, "Hey Bob, you free for coffee tomorrow?", 6.5)
        _emit(d_ab.id, bob, "Sure, 10am at the usual place?", 6.3)
        _emit(d_ab.id, alice, "Works for me. See you then!", 6.1)
        _emit(d_ab.id, bob, "I just pushed the design review notes.", 2.0)
        _emit(d_ab.id, alice, "Got them, will read tonight.", 1.5)

    if not _conversation_has_messages(db, d_ac.id):
        _emit(d_ac.id, carol, "Alice, did you finish the API contract?", 5.5)
        _emit(d_ac.id, alice, "Almost — adding the last 2 endpoints today.", 5.3)
        _emit(d_ac.id, carol, "Sweet. Let me know when it's up.", 5.0)
        _emit(d_ac.id, alice, "Done. Documented in /docs/api.md", 2.5)
        _emit(d_ac.id, carol, "Perfect, thanks!", 2.3)

    if not _conversation_has_messages(db, d_bd.id):
        _emit(d_bd.id, bob, "Dan, the deploy looks good", 4.0)
        _emit(d_bd.id, dan, "Thanks for the heads up", 3.9)
        _emit(d_bd.id, bob, "Anytime. How's the family?", 3.5)
        _emit(d_bd.id, dan, "All good. Kids started school this week.", 3.4)
        _emit(d_bd.id, bob, "Time flies. Tell them I said hi.", 1.0)

    if not _conversation_has_messages(db, d_ce.id):
        _emit(d_ce.id, carol, "Eve, are we still on for the hike Saturday?", 4.5)
        _emit(d_ce.id, eve, "Yes! Bringing the good snacks.", 4.4)
        _emit(d_ce.id, carol, "Trail head at 8am?", 4.3)
        _emit(d_ce.id, eve, "Sounds good 👍", 4.2)
        _emit(d_ce.id, carol, "See you there!", 0.5)

    if not _conversation_has_messages(db, phoenix.id):
        first = _emit(phoenix.id, alice, "Standup in 10, who's joining?", 6.0)
        _emit(phoenix.id, bob, "I'll be there.", 5.95)
        # Reply to Alice's standup message
        _emit(
            phoenix.id,
            carol,
            "Joining in 5, saving my update for last.",
            5.9,
            parent_id=first.id,
        )
        _emit(phoenix.id, alice, "Updated the tracker with this week's tasks.", 3.0)
        _emit(phoenix.id, bob, "I'll handle the migration script.", 2.8)

    if not _conversation_has_messages(db, family.id):
        _emit(family.id, dan, "Sunday dinner at my place, 6pm.", 3.5)
        _emit(family.id, eve, "I'll bring dessert.", 3.4)
        _emit(family.id, alice, "Can I bring anything?", 3.3)
        _emit(family.id, dan, "Just yourselves!", 3.25)
        _emit(family.id, bob, "Can't make it this week, sorry.", 0.8)

    if not _conversation_has_messages(db, squad.id):
        _emit(squad.id, carol, "Game night Friday?", 2.5)
        _emit(squad.id, dan, "I'm in.", 2.4)
        _emit(squad.id, eve, "Same. What are we playing?", 2.3)
        # Image-type message with attachment
        img = _emit(
            squad.id,
            dan,
            "https://picsum.photos/seed/squad/600/400",
            2.0,
            type=MessageType.IMAGE,
        )
        db.add(
            Attachment(
                message_id=img.id,
                url=img.content,
                mime="image/jpeg",
                size_bytes=102400,
            )
        )
        _emit(squad.id, carol, "That map looks wild, count me in.", 1.8)

    # 4) Reaction (idempotent: check before insert) -----------------------
    if db.query(MessageReaction).count() == 0 and all_messages:
        # React to the very first message we created with a thumbs up
        first = all_messages[0]
        db.add(
            MessageReaction(
                message_id=first.id,
                user_id=carol.id,
                emoji="👍",
            )
        )

    db.flush()

    # 5) message_status rows ---------------------------------------------
    # Anything in the last ~24h is `read`; older is `delivered`. This gives
    # the UI a believable mix of states to render.
    read_cutoff = NOW - timedelta(hours=24)
    for msg in (
        db.query(Message).all()
    ):
        sender = msg.sender_id
        for p in (
            db.query(ConversationParticipant)
            .filter(ConversationParticipant.conversation_id == msg.conversation_id)
            .all()
        ):
            if p.user_id == sender:
                continue
            _add_status(
                db,
                message_id=msg.id,
                user_id=p.user_id,
                state=(
                    MessageStatusState.READ
                    if msg.created_at and msg.created_at >= read_cutoff
                    else MessageStatusState.DELIVERED
                ),
            )

    # 6) last_message_at on each conversation ----------------------------
    for conv in db.query(Conversation).all():
        latest = (
            db.query(func.max(Message.created_at))
            .filter(Message.conversation_id == conv.id)
            .scalar()
        )
        if latest is not None:
            conv.last_message_at = latest

    # Touch users.last_seen so it isn't all-NULL in the UI
    for u in users_by_id.values():
        if u.last_seen is None:
            u.last_seen = NOW - timedelta(hours=2)

    db.commit()


# --- Entry points ---------------------------------------------------------

def run() -> None:
    """Public entry point used by `python -m app.seed` and by tests."""
    # Ensure tables exist before we try to query them. Idempotent.
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        sentinel = db.query(User).filter(User.phone == "+15550000001").one_or_none()
        if sentinel is not None:
            print("[seed] DB already seeded; nothing to do.")
            return
        _seed(db)
        print("[seed] Seeded successfully.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
