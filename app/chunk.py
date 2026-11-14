"""LLM semantic chunking. One cheap-model call per section.

- Long docs (>MAX_SECTION_CHARS) are pre-split on paragraph boundaries
  before chunking, since chunk quality degrades on very long inputs. Text
  without blank lines (transcripts) falls back to lines, then spaces. A
  section whose reply is truncated is halved and retried.
- At most MAX_SECTIONS calls are made per resource, retried halves included:
  a backstop behind the extraction cap (app.extract.MAX_EXTRACTED_CHARS) for
  text extracted before it, and for replies that keep truncating. A cap stop
  is permanent, so its partial result is final; a deadline stop is transient,
  so it raises and stores nothing, letting a later run start over.
- Short texts skip the LLM entirely (single chunk, no cost).
- Cache: resources that already have chunks and status "extracted" are
  skipped; status "pending" with stale chunks means content changed ->
  regenerate, swapping the old chunks out only once all sections succeeded
  (no versioning, per plan). Blank text is "skipped", never an empty chunk.
- Offsets are located locally with str.find (model ranges are unreliable),
  so Chunk.start_char/end_char support a future "show source" UI.
"""

from __future__ import annotations

import hashlib

from sqlmodel import Session, select

from app.extract import CAPPED_PREFIX, is_cap_note
from app.jobs import Shutdown
from app.llm import LLMClient, TruncatedError
from app.llm_schemas import ChunkReply
from app.models import Chunk

MAX_SECTION_CHARS = 10000
# LLM calls per resource; presplit packs paragraphs greedily, so capped text
# stays well under this
MAX_SECTIONS = 100
MIN_LLM_CHARS = 300
MIN_SPLIT_CHARS = 1000  # a truncated reply on a shorter section is an error
SECTIONS_CAPPED = "sections are studied: the rest is past the size cap"
SECTIONS_JOIN = ". Of those, "  # character cap note + section cap note
FAILED_JOIN = ". Chunking failed: "  # character cap note + retryable error


class ChunkingError(RuntimeError):
    """The chunker returned no usable chunks; existing chunks are kept."""


class ChunkingDeadline(RuntimeError):
    """The run's time budget ran out mid-resource. Completed sections are
    preserved as a checkpoint (see chunk_resource) so the next run resumes
    instead of restarting; the checkpoint is not final, so its chunks are
    not stored until every section has succeeded."""

    def __init__(self, message="time budget exhausted", completed=None,
                 resume_from=0, calls=0, in_flight=None):
        super().__init__(message)
        self.completed = list(completed or [])
        # aliases for callers/tests that name these differently
        self.completed_chunks = self.completed
        self.chunks = self.completed
        self.resume_from = resume_from
        self.next_section = resume_from
        self.resume_at = resume_from
        # cumulative LLM calls for the resource, across resumed runs
        self.calls = calls
        # a section left mid-split: {"index", "partial" (finished halves'
        # chunks), "pending" (halves not yet sent)}, or None
        self.in_flight = in_flight

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
                   max_calls: int = MAX_SECTIONS,
                   deadline: float | None = None,
                   resume_from: int = 0,
                   calls_before: int = 0,
                   resume_state: dict | None = None) -> tuple[list[dict], int]:
    """Chunk sections in order with at most max_calls LLM calls, halves of a
    truncated section included. calls_before counts calls already made for
    this resource by earlier (deadline-stopped) runs, so the cap is per
    resource, not per run. Returns (chunks, sections left unstudied) for a
    cap stop. Raises ChunkingDeadline when the deadline passes: a deadline
    stop must not be stored as a final, cached result."""
    import time
    out, calls = [], calls_before
    # only sections not already checkpointed are processed
    todo = [(i, s) for i, s in reversed(list(enumerate(sections)))
            if i >= resume_from]  # a stack
    current_i = None
    current_chunks: list[dict] = []
    # next original section to process (resume point while one is in flight)
    next_section = resume_from
    if resume_state:
        # a section was mid-split at the last stop: its finished halves and
        # its unsent halves come back, so no call is made twice
        idx = int(resume_state["index"])
        todo = [t for t in todo if t[0] != idx]
        todo.extend((idx, h) for h in reversed(resume_state["pending"]))
        current_i = idx
        current_chunks = list(resume_state.get("partial") or [])
        next_section = idx
    def _in_flight(unsent_now: list[str] | None = None) -> dict | None:
        """The section being split, for a checkpoint: its finished halves and
        its unsent ones, next one first. unsent_now: a section already taken
        off todo for a call that a stop interrupted."""
        if current_i is None:
            return None
        return {
            "index": current_i,
            "partial": current_chunks,
            "pending": list(unsent_now or [])
            + [s for j, s in reversed(todo) if j == current_i],
        }

    while todo:
        if calls >= max_calls:
            # cap stop is final: keep whatever is in flight too
            return out + current_chunks, len({i for i, _ in todo})
        if deadline is not None and time.monotonic() >= deadline:
            raise ChunkingDeadline(
                "time budget exhausted", completed=out, resume_from=next_section,
                calls=calls, in_flight=_in_flight())
        i, section = todo.pop()
        # a new original section starts: the previous one is complete
        if current_i is not None and i != current_i:
            out.extend(current_chunks)
            current_chunks = []
            next_section = i
        current_i = i
        next_section = i
        calls += 1
        try:
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
                # section stays in flight until all of its halves are done
                continue
            chunks = [c.model_dump() for c in ChunkReply.model_validate(data).chunks]
            current_chunks.extend(chunks)
        except Shutdown as e:
            # a SIGTERM mid-call: this section was taken off todo, so it is
            # unsent and must come back first on resume. chunk_resource saves it.
            e.checkpoint = ChunkingDeadline(
                "stopped", completed=out, resume_from=next_section, calls=calls,
                in_flight=_in_flight([section]))
            raise
        # if nothing with the same index remains, this section is complete
        if not any(x[0] == i for x in todo):
            out.extend(current_chunks)
            current_chunks = []
            current_i = None
            next_section = i + 1
    # promote anything left in flight (last section)
    out.extend(current_chunks)
    return out, 0


