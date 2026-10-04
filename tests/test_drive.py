"""Drive downloads for Classroom materials (issue #37), against a fake service."""

import pytest

from app import drive
from app.extract import ExtractError

FID = "1AbCdEfGhIjKlMnOp"
TARGET = "1TargetFileIdXyz"


class _Req:
    def __init__(self, result):
        self.result = result
        self.headers = {}

    def execute(self, num_retries=0):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _Files:
    def __init__(self, meta, media=b"", exported=b"", fail=None, by_id=None):
        self.meta, self.media, self.exported, self.fail = meta, media, exported, fail
        self.by_id = by_id or {}  # per-file metadata, e.g. a shortcut's target
        self.calls, self.requests = [], []

    def _req(self, result):
        self.requests.append(req := _Req(result))
        return req

    def get(self, fileId, fields, supportsAllDrives):
        self.calls.append(("get", fileId))
        return self._req(self.fail or self.by_id.get(fileId, self.meta))

    def get_media(self, fileId, supportsAllDrives):
        self.calls.append(("media", fileId))
        return self._req(self.media)

    def export(self, fileId, mimeType):
        self.calls.append(("export", fileId, mimeType))
        return self._req(self.exported)


class _Service:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def _client(**kw):
    files = _Files(**kw)
    return drive.DriveClient(_Service(files)), files


def _http_error(status, content=b"{}"):
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    return HttpError(Response({"status": status}), content)


@pytest.mark.parametrize("url", [
    f"https://drive.google.com/file/d/{FID}/view?usp=drive_web",
    f"https://docs.google.com/document/d/{FID}/edit",
    f"https://drive.google.com/open?id={FID}",
])
def test_file_id_from_drive_urls(url):
    assert drive.file_id(url) == FID


def test_file_id_rejects_other_urls():
    assert drive.file_id("https://example.com/notes.pdf") is None
    assert drive.file_id(None) is None


def test_binary_file_downloads_media():
    client, files = _client(meta={"mimeType": "application/pdf", "size": "10"},
                            media=b"%PDF")
    assert client.download(f"https://drive.google.com/file/d/{FID}/view") == (
        b"%PDF", "application/pdf")
    assert files.calls == [("get", FID), ("media", FID)]


@pytest.mark.parametrize("google_type, export_mime", sorted(drive.EXPORTS.items()))
def test_google_files_are_exported(google_type, export_mime):
    client, files = _client(meta={"mimeType": google_type}, exported=b"slide text")
    blob, mime = client.download(f"https://docs.google.com/x/d/{FID}/edit")
    assert (blob, mime) == (b"slide text", export_mime)
    assert ("media", FID) not in files.calls


