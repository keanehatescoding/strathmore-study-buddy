"""Reuse work another user's copy of the same material already paid for.

Every enrolled user syncs their own Course/Topic/Resource rows, so without
this each one pays the download, the chunker call and the quiz calls again.
Rows stay per user (review states, purges and retirement are untouched); the
pipeline copies a donor's results instead of redoing them:

- extraction: a donor with the same (source, type, content_hash) whose text
  is extracted. Hashes are the source's change markers (app.sync), so equal
  hashes mean the same file the user's own source listed to them. Classroom
  Drive files are the exception: they hash as their Drive id, which a course
  can list without the user's grant being able to open the file, so those are
  shared only when the user's own downloader confirms it can (`can_read`).
- chunks: a donor with byte-identical extracted_text that is chunked, so the
  copied offsets are valid in the user's text.
- quiz items: a donor chunk with identical content, under a resource of the
  same material (source, type, hash), that has a QuizAttempt for the
  attempt (copied even when it yielded no items, so nothing-quizzable chunks
  aren't re-billed either).

Chunks and quiz items are only ever copied from text the user already has, so
sharing them can't reveal anything new.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.extract import is_cap_note
from app.models import Chunk, QuizAttempt, QuizItem, Resource


def _same_material(r: Resource, *cols):
    return select(*(cols or (Resource,))).where(
        Resource.content_hash == r.content_hash, Resource.source == r.source,
        Resource.type == r.type, Resource.id != r.id,
    ).order_by(Resource.id)


def copy_extraction(session: Session, r: Resource, downloader=None) -> bool:
    """Take a donor's extracted text; True when one was found. Uncommitted.

    `downloader` is the one the user's own download would use; a Classroom
    file is shared only when it has a `can_read` that approves the file."""
    if r.content_hash is None:
        return False
    if r.source == "classroom" and r.type == "file":
        can_read = getattr(downloader, "can_read", None)
        if can_read is None or not _donor_exists(session, r) or not can_read(r.raw_url):
            return False
    # streamed text only: a blank donor must not end the search, and every
    # matching copy carries a whole document
    texts = session.exec(_same_material(r, Resource.extracted_text, Resource.error).where(
        Resource.status == "extracted", Resource.extracted_text.is_not(None),
    ).execution_options(yield_per=1))
    with texts:  # closes the server-side cursor when a donor is found early
        for text, error in texts:
            if text.strip():
                r.extracted_text = text
                r.status = "extracted"
                r.error = error if is_cap_note(error) else None
                return True
    return False


def _donor_exists(session: Session, r: Resource) -> bool:
    """Cheap pre-check so the Drive call is only made when it could pay off."""
    return session.exec(_same_material(r, Resource.id).where(
        Resource.status == "extracted", Resource.extracted_text.is_not(None),
    ).limit(1)).first() is not None


def copy_chunks(session: Session, r: Resource) -> int:
    """Copy a chunked donor's chunks onto r, replacing what r has; returns the
    number copied (0 = no donor, nothing touched). Commits; raises
    ContentChanged (nothing kept) if a sync replaced r's content meanwhile."""
    from app.sync import ContentChanged, _purge_derived, commit_if_current

    if r.content_hash is None or not (r.extracted_text or "").strip():
        return 0
    has_chunks = select(Chunk.id).where(Chunk.resource_id == Resource.id).exists()
    donor = session.exec(_same_material(r).where(
        Resource.status == "extracted", Resource.extracted_text == r.extracted_text,
        has_chunks,
    ).limit(1)).first()
    if donor is None:
        return 0
    chunks = session.exec(
        select(Chunk).where(Chunk.resource_id == donor.id).order_by(Chunk.order)
    ).all()
    _purge_derived(session, r.id)  # stale chunks from older content, if any
    for c in chunks:
        session.add(Chunk(resource_id=r.id, title=c.title, content=c.content,
                          order=c.order, start_char=c.start_char, end_char=c.end_char))
    seen_hash = r.content_hash
    r.status = "extracted"
    if not is_cap_note(r.error):
        r.error = None
    session.add(r)
    if not commit_if_current(session, r.id, seen_hash):
        raise ContentChanged(r.id)
    return len(chunks)


def copy_quiz(session: Session, chunk: Chunk, attempt: int = 1) -> list[QuizItem] | None:
    """Copy a donor chunk's quiz items for `attempt`; None when there is no
    donor, else the copied items (possibly none). Commits."""
    r = session.get(Resource, chunk.resource_id)
    if r is None or r.content_hash is None:
        return None
    tried = select(QuizAttempt.chunk_id).where(
        QuizAttempt.chunk_id == Chunk.id, QuizAttempt.attempt == attempt
    ).exists()
    donor = session.exec(
        select(Chunk).join(Resource, Resource.id == Chunk.resource_id).where(
            Resource.content_hash == r.content_hash, Resource.source == r.source,
            Resource.type == r.type, Chunk.id != chunk.id, Chunk.content == chunk.content, tried,
        ).order_by(Chunk.id).limit(1)
    ).first()
    if donor is None:
        return None
    donor_key, key = f"{donor.id}:{attempt}", f"{chunk.id}:{attempt}"
    created = []
    for item in session.exec(
        select(QuizItem).where(QuizItem.chunk_id == donor.id).order_by(QuizItem.generation_key)
    ):
        if item.generation_key != donor_key and not item.generation_key.startswith(
                donor_key + ":"):
            continue  # another attempt's items
        row = QuizItem(
            chunk_id=chunk.id, question=item.question, question_type=item.question_type,
            options=item.options, correct_answer=item.correct_answer,
            grading_criteria=item.grading_criteria, explanation=item.explanation,
            difficulty=item.difficulty,
            generation_key=key + item.generation_key[len(donor_key):],
        )
        session.add(row)
        created.append(row)
    session.add(QuizAttempt(chunk_id=chunk.id, attempt=attempt))
    session.commit()
    return created
