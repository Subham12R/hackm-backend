import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from src.api.deps import authenticated_user
from src.audit import audit
from src.auth.policies import ADMIN_READ_ROLES, EVENT_CONTROL_ROLES
from src.cache import PUBLIC_GALLERY
from src.db.models.identity import User
from src.db.models.media import GalleryPhoto
from src.events.service import ACTIVE_EVENT_ID
from src.storage.s3 import GALLERY_PREFIX, MAX_PHOTO_BYTES, PHOTO_TYPES, Storage

# Only keys this API signed: gallery/YYYY/MM/<uuid hex>.<ext>
GALLERY_KEY = re.compile(rf"{GALLERY_PREFIX}\d{{4}}/\d{{2}}/[0-9a-f]{{32}}\.(jpg|png|webp|avif)")
Caption = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


class UploadRequest(BaseModel):
    content_type: str
    size_bytes: int = Field(ge=1, le=MAX_PHOTO_BYTES)


class PhotoCreate(BaseModel):
    object_key: Annotated[str, StringConstraints(max_length=300)]
    caption: Caption = ""
    width: int | None = Field(default=None, ge=1, le=20_000)
    height: int | None = Field(default=None, ge=1, le=20_000)


class PhotoUpdate(BaseModel):
    caption: Caption | None = None
    published: bool | None = None


def admin_gallery_router(public_key: Ed25519PublicKey) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/gallery", tags=["admin-gallery"])

    def storage(request: Request) -> Storage:
        configured = request.app.state.storage
        if configured is None:
            raise HTTPException(503, "photo storage is not configured")
        return configured

    @router.get("")
    def list_photos(request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, ADMIN_READ_ROLES)
        with session:
            photos = session.scalars(
                select(GalleryPhoto).where(GalleryPhoto.event_id == ACTIVE_EVENT_ID).order_by(GalleryPhoto.created_at.desc())
            ).all()
            uploader_ids = {photo.uploaded_by_user_id for photo in photos if photo.uploaded_by_user_id}
            names = {user.id: user.display_name for user in session.scalars(select(User).where(User.id.in_(uploader_ids)))} if uploader_ids else {}
            configured = request.app.state.storage
            return {
                "configured": configured is not None,
                "max_bytes": MAX_PHOTO_BYTES,
                "content_types": sorted(PHOTO_TYPES),
                "items": [
                    {**photo_payload(photo, configured), "uploaded_by": names.get(photo.uploaded_by_user_id)}
                    for photo in photos
                ],
            }

    @router.post("/uploads", status_code=201)
    def sign_upload(payload: UploadRequest, request: Request, authorization: str = Header()):
        session, _user = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        session.close()
        extension = PHOTO_TYPES.get(payload.content_type)
        if extension is None:
            raise HTTPException(422, "photos must be JPEG, PNG, WebP or AVIF")
        key = f"{GALLERY_PREFIX}{datetime.now(UTC):%Y/%m}/{uuid4().hex}.{extension}"
        return {"object_key": key, **storage(request).presign_upload(key, payload.content_type)}

    @router.post("", status_code=201)
    def create_photo(payload: PhotoCreate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            bucket = storage(request)
            if not GALLERY_KEY.fullmatch(payload.object_key):
                raise HTTPException(422, "unknown upload")
            if session.scalar(select(GalleryPhoto.id).where(GalleryPhoto.object_key == payload.object_key)):
                raise HTTPException(409, "photo already added")
            stored = bucket.head(payload.object_key)
            if stored is None:
                raise HTTPException(409, "the upload has not reached storage yet")
            if stored.content_type not in PHOTO_TYPES or stored.size_bytes > MAX_PHOTO_BYTES:
                bucket.delete(payload.object_key)
                raise HTTPException(422, "photos must be JPEG, PNG, WebP or AVIF and at most 15 MB")
            photo = GalleryPhoto(
                event_id=ACTIVE_EVENT_ID,
                object_key=payload.object_key,
                caption=payload.caption,
                content_type=stored.content_type,
                size_bytes=stored.size_bytes,
                width=payload.width,
                height=payload.height,
                published=True,
                uploaded_by_user_id=actor.id,
            )
            session.add(photo)
            session.flush()
            audit(session, actor, "Gallery photo added", "gallery_photo", photo.id, caption=payload.caption or None)
            session.commit()
            request.app.state.cache.invalidate(PUBLIC_GALLERY)
            return {**photo_payload(photo, bucket), "uploaded_by": actor.display_name}

    @router.patch("/{photo_id}")
    def update_photo(photo_id: UUID, payload: PhotoUpdate, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            photo = session.get(GalleryPhoto, photo_id, with_for_update=True)
            if photo is None:
                raise HTTPException(404, "photo not found")
            changes = {}
            if payload.caption is not None and payload.caption != photo.caption:
                photo.caption = changes["caption"] = payload.caption
            if payload.published is not None and payload.published != photo.published:
                photo.published = changes["published"] = payload.published
            if changes:
                audit(session, actor, "Gallery photo updated", "gallery_photo", photo.id, **changes)
                session.commit()
                request.app.state.cache.invalidate(PUBLIC_GALLERY)
            return photo_payload(photo, request.app.state.storage)

    @router.delete("/{photo_id}", status_code=204)
    def delete_photo(photo_id: UUID, request: Request, authorization: str = Header()):
        session, actor = authenticated_user(request, authorization, public_key, EVENT_CONTROL_ROLES)
        with session:
            photo = session.get(GalleryPhoto, photo_id, with_for_update=True)
            if photo is None:
                raise HTTPException(404, "photo not found")
            # Remove the object first: a leftover row is visible and retryable, a leftover object is not.
            storage(request).delete(photo.object_key)
            audit(session, actor, "Gallery photo deleted", "gallery_photo", photo.id, key=photo.object_key)
            session.delete(photo)
            session.commit()
            request.app.state.cache.invalidate(PUBLIC_GALLERY)

    return router


def photo_payload(photo: GalleryPhoto, storage: Storage | None) -> dict:
    return {
        "id": str(photo.id),
        "url": storage.public_url(photo.object_key) if storage else None,
        "caption": photo.caption,
        "content_type": photo.content_type,
        "size_bytes": photo.size_bytes,
        "width": photo.width,
        "height": photo.height,
        "published": photo.published,
        "created_at": photo.created_at.isoformat(),
    }
