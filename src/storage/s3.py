"""S3 storage for gallery photos.

Browsers upload straight to the bucket with a presigned POST, so photo bytes never pass
through the API. The API only signs uploads, confirms the object landed, and deletes it.
Credentials come from the standard AWS chain (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY,
an instance role, ...); nothing secret is stored here.
"""

import os
from dataclasses import dataclass
from typing import Protocol

GALLERY_PREFIX = "gallery/"
MAX_PHOTO_BYTES = 15 * 1024 * 1024
UPLOAD_TTL_SECONDS = 10 * 60
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/avif": "avif"}


@dataclass(frozen=True)
class StoredObject:
    size_bytes: int
    content_type: str


class Storage(Protocol):
    def presign_upload(self, key: str, content_type: str) -> dict: ...
    def head(self, key: str) -> StoredObject | None: ...
    def delete(self, key: str) -> None: ...
    def public_url(self, key: str) -> str: ...


class S3Storage:
    def __init__(self, bucket: str, region: str, public_base_url: str | None = None, endpoint_url: str | None = None) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            region_name=region,
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

    def presign_upload(self, key: str, content_type: str) -> dict:
        # A presigned POST (unlike PUT) lets S3 itself enforce the size limit and content type.
        post = self._client.generate_presigned_post(
            Bucket=self.bucket,
            Key=key,
            Fields={"Content-Type": content_type, "Cache-Control": "public, max-age=31536000, immutable"},
            Conditions=[
                {"Content-Type": content_type},
                {"Cache-Control": "public, max-age=31536000, immutable"},
                ["content-length-range", 1, MAX_PHOTO_BYTES],
            ],
            ExpiresIn=UPLOAD_TTL_SECONDS,
        )
        return {"url": post["url"], "fields": post["fields"]}

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
    """None when no bucket is configured; gallery uploads then answer 503 and the rest of the API runs."""
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        return None
    region = os.getenv("S3_REGION") or os.getenv("AWS_REGION") or "ap-south-1"
    return S3Storage(bucket, region, os.getenv("S3_PUBLIC_BASE_URL"), os.getenv("S3_ENDPOINT_URL"))
