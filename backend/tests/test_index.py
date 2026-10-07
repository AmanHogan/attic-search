import sqlite3
from collections.abc import Callable
from itertools import pairwise

import pytest

from attic import index
from attic.config import Paths
from attic.extract import DocFacts
from attic.index import run_index
from attic.text import run_ocr


def doc(title: str, doc_type: str = "letter", when: str | None = "2024-03-05") -> DocFacts:
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


def test_short_page_is_one_chunk() -> None:
    assert index._split("a short page", max_chars=100, overlap=20) == ["a short page"]


def test_long_page_is_split_with_overlap() -> None:
    """Long pages split between lines; each chunk starts with the previous chunk's last line(s)."""
    lines = [f"line {n:02d} " + "x" * 30 for n in range(20)]

    chunks = index._split("\n".join(lines), max_chars=200, overlap=50)

    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    for prev, nxt in pairwise(chunks):
        assert nxt.splitlines()[0] == prev.splitlines()[-1]
    assert all(line in "\n".join(chunks) for line in lines)


def test_header_formats() -> None:
    assert index._header("form", "2026-09-22", "2026-09-22", "Rx", None, None) == ("[form | 2026-09-22 | Rx]")
    assert index._header("note", "2009-03-01", "2009-05-31", "Poem", None, None) == (
        "[note | 2009-03-01 to 2009-05-31 | Poem]"
    )
    assert index._header(None, None, None, None, None, None) == "[unknown | undated | untitled]"


def test_header_carries_tags_and_summary() -> None:
    """Tags and the summary ride along on every chunk, so a page of bare figures is findable."""
    header = index._header("record", None, None, "Student Shot Record", "immunization", "Shots given.")
    assert header == ("[record | undated | Student Shot Record]\nTags: immunization\nSummary: Shots given.")


def test_index_stores_chunks_with_header(
    conn: sqlite3.Connection, build_archive: Callable[..., None]
) -> None:
    """Each page becomes a chunk that starts with the document header, with an embedding.

    Args:
        conn (sqlite3.Connection): Initialized test database.
        build_archive (Callable[..., None]): Runs documents through the pipeline.
    """
    build_archive([("a.pdf", "Dear grandma, thank you for the birthday card", doc("Thank-you letter"))])

    rows = conn.execute("SELECT text, embed_model, embedding FROM chunks").fetchall()
    assert len(rows) == 1
    assert rows[0]["text"].startswith("[letter | 2024-03-05 | Thank-you letter]\nDear grandma")
    assert rows[0]["embed_model"] == "nomic-embed-text"
    assert len(rows[0]["embedding"]) == 256 * 4  # fake embedding: 256 float32s
    assert conn.execute("SELECT status FROM jobs WHERE stage = 'index'").fetchone()[0] == "done"


def test_waits_for_text(paths: Paths, conn: sqlite3.Connection, build_archive: Callable[..., None]) -> None:
    """A document is not indexed until every one of its pages has text.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        build_archive (Callable[..., None]): Runs documents through the pipeline.
    """
    build_archive([("a.pdf", "Dear grandma, thank you for the card", doc("Letter"))], with_index=False)
    with conn:
        conn.execute("UPDATE jobs SET status = 'pending' WHERE stage = 'ocr'")

    assert list(run_index(conn, paths)) == []
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_indexes_without_extract(
    paths: Paths, conn: sqlite3.Connection, build_archive: Callable[..., None]
) -> None:
    """Text alone is enough to index, so search works without ever running the LLM.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        build_archive (Callable[..., None]): Runs documents through the pipeline.
    """
    build_archive([("a.pdf", "Dear grandma, thank you for the card", doc("Letter"))], with_index=False)
    with conn:
        conn.execute("DELETE FROM jobs WHERE stage = 'extract'")
        conn.execute("UPDATE documents SET doc_type = NULL, title = NULL, summary = NULL")

    assert [r.status for r in run_index(conn, paths)] == ["done"]
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1


def test_rerun_is_noop(paths: Paths, conn: sqlite3.Connection, build_archive: Callable[..., None]) -> None:
    build_archive([("a.pdf", "Dear grandma, thank you for the card", doc("Letter"))])

    assert list(run_index(conn, paths)) == []
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1


def test_embedding_failure_is_recorded(
    paths: Paths,
    conn: sqlite3.Connection,
    build_archive: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If embedding fails, the job is marked error and no chunks are stored.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        build_archive (Callable[..., None]): Runs documents through the pipeline.
        monkeypatch (pytest.MonkeyPatch): Replaces embedding with a failing stub.
    """
    build_archive([("a.pdf", "Dear grandma, thank you for the card", doc("Letter"))], with_index=False)

    def boom(texts: list[str], prefix: str) -> None:
        raise ConnectionError("ollama not running")

    monkeypatch.setattr(index, "embed", boom)
    [r] = list(run_index(conn, paths))

    assert r.status == "error" and r.error == "ollama not running"
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_new_text_requeues_indexing(
    paths: Paths, conn: sqlite3.Connection, build_archive: Callable[..., None]
) -> None:
    """Re-reading a page's text marks its document for re-indexing.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        build_archive (Callable[..., None]): Runs documents through the pipeline.
    """
    build_archive([("a.pdf", "Dear grandma, thank you for the card", doc("Letter"))])
    assert conn.execute("SELECT status FROM jobs WHERE stage = 'index'").fetchone()[0] == "done"

    with conn:
        conn.execute("UPDATE jobs SET status = 'pending' WHERE stage = 'ocr'")
    list(run_ocr(conn, paths))

    assert conn.execute("SELECT status FROM jobs WHERE stage = 'index'").fetchone()[0] == "pending"
