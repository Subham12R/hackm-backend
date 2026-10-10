"""S3 storage for gallery photos.

Browsers upload straight to the bucket with a presigned POST (AWS) or PUT (S3-compatible
stores such as Cloudflare R2, which has no POST uploads), so photo bytes never pass
through the API. The API only signs uploads, confirms the object landed, and deletes it.
Credentials come from the standard AWS chain (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY,
an instance role, ...); nothing secret is stored here.
"""

import logging
import os
from dataclasses import dataclass
from typing import Protocol

GALLERY_PREFIX = "gallery/"
MAX_PHOTO_BYTES = 15 * 1024 * 1024
UPLOAD_TTL_SECONDS = 10 * 60
CACHE_CONTROL = "public, max-age=31536000, immutable"
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/avif": "avif"}
EMAIL_PREFIX = "email/"
# Email clients render GIF, JPEG and PNG reliably; WebP/AVIF are left out on purpose.
EMAIL_IMAGE_TYPES = {"image/gif": "gif", "image/jpeg": "jpg", "image/png": "png"}
MAX_EMAIL_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class StoredObject:
    size_bytes: int
    content_type: str


class Storage(Protocol):
    def presign_upload(self, key: str, content_type: str, size_bytes: int) -> dict: ...
    def head(self, key: str) -> StoredObject | None: ...
    def delete(self, key: str) -> None: ...
    def public_url(self, key: str) -> str: ...


class S3Storage:
    def __init__(
        self,
        bucket: str,
        region: str,
        public_base_url: str | None = None,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        # R2 and most S3-compatible stores do not implement POST uploads, so they get a presigned PUT.
        self._post_uploads = endpoint_url is None
        self._client = boto3.client(
            "s3",
            region_name=region,
            # None falls back to the standard AWS chain (AWS_ACCESS_KEY_ID, an instance role, ...).
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            # The regional endpoint avoids the 307 redirect a new bucket gets on the global one, which breaks browser POSTs.
            endpoint_url=endpoint_url or f"https://s3.{region}.amazonaws.com",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3},
                s3={"addressing_style": "path" if endpoint_url else "virtual"},
            ),
        )
        if public_base_url:
            self._public_base = public_base_url.rstrip("/")
        elif endpoint_url:
            self._public_base = f"{endpoint_url.rstrip('/')}/{bucket}"
        else:
            self._public_base = f"https://{bucket}.s3.{region}.amazonaws.com"

    def presign_upload(self, key: str, content_type: str, size_bytes: int) -> dict:
        if not self._post_uploads:
            # Content type and length are signed, so the store rejects a different file; the API
            # still checks the object with HEAD before listing it.
            url = self._client.generate_presigned_url(
                "put_object",
                Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type, "CacheControl": CACHE_CONTROL, "ContentLength": size_bytes},
                ExpiresIn=UPLOAD_TTL_SECONDS,
            )
            return {"method": "PUT", "url": url, "headers": {"Content-Type": content_type, "Cache-Control": CACHE_CONTROL}}
        # A presigned POST lets S3 itself enforce the size limit and content type.
        post = self._client.generate_presigned_post(
            Bucket=self.bucket,
            Key=key,
            Fields={"Content-Type": content_type, "Cache-Control": CACHE_CONTROL},
            Conditions=[
                {"Content-Type": content_type},
                {"Cache-Control": CACHE_CONTROL},
                ["content-length-range", 1, MAX_PHOTO_BYTES],
            ],
            ExpiresIn=UPLOAD_TTL_SECONDS,
        )
        return {"method": "POST", "url": post["url"], "fields": post["fields"]}

    def head(self, key: str) -> StoredObject | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return StoredObject(size_bytes=int(response["ContentLength"]), content_type=response.get("ContentType", ""))

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def public_url(self, key: str) -> str:
        return f"{self._public_base}/{key}"


def storage_from_env() -> S3Storage | None:
    """None when no bucket is configured or its settings are unusable; gallery uploads then
    answer 503 and the rest of the API runs. Storage settings never stop the API from starting."""
    log = logging.getLogger(__name__)
    if bucket := os.getenv("R2_BUCKET"):
        account_id = os.getenv("R2_ACCOUNT_ID")
        endpoint = os.getenv("R2_ENDPOINT_URL") or (account_id and f"https://{account_id}.r2.cloudflarestorage.com")
        public_url = os.getenv("R2_PUBLIC_URL")
        key_id = os.getenv("R2_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID")
        secret = os.getenv("R2_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
        missing = [
            name
            for name, value in (
                ("R2_ACCOUNT_ID", endpoint),
                ("R2_PUBLIC_URL", public_url),
                ("R2_ACCESS_KEY_ID", key_id),
                ("R2_SECRET_ACCESS_KEY", secret),
            )
            if not value
        ]
        if missing:
            log.warning("gallery storage off: R2_BUCKET is set but %s is missing", ", ".join(missing))
            return None
        settings = (bucket, "auto", public_url, endpoint, key_id, secret)
    else:
        bucket = os.getenv("S3_BUCKET")
        if not bucket:
            return None
        region = os.getenv("S3_REGION") or os.getenv("AWS_REGION") or "ap-south-1"
        settings = (bucket, region, os.getenv("S3_PUBLIC_BASE_URL"), os.getenv("S3_ENDPOINT_URL"))
    try:
        return S3Storage(*settings)
    except Exception:
        log.warning("gallery storage off: could not set up the storage client", exc_info=True)
        return None
