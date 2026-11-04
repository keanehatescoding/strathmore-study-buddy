"""with_text_cap_note: swap the extraction cap note in a resource's error,
keeping what chunking said about the chunks (#133 review)."""

import pytest

from app.chunk import FAILED_JOIN, SECTIONS_CAPPED, SECTIONS_JOIN, with_text_cap_note
from app.extract import CAPPED_PREFIX

OLD = f"{CAPPED_PREFIX}10 of 90 characters are studied: the rest is past the size cap"
NEW = f"{CAPPED_PREFIX}20 of 90 characters are studied: the rest is past the size cap"
SECTIONS = f"3 of 9 {SECTIONS_CAPPED}"


@pytest.mark.parametrize("error, note, expected", [
    (None, None, None),
    (None, NEW, NEW),
    (OLD, NEW, NEW),
    (OLD, None, None),
    (f"{OLD}{SECTIONS_JOIN}{SECTIONS}", NEW, f"{NEW}{SECTIONS_JOIN}{SECTIONS}"),
    (f"{OLD}{SECTIONS_JOIN}{SECTIONS}", None, f"{CAPPED_PREFIX}{SECTIONS}"),
    (f"{CAPPED_PREFIX}{SECTIONS}", NEW, f"{NEW}{SECTIONS_JOIN}{SECTIONS}"),
    (f"{CAPPED_PREFIX}{SECTIONS}", None, f"{CAPPED_PREFIX}{SECTIONS}"),
    (f"{OLD}{FAILED_JOIN}LLMError: x", NEW, f"{NEW}{FAILED_JOIN}LLMError: x"),
    (f"{OLD}{FAILED_JOIN}LLMError: x", None, "LLMError: x"),
    ("LLMError: x", NEW, f"{NEW}{FAILED_JOIN}LLMError: x"),
    ("LLMError: x", None, "LLMError: x"),
])
def test_with_text_cap_note(error, note, expected):
    assert with_text_cap_note(error, note) == expected
