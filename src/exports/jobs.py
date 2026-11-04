"""Build CSV exports in the background, 500 rows at a time.

A job is claimed with SKIP LOCKED so only one worker builds it. Rows are read with keyset
paging on the dataset's order (never OFFSET, never the whole table at once) and each chunk's
related data is loaded by the dataset's `enrich` in a few IN (...) queries.
"""

import csv
import io
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, tuple_, update
from sqlalchemy.orm import Session

from src.db.models.export import ExportJob
from src.db.session import create_db_engine
from src.exports.datasets import DATASETS

logger = logging.getLogger(__name__)
CHUNK = 500
KEEP_FOR = timedelta(hours=24)
STUCK_AFTER = timedelta(minutes=10)
MAX_BYTES = 25 * 1024 * 1024
# A cell starting with one of these runs as a formula in Excel / Sheets; prefix it so it stays text.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(FORMULA_START) else text


def tidy(session: Session) -> None:
    """Expire files older than a day and fail jobs a restart left half-built."""
    now = datetime.now(UTC)
    session.execute(update(ExportJob).where(ExportJob.status == "READY", ExportJob.expires_at < now).values(status="EXPIRED", content=None))
    session.execute(
        update(ExportJob).where(ExportJob.status == "RUNNING", ExportJob.started_at < now - STUCK_AFTER)
        .values(status="FAILED", error="the export stopped part-way (the server restarted); retry it", finished_at=now)
    )


def build(session: Session, job: ExportJob) -> tuple[bytes, int]:
    dataset = DATASETS[job.dataset]
    columns = [column for column in dataset.columns if column.key in set(job.fields)] or dataset.columns
    order = dataset.order()
    base = dataset.query(session, job.filters or {})
    if job.scope == "SELECTED":
        base = base.where(dataset.id_column.in_([UUID(str(item)) for item in job.ids or []]))
    keys = [expression.label(f"_key{index}") for index, expression in enumerate(order)]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([column.label for column in columns])
    last = None
    count = 0
    while True:
        query = base.add_columns(*keys).order_by(*order).limit(CHUNK)
        if last is not None:
            query = query.where(tuple_(*order) > tuple_(*last))
        rows = session.execute(query).all()
        if not rows:
            break
        width = len(rows[0]) - len(keys)
        context = dataset.enrich(session, [row[:width] for row in rows], job.filters or {})
        for row in rows:
            entity = row[:width]
            writer.writerow([safe_cell(column.get(entity, context)) for column in columns])
        count += len(rows)
        last = tuple(rows[-1][width:])
        job.row_count = count
        session.commit()
        if buffer.tell() > MAX_BYTES:
            raise ValueError("the export is larger than 25 MB; narrow it with filters or fewer fields")
        if len(rows) < CHUNK:
            break
    # A byte-order mark so Excel opens UTF-8 (names in any script) correctly.
    return buffer.getvalue().encode("utf-8-sig"), count


def run_export(database_url: str, job_id: UUID) -> None:
    with Session(create_db_engine(database_url)) as session:
        job = session.scalar(select(ExportJob).where(ExportJob.id == job_id, ExportJob.status == "QUEUED").with_for_update(skip_locked=True))
        if job is None:
            return
        job.status = "RUNNING"
        job.started_at = datetime.now(UTC)
        job.row_count = 0
        session.commit()
        try:
            content, count = build(session, job)
        except Exception as error:
            session.rollback()
            job = session.get(ExportJob, job_id)
            job.status = "FAILED"
            job.error = str(error)[:500]
            job.finished_at = datetime.now(UTC)
            session.commit()
            logger.warning("export %s failed", job_id, exc_info=True)
            return
        now = datetime.now(UTC)
        job.content = content
        job.size_bytes = len(content)
        job.row_count = count
        job.status = "READY"
        job.finished_at = now
        job.expires_at = now + KEEP_FOR
        session.commit()
