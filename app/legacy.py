"""Text from the legacy Office formats: .ppt and .doc (97-2003), and RTF.

.ppt and .doc are OLE2 compound files, read here with olefile: no converter
binary, nothing to install in the image. RTF is here because Word saves it
under a .doc name. Only the text is read; layout, pictures and embedded
objects are ignored. The binary formats are [MS-PPT] and [MS-DOC].

A file a parser can't follow raises ValueError, struct.error or one of
olefile's errors; extract.py turns those into an ExtractError.
"""

from __future__ import annotations

import io
import re
import struct
from typing import Iterator

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# A stream is read into memory whole: refuse one past this (the download cap
# already bounds the file, this bounds a file that lies about a stream).
MAX_STREAM_BYTES = 200 * 1024 * 1024
# Records nest (drawing groups inside groups); real files stay under ten deep.
MAX_DEPTH = 64


def is_ole(blob: bytes) -> bool:
    return blob.startswith(OLE_MAGIC)


def _open(blob: bytes):
    import olefile

    if not is_ole(blob):
        raise ValueError("not an OLE2 file")
    return olefile.OleFileIO(io.BytesIO(blob))


def _stream(ole, name: str) -> bytes:
    if not ole.exists(name):
        raise ValueError(f"no {name!r} stream")
    if ole.get_size(name) > MAX_STREAM_BYTES:
        raise ValueError(f"{name!r} stream is too large")
    return ole.openstream(name).read()


# --- PowerPoint -------------------------------------------------------------

_DOCUMENT = 0x03E8
_SLIDE = 0x03EE
_SLIDE_PERSIST = 0x03F3
_OUTLINE_TEXT_REF = 0x0F9E
_TEXT_HEADER = 0x0F9F
_TEXT_CHARS = 0x0FA0
_TEXT_BYTES = 0x0FA8
_SLIDE_LIST = 0x0FF0
_USER_EDIT = 0x0FF5
_PERSIST_DIRECTORY = 0x1772
_CLIENT_TEXTBOX = 0xF00D  # holds text records without being flagged a container
_ENCRYPTED_TOKEN = 0xF3D1C4DF


def _records(data: bytes, start: int, end: int) -> Iterator[tuple[int, int, int, int, int]]:
    """(version, instance, type, body start, body end) of each record in
    data[start:end]. A record running past the end stops the walk."""
    pos = start
    while pos + 8 <= end:
        ver_inst, rec_type, length = struct.unpack_from("<HHI", data, pos)
        body = pos + 8
        if body + length > end:
            return
        yield ver_inst & 0xF, ver_inst >> 4, rec_type, body, body + length
        pos = body + length


def _record_at(data: bytes, offset: int, rec_type: int) -> tuple[int, int]:
    """Body bounds of the record at offset, which must be a rec_type."""
    for _, _, found, start, end in _records(data, offset, len(data)):
        if found == rec_type:
            return start, end
        break
    raise ValueError(f"no record {rec_type:#06x} at offset {offset}")


def _ppt_text(data: bytes, rec_type: int, start: int, end: int) -> str:
    if rec_type == _TEXT_CHARS:
        text = data[start:end].decode("utf-16-le", errors="replace")
    else:  # TextBytesAtom: the low byte of each UTF-16 unit
        text = data[start:end].decode("latin-1")
    # \r ends a paragraph, \x0b is a line break inside one
    return re.sub(r"[\r\x0b]", "\n", text).strip()


def _persist_offsets(data: bytes, edit_offset: int) -> tuple[dict[int, int], int]:
    """({persist id: stream offset}, the document's persist id), following
    the chain of saves from the newest. A file saved more than once keeps its
    older copies of each slide in the stream; the newest save's entry wins."""
    offsets: dict[int, int] = {}
    doc_id = None
    seen = set()
    while edit_offset and edit_offset not in seen:
        seen.add(edit_offset)
        start, _ = _record_at(data, edit_offset, _USER_EDIT)
        edit_offset, directory, this_doc_id = struct.unpack_from("<III", data, start + 8)
        doc_id = doc_id or this_doc_id
        pos, end = _record_at(data, directory, _PERSIST_DIRECTORY)
        while pos + 4 <= end:
            (entry,) = struct.unpack_from("<I", data, pos)
            first, count = entry & 0xFFFFF, entry >> 20
            pos += 4
            for i, (offset,) in enumerate(struct.iter_unpack("<I", data[pos:pos + 4 * count])):
                offsets.setdefault(first + i, offset)
            pos += 4 * count
    if doc_id not in offsets:
        raise ValueError("no document record")
    return offsets, doc_id


