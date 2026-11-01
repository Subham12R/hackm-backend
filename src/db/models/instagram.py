from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models.identity import utc_now


class InstagramToken(Base):
    """The one long-lived Instagram token the public feed uses, kept fresh by the backend.

    INSTAGRAM_ACCESS_TOKEN seeds it; after the first refresh the stored token wins, so the env var
    never has to be rotated by hand while refreshes keep succeeding.
    """

    __tablename__ = "instagram_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    access_token: Mapped[str] = mapped_column(Text)
    # SHA-256 of the env token this row grew from; pasting a new env token replaces the stored one.
    seed_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
