"""Authenticated stories API. Stories are visible for 24 hours."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import Contact, Conversation, ConversationParticipant, ConversationType, Story, StoryReaction, StoryView, User, UserAccountState

router = APIRouter(prefix="/stories", tags=["stories"])


class StoryCreate(BaseModel):
    media_url: str | None = Field(default=None, max_length=1024)
    media_type: Literal["image", "video"] = "image"
    text: str | None = Field(default=None, max_length=500)
    background: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def require_content(self):
        if not self.media_url and not (self.text and self.text.strip()):
            raise ValueError("Add text, a photo, or a video to your story")
        if self.media_url and not self.media_url.startswith("https://"):
            raise ValueError("Story media URL must use HTTPS")
        if self.media_type == "video" and not self.media_url:
            raise ValueError("A video story needs an uploaded video")
        return self


class StoryBatchCreate(BaseModel):
    stories: list[StoryCreate] = Field(min_length=1, max_length=10)


class StoryUser(BaseModel):
    id: int
    display_name: str | None
    username: str | None
    avatar_url: str | None


class StoryOut(BaseModel):
    id: int
    user: StoryUser
    media_url: str | None
    media_type: str
    text: str | None
    background: str
    created_at: datetime
    expires_at: datetime
    viewed_by_me: bool
    view_count: int
    reaction_counts: dict[str, int]
    reactions: list["StoryReactionOut"]
    viewers: list["StoryViewerOut"]
    my_reaction: str | None


class StoryReactionOut(BaseModel):
    user_id: int
    display_name: str | None
    username: str | None
    avatar_url: str | None
    emoji: str


class StoryViewerOut(BaseModel):
    user_id: int
    display_name: str | None
    username: str | None
    avatar_url: str | None
    viewed_at: datetime


class StoryGroup(BaseModel):
    user: StoryUser
    stories: list[StoryOut]


class StoryReactionIn(BaseModel):
    emoji: str

    @model_validator(mode="after")
    def validate_emoji(self):
        if self.emoji not in {"❤️", "😂", "😮", "😢", "🔥", "👍"}:
            raise ValueError("Choose one of the supported story reactions")
        return self


def _mutual_contact_ids(db: Session, user_id: int) -> set[int]:
    """Return users who have mutually added each other as a contact."""
    my_contacts = set(db.execute(
        select(Contact.contact_id).where(Contact.owner_id == user_id)
    ).scalars())
    if not my_contacts:
        return set()
    contacts_who_added_me = set(db.execute(
        select(Contact.owner_id).where(Contact.contact_id == user_id)
    ).scalars())
    return my_contacts & contacts_who_added_me


def _story_audience_ids(db: Session, user_id: int) -> set[int]:
    """Friends are mutual contacts or participants in an existing direct chat."""
    direct_peers = set(db.execute(
        select(ConversationParticipant.user_id)
        .join(Conversation, Conversation.id == ConversationParticipant.conversation_id)
        .where(
            Conversation.type == ConversationType.DIRECT,
            ConversationParticipant.conversation_id.in_(
                select(ConversationParticipant.conversation_id).where(
                    ConversationParticipant.user_id == user_id
                )
            ),
            ConversationParticipant.user_id != user_id,
        )
    ).scalars())
    return _mutual_contact_ids(db, user_id) | direct_peers


def _can_view_story(db: Session, story: Story, user_id: int) -> bool:
    return story.user_id == user_id or story.user_id in _story_audience_ids(db, user_id)


def _group_stories(db: Session, current_user: User) -> list[StoryGroup]:
    now = datetime.now(timezone.utc)
    visible_user_ids = _story_audience_ids(db, current_user.id) | {current_user.id}
    rows = db.execute(
        select(Story, User)
        .join(User, User.id == Story.user_id)
        .where(Story.expires_at > now, Story.user_id.in_(visible_user_ids))
        .where(~Story.user_id.in_(select(UserAccountState.user_id).where(UserAccountState.status == "deletion_pending")))
        .order_by(Story.created_at.asc(), Story.id.asc())
    ).all()
    story_ids = [story.id for story, _ in rows]
    views = {}
    if story_ids:
        views = dict(db.execute(
            select(StoryView.story_id, func.count(StoryView.id))
            .where(StoryView.story_id.in_(story_ids))
            .group_by(StoryView.story_id)
        ).all())
    viewed = set(db.execute(
        select(StoryView.story_id).where(
            StoryView.user_id == current_user.id,
            StoryView.story_id.in_(story_ids),
        )
    ).scalars()) if story_ids else set()

    reaction_counts: dict[int, dict[str, int]] = {}
    story_reactions: dict[int, list[StoryReactionOut]] = {}
    story_viewers: dict[int, list[StoryViewerOut]] = {}
    if story_ids:
        for story_id, emoji, count in db.execute(
            select(StoryReaction.story_id, StoryReaction.emoji, func.count(StoryReaction.id))
            .where(StoryReaction.story_id.in_(story_ids))
            .group_by(StoryReaction.story_id, StoryReaction.emoji)
        ).all():
            reaction_counts.setdefault(story_id, {})[emoji] = count
        my_reactions = dict(db.execute(
            select(StoryReaction.story_id, StoryReaction.emoji).where(
                StoryReaction.user_id == current_user.id,
                StoryReaction.story_id.in_(story_ids),
            )
        ).all())
        own_story_ids = [story.id for story, _ in rows if story.user_id == current_user.id]
        if own_story_ids:
            for reaction, user in db.execute(
                select(StoryReaction, User)
                .join(User, User.id == StoryReaction.user_id)
                .where(StoryReaction.story_id.in_(own_story_ids))
                .order_by(StoryReaction.created_at.asc())
            ).all():
                story_reactions.setdefault(reaction.story_id, []).append(StoryReactionOut(
                    user_id=user.id,
                    display_name=user.display_name,
                    username=user.username,
                    avatar_url=user.avatar_url,
                    emoji=reaction.emoji,
                ))
            for view, user in db.execute(
                select(StoryView, User)
                .join(User, User.id == StoryView.user_id)
                .where(StoryView.story_id.in_(own_story_ids))
                .order_by(StoryView.viewed_at.desc())
            ).all():
                story_viewers.setdefault(view.story_id, []).append(StoryViewerOut(
                    user_id=user.id,
                    display_name=user.display_name,
                    username=user.username,
                    avatar_url=user.avatar_url,
                    viewed_at=view.viewed_at,
                ))
    else:
        my_reactions = {}

    groups: dict[int, tuple[StoryUser, list[StoryOut]]] = {}
    for story, user in rows:
        user_out = StoryUser(id=user.id, display_name=user.display_name, username=user.username, avatar_url=user.avatar_url)
        if user.id not in groups:
            groups[user.id] = (user_out, [])
        groups[user.id][1].append(StoryOut(
            id=story.id,
            user=user_out,
            media_url=story.media_url,
            media_type=story.media_type,
            text=story.text,
            background=story.background,
            created_at=story.created_at,
            expires_at=story.expires_at,
            viewed_by_me=story.id in viewed,
            view_count=views.get(story.id, 0) if story.user_id == current_user.id else 0,
            reaction_counts=reaction_counts.get(story.id, {}) if story.user_id == current_user.id else {},
            reactions=story_reactions.get(story.id, []),
            viewers=story_viewers.get(story.id, []),
            my_reaction=my_reactions.get(story.id),
        ))
    # Put the caller's own story first, then stories not yet viewed.
    ordered = sorted(groups.values(), key=lambda group: (
        group[0].id != current_user.id,
        all(item.viewed_by_me for item in group[1]),
        group[1][-1].created_at,
    ))
    return [StoryGroup(user=user, stories=stories) for user, stories in ordered]


@router.get("", response_model=list[StoryGroup])
def list_stories(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _group_stories(db, current_user)


@router.post("", response_model=StoryOut, status_code=status.HTTP_201_CREATED)
def create_story(payload: StoryCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    story = Story(
        user_id=current_user.id,
        media_url=payload.media_url,
        media_type=payload.media_type,
        text=payload.text.strip() if payload.text else None,
        background=payload.background,
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    db.add(story)
    db.commit()
    db.refresh(story)
    return StoryOut(
        id=story.id,
        user=StoryUser(id=current_user.id, display_name=current_user.display_name, username=current_user.username, avatar_url=current_user.avatar_url),
        media_url=story.media_url,
        media_type=story.media_type,
        text=story.text,
        background=story.background,
        created_at=story.created_at,
        expires_at=story.expires_at,
        viewed_by_me=False,
        view_count=0,
        reaction_counts={},
        reactions=[],
        viewers=[],
        my_reaction=None,
    )


@router.post("/batch", response_model=list[StoryOut], status_code=status.HTTP_201_CREATED)
def create_stories_batch(
    payload: StoryBatchCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create up to ten story items together with a single database commit."""
    now = datetime.now(timezone.utc)
    stories = [Story(
        user_id=current_user.id,
        media_url=item.media_url,
        media_type=item.media_type,
        text=item.text.strip() if item.text else None,
        background=item.background,
        created_at=now,
        expires_at=now + timedelta(hours=24),
    ) for item in payload.stories]
    db.add_all(stories)
    db.commit()
    return [StoryOut(
        id=story.id,
        user=StoryUser(id=current_user.id, display_name=current_user.display_name, username=current_user.username, avatar_url=current_user.avatar_url),
        media_url=story.media_url,
        media_type=story.media_type,
        text=story.text,
        background=story.background,
        created_at=story.created_at,
        expires_at=story.expires_at,
        viewed_by_me=False,
        view_count=0,
        reaction_counts={},
        reactions=[],
        viewers=[],
        my_reaction=None,
    ) for story in stories]


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_story_media(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """Upload one story photo or video to ImageKit through the backend."""
    del current_user
    if not file.filename:
        raise HTTPException(status_code=400, detail="Choose a photo or video")
    mime = (file.content_type or "application/octet-stream").split(";", 1)[0].lower()
    is_image = mime.startswith("image/") and mime != "image/svg+xml"
    is_video = mime in {"video/mp4", "video/webm", "video/quicktime"}
    if not is_image and not is_video:
        raise HTTPException(status_code=400, detail="Choose a supported photo or video (MP4, WebM, or MOV)")
    max_bytes = 50 * 1024 * 1024 if is_video else 5 * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty files cannot be uploaded")
    if len(data) > max_bytes:
        limit = "50 MB" if is_video else "5 MB"
        media_kind = "videos" if is_video else "photos"
        raise HTTPException(status_code=413, detail=f"Story {media_kind} must be {limit} or smaller")

    private_key = get_settings().imagekit_private_key.strip()
    if not private_key:
        raise HTTPException(status_code=503, detail="Image uploads are not configured")
    import httpx

    filename = file.filename.replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "")[:180] or "story-media"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "https://upload.imagekit.io/api/v1/files/upload",
                auth=(private_key, ""),
                data={"fileName": filename, "useUniqueFileName": "true"},
                files={"file": (filename, data, mime)},
            )
            response.raise_for_status()
            url = response.json().get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError("ImageKit did not return a secure media URL")
    except (httpx.HTTPError, ValueError) as exc:
        detail = "Story media upload failed"
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
            detail = "ImageKit rejected the upload credentials"
        raise HTTPException(status_code=502, detail=detail) from exc
    return {"url": url, "media_type": "video" if is_video else "image"}


