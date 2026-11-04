from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.models.identity import utc_now


class ExportJob(Base):
    """One CSV export, built in the background in chunks and downloadable for 24 hours."""

    __tablename__ = "export_jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    # A key of src.exports.datasets.DATASETS.
    dataset: Mapped[str] = mapped_column(String(30))
    fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    # SELECTED (ids), FILTERED (filters) or ALL.
    scope: Mapped[str] = mapped_column(String(10))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # QUEUED -> RUNNING -> READY (-> EXPIRED after 24h), or FAILED.
    status: Mapped[str] = mapped_column(String(10), default="QUEUED", index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    file_name: Mapped[str] = mapped_column(String(200), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # The CSV itself; cleared when the job expires. Kept in Postgres so it is never behind a public URL.
    content: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    error: Mapped[str | None] = mapped_column(String(500))
    requested_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    download_count: Mapped[int] = mapped_column(Integer, default=0)
