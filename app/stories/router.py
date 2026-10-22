"""Authenticated stories API. Stories are visible for 24 hours."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app.models import Story, StoryView, User

router = APIRouter(prefix="/stories", tags=["stories"])


class StoryCreate(BaseModel):
    media_url: str | None = Field(default=None, max_length=1024)
    text: str | None = Field(default=None, max_length=500)
    background: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def require_content(self):
        if not self.media_url and not (self.text and self.text.strip()):
            raise ValueError("Add text or an image to your story")
        if self.media_url and not self.media_url.startswith("https://"):
            raise ValueError("Story image URL must use HTTPS")
        return self


class StoryUser(BaseModel):
    id: int
    display_name: str | None
    username: str | None
    avatar_url: str | None


class StoryOut(BaseModel):
    id: int
    user: StoryUser
    media_url: str | None
    text: str | None
    background: str
    created_at: datetime
    expires_at: datetime
    viewed_by_me: bool
    view_count: int


class StoryGroup(BaseModel):
    user: StoryUser
    stories: list[StoryOut]


def _group_stories(db: Session, current_user: User) -> list[StoryGroup]:
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(Story, User)
        .join(User, User.id == Story.user_id)
        .where(Story.expires_at > now)
        .order_by(Story.created_at.asc())
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

    groups: dict[int, tuple[StoryUser, list[StoryOut]]] = {}
    for story, user in rows:
        user_out = StoryUser(id=user.id, display_name=user.display_name, username=user.username, avatar_url=user.avatar_url)
        if user.id not in groups:
            groups[user.id] = (user_out, [])
        groups[user.id][1].append(StoryOut(
            id=story.id,
            user=user_out,
            media_url=story.media_url,
            text=story.text,
            background=story.background,
            created_at=story.created_at,
            expires_at=story.expires_at,
            viewed_by_me=story.id in viewed,
            view_count=views.get(story.id, 0),
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
        text=story.text,
        background=story.background,
        created_at=story.created_at,
        expires_at=story.expires_at,
        viewed_by_me=False,
        view_count=0,
    )


@router.post("/{story_id}/view", status_code=status.HTTP_204_NO_CONTENT)
def view_story(story_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    story = db.get(Story, story_id)
    now = datetime.now(timezone.utc)
    if story is None or story.expires_at <= now:
        raise HTTPException(status_code=404, detail="Story not found or expired")
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
