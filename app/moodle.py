"""Moodle web-services client. Token auth, REST/JSON, stdlib only.

Key API facts (easy to get wrong):
- Every call is a POST to /webservice/rest/server.php with
  wstoken + wsfunction + moodlewsrestformat=json.
- Errors come back as HTTP 200 with an {"exception": ...} body.
- File download: the `fileurl` from core_course_get_contents alone serves
  a login page. Append ?token=YOUR_TOKEN (or &token=) to get bytes.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from app.extract import MAX_DOWNLOAD_BYTES, ExtractError, html_to_text, too_large_message


class MoodleError(RuntimeError):
    def __init__(self, message: str, errorcode: str | None = None):
        super().__init__(message)
        self.errorcode = errorcode  # Moodle's errorcode, when Moodle sent one


class TokenRejected(MoodleError):
    """Moodle no longer accepts the token (expired, revoked by an admin, or
    reset by the student): retrying can't help until they reconnect."""


# Moodle's errorcode for a token it doesn't know (any more)
REJECTED_TOKEN_ERRORS = {"invalidtoken"}
# An expired token fails as "accessexception", but so does a live one calling
# a function its service doesn't allow. site_info is allowed in every
# service, so only there does it mean the token itself was refused.
SITE_INFO = "core_webservice_get_site_info"


class ForeignURLError(MoodleError):
    """A download URL outside this Moodle's pluginfile endpoints. Never
    fetched: the token would be sent along with it."""


class MoodleClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        if not token:
            raise MoodleError("Moodle token is empty — set MOODLE_TOKEN in .env")
        self.base_url = base_url.rstrip("/")
        # Tolerate the token endpoint being pasted as the base URL.
        if self.base_url.endswith("/login/token.php"):
            self.base_url = self.base_url[: -len("/login/token.php")]
        self.token = token
        self.timeout = timeout

    def call(self, function: str, **params):
        """Call a web-service function, return decoded JSON."""
        payload = {
            "wstoken": self.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
            **{k: v for k, v in params.items()},
        }
        data = urllib.parse.urlencode(payload, doseq=True).encode()
        req = urllib.request.Request(
            f"{self.base_url}/webservice/rest/server.php", data=data, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except Exception as e:
            raise MoodleError(f"{function} request failed: {e}") from e
        if isinstance(body, dict) and body.get("exception"):
            code = body.get("errorcode")
            rejected = code in REJECTED_TOKEN_ERRORS or (
                code == "accessexception" and function == SITE_INFO)
            error = TokenRejected if rejected else MoodleError
            raise error(f"{function}: {code}: {body.get('message')}", errorcode=code)
        return body

    # -- capability probe: run first, confirms which functions this token may call
    def site_info(self):
        return self.call(SITE_INFO)

    def get_users_courses(self, userid: int):
        return self.call("core_enrol_get_users_courses", userid=userid)

    def get_course_contents(self, courseid: int):
        return self.call("core_course_get_contents", courseid=courseid)

    def get_assignments(self, *courseids: int):
        params = {f"courseids[{i}]": c for i, c in enumerate(courseids)}
        return self.call("mod_assign_get_assignments", **params)

    def serves(self, fileurl: str) -> bool:
        """Whether fileurl is one of this site's pluginfile.php endpoints."""
        base = urllib.parse.urlsplit(self.base_url)
        url = urllib.parse.urlsplit(fileurl)
        prefix = base.path.rstrip("/")
        return (
            url.scheme in (base.scheme, "https")
            and url.netloc.lower() == base.netloc.lower()
            and url.path.startswith((f"{prefix}/webservice/pluginfile.php/",
                                     f"{prefix}/pluginfile.php/"))
        )

    def download(self, fileurl: str) -> tuple[bytes, str | None]:
        """Download a fileurl. Returns (bytes, mime_type).

        Over MAX_DOWNLOAD_BYTES raises ExtractError (permanent): refused on
        Content-Length when sent, else once the streamed body passes the cap."""
        if not self.serves(fileurl):
            raise ForeignURLError(f"refusing to send the Moodle token to {fileurl}")
        sep = "&" if "?" in fileurl else "?"
        url = f"{fileurl}{sep}token={urllib.parse.quote(self.token)}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                mime = resp.headers.get_content_type()
                length = resp.headers.get("Content-Length", "")
                if length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES:
                    raise ExtractError(too_large_message())
                parts, size = [], 0
                while chunk := resp.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ExtractError(too_large_message())
                    parts.append(chunk)
                blob = b"".join(parts)
        except ExtractError:
            raise
        except Exception as e:
            raise MoodleError(f"download failed for {fileurl}: {e}") from e
        if mime == "text/html" and blob.lstrip()[:1] == b"<":
            raise MoodleError(
                f"download returned a login page for {fileurl} — token rejected?"
            )
        return blob, mime


_WS_FILES = "/webservice/pluginfile.php/"
_FILES = "/pluginfile.php/"
_TOKEN_PARAMS = {"token", "wstoken", "forcedownload"}


def browser_url(fileurl: str) -> str:
    """The URL a signed-in student opens for a webservice fileurl.

    /webservice/pluginfile.php/ serves only with a token, which must never
    reach a link; /pluginfile.php/ serves the same file to the browser's own
    Moodle session. Token/forcedownload query params are dropped. Anything that
    isn't a pluginfile URL comes back unchanged."""
    url = urllib.parse.urlsplit(fileurl)
    head, ws, tail = url.path.partition(_WS_FILES)
    path = f"{head}{_FILES}{tail}" if ws else url.path
    if _FILES not in path:
        return fileurl
    query = urllib.parse.urlencode([
        (k, v) for k, v in urllib.parse.parse_qsl(url.query, keep_blank_values=True)
        if k.lower() not in _TOKEN_PARAMS
    ])
    return urllib.parse.urlunsplit(url._replace(path=path, query=query))


# -- adapter: API responses -> app.sync dataclasses ----------------------------

from app.sync import (  # noqa: E402
    AssignmentData,
    CourseData,
    PartialAssignments,
    ResourceData,
    TopicData,
    link_type,
)


def _file_data(topic_source_id: str, source_id: str, title: str, c: dict) -> ResourceData:
    return ResourceData(
        topic_source_id=topic_source_id,
        source_id=source_id,
        type="file",
        title=title,
        raw_url=c["fileurl"],
        mime_type=c.get("mimetype"),
        fingerprint=_file_fingerprint(c),
        size=c.get("filesize"),
    )


def _file_fingerprint(c: dict) -> str:
    """Change marker from core_course_get_contents file metadata, so sync
    never downloads files (extraction fetches bytes later via raw_url).

    The API exposes no content hash, but fileurl embeds the module revision
    (.../mod_resource/content/<rev>/...), which Moodle bumps on every save, so
    a replaced file changes the marker even at the same size and mtime.
    """
    return "|".join(
        str(c.get(k) or "") for k in ("fileurl", "filesize", "timemodified")
    )


class MoodleAdapter:
    """Maps Moodle API shapes onto the normalized sync dataclasses.

    Course contents and page texts are fetched once per course and reused by
    fetch_topics/fetch_resources/fetch_assignments. fetch_topics (the first
    call sync makes per course) refreshes the snapshot.
    """

    source = "moodle"

    def __init__(self, client: MoodleClient):
        self.client = client
        self._contents: dict[str, list] = {}
        self._pages: dict[str, dict[int, str | None] | None] = {}

    def _course_contents(self, course_source_id: str, refresh: bool = False) -> list:
        if refresh or course_source_id not in self._contents:
            self._contents[course_source_id] = self.client.get_course_contents(
                int(course_source_id)
            )
            self._pages.pop(course_source_id, None)
        return self._contents[course_source_id]

    def fetch_courses(self) -> list[CourseData]:
        info = self.client.site_info()
        courses = self.client.get_users_courses(info["userid"])
        return [
            CourseData(
                source_id=str(c["id"]),
                name=c.get("fullname") or c.get("shortname", ""),
                code=c.get("shortname"),
            )
            for c in courses
        ]

    def fetch_topics(self, course_source_id: str) -> list[TopicData]:
        sections = self._course_contents(course_source_id, refresh=True)
        topics = []
        for i, s in enumerate(sections):
            name = (s.get("name") or "").strip()
            topics.append(
                TopicData(
                    source_id=str(s["id"]),
                    title=name or f"Section {i}",
                    order=i,
                )
            )
        return topics

    def _page_text(self, course_source_id: str, module_id: int) -> str | None:
        """Best-effort page content. None when it couldn't be fetched this run
        (function not allowed, request failed, page missing from the reply)."""
        if course_source_id not in self._pages:
            try:
                pages = self.client.call(
                    "mod_page_get_pages_by_courses",
                    **{"courseids[0]": int(course_source_id)},
                )
                self._pages[course_source_id] = {
                    p.get("coursemodule"): p.get("content")
                    for p in pages.get("pages", [])
                }
            except MoodleError:
                self._pages[course_source_id] = None
        by_module = self._pages[course_source_id]
        return None if by_module is None else by_module.get(module_id)

    def fetch_resources(
        self, course_source_id: str, topic_source_id: str
    ) -> list[ResourceData]:
        sections = self._course_contents(course_source_id)
        section = next((s for s in sections if str(s["id"]) == topic_source_id), None)
        if section is None:
            return []
        out: list[ResourceData] = []
        for mod in section.get("modules", []):
            modname = mod.get("modname")
            if modname in ("assign", "label", "forum", "quiz", "feedback", "choice"):
                continue  # not learnable content (assign handled separately)
            if modname == "folder":
                for c in mod.get("contents", []):
                    if c.get("type") != "file":
                        continue
                    out.append(_file_data(
                        topic_source_id,
                        f"{mod['id']}:{c.get('filepath', '/')}{c['filename']}",
                        c["filename"], c,
                    ))
            elif modname == "url":
                contents = mod.get("contents", [])
                target = contents[0]["fileurl"] if contents else None
                if not target:
                    continue
                out.append(
                    ResourceData(
                        topic_source_id=topic_source_id,
                        source_id=str(mod["id"]),
                        type=link_type(target),
                        title=mod.get("name", target),
                        raw_url=target,
                        content_bytes=target.encode("utf-8"),  # hash the URL
                    )
                )
            elif modname == "page":
                html = self._page_text(course_source_id, mod["id"])
                out.append(
                    ResourceData(
                        topic_source_id=topic_source_id,
                        source_id=str(mod["id"]),
                        type="page_text",
                        title=mod.get("name", "Page"),
                        raw_url=(mod.get("url")),
                        # Hash the raw HTML (as before this was converted), store
                        # the text. No HTML -> no hash: sync keeps what it has
                        # instead of treating an outage as an edit that purges
                        # progress.
                        content_bytes=None if html is None else html.encode("utf-8"),
                        text=None if html is None else html_to_text(html),
                    )
                )
            elif modname == "resource":
                files = [c for c in mod.get("contents", []) if c.get("type") == "file"]
                if not files:
                    continue
                if len(files) == 1:
                    c = files[0]
                    out.append(_file_data(
                        topic_source_id, str(mod["id"]),
                        mod.get("name") or c["filename"], c,
                    ))
                else:  # same per-file treatment as folders
                    for c in files:
                        out.append(_file_data(
                            topic_source_id,
                            f"{mod['id']}:{c.get('filepath', '/')}{c['filename']}",
                            c["filename"], c,
                        ))
            # else: unknown modname — skip silently in v1 (visible via counts)
        return out

    def fetch_content(self, data: ResourceData) -> bytes:
        """File bytes; sync uses this only to verify legacy content hashes."""
        return self.client.download(data.raw_url)[0]

    def fetch_assignments(self, course_source_id: str) -> list[AssignmentData]:
        from datetime import datetime, timezone

        sections = self._course_contents(course_source_id)
        cmid_to_section = {
            str(mod["id"]): str(s["id"])
            for s in sections
            for mod in s.get("modules", [])
        }
        resp = self.client.get_assignments(int(course_source_id))
        out: list[AssignmentData] = []
        for course in resp.get("courses", []):
            for a in course.get("assignments", []):
                due = a.get("duedate") or 0
                out.append(
                    AssignmentData(
                        source_id=str(a["id"]),
                        title=a.get("name", "Assignment"),
                        topic_source_id=cmid_to_section.get(str(a.get("cmid"))),
                        due_date=(
                            datetime.fromtimestamp(due, timezone.utc) if due else None
                        ),
                        description=a.get("intro"),
                    )
                )
        # warnings ride along with a 200 (e.g. no access to some context):
        # keep what came back, but don't treat it as the full list
        return PartialAssignments(out) if resp.get("warnings") else out