def _slide_list(data: bytes, doc_start: int, doc_end: int) -> list[tuple[int, list[str]]]:
    """[(slide persist id, its placeholder texts)] in presentation order.
    PowerPoint keeps title and body placeholder text here, in the document,
    and has the slide's shapes point at it by the index of its header."""
    slides: list[tuple[int, list[str]]] = []
    for ver, instance, rec_type, start, end in _records(data, doc_start, doc_end):
        if rec_type != _SLIDE_LIST or instance != 0:  # 1 = masters, 2 = notes
            continue
        for _, _, child, c_start, c_end in _records(data, start, end):
            if child == _SLIDE_PERSIST:
                slides.append((struct.unpack_from("<I", data, c_start)[0], []))
            elif child == _TEXT_HEADER and slides:
                slides[-1][1].append("")  # one slot per placeholder, text or not
            elif child in (_TEXT_CHARS, _TEXT_BYTES) and slides and slides[-1][1]:
                slides[-1][1][-1] = _ppt_text(data, child, c_start, c_end)
    return slides


def _shape_texts(data: bytes, start: int, end: int, outline: list[str],
                 used: set[int], out: list[str], depth: int = 0) -> None:
    """Texts of a slide's shapes, in drawing order: text boxes carry their
    own text, placeholders refer to the document's outline text."""
    if depth > MAX_DEPTH:
        raise ValueError("records nested too deep")
    for ver, _, rec_type, r_start, r_end in _records(data, start, end):
        if rec_type in (_TEXT_CHARS, _TEXT_BYTES):
            out.append(_ppt_text(data, rec_type, r_start, r_end))
        elif rec_type == _OUTLINE_TEXT_REF and r_end - r_start >= 4:
            (index,) = struct.unpack_from("<i", data, r_start)
            if 0 <= index < len(outline) and index not in used:
                used.add(index)
                out.append(outline[index])
        elif ver == 0xF or rec_type == _CLIENT_TEXTBOX:
            _shape_texts(data, r_start, r_end, outline, used, out, depth + 1)


def ppt_slides(blob: bytes) -> list[str]:
    """The text of each slide of a .ppt/.pps, in order; "" for a slide with
    none. Master slides and speaker notes are left out."""
    with _open(blob) as ole:
        data = _stream(ole, "PowerPoint Document")
        current = _stream(ole, "Current User")
    token, edit_offset = struct.unpack_from("<II", current, 12)
    if token == _ENCRYPTED_TOKEN:
        raise ValueError("the presentation is password-protected")
    offsets, doc_id = _persist_offsets(data, edit_offset)
    doc_start, doc_end = _record_at(data, offsets[doc_id], _DOCUMENT)
    slides = []
    for persist_id, outline in _slide_list(data, doc_start, doc_end):
        texts: list[str] = []
        used: set[int] = set()
        if persist_id in offsets:
            start, end = _record_at(data, offsets[persist_id], _SLIDE)
            _shape_texts(data, start, end, outline, used, texts)
        # outline text no shape pointed at still belongs to the slide
        texts += [t for i, t in enumerate(outline) if i not in used]
        # "*" alone is where the slide number or the date gets drawn
        slides.append("\n".join(t for t in texts if t and t != "*"))
    return slides


# --- Word -------------------------------------------------------------------

_WORD_IDENT = 0xA5EC
_ENCRYPTED = 0x0100
_TABLE_1 = 0x0200
_FIELD_BEGIN, _FIELD_SEPARATOR, _FIELD_END = "\x13", "\x14", "\x15"
_CELL = "\x07"


