import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from attic.config import Paths
from attic.ingest import ingest_inbox
from attic.merge import merge_documents
from attic.text import run_ocr


@pytest.fixture
def three_photos(paths: Paths, conn: sqlite3.Connection, make_pdf: Callable[..., Path]) -> None:
    """Three single-page documents (a.pdf, b.pdf, c.pdf), ingested and OCR'd.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        make_pdf (Callable[..., Path]): Test PDF factory.
    """
    for name in ("a", "b", "c"):
        make_pdf(paths.inbox / f"{name}.pdf", text=f"essay page {name} with enough text to skip OCR")
    ingest_inbox(conn, paths)
    list(run_ocr(conn, paths))


@pytest.mark.usefixtures("three_photos")
def test_merge_by_filename_keeps_given_order(conn: sqlite3.Connection) -> None:
    """Pages end up in one document, numbered in the order the files were given.

    Args:
        conn (sqlite3.Connection): Initialized test database.
    """
    doc_id, pages = merge_documents(conn, ["c.pdf", "a.pdf", "b.pdf"])

    assert pages == 3
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    order = conn.execute(
        "SELECT r.source_filename FROM pages p JOIN raw_files r ON r.sha256 = p.raw_sha256"
        " WHERE p.doc_id = ? ORDER BY p.page_no",
        (doc_id,),
    ).fetchall()
    assert [row[0] for row in order] == ["c.pdf", "a.pdf", "b.pdf"]
    assert {row[0] for row in conn.execute("SELECT doc_id FROM pages_fts")} == {doc_id}


@pytest.mark.usefixtures("three_photos")
def test_merge_clears_facts_and_jobs(conn: sqlite3.Connection) -> None:
    doc_ids = [row[0] for row in conn.execute("SELECT doc_id FROM documents ORDER BY doc_id")]
    with conn:
        for doc_id in doc_ids:
            conn.execute("UPDATE documents SET title = 'page title' WHERE doc_id = ?", (doc_id,))
            conn.execute("INSERT INTO tags VALUES (?, 'essay')", (doc_id,))
            conn.execute(
                "INSERT INTO jobs (job_id, target_id, stage, version, status, updated_at)"
                " VALUES (?, ?, 'extract', 4, 'done', 'now')",
                (f"j-{doc_id}", doc_id),
            )

    target, _ = merge_documents(conn, doc_ids)

    assert conn.execute("SELECT title FROM documents WHERE doc_id = ?", (target,)).fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM tags").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM jobs WHERE stage = 'extract'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM jobs WHERE stage = 'ocr'").fetchone()[0] == 3


@pytest.mark.usefixtures("three_photos")
@pytest.mark.parametrize("refs", [["a.pdf"], ["a.pdf", "a.pdf"], ["a.pdf", "missing.jpg"]])
def test_merge_rejects_bad_input(conn: sqlite3.Connection, refs: list[str]) -> None:
    with pytest.raises(ValueError):
        merge_documents(conn, refs)
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 3
