"""Text extraction per resource type. No LLM here.

Contract with sync (important): content_hash stays the RAW-content hash
computed at sync time. Extraction only fills extracted_text + status, so the
next sync's hash check keeps working instead of re-queueing forever.

Outcomes per resource:
  extracted -> extracted_text set, status "extracted"
  unusable video / empty doc -> status "skipped" (never silently stuck)
  broken file -> status "failed" + error
"""

from __future__ import annotations

import codecs
import io
import re
import zipfile
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import parse_qs, urlparse

# Downloads above this are refused (Content-Length, then a streaming cap).
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
# DOCX/PPTX are zips: refuse ones that would inflate past this in memory.
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000
# Text past this is dropped (about a 250-page book): every 10k characters is
# a chunker call, and each chunk's quiz is billed again on top.
MAX_EXTRACTED_CHARS = 500_000
CAPPED_PREFIX = "Only the first "  # every cap note starts with this


class ExtractError(RuntimeError):
    pass


class SkipResource(RuntimeError):
    pass


def too_large_message(what: str = "file") -> str:
    return f"{what} is over {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB"


def strip_nul(text: str) -> str:
    """Drop NUL characters: Postgres text columns reject them."""
    return text.replace("\x00", "")


def cap_text(text: str) -> tuple[str, str | None]:
    """Strip NULs, then truncate text over MAX_EXTRACTED_CHARS, on a line break
    when one is close to the cap; returns (text, note), the note None when
    untouched by the cap. Every extractor's output passes through here."""
    text = strip_nul(text)
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text, None
    cut = text.rfind("\n", 0, MAX_EXTRACTED_CHARS)
    if cut < MAX_EXTRACTED_CHARS * 9 // 10:
        cut = MAX_EXTRACTED_CHARS
    return text[:cut], (f"{CAPPED_PREFIX}{cut:,} of {len(text):,} characters "
                        "are studied: the rest is past the size cap")


def is_cap_note(error: str | None) -> bool:
    """A cap note is not a failure: chunking and sharing keep it."""
    return bool(error) and error.startswith(CAPPED_PREFIX)


def _check_zip(blob: bytes, kind: str) -> None:
    """Refuse zip bombs before python-pptx/python-docx inflate them.

    Declared sizes are enough: zipfile stops reading a member at its declared
    file_size, so a member can't inflate past what the directory says."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            infos = z.infolist()
    except zipfile.BadZipFile as e:
        raise ExtractError(f"{kind} is not a valid file: {e}") from e
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ExtractError(f"{kind} has too many parts ({len(infos)})")
    if sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
        raise ExtractError(
            f"{kind} unpacks to over {MAX_UNZIPPED_BYTES // (1024 * 1024)} MB"
        )


def extract_pdf(blob: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(blob))
    pages = [(p.extract_text() or "") for p in reader.pages]
    text = "\n\n".join(t.strip() for t in pages if t.strip())
    if not text.strip():
        raise ExtractError("PDF has no extractable text (scanned images?)")
    return text


def extract_pptx(blob: bytes) -> str:
    from pptx import Presentation

    _check_zip(blob, "PPTX")
    prs = Presentation(io.BytesIO(blob))
    slides = []
    for i, slide in enumerate(prs.slides, 1):
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text.strip()
                if t:
                    parts.append(t)
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
        if parts:
            slides.append(f"--- Slide {i} ---\n" + "\n".join(parts))
    text = "\n\n".join(slides)
    if not text.strip():
        raise ExtractError("PPTX has no extractable text")
    return text


def extract_docx(blob: bytes) -> str:
    from docx import Document

    _check_zip(blob, "DOCX")
    doc = Document(io.BytesIO(blob))
    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n\n".join(parts)
    if not text.strip():
        raise ExtractError("DOCX has no extractable text")
    return text


TRANSCRIPT_LANGUAGES = ("en", "en-US", "en-GB")


def _fetch_transcript(api, video_id: str):
    """English transcript if there is one; otherwise whatever the video has,
    translated to English when YouTube can, else in its own language.
    Lists the video's captions once and picks from that list.
    Written against youtube-transcript-api 1.2.x (pinned in pyproject)."""
    from youtube_transcript_api import NoTranscriptFound, YouTubeTranscriptApiException

    transcripts = api.list(video_id)
    try:  # uploader-written before auto-generated, per language
        return transcripts.find_transcript(TRANSCRIPT_LANGUAGES).fetch()
    except NoTranscriptFound:
        pass
    available = list(transcripts)
    if not available:
        raise SkipResource(f"no transcripts listed for {video_id}")
    # Uploader-written captions beat auto-generated ones.
    transcript = min(available, key=lambda t: t.is_generated)
    if transcript.is_translatable:
        try:
            return transcript.translate("en").fetch()
        except YouTubeTranscriptApiException:
            pass  # translation is best-effort; fall back to the original
    return transcript.fetch()


_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# path prefixes that carry the id as the next segment: /shorts/<id> etc.
_ID_PATHS = {"shorts", "embed", "live", "v", "e"}


def youtube_video_id(url: str | None) -> str | None:
    """The video id of a YouTube video URL, from host + path (+ ?v=).

    None for anything else, including YouTube pages that aren't one video
    (channels, playlists, search)."""
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    for prefix in ("www.", "m.", "music."):
        host = host.removeprefix(prefix)
    parts = [p for p in parsed.path.split("/") if p]
    candidate = None
    if host == "youtu.be":
        candidate = parts[0] if parts else None
    elif host in ("youtube.com", "youtube-nocookie.com"):
        if parts[:1] == ["watch"]:
            candidate = parse_qs(parsed.query).get("v", [None])[0]
        elif len(parts) >= 2 and parts[0] in _ID_PATHS:
            candidate = parts[1]
    if candidate and _VIDEO_ID.fullmatch(candidate):
        return candidate
    return None


def extract_transcript(url: str) -> str:
    """YouTube transcript. Raises SkipResource when unavailable (v1 policy)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    try:
        video_id = youtube_video_id(url)
        if not video_id:
            raise SkipResource(f"cannot parse video id from {url}")
        transcript = _fetch_transcript(YouTubeTranscriptApi(), video_id)
        text = " ".join(s.text.strip() for s in transcript if s.text.strip())
        if not text.strip():
            raise SkipResource(f"empty transcript for {url}")
        return text
    except SkipResource:
        raise
    except Exception as e:
        raise SkipResource(f"no transcript for {url}: {e}") from e


