from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.orm import undefer

from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import EVENT_CONTROL_ROLES
from src.db.models.export import ExportJob
from src.db.models.identity import User
from src.events.service import ACTIVE_EVENT_ID
from src.exports.datasets import DATASETS, datasets_payload
from src.exports.jobs import run_export, tidy

FilterValue = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]


class ExportRequest(BaseModel):
    dataset: Literal[tuple(DATASETS)]  # type: ignore[valid-type]
    fields: list[Annotated[str, StringConstraints(max_length=40)]] = Field(default_factory=list, max_length=60)
    scope: Literal["SELECTED", "FILTERED", "ALL"] = "FILTERED"
    # The table's own filters: search, status, category, finals, review, stage_id, track_id, round_id.
    filters: dict[Literal["search", "status", "category", "finals", "review", "stage_id", "track_id", "round_id"], FilterValue] = Field(default_factory=dict)
    ids: list[UUID] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def consistent(self):
        known = {column.key for column in DATASETS[self.dataset].columns}
        unknown = [field for field in self.fields if field not in known]
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(unknown)}")
        if self.scope == "SELECTED" and not self.ids:
            raise ValueError("tick at least one row to export the selection")
        if self.filters.get("stage_id") == "all" and self.dataset != "submissions":
            raise ValueError("only the Submissions export can cover all events at once")
        for key in ("stage_id", "track_id", "round_id"):
            if self.filters.get(key) and not (key == "stage_id" and self.filters[key] == "all"):
                UUID(self.filters[key])
        return self


def admin_exports_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/exports", tags=["admin-exports"])

    def staff(request: Request, authorization: str):
        # Exports carry participants' personal data, so only Event Control may make or download them.
        return authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)

    def load(session, job_id: UUID, lock: bool = False) -> ExportJob:
        job = session.get(ExportJob, job_id, with_for_update=lock)
        if job is None or job.event_id != ACTIVE_EVENT_ID:
            raise HTTPException(404, "export not found")
        return job

    @router.get("/datasets")
    def list_datasets(request: Request, authorization: str = Header()):
        session, _user = staff(request, authorization)
        session.close()
        return {"datasets": datasets_payload()}

    @router.get("")
    def list_exports(request: Request, authorization: str = Header()):
        session, _user = staff(request, authorization)
        with session:
            tidy(session)
            session.commit()
            jobs = session.scalars(select(ExportJob).where(ExportJob.event_id == ACTIVE_EVENT_ID).order_by(ExportJob.created_at.desc()).limit(50)).all()
            names = dict(session.execute(select(User.id, User.display_name).where(User.id.in_({job.requested_by_user_id for job in jobs}))).tuples().all()) if jobs else {}
            return {"items": [_payload(job, names) for job in jobs]}

    @router.post("", status_code=202)
    def start_export(payload: ExportRequest, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = staff(request, authorization)
        with session:
            tidy(session)
            dataset = DATASETS[payload.dataset]
            fields = payload.fields or [column.key for column in dataset.columns if column.default]
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
            job = ExportJob(
                event_id=ACTIVE_EVENT_ID, dataset=payload.dataset, fields=fields, scope=payload.scope,
                filters=dict(payload.filters) if payload.scope == "FILTERED" else {k: v for k, v in payload.filters.items() if k in ("stage_id", "round_id")},
                ids=[str(item) for item in payload.ids] if payload.scope == "SELECTED" else [],
                status="QUEUED", requested_by_user_id=actor.id, file_name=f"hackmatrix-{payload.dataset}-{stamp}.csv",
            )
            session.add(job)
            session.flush()
            audit(session, actor, "Exported CSV", "export", job.id, dataset=payload.dataset, scope=payload.scope, fields=len(fields),
                  selected=len(job.ids), filters=", ".join(f"{key}={value}" for key, value in job.filters.items()))
            session.commit()
            background.add_task(run_export, request.app.state.database_url, job.id)
            return _payload(job, {actor.id: actor.display_name})

    @router.get("/{job_id}")
    def export_status(job_id: UUID, request: Request, authorization: str = Header()):
        session, _user = staff(request, authorization)
        with session:
            tidy(session)
            session.commit()
            job = load(session, job_id)
            requester = session.get(User, job.requested_by_user_id)
            return _payload(job, {job.requested_by_user_id: requester.display_name if requester else None})

    @router.get("/{job_id}/download")
    def download(job_id: UUID, request: Request, authorization: str = Header()):
        session, actor = staff(request, authorization)
        with session:
            tidy(session)
            session.commit()
            job = session.scalar(select(ExportJob).options(undefer(ExportJob.content)).where(ExportJob.id == job_id).with_for_update())
            if job is None or job.event_id != ACTIVE_EVENT_ID:
                raise HTTPException(404, "export not found")
            if job.status != "READY" or job.content is None:
                raise HTTPException(409, "this export is not ready" if job.status in ("QUEUED", "RUNNING") else "this export has expired; start a new one")
            job.download_count += 1
            audit(session, actor, "Downloaded CSV export", "export", job.id, dataset=job.dataset, rows=job.row_count)
            content, name = job.content, job.file_name
            session.commit()
            return Response(content, media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @router.post("/{job_id}/retry", status_code=202)
    def retry(job_id: UUID, request: Request, background: BackgroundTasks, authorization: str = Header()):
        session, actor = staff(request, authorization)
        with session:
            job = load(session, job_id, lock=True)
            if job.status not in ("FAILED", "EXPIRED"):
                raise HTTPException(409, "only failed or expired exports can be run again")
            job.status, job.error, job.content, job.row_count = "QUEUED", None, None, 0
            job.started_at = job.finished_at = job.expires_at = None
            audit(session, actor, "Retried CSV export", "export", job.id, dataset=job.dataset)
            session.commit()
            background.add_task(run_export, request.app.state.database_url, job.id)
            return _payload(job, {actor.id: actor.display_name})

    @router.delete("/{job_id}", status_code=204)
    def delete_export(job_id: UUID, request: Request, authorization: str = Header()):
        session, actor = staff(request, authorization)
        with session:
            job = load(session, job_id, lock=True)
            audit(session, actor, "Deleted CSV export", "export", job.id, dataset=job.dataset)
            session.delete(job)
            session.commit()

    return router


def _payload(job: ExportJob, names: dict) -> dict:
    dataset = DATASETS.get(job.dataset)
    return {
        "id": str(job.id), "dataset": job.dataset, "dataset_label": dataset.label if dataset else job.dataset,
        "fields": job.fields, "scope": job.scope, "filters": job.filters, "selected": len(job.ids or []),
        "status": job.status, "row_count": job.row_count, "file_name": job.file_name, "size_bytes": job.size_bytes,
        "error": job.error, "requested_by": names.get(job.requested_by_user_id),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "download_count": job.download_count,
    }
