"""LLM semantic chunking. One cheap-model call per section.

- Long docs (>MAX_SECTION_CHARS) are pre-split on paragraph boundaries
  before chunking, since chunk quality degrades on very long inputs. Text
  without blank lines (transcripts) falls back to lines, then spaces. A
  section whose reply is truncated is halved and retried.
- At most MAX_SECTIONS calls are made per resource, retried halves included:
  a backstop behind the extraction cap (app.extract.MAX_EXTRACTED_CHARS) for
  text extracted before it, and for replies that keep truncating.
- Short texts skip the LLM entirely (single chunk, no cost).
- Cache: resources that already have chunks and status "extracted" are
  skipped; status "pending" with stale chunks means content changed ->
  regenerate, swapping the old chunks out only once all sections succeeded
  (no versioning, per plan). Blank text is "skipped", never an empty chunk.
- Offsets are located locally with str.find (model ranges are unreliable),
  so Chunk.start_char/end_char support a future "show source" UI.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.extract import CAPPED_PREFIX, is_cap_note
from app.llm import LLMClient, TruncatedError
from app.llm_schemas import ChunkReply
from app.models import Chunk

MAX_SECTION_CHARS = 10000
# LLM calls per resource; presplit packs paragraphs greedily, so capped text
# stays well under this
MAX_SECTIONS = 100
MIN_LLM_CHARS = 300
MIN_SPLIT_CHARS = 1000  # a truncated reply on a shorter section is an error


class ChunkingError(RuntimeError):
    """The chunker returned no usable chunks; existing chunks are kept."""

SYSTEM = """You split study material into coherent chunks for quiz generation.
Rules:
- Each chunk covers ONE concept: not too granular, not too broad.
- "content" must be copied VERBATIM from the source text (no rewording, no summarizing).
- "title" is a short label for the concept.
- Skip boilerplate (headers, page numbers, reference lists) — fewer good chunks beat filler.
- Return JSON: {"chunks": [{"title": ..., "content": ...}]}"""


def presplit(text: str, max_chars: int = MAX_SECTION_CHARS,
             seps: tuple[str, ...] = ("\n\n", "\n", " ")) -> list[str]:
    """Sections of at most max_chars, packed from paragraphs; a piece still
    too long is split on the next separator, and as a last resort cut."""
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    if not seps:
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]
    sep, rest = seps[0], seps[1:]
    sections, current = [], ""
    for part in text.split(sep):
        if not part.strip():
            continue
        for piece in presplit(part, max_chars, rest):
            if current and len(current) + len(sep) + len(piece) > max_chars:
                sections.append(current)
                current = ""
            current = f"{current}{sep}{piece}" if current else piece
    if current.strip():
        sections.append(current)
    return sections


def _norm_with_map(s: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to single spaces; return normed + index map."""
    norm_chars, index_map = [], []
    prev_space = True  # strip leading whitespace
    for i, ch in enumerate(s):
        if ch.isspace():
            if not prev_space:
                norm_chars.append(" ")
                index_map.append(i)
            prev_space = True
        else:
            norm_chars.append(ch)
            index_map.append(i)
            prev_space = False
    if norm_chars and norm_chars[-1] == " ":  # strip trailing
        norm_chars.pop()
        index_map.pop()
    return "".join(norm_chars), index_map


def locate(content: str, full: str) -> tuple[int | None, int | None]:
    start = full.find(content)
    if start >= 0:
        return start, start + len(content)
    # fallback: whitespace-insensitive match, mapped back to original offsets
    needle = " ".join(content.split())
    if not needle:
        return None, None
    norm_full, index_map = _norm_with_map(full)
    at = norm_full.find(needle)
    if at < 0:
        return None, None
    start_orig = index_map[at]
    end_orig = index_map[at + len(needle) - 1] + 1
    return start_orig, end_orig


def chunk_sections(sections: list[str], llm: LLMClient,
                   max_calls: int = MAX_SECTIONS) -> tuple[list[dict], int]:
    """Chunk sections in order with at most max_calls LLM calls, halves of a
    truncated section included. Returns (chunks, sections left unstudied)."""
    out, calls = [], 0
    todo = [(i, s) for i, s in reversed(list(enumerate(sections)))]  # a stack
    while todo:
        if calls >= max_calls:
            return out, len({i for i, _ in todo})
        i, section = todo.pop()
        calls += 1
        try:
            data = llm.complete_json(
                SYSTEM,
                f"Split the following study material (part {i + 1}/{len(sections)}):"
                f"\n\n{section}",
                required_key="chunks",
            )
        except TruncatedError:
            if len(section) < MIN_SPLIT_CHARS:
                raise
            # the reply outgrew the output limit: two halves fit
            halves = presplit(section, len(section) // 2 + 1)
            todo.extend((i, h) for h in reversed(halves))
            continue
        out.extend(c.model_dump() for c in ChunkReply.model_validate(data).chunks)
    return out, 0


def needs_llm(resource) -> bool:
    return len(resource.extracted_text or "") >= MIN_LLM_CHARS


def chunk_resource(session: Session, resource, llm: LLMClient | None = None) -> int:
    """Chunk one extracted resource. Returns number of chunks created.

    Returns 0 without touching anything when cached, or when the text is long
    enough to need an LLM and none was given (extract-only runs must not mark
    resources failed or drop their existing chunks). LLM errors and an empty
    result (ChunkingError) raise before any existing chunk is touched, and
    ContentChanged rolls everything back when a sync replaced the content
    meanwhile (the chunks would be of the old text).
    """
    from app.sync import ContentChanged, _purge_derived, commit_if_current

    existing = session.exec(
        select(Chunk).where(Chunk.resource_id == resource.id)
    ).all()
    if existing and resource.status == "extracted":
        return 0  # cached
    if llm is None and needs_llm(resource):
        return 0  # leave as-is for a run that has an LLM

    text = resource.extracted_text or ""
    note = resource.error if is_cap_note(resource.error) else None
    seen_hash = resource.content_hash  # loaded with the text: what it is of
    if not text.strip():
        items = []
    elif needs_llm(resource):
        sections = presplit(text)
        items, left = chunk_sections(sections, llm)  # paid work: before any delete
        if left:
            note = (f"{CAPPED_PREFIX}{len(sections) - left} of {len(sections)} "
                    "sections are studied: the rest is past the size cap")
        if not items:
            raise ChunkingError("chunker produced no chunks")
    else:
        items = [{"title": text.strip()[:80], "content": text}]
    # content changed -> regenerate, don't version; explicit purge so quiz
    # items and reviews of the old chunks go too on SQLite (no FK cascade)
    if existing:
        _purge_derived(session, resource.id)
    for order, item in enumerate(items):
        start, end = locate(item["content"], text)
        session.add(
            Chunk(
                resource_id=resource.id, title=item["title"],
                content=item["content"], order=order,
                start_char=start, end_char=end,
            )
        )
    resource.status = "extracted" if items else "skipped"
    resource.error = note if items else "no text to chunk"
    session.add(resource)
    # old chunks out, new ones in: one transaction, kept only if still current
    if not commit_if_current(session, resource.id, seen_hash):
        raise ContentChanged(resource.id)
    return len(items)