def test_unsupported_google_type_and_oversize_fail_permanently(monkeypatch):
    client, _ = _client(meta={"mimeType": "application/vnd.google-apps.form"})
    with pytest.raises(ExtractError, match="unsupported"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")
    client, files = _client(meta={"mimeType": "application/pdf",
                                  "size": str(drive.MAX_BYTES + 1)})
    with pytest.raises(ExtractError, match="MB"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")
    assert ("media", FID) not in files.calls  # checked before downloading


def test_missing_file_fails_permanently():
    client, _ = _client(meta={}, fail=_http_error(404))
    with pytest.raises(ExtractError, match="not found"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")


def test_token_without_drive_grant_stays_retryable():
    from google.auth.exceptions import RefreshError

    client, _ = _client(meta={}, fail=RefreshError("invalid_scope"))
    with pytest.raises(drive.DriveError, match="sign in again"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")
    body = b'{"error": {"errors": [{"reason": "insufficientPermissions"}]}}'
    client, _ = _client(meta={}, fail=_http_error(403, body))
    with pytest.raises(drive.DriveError, match="sign in again"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")


def test_server_error_stays_retryable():
    client, _ = _client(meta={}, fail=_http_error(503))
    with pytest.raises(drive.DriveError):
        client.download(f"https://drive.google.com/file/d/{FID}/view")


def test_non_drive_url_fails_permanently():
    client, files = _client(meta={})
    with pytest.raises(ExtractError, match="not a Drive"):
        client.download("https://example.com/notes.pdf")
    assert files.calls == []


def test_can_read_asks_for_metadata_only():
    client, files = _client(meta={"capabilities": {"canDownload": True}})
    assert client.can_read(f"https://drive.google.com/file/d/{FID}/view")
    assert files.calls == [("get", FID)]


@pytest.mark.parametrize("meta", [
    {"id": FID, "capabilities": {"canDownload": False}},  # view-only
    {"id": FID},  # capability not reported
])
def test_can_read_needs_download_permission_not_just_visibility(meta):
    client, _ = _client(meta=meta)
    assert not client.can_read(f"https://drive.google.com/file/d/{FID}/view")


@pytest.mark.parametrize("fail", [404, 403, 500])
def test_can_read_is_false_on_any_refusal(fail):
    client, _ = _client(meta={}, fail=_http_error(fail))
    assert not client.can_read(f"https://drive.google.com/file/d/{FID}/view")


def test_can_read_rejects_non_drive_urls():
    client, files = _client(meta={"capabilities": {"canDownload": True}})
    assert not client.can_read("https://example.com/x.pdf") and files.calls == []


def _shortcut(target=TARGET):
    return {"mimeType": drive.SHORTCUT, "shortcutDetails": {"targetId": target}}


def test_shortcut_downloads_its_target():
    client, files = _client(meta=_shortcut(), media=b"%PDF", by_id={
        TARGET: {"mimeType": "application/pdf", "size": "10"}})
    assert client.download(f"https://drive.google.com/file/d/{FID}/view") == (
        b"%PDF", "application/pdf")
    assert files.calls == [("get", FID), ("get", TARGET), ("media", TARGET)]


def _key_headers(files):
    return [r.headers.get("X-Goog-Drive-Resource-Keys") for r in files.requests]


@pytest.mark.parametrize("target_meta, last_call", [
    ({"mimeType": "application/pdf", "size": "10"}, ("media", TARGET)),
    ({"mimeType": "application/vnd.google-apps.document"},
     ("export", TARGET, "text/plain")),
])
def test_link_shared_target_sends_its_resource_key(target_meta, last_call):
    shortcut = _shortcut()
    shortcut["shortcutDetails"]["targetResourceKey"] = "0-rk"
    client, files = _client(meta=shortcut, media=b"%PDF", exported=b"text",
                            by_id={TARGET: target_meta})
    client.download(f"https://drive.google.com/file/d/{FID}/view")
    assert files.calls[-1] == last_call
    # not on the shortcut itself; on the target's metadata and bytes
    assert _key_headers(files) == [None, f"{TARGET}/0-rk", f"{TARGET}/0-rk"]


def test_unkeyed_requests_send_no_resource_key_header():
    client, files = _client(meta=_shortcut(), media=b"%PDF", by_id={
        TARGET: {"mimeType": "application/pdf", "size": "10"}})
    client.download(f"https://drive.google.com/file/d/{FID}/view")
    assert _key_headers(files) == [None, None, None]


def test_shortcut_to_google_doc_exports_the_target():
    client, files = _client(meta=_shortcut(), exported=b"doc text", by_id={
        TARGET: {"mimeType": "application/vnd.google-apps.document"}})
    assert client.download(f"https://drive.google.com/file/d/{FID}/view") == (
        b"doc text", "text/plain")
    assert files.calls[-1] == ("export", TARGET, "text/plain")


def test_shortcut_target_size_is_checked():
    client, files = _client(meta=_shortcut(), by_id={
        TARGET: {"mimeType": "application/pdf", "size": str(drive.MAX_BYTES + 1)}})
    with pytest.raises(ExtractError, match="MB"):
        client.download(f"https://drive.google.com/file/d/{FID}/view")
    assert ("media", TARGET) not in files.calls


@pytest.mark.parametrize("meta, by_id, match", [
    ({"mimeType": drive.SHORTCUT}, {}, "no target"),
    (_shortcut(), {TARGET: _shortcut(FID)}, "another shortcut"),
    (_shortcut(), {TARGET: {"mimeType": "application/vnd.google-apps.folder"}},
     "unsupported"),
])
def test_broken_shortcuts_fail_permanently(meta, by_id, match):
    client, _ = _client(meta=meta, by_id=by_id)
    with pytest.raises(ExtractError, match=match):
        client.download(f"https://drive.google.com/file/d/{FID}/view")


def test_can_read_checks_the_shortcut_target():
    url = f"https://drive.google.com/file/d/{FID}/view"
    client, files = _client(meta=_shortcut(), by_id={
        TARGET: {"mimeType": "application/pdf", "capabilities": {"canDownload": True}}})
    assert client.can_read(url)
    assert files.calls == [("get", FID), ("get", TARGET)]
    shortcut = _shortcut()
    shortcut["shortcutDetails"]["targetResourceKey"] = "0-rk"
    client, files = _client(meta=shortcut, by_id={
        TARGET: {"mimeType": "application/pdf", "capabilities": {"canDownload": True}}})
    assert client.can_read(url) and _key_headers(files) == [None, f"{TARGET}/0-rk"]
    # the shortcut itself being downloadable says nothing about the target
    client, _ = _client(meta={**_shortcut(), "capabilities": {"canDownload": True}},
                        by_id={TARGET: {"mimeType": "application/pdf",
                                        "capabilities": {"canDownload": False}}})
    assert not client.can_read(url)