_BOMS = (  # UTF-32 first: its LE BOM starts with UTF-16's
    (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode_text(blob: bytes) -> str:
    """Text file bytes -> str: BOM first, then UTF-8, then latin-1. NULs are
    dropped (Postgres text can't hold them)."""
    for bom, encoding in _BOMS:
        if blob.startswith(bom):
            text = blob.decode(encoding, errors="replace")
            break
    else:
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            text = blob.decode("latin-1")
    return strip_nul(text)


class _TextParser(HTMLParser):
    SKIP = {"script", "style", "head", "noscript", "template", "svg"}
    BLOCK = {"p", "div", "br", "hr", "li", "ul", "ol", "tr", "table", "section",
             "article", "header", "footer", "blockquote", "pre", "h1", "h2", "h3",
             "h4", "h5", "h6", "dt", "dd", "figcaption", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n- " if tag == "li" else "\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK and tag != "li":  # the next <li> breaks the line
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Readable text from an HTML page: tags, scripts and styles dropped,
    entities decoded, block elements on their own lines."""
    parser = _TextParser()
    parser.feed(strip_nul(html))
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    text = "\n".join(line.removeprefix("| ") for line in lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_bytes(blob: bytes, mime: str | None, filename: str = "") -> str:
    name = filename.lower()
    mime = (mime or "").lower()
    if "pdf" in mime or name.endswith(".pdf"):
        return extract_pdf(blob)
    if "presentation" in mime or name.endswith(".pptx"):
        return extract_pptx(blob)
    if "wordprocessing" in mime or name.endswith(".docx"):
        return extract_docx(blob)
    if "html" in mime or name.endswith((".html", ".htm")):
        return html_to_text(decode_text(blob))
    if mime.startswith("text/") or name.endswith((".txt", ".md", ".csv")):
        return decode_text(blob)
    raise ExtractError(f"unsupported type (mime={mime or '?'}, file={filename or '?'})")


Downloader = Callable[[str], "tuple[bytes, str | None]"]


def extract_resource_text(resource, downloader: Downloader | None = None) -> str:
    """Return extracted text for a Resource row. Raises SkipResource/ExtractError."""
    if resource.type == "page_text":
        if resource.extracted_text:
            return resource.extracted_text
        raise SkipResource("page had no retrievable content at sync time")
    if resource.type == "video":
        if not resource.raw_url:
            raise SkipResource("video has no URL")
        return extract_transcript(resource.raw_url)
    if resource.type == "link":
        raise SkipResource("web links aren't read, only files, pages and videos")
    if resource.type == "file":
        if downloader is None:
            raise ExtractError("no downloader available for file resource")
        filename = (resource.raw_url or "").split("?")[0].rsplit("/", 1)[-1]
        blob, mime = downloader(resource.raw_url)
        return extract_bytes(blob, mime or resource.mime_type, filename)
    raise ExtractError(f"unknown resource type {resource.type!r}")
