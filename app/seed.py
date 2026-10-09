"""Idempotent seed script.

Run from the project root with `python -m app.seed` (or directly via
`python -m app.seed` from `backend/`). Re-running is a no-op: we look up
existing rows by deterministic keys (phone, conversation shape, content)
and skip work that's already done.

Layout of the seeded DB:
  - 7 users (Alice, Bob, Carol, Dan, Eve, Maya, Noah)
  - 6 groups and 6 direct chats (12 conversations total)
  - 10 conversations visible to Alice
  - 50+ messages, including multiple replies, image/file attachments, and reactions
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
        # Fill missing demo profile fields on older persistent databases.
        if user.username is None and username is not None:
            user.username = username
        if user.display_name is None:
            user.display_name = display_name
        if user.avatar_url is None:
            user.avatar_url = avatar_url
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


def _find_seed_message(db: Session, conversation_id: int, content: str) -> Message | None:
    return (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.content == content)
        .order_by(Message.id)
        .first()
    )


def _ensure_seed_attachment(
    db: Session, *, message: Message | None, url: str, mime: str, size_bytes: int
) -> None:
    if message is None:
        return
    exists = (
        db.query(Attachment)
        .filter(Attachment.message_id == message.id, Attachment.url == url)
        .one_or_none()
    )
    if exists is None:
        db.add(
            Attachment(
                message_id=message.id,
                url=url,
                mime=mime,
                size_bytes=size_bytes,
            )
        )


def _ensure_seed_reaction(
    db: Session, *, message: Message | None, user: User, emoji: str
) -> None:
    if message is None:
        return
    exists = (
        db.query(MessageReaction)
        .filter(
            MessageReaction.message_id == message.id,
            MessageReaction.user_id == user.id,
            MessageReaction.emoji == emoji,
        )
        .one_or_none()
    )
    if exists is None:
        db.add(MessageReaction(message_id=message.id, user_id=user.id, emoji=emoji))


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
        username="bobmartinez",
        display_name="Bob Martinez",
        avatar_url="https://i.pravatar.cc/150?u=bob",
    )
    carol = _get_or_create_user(
        db,
        phone="+15550000003",
        username="carolsingh",
        display_name="Carol Singh",
        avatar_url="https://i.pravatar.cc/150?u=carol",
    )
    dan = _get_or_create_user(
        db,
        phone="+15550000004",
        username="danobrien",
        display_name="Dan O'Brien",
        avatar_url="https://i.pravatar.cc/150?u=dan",
    )
    eve = _get_or_create_user(
        db,
        phone="+15550000005",
        username="evetanaka",
        display_name="Eve Tanaka",
        avatar_url="https://i.pravatar.cc/150?u=eve",
    )
    maya = _get_or_create_user(
        db,
        phone="+15550000006",
        username="mayabrooks",
        display_name="Maya Brooks",
        avatar_url="https://i.pravatar.cc/150?u=maya",
    )
    noah = _get_or_create_user(
        db,
        phone="+15550000007",
        username="noahwilliams",
        display_name="Noah Williams",
        avatar_url="https://i.pravatar.cc/150?u=noah",
    )
    users_by_id = {u.id: u for u in (alice, bob, carol, dan, eve, maya, noah)}

    # 2) Conversations ------------------------------------------------------
    # 6 groups + 6 direct chats = 12 total. Alice participates in 10
    # conversations so the demo inbox feels populated on first login.
    phoenix = _get_or_create_group(
        db, "Project Phoenix", [alice.id, bob.id, carol.id]
    )
    family = _get_or_create_group(
        db, "Family Group", [alice.id, bob.id, dan.id, eve.id]
    )
    squad = _get_or_create_group(
        db, "Squad Goals", [alice.id, carol.id, dan.id, eve.id]
    )
    design = _get_or_create_group(
        db, "Design Reviews", [alice.id, maya.id, bob.id, carol.id]
    )
    weekend = _get_or_create_group(
        db, "Weekend Plans", [alice.id, noah.id, dan.id, eve.id]
    )
    launch = _get_or_create_group(
        db, "Product Launch", [alice.id, bob.id, carol.id, dan.id, eve.id]
    )

    d_ab = _get_or_create_direct(db, alice.id, bob.id)
    d_ac = _get_or_create_direct(db, alice.id, carol.id)
    d_bd = _get_or_create_direct(db, bob.id, dan.id)
    d_ce = _get_or_create_direct(db, carol.id, eve.id)
    d_am = _get_or_create_direct(db, alice.id, maya.id)
    d_an = _get_or_create_direct(db, alice.id, noah.id)

    # 2b) Contacts --------------------------------------------------------
    # Add a few contacts for Alice and cross-address-book rows so
    # contact lookup includes both saved and unsaved seeded users.
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
    _add_contact(alice, maya)
    _add_contact(alice, noah)
    _add_contact(bob, alice)
    _add_contact(dan, alice)

    # 3) Messages -----------------------------------------------------------
    # Each seeded conversation gets a compact timeline; Alice sees ten
    # populated conversations after signing in.

    def _emit(conv_id: int, sender: User, content: str, days_ago: float, **kw):
        m = _add_message(
            db,
            conversation_id=conv_id,
            sender_id=sender.id,
            content=content,
            created_at=_ago(days_ago),
            **kw,
        )
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

    if not _conversation_has_messages(db, d_am.id):
        _emit(d_am.id, alice, "Can you review the poster direction?", 2.2)
        _emit(d_am.id, maya, "The blue version feels clearer.", 2.1)
        _emit(d_am.id, alice, "Agreed. Here's the updated mockup.", 2.0)
        _emit(d_am.id, maya, "Much better — the title has room now.", 1.9)
        _emit(d_am.id, alice, "Great, I'll send this to the team.", 1.8)

    if not _conversation_has_messages(db, d_an.id):
        parent = _emit(d_an.id, alice, "Could you review the launch brief?", 2.0)
        _emit(d_an.id, noah, "Sure, I can look now.", 1.9)
        _emit(d_an.id, alice, "I attached the latest draft.", 1.8)
        _emit(
            d_an.id,
            noah,
            "The timeline looks solid. I left one note on the first section.",
            1.7,
            parent_id=parent.id,
        )
        _emit(d_an.id, alice, "Thanks — I'll update it before stand-up.", 1.6)

    if not _conversation_has_messages(db, design.id):
        _emit(design.id, alice, "I put the new screens in the review folder.", 1.6)
        _emit(design.id, bob, "The new navigation is much easier to scan.", 1.5)
        _emit(design.id, maya, "Adding the annotated brief here, too.", 1.4)
        _emit(design.id, carol, "The spacing feels good on mobile.", 1.3)
        _emit(design.id, alice, "Nice — let's use this version.", 1.2)

    if not _conversation_has_messages(db, weekend.id):
        _emit(weekend.id, dan, "Saturday trail plan: meet at 8?", 1.4)
        _emit(weekend.id, eve, "Yes, I'll bring snacks.", 1.3)
        _emit(weekend.id, noah, "Sharing the route photo.", 1.2)
        _emit(weekend.id, alice, "That view is worth the early start.", 1.1)
        _emit(weekend.id, dan, "Parking lot by the north entrance.", 1.0)

    if not _conversation_has_messages(db, launch.id):
        _emit(launch.id, bob, "Milestone two is ready for review.", 1.1)
        _emit(launch.id, carol, "The test build is looking good.", 1.0)
        _emit(launch.id, alice, "I attached the checklist for tomorrow.", 0.9)
        _emit(launch.id, dan, "I can take the first two items.", 0.8)
        _emit(launch.id, eve, "I'll cover the release notes.", 0.7)

    # Rich sample content is ensured separately so existing persistent
    # databases receive new examples on the next deployment as well.
    mockup_message = _find_seed_message(
        db, d_am.id, "Agreed. Here's the updated mockup."
    )
    _ensure_seed_attachment(
        db,
        message=mockup_message,
        url="https://picsum.photos/seed/signal-mockup/720/480",
        mime="image/jpeg",
        size_bytes=184320,
    )
    _ensure_seed_attachment(
        db,
        message=mockup_message,
        url="https://picsum.photos/seed/signal-layout/720/480",
        mime="image/jpeg",
        size_bytes=163840,
    )
    _ensure_seed_attachment(
        db,
        message=_find_seed_message(db, d_an.id, "I attached the latest draft."),
        url="https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
        mime="application/pdf",
        size_bytes=13264,
    )
    _ensure_seed_attachment(
        db,
        message=_find_seed_message(db, design.id, "Adding the annotated brief here, too."),
        url="https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
        mime="application/pdf",
        size_bytes=13264,
    )
    _ensure_seed_attachment(
        db,
        message=_find_seed_message(db, weekend.id, "Sharing the route photo."),
        url="https://picsum.photos/seed/weekend-trail/720/480",
        mime="image/jpeg",
        size_bytes=172032,
    )
    _ensure_seed_attachment(
        db,
        message=_find_seed_message(db, launch.id, "I attached the checklist for tomorrow."),
        url="https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
        mime="application/pdf",
        size_bytes=13264,
    )

    # 4) Reactions (idempotent: ensure each sample reaction once) ---------
    _ensure_seed_reaction(
        db,
        message=_find_seed_message(db, phoenix.id, "Standup in 10, who's joining?"),
        user=bob,
        emoji="👍",
    )
    _ensure_seed_reaction(
        db,
        message=_find_seed_message(db, d_am.id, "The blue version feels clearer."),
        user=alice,
        emoji="💙",
    )
    _ensure_seed_reaction(
        db,
        message=_find_seed_message(db, design.id, "The new navigation is much easier to scan."),
        user=maya,
        emoji="✨",
    )
    _ensure_seed_reaction(
        db,
        message=_find_seed_message(db, weekend.id, "That view is worth the early start."),
        user=eve,
        emoji="❤️",
    )
    _ensure_seed_reaction(
        db,
        message=_find_seed_message(db, launch.id, "Milestone two is ready for review."),
        user=alice,
        emoji="🚀",
    )

    db.flush()

    # 5) message_status rows ---------------------------------------------
    # Anything in the last ~24h is `read`; older is `delivered`. This gives
    # the UI a believable mix of states to render.
    read_cutoff = NOW - timedelta(hours=24)
    for msg in (
        db.query(Message).all()
    ):
        # SQLite drops timezone metadata from DateTime(timezone=True) values.
        # Seed timestamps are UTC, so restore UTC before comparing them with
        # the timezone-aware cutoff. Postgres values remain unchanged.
        created_at = msg.created_at
        if created_at is not None and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
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
                    if created_at and created_at >= read_cutoff
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
    """Ensure demo records exist, including additions to an older database."""
    # Keep startup seeding idempotent while allowing a later deploy to add
    # new sample conversations to an already-populated persistent database.
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        _seed(db)
        print("[seed] Demo data ensured.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
