import sqlite3
from collections.abc import Callable

import pytest

from attic.extract import DocFacts
from attic.search import search


def doc(title: str, doc_type: str, when: str | None) -> DocFacts:
    return DocFacts(
        doc_type=doc_type,  # type: ignore[arg-type]
        title=title,
        date_start=when,
        date_end=when,
        people=[],
        tags=[],
        summary="",
        sensitive=False,
        amount=None,
    )


DOCS = [
    (
        "oil.pdf",
        "JIFFY LUBE oil change full synthetic total 45.99",
        doc("Oil change", "receipt", "2024-03-05"),
    ),
    (
        "letter.pdf",
        "Dear grandma, thank you for the birthday card",
        doc("Thank-you letter", "letter", "2019-06-01"),
    ),
    (
        "rx.pdf",
        "Wink Eye Doctors contact lens prescription sphere",
        doc("Eye prescription", "form", "2026-09-22"),
    ),
]
"""Three documents of different types and years, run through the full pipeline."""


@pytest.fixture
def archive(build_archive: Callable[..., None]) -> None:
    """The three DOCS, ingested, OCR'd, extracted, and indexed.

    Args:
        build_archive (Callable[..., None]): Runs documents through the pipeline.
    """
    build_archive(DOCS)


@pytest.mark.usefixtures("archive")
def test_finds_best_match_first(conn: sqlite3.Connection) -> None:
    hits = search(conn, "oil change")

    assert hits[0].title == "Oil change"
    assert hits[0].matched_by == "both"
    assert "oil change" in hits[0].snippet.lower()


@pytest.mark.usefixtures("archive")
def test_meaning_only_match(conn: sqlite3.Connection) -> None:
    """A query sharing no 3+ letter word with the text can still match through the embedded header.

    Args:
        conn (sqlite3.Connection): Initialized test database.
    """
    hits = search(conn, "eye prescription")

    assert hits[0].title == "Eye prescription"


@pytest.mark.usefixtures("archive")
def test_type_filter(conn: sqlite3.Connection) -> None:
    hits = search(conn, "oil change grandma", doc_type="letter")

    assert [h.title for h in hits] == ["Thank-you letter"]


@pytest.mark.usefixtures("archive")
def test_year_filter(conn: sqlite3.Connection) -> None:
    hits = search(conn, "oil change grandma prescription", year=2026)

    assert [h.title for h in hits] == ["Eye prescription"]


@pytest.mark.usefixtures("archive")
def test_punctuation_does_not_break_query(conn: sqlite3.Connection) -> None:
    hits = search(conn, 'what\'s "$45.99"?! (total) OR -AND')

    assert hits[0].title == "Oil change"


def test_keyword_search_works_before_indexing(
    conn: sqlite3.Connection, build_archive: Callable[..., None]
) -> None:
    build_archive(DOCS, with_index=False)

    hits = search(conn, "grandma")

    assert [h.title for h in hits] == ["Thank-you letter"]
    assert hits[0].matched_by == "keyword"


@pytest.mark.usefixtures("archive")
def test_no_matches(conn: sqlite3.Connection) -> None:
    assert search(conn, "zz", doc_type="photo") == []
