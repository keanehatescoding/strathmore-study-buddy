"""Download Classroom Drive-file materials as the course owner.

Classroom only hands out Drive file metadata; the bytes come from the Drive
API under the owner's refresh token, which needs the drive.readonly scope
(requested at sign-in since issue #37; older tokens must sign in again).

- Binary files (PDF, PPTX, DOCX, text): files.get_media, capped at MAX_BYTES.
- Google Docs/Slides/Sheets have no bytes: exported to text/plain or CSV.
- Shortcuts are followed one hop to shortcutDetails.targetId (Drive refuses
  to create a shortcut to a shortcut); the target's metadata decides. A
  link-shared target may need the shortcut's targetResourceKey, sent in the
  X-Goog-Drive-Resource-Keys header on every request for the target.
- DriveError = worth retrying (no Drive grant yet, network, 5xx): the
  pipeline leaves the resource pending. ExtractError = permanent (gone,
  unsupported type, too large): the resource is marked failed.

google-* imports are lazy, like app.classroom.
"""

from __future__ import annotations

import re

from app.extract import MAX_DOWNLOAD_BYTES, ExtractError

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
MAX_BYTES = MAX_DOWNLOAD_BYTES

SHORTCUT = "application/vnd.google-apps.shortcut"

# Google-native types -> export mime the extractor reads
EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}

_ID_PATTERNS = (
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),  # .../file/d/<id>/view, docs .../d/<id>/edit
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),  # open?id=<id>, uc?id=<id>
)


class DriveError(RuntimeError):
    """Download failed in a way a later run may fix; keep the resource pending."""


def file_id(url: str | None) -> str | None:
    for pattern in _ID_PATTERNS:
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return None


def build_service(client_id: str, client_secret: str, refresh_token: str):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=[SCOPE],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


class DriveClient:
    def __init__(self, service):
        self.service = service

    def __call__(self, url: str) -> tuple[bytes, str | None]:
        return self.download(url)

    def can_read(self, url: str | None) -> bool:
        """Whether this grant could download the file itself (a metadata call,
        no bytes). Seeing the file is not enough: an owner can let viewers open
        a file but not download or export it, so canDownload must be set.
        False on any failure: a caller only ever skips a shortcut on it."""
        fid = file_id(url)
        if fid is None:
            return False
        try:
            _, _, meta = self._resolve(fid, "capabilities/canDownload")
        except (DriveError, ExtractError):
            return False
        return (meta.get("capabilities") or {}).get("canDownload") is True

    def download(self, url: str) -> tuple[bytes, str | None]:
        """Downloader for app.extract: Drive URL -> (bytes, mime)."""
        fid = file_id(url)
        if fid is None:
            raise ExtractError(f"not a Drive file URL: {url}")
        files = self.service.files()
        fid, key, meta = self._resolve(fid, "size")
        mime = meta.get("mimeType") or ""
        if mime in EXPORTS:
            blob = self._call(_keyed(files.export(fileId=fid, mimeType=EXPORTS[mime]),
                                     fid, key))
            return _capped(blob), EXPORTS[mime]
        if mime.startswith("application/vnd.google-apps."):
            raise ExtractError(f"unsupported Google file type {mime}")
        if int(meta.get("size") or 0) > MAX_BYTES:
            raise ExtractError(f"Drive file is over {MAX_BYTES // (1024 * 1024)} MB")
        blob = self._call(_keyed(files.get_media(fileId=fid, supportsAllDrives=True),
                                 fid, key))
        return _capped(blob), mime or None

    def _resolve(self, fid: str, fields: str) -> tuple[str, str | None, dict]:
        """(file id, resource key, metadata with mimeType + `fields`), following
        a shortcut to its target. A shortcut costs one extra metadata call."""
        files = self.service.files()
        meta = self._call(files.get(fileId=fid, fields=f"mimeType,shortcutDetails,{fields}",
                                    supportsAllDrives=True))
        if meta.get("mimeType") != SHORTCUT:
            return fid, None, meta
        details = meta.get("shortcutDetails") or {}
        target, key = details.get("targetId"), details.get("targetResourceKey")
        if not target:
            raise ExtractError("Drive shortcut has no target")
        meta = self._call(_keyed(files.get(fileId=target, fields=f"mimeType,{fields}",
                                           supportsAllDrives=True), target, key))
        if meta.get("mimeType") == SHORTCUT:
            raise ExtractError("Drive shortcut points at another shortcut")
        return target, key, meta

    @staticmethod
    def _call(request):
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=2)
        except RefreshError as e:
            # e.g. invalid_scope: the token predates the Drive grant
            raise DriveError(
                "Google refused the token for Drive; the owner must sign in again "
                f"to grant Drive access ({e})"
            ) from e
        except HttpError as e:
            status = e.resp.status
            if status == 404:
                raise ExtractError("Drive file not found or not shared with the owner") from e
            if status in (401, 403) and b"insufficient" in (e.content or b"").lower():
                raise DriveError(
                    "token lacks Drive access; the owner must sign in again"
                ) from e
            if status in (401, 403):
                raise ExtractError(f"Drive denied access ({status})") from e
            raise DriveError(f"Drive returned {status}") from e
        except (OSError, TimeoutError) as e:
            raise DriveError(f"Drive download failed: {e}") from e


def _keyed(request, fid: str, key: str | None):
    """Attach a link-shared file's resource key to a Drive request."""
    if key:
        request.headers["X-Goog-Drive-Resource-Keys"] = f"{fid}/{key}"
    return request


def _capped(blob) -> bytes:
    blob = blob.encode() if isinstance(blob, str) else blob
    if len(blob) > MAX_BYTES:
        raise ExtractError(f"Drive file is over {MAX_BYTES // (1024 * 1024)} MB")
    return blob