def _pieces(table: bytes, clx_start: int, clx_len: int) -> list[tuple[int, int, int, bool]]:
    """The piece table: [(first cp, end cp, stream offset, is 8-bit)]. The
    text of a document sits in the WordDocument stream in pieces, out of
    order once it has been fast-saved; this maps character positions to them."""
    pos, end = clx_start, clx_start + clx_len
    if end > len(table):
        raise ValueError("piece table runs past the table stream")
    while pos < end:
        kind = table[pos]
        if kind == 1:  # property modifiers: skipped
            (size,) = struct.unpack_from("<H", table, pos + 1)
            pos += 3 + size
        elif kind == 2:
            (size,) = struct.unpack_from("<I", table, pos + 1)
            pos += 5
            count = (size - 4) // 12
            if count < 0 or pos + size > end:
                raise ValueError("bad piece table")
            cps = struct.unpack_from(f"<{count + 1}I", table, pos)
            pieces = []
            for i in range(count):
                (fc,) = struct.unpack_from("<I", table, pos + 4 * (count + 1) + 8 * i + 2)
                compressed = bool(fc & 0x40000000)
                fc &= 0x3FFFFFFF
                pieces.append((cps[i], cps[i + 1], fc // 2 if compressed else fc, compressed))
            return pieces
        else:
            break
    raise ValueError("no piece table")


def _drop_field_codes(text: str) -> str:
    """Keep a field's result, drop its instructions: HYPERLINK "…" and
    PAGEREF codes sit in the text between \\x13 and \\x14."""
    out: list[str] = []
    in_code: list[bool] = []  # one entry per open field
    code_depth = 0  # open fields still in their instructions
    for ch in text:
        if ch == _FIELD_BEGIN:
            in_code.append(True)
            code_depth += 1
        elif ch == _FIELD_SEPARATOR:
            if in_code and in_code[-1]:
                in_code[-1] = False
                code_depth -= 1
        elif ch == _FIELD_END:
            if in_code and in_code.pop():
                code_depth -= 1
        elif not code_depth:
            out.append(ch)
    return "".join(out)


def _tidy_doc(text: str) -> str:
    text = _drop_field_codes(text)
    text = text.replace("\x1e", "-").replace("\x1f", "")  # non-breaking / optional hyphen
    text = re.sub(r"[\r\x0b\x0c]", "\n", text)  # paragraph, line and page breaks
    # A table cell ends with \x07 and so does its row, so a doubled mark ends
    # a row (or follows an empty cell, which then breaks the line early).
    text = text.replace(_CELL * 2, "\n")
    lines = []
    for line in text.split("\n"):
        cells = [c.strip() for c in line.split(_CELL)]
        line = " | ".join(c for c in cells if c)
        # what's left of the control characters marks pictures and footnotes
        lines.append(re.sub(r"[\x00-\x08\x0e-\x1f]", "", line).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def doc_text(blob: bytes) -> str:
    """The body text of a .doc: paragraphs one per line, table rows as
    "a | b". Footnotes, headers and text boxes are left out."""
    with _open(blob) as ole:
        word = _stream(ole, "WordDocument")
        ident, _ = struct.unpack_from("<HH", word, 0)
        (flags,) = struct.unpack_from("<H", word, 0x0A)
        if ident != _WORD_IDENT:
            raise ValueError("older than Word 97, which isn't read")
        if flags & _ENCRYPTED:
            raise ValueError("the document is password-protected")
        table = _stream(ole, "1Table" if flags & _TABLE_1 else "0Table")
    (body_chars,) = struct.unpack_from("<i", word, 0x4C)
    clx_start, clx_len = struct.unpack_from("<II", word, 0x1A2)
    parts = []
    for first, last, offset, compressed in _pieces(table, clx_start, clx_len):
        last = min(last, body_chars)  # past the body: footnotes, headers, ...
        if last <= first:
            break
        size = last - first
        if compressed:
            raw = word[offset:offset + size]
            parts.append(raw.decode("cp1252", errors="replace"))
        else:
            raw = word[offset:offset + 2 * size]
            parts.append(raw.decode("utf-16-le", errors="replace"))
    return _tidy_doc("".join(parts))


# --- RTF --------------------------------------------------------------------

RTF_MAGIC = b"{\\rtf"

# Groups that hold no body text. Most such groups announce themselves with \*
# and are dropped without being listed here.
_RTF_SKIP = frozenset({
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "fldinst", "listtext",
    "pntext", "header", "headerl", "headerr", "headerf", "footer", "footerl",
    "footerr", "footerf", "footnote", "listtable", "listoverridetable", "revtbl",
    "rsidtbl", "filetbl", "generator", "template", "xmlnstbl", "pgdsctbl",
})
_RTF_TEXT = {
    "par": "\n", "line": "\n", "sect": "\n", "page": "\n", "row": "\n",
    "cell": " | ", "tab": "\t", "emdash": "\u2014", "endash": "\u2013",
    "bullet": "\u2022", "lquote": "\u2018", "rquote": "\u2019",
    "ldblquote": "\u201c", "rdblquote": "\u201d", "emspace": " ", "enspace": " ",
}
_RTF_SYMBOL = {"~": "\u00a0", "_": "-", "-": "", "\n": "\n", "\r": "\n",
               "\\": "\\", "{": "{", "}": "}"}
_RTF_TOKEN = re.compile(
    r"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?"  # control word, with its argument
    r"|\\'([0-9a-fA-F]{2})"               # a byte in the document's codepage
    r"|\\([^a-zA-Z])"                     # control symbol
    r"|([{}])"
    r"|[\r\n]+"                           # line breaks in the source mean nothing
    r"|([^\\{}\r\n]+)",                   # text
    re.DOTALL,
)


def rtf_text(blob: bytes) -> str:
    """The body text of an RTF document: paragraphs one per line, table rows
    as "a | b". Headers, footnotes, pictures and field codes are left out."""
    if not blob.startswith(RTF_MAGIC):
        raise ValueError("not an RTF file")
    src = blob.decode("latin-1")  # one char per byte; real text is escaped
    out: list[str] = []
    pending = bytearray()  # \'hh bytes waiting for the rest of their character
    codepage = "cp1252"
    skipping, uc = False, 1  # per group: in a skipped destination; \uc value
    stack: list[tuple[bool, int]] = []
    skip = 0  # fallback characters still to drop after a \u

    def flush() -> None:
        if pending:
            out.append(pending.decode(codepage, errors="replace"))
            pending.clear()

    pos = 0
    while m := _RTF_TOKEN.match(src, pos):
        pos = m.end()
        word, arg, hexbyte, symbol, brace, text = m.groups()
        if hexbyte:
            if skip:
                skip -= 1
            elif not skipping:
                pending.append(int(hexbyte, 16))
            continue
        flush()
        if brace == "{":
            stack.append((skipping, uc))
        elif brace == "}":
            if not stack:
                break  # the document's own group closed
            skipping, uc = stack.pop()
            skip = 0
        elif word:
            number = int(arg) if arg else None
            if word == "bin" and number and number > 0:
                pos += number  # raw bytes, braces and backslashes included
            elif word in _RTF_SKIP:
                skipping = True
            elif word == "uc" and number is not None:
                uc = max(number, 0)
            elif word == "ansicpg" and number:
                codepage = _rtf_codepage(number)
            elif skipping:
                pass
            elif word == "u" and number is not None:
                out.append(chr(number % 0x10000))
                skip = uc
            elif word in _RTF_TEXT:
                out.append(_RTF_TEXT[word])
        elif symbol:
            if symbol == "*":
                skipping = True
            elif not skipping:
                out.append(_RTF_SYMBOL.get(symbol, ""))
        elif text:
            if skip:
                text, skip = text[skip:], max(skip - len(text), 0)
            if text and not skipping:
                out.append(text.encode("latin-1").decode(codepage, errors="replace"))
    flush()
    # \u gives UTF-16 units: join surrogate pairs, replace stray ones
    joined = "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    lines = (line.strip().removesuffix("|").rstrip() for line in joined.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _rtf_codepage(number: int) -> str:
    import codecs

    try:
        return codecs.lookup(f"cp{number}").name
    except LookupError:
        return "cp1252"
