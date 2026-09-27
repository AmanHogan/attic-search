import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from attic.config import Paths
from attic.db import SCHEMA_VERSION, connect, init_db
from attic.ingest import ingest_file, ingest_inbox


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def test_init_is_idempotent(paths: Paths, conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert init_db(conn) is False
    assert init_db(connect(paths.db)) is False


def test_ingest_inbox_pdf(paths: Paths, conn: sqlite3.Connection, make_pdf: Callable[..., Path]) -> None:
    """An inbox PDF is moved to raw/, rendered, recorded, and queued for OCR.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        make_pdf (Callable[..., Path]): Test PDF factory.
    """
    make_pdf(paths.inbox / "2026-09-20_receipts_001.pdf", pages=3)

    [r] = ingest_inbox(conn, paths)

    assert r.status == "ingested" and r.pages == 3
    assert not (paths.inbox / "2026-09-20_receipts_001.pdf").exists()
    assert (paths.raw / f"{r.sha256}.pdf").exists()

    raw = conn.execute("SELECT * FROM raw_files").fetchone()
    assert raw["source_filename"] == "2026-09-20_receipts_001.pdf"
    assert raw["scanned_at"] == "2026-09-20"
    assert raw["page_count"] == 3

    pages = conn.execute("SELECT * FROM pages ORDER BY page_no").fetchall()
    assert [p["page_no"] for p in pages] == [1, 2, 3]
    assert all(p["doc_id"] == r.doc_id for p in pages)
    for p in pages:
        assert (paths.archive / p["image_path"]).exists()
        assert (paths.archive / p["thumb_path"]).exists()
        assert len(p["phash"]) == 16

    jobs = conn.execute("SELECT stage, status FROM jobs").fetchall()
    assert len(jobs) == 3 and all(tuple(j) == ("ocr", "pending") for j in jobs)


def test_redrop_is_noop(
    paths: Paths, conn: sqlite3.Connection, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    """Dropping the same bytes again adds no rows and moves the file to duplicates/.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        make_pdf (Callable[..., Path]): Test PDF factory.
        tmp_path (Path): Scratch directory outside the inbox.
    """
    original = make_pdf(tmp_path / "keep.pdf")
    shutil.copy(original, paths.inbox / "a.pdf")
    ingest_inbox(conn, paths)
    before = {t: count(conn, t) for t in ("raw_files", "documents", "pages", "jobs")}

    shutil.copy(original, paths.inbox / "a.pdf")
    [r] = ingest_inbox(conn, paths)

    assert r.status == "duplicate"
    assert {t: count(conn, t) for t in before} == before
    assert (paths.duplicates / "a.pdf").exists()
    assert not (paths.inbox / "a.pdf").exists()


def test_file_outside_inbox_is_copied_not_moved(
    paths: Paths, conn: sqlite3.Connection, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    src = make_pdf(tmp_path / "elsewhere.pdf")

    r = ingest_file(conn, paths, src)

    assert r.status == "ingested"
    assert src.exists()


def test_bad_file_is_rejected_and_left_alone(paths: Paths, conn: sqlite3.Connection) -> None:
    bad = paths.inbox / "broken.pdf"
    bad.write_bytes(b"not a pdf at all")

    [r] = ingest_inbox(conn, paths)

    assert r.status == "error"
    assert bad.exists()
    assert count(conn, "raw_files") == 0


def test_retry_after_crash_before_commit(
    paths: Paths, conn: sqlite3.Connection, make_pdf: Callable[..., Path]
) -> None:
    """Raw copy and renders exist but no DB rows: re-running completes ingest.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        make_pdf (Callable[..., Path]): Test PDF factory.
    """
    src = make_pdf(paths.inbox / "a.pdf", pages=2)
    data = src.read_bytes()
    r1 = ingest_file(conn, paths, src)
    with conn:
        for t in ("jobs", "pages", "documents", "raw_files"):
            conn.execute(f"DELETE FROM {t}")
    (paths.inbox / "a.pdf").write_bytes(data)

    r2 = ingest_inbox(conn, paths)[0]

    assert r2.status == "ingested" and r2.sha256 == r1.sha256
    assert count(conn, "pages") == 2


def test_ingest_image_directly(
    paths: Paths, conn: sqlite3.Connection, make_image: Callable[..., Path]
) -> None:
    """A photo is stored as-is in raw/ and becomes one page, with no PDF involved.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        make_image (Callable[..., Path]): Test image factory.
    """
    make_image(paths.inbox / "photo.JPG")

    [r] = ingest_inbox(conn, paths)

    assert r.status == "ingested" and r.pages == 1
    raw = conn.execute("SELECT raw_path FROM raw_files").fetchone()
    assert raw["raw_path"] == f"raw/{r.sha256}.jpg"
    assert (paths.archive / raw["raw_path"]).exists()
    page = conn.execute("SELECT image_path, thumb_path FROM pages").fetchone()
    assert (paths.archive / page["image_path"]).exists()
    assert (paths.archive / page["thumb_path"]).exists()
    assert count(conn, "jobs") == 1


def test_image_rotated_upright(
    paths: Paths, conn: sqlite3.Connection, make_image: Callable[..., Path]
) -> None:
    """A photo with EXIF rotation is saved rotated, so OCR sees text the right way up.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        make_image (Callable[..., Path]): Test image factory.
    """
    make_image(paths.inbox / "sideways.jpg", size=(200, 100), orientation=6)

    ingest_inbox(conn, paths)

    page = conn.execute("SELECT image_path FROM pages").fetchone()
    with Image.open(paths.archive / page["image_path"]) as img:
        assert img.size == (100, 200)


def test_bad_image_is_rejected(paths: Paths, conn: sqlite3.Connection) -> None:
    bad = paths.inbox / "broken.jpg"
    bad.write_bytes(b"not an image")

    [r] = ingest_inbox(conn, paths)

    assert r.status == "error"
    assert bad.exists()


def test_unsupported_files_are_skipped(paths: Paths, conn: sqlite3.Connection, tmp_path: Path) -> None:
    (paths.inbox / "notes.txt").write_text("hi")
    other = tmp_path / "notes.txt"
    other.write_text("hi")

    assert ingest_inbox(conn, paths) == []
    assert ingest_file(conn, paths, other).status == "error"
    assert (paths.inbox / "notes.txt").exists()


def test_migrate_v1_database(paths: Paths, conn: sqlite3.Connection) -> None:
    """A v1 database (no raw_path column) is upgraded in place, keeping its rows.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database, downgraded to v1 here.
    """
    conn.execute("ALTER TABLE raw_files DROP COLUMN raw_path")
    conn.execute("PRAGMA user_version = 1")
    with conn:
        conn.execute(
            "INSERT INTO raw_files (sha256, source_filename, page_count, ingested_at)"
            " VALUES ('abc', 'a.pdf', 1, 'now')"
        )

    assert init_db(conn) is False

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT raw_path FROM raw_files").fetchone()[0] == "raw/abc.pdf"
