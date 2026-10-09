from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models.identity import utc_now


class GalleryPhoto(Base):
    __tablename__ = "gallery_photos"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    # S3 object key under the gallery/ prefix; the public URL is derived from it.
    object_key: Mapped[str] = mapped_column(String(300), unique=True)
    caption: Mapped[str] = mapped_column(String(300), default="")
    content_type: Mapped[str] = mapped_column(String(60))
    size_bytes: Mapped[int] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    # Hidden photos stay in the bucket and the admin list but leave the public gallery.
    published: Mapped[bool] = mapped_column(Boolean, default=True)
    uploaded_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