@router.post("/{story_id}/view", status_code=status.HTTP_204_NO_CONTENT)
def view_story(story_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    now = datetime.now(timezone.utc)
    if story is None or story.expires_at <= now:
        raise HTTPException(status_code=404, detail="Story not found or expired")
    if not _can_view_story(db, story, current_user.id):
        raise HTTPException(status_code=404, detail="Story not found")
    if story.user_id == current_user.id:
        return
    db.add(StoryView(story_id=story.id, user_id=current_user.id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # Repeated views are idempotent.


@router.delete("/{story_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_story(story_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    if story is None:
        raise HTTPException(status_code=404, detail="Story not found")
    if story.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own stories")
    db.delete(story)
    db.commit()


@router.put("/{story_id}/reaction", status_code=status.HTTP_204_NO_CONTENT)
def react_to_story(
    story_id: int,
    payload: StoryReactionIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    story = db.get(Story, story_id)
    if story is None or story.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=404, detail="Story not found or expired")
    if not _can_view_story(db, story, current_user.id):
        raise HTTPException(status_code=404, detail="Story not found")
    if story.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You can't react to your own story")
    reaction = db.execute(select(StoryReaction).where(
        StoryReaction.story_id == story.id,
        StoryReaction.user_id == current_user.id,
    )).scalar_one_or_none()
    if reaction is None:
        db.add(StoryReaction(story_id=story.id, user_id=current_user.id, emoji=payload.emoji))
    else:
        reaction.emoji = payload.emoji
    db.commit()


@router.delete("/{story_id}/reaction", status_code=status.HTTP_204_NO_CONTENT)
def remove_story_reaction(
    story_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    reaction = db.execute(select(StoryReaction).where(
        StoryReaction.story_id == story_id,
        StoryReaction.user_id == current_user.id,
    )).scalar_one_or_none()
    if reaction is not None:
        db.delete(reaction)
        db.commit()