def text_cap_note(error: str | None) -> str | None:
    """The extraction (character) cap note in error, minus any section cap
    note or chunking error added since, so re-chunking never stacks them."""
    if not is_cap_note(error):
        return None
    note = error.split(FAILED_JOIN)[0].split(SECTIONS_JOIN)[0]
    return None if note.endswith(SECTIONS_CAPPED) else note


def with_text_cap_note(error: str | None, note: str | None) -> str | None:
    """error with its extraction cap note replaced by note (None: removed),
    keeping any section cap note or chunking failure after it. For a text
    refresh that keeps the chunks: what was said about them still holds."""
    old = text_cap_note(error)
    rest = error[len(old):] if old else (error or "")
    if old is None and is_cap_note(error):  # section cap note alone
        rest = SECTIONS_JOIN + rest[len(CAPPED_PREFIX):]
    elif old is None and rest:  # a failure alone
        rest = FAILED_JOIN + rest
    if note:
        return note + rest
    if rest.startswith(SECTIONS_JOIN):
        return CAPPED_PREFIX + rest[len(SECTIONS_JOIN):]
    if rest.startswith(FAILED_JOIN):
        return rest[len(FAILED_JOIN):]
    return rest or None


def needs_llm(resource) -> bool:
    return len(resource.extracted_text or "") >= MIN_LLM_CHARS


def _checkpoint(stop: ChunkingDeadline, text_id: str, completed_chunks: list) -> dict:
    """The resumable checkpoint for a stop: sections done so far, not final chunks."""
    return {
        "text_id": text_id,
        "next_section": int(stop.resume_from),
        "chunks": completed_chunks + list(stop.completed),
        "calls": int(stop.calls),
        "in_flight": stop.in_flight,
    }


def chunk_resource(session: Session, resource, llm: LLMClient | None = None,
                   deadline: float | None = None) -> int:
    """Chunk one extracted resource. Returns number of chunks created.

    Returns 0 without touching anything when cached, or when the text is long
    enough to need an LLM and none was given (extract-only runs must not mark
    resources failed or drop their existing chunks). LLM errors, a deadline
    stop (ChunkingDeadline) and an empty result (ChunkingError) raise before
    any existing chunk is touched, and ContentChanged rolls everything back
    when a sync replaced the content meanwhile (the chunks would be of the
    old text).
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
    note = text_cap_note(resource.error)
    seen_hash = resource.content_hash  # loaded with the text: what it is of
    # a previous deadline stop may have preserved a checkpoint; resume from it
    checkpoint = getattr(resource, "chunk_checkpoint", None)
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    text_id = hashlib.sha256(text.encode()).hexdigest()
    if checkpoint.get("text_id") != text_id:
        checkpoint = {}  # made for different text: start over
    resume_from = int(checkpoint.get("next_section", 0) or 0)
    completed_chunks = list(checkpoint.get("chunks") or [])
    prior_calls = int(checkpoint.get("calls", 0) or 0)
    in_flight = checkpoint.get("in_flight")
    if not isinstance(in_flight, dict):
        in_flight = None
    if not text.strip():
        items = []
        resource.chunk_checkpoint = None
    elif needs_llm(resource):
        sections = presplit(text)
        # if the checkpoint already covers everything, just finalize it
        try:
            new_chunks, left = chunk_sections(
                sections, llm, deadline=deadline, resume_from=resume_from,
                calls_before=prior_calls, resume_state=in_flight,
            )  # paid work: before any delete
        except ChunkingDeadline as e:
            # preserve the sections done so far as a resumable checkpoint,
            # without storing (or marking) them as final chunks
            resource.chunk_checkpoint = _checkpoint(e, text_id, completed_chunks)
            session.add(resource)
            if not commit_if_current(session, resource.id, seen_hash, seen_text=text):
                raise ContentChanged(resource.id) from e
            raise
        except Shutdown as e:
            # SIGTERM (a Cloud Run timeout, a redeploy): keep the finished
            # sections for the next run. The stop itself still goes through.
            stop = getattr(e, "checkpoint", None)
            if stop is not None:
                resource.chunk_checkpoint = _checkpoint(stop, text_id, completed_chunks)
                session.add(resource)
                commit_if_current(session, resource.id, seen_hash, seen_text=text)
            raise
        items = completed_chunks + new_chunks
        if left:
            capped = f"{len(sections) - left} of {len(sections)} {SECTIONS_CAPPED}"
            # keep both: the text was cut, then only part of what's left chunked
            note = (f"{note}{SECTIONS_JOIN}{capped}" if note
                    else f"{CAPPED_PREFIX}{capped}")
        if not items:
            raise ChunkingError("chunker produced no chunks")
        # finished: drop any checkpoint
        resource.chunk_checkpoint = None
    else:
        items = [{"title": text.strip()[:80], "content": text}]
        resource.chunk_checkpoint = None
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
    if not commit_if_current(session, resource.id, seen_hash, seen_text=text):
        raise ContentChanged(resource.id)
    return len(items)
