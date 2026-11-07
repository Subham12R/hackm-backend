"""Protected problem-statement resources: Google files the dashboard shows inline without handing out their link.

An organiser marks a resource as protected. Participants then get only its label; the dashboard asks the
backend for the file, and the backend fetches it from Google as a PDF and streams it to signed-in teams.
The Google link itself never leaves the server.
"""

import re
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import HTTPException

CACHE_SECONDS = 300
MAX_BYTES = 25 * 1024 * 1024
_MAX_ENTRIES = 32

_FILE_ID = r"[A-Za-z0-9_-]{20,}"
# docs.google.com/<kind>/d/<id>/... (also with a /u/<n>/ account prefix) -> the PDF export of that file.
_DOCS = re.compile(rf"^/(document|spreadsheets|presentation)(?:/u/\d+)?/d/({_FILE_ID})(?:/|$)")
_DRIVE_FILE = re.compile(rf"^/file(?:/u/\d+)?/d/({_FILE_ID})(?:/|$)")
_EXPORT = {
    "document": "https://docs.google.com/document/d/{id}/export?format=pdf",
    "spreadsheets": "https://docs.google.com/spreadsheets/d/{id}/export?format=pdf",
    "presentation": "https://docs.google.com/presentation/d/{id}/export/pdf",
}
_DRIVE_DOWNLOAD = "https://drive.google.com/uc?export=download&id={id}"

UNSUPPORTED = "protected resources must be a Google Docs, Sheets, Slides or Drive file link"
UNAVAILABLE = ("this resource could not be loaded. Ask an organiser to check that it is shared as "
               "'Anyone with the link can view'")

_cache: dict[str, tuple[float, bytes]] = {}
_lock = threading.Lock()


def pdf_url(url: str) -> str | None:
    """The address Google serves this file as a PDF from, or None for links that cannot be shown protected."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host == "docs.google.com":
        match = _DOCS.match(parsed.path)
        return _EXPORT[match.group(1)].format(id=match.group(2)) if match else None
    if host == "drive.google.com":
        match = _DRIVE_FILE.match(parsed.path)
        if match:
            return _DRIVE_DOWNLOAD.format(id=match.group(1))
        if parsed.path in ("/open", "/uc"):
            ids = parse_qs(parsed.query).get("id", [])
            if ids and re.fullmatch(_FILE_ID, ids[0]):
                return _DRIVE_DOWNLOAD.format(id=ids[0])
    return None


def participant_resources(resources: list[dict] | None) -> list[dict]:
    """What a team sees: open links as they are, protected ones as a label and position only."""
    shown = []
    for index, item in enumerate(resources or []):
        if item.get("protected"):
            shown.append({"label": item["label"], "url": "", "protected": True, "index": index})
        else:
            shown.append({"label": item["label"], "url": item["url"], "protected": False, "index": index})
    return shown


def fetch_pdf(url: str) -> bytes:
    """The resource as PDF bytes, kept for a few minutes so a room full of teams opening it costs one fetch."""
    source = pdf_url(url)
    if source is None:
        raise HTTPException(404, "resource not found")
    now = time.monotonic()
    with _lock:
        hit = _cache.get(source)
        if hit and hit[0] > now:
            return hit[1]
    data = _download(source)
    with _lock:
        for key in [key for key, (expires, _) in _cache.items() if expires <= now]:
            del _cache[key]
        while len(_cache) >= _MAX_ENTRIES:
            del _cache[next(iter(_cache))]
        _cache[source] = (now + CACHE_SECONDS, data)
    return data


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _download(source: str) -> bytes:
    try:
        with httpx.Client(follow_redirects=True, timeout=20) as client, client.stream("GET", source) as response:
            if response.status_code != 200:
                raise HTTPException(502, UNAVAILABLE)
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(502, "this resource is too large to show here")
                chunks.append(chunk)
    except httpx.HTTPError:
        raise HTTPException(502, UNAVAILABLE) from None
    data = b"".join(chunks)
    # A private file answers with Google's sign-in page, not an error code.
    if not data.startswith(b"%PDF"):
        raise HTTPException(502, UNAVAILABLE)
    return data
