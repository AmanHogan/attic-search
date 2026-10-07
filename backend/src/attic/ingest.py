"""Ingest PDFs and images: hash, dedupe, store raw, make page images, record rows, enqueue OCR.

Order is chosen so a crash at any point is recoverable by re-running:
raw copy and page images are overwritten idempotently, and nothing counts as
ingested until the DB transaction commits. The inbox file is removed last.
"""

import hashlib
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pymupdf
from PIL import Image
from pptx import Presentation
from ulid import ULID

from attic import config
from attic.config import Paths
from attic.db import now
from attic.render import RenderedPage, render_image, render_pdf

SCAN_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_")
"""Scan regular expression: YYYY-MM-DD_batch_NNN.pdf, e.g. 2023-01-15_batch_001.pdf"""


@dataclass
class IngestResult:
    """Outcome of ingesting one file.

    Attributes:
        source (Path): The file that was ingested.
        status (str): "ingested", "duplicate", or "error".
        sha256 (str | None): File hash; None if the file was rejected before hashing.
        doc_id (str | None): New document ID; set only when ingested.
        pages (int): Page count; set only when ingested.
        error (str | None): Why the file was rejected; set only on error.
    """

    source: Path
    status: str
    sha256: str | None = None
    doc_id: str | None = None
    pages: int = 0
    error: str | None = None


# --- start private functions ---


def _move_unique(src: Path, dest_dir: Path) -> Path:
    """Move a file into a directory, adding `.1`, `.2`, ... to the name if taken.

    Args:
        src (Path): File to move.
        dest_dir (Path): Directory to move it into.

    Returns:
        Path: Where the file ended up.
    """
    dest = dest_dir / src.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem}.{n}{src.suffix}"
        n += 1
    shutil.move(src, dest)
    return dest


def _is_tiny_image(path: Path) -> bool:
    """Whether a file is an image too small to be a scan or photo (an icon or sprite)."""
    if path.suffix.lower() not in config.IMAGE_SUFFIXES:
        return False
    try:
        with Image.open(path) as im:
            return min(im.size) < config.IMAGE_MIN_SIDE
    except Exception:
        return False  # unreadable: let ingest report it


# --- end private functions ---


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def scanned_at(path: Path) -> str:
    """Determine when a file was scanned.

    Args:
        path (Path): Scanned file, ideally named `YYYY-MM-DD_batch_NNN.pdf`.

    Returns:
        str: ISO date from the filename, else from the file's modification time.
    """
    if m := SCAN_DATE_RE.match(path.name):
        try:
            return date.fromisoformat(m.group(1)).isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def check_pdf(path: Path) -> int:
    """Validate that a file is a usable PDF.

    Args:
        path (Path): File to check.

    Returns:
        int: Number of pages.

    Raises:
        ValueError: If the file is unreadable, not a PDF, password-protected, or empty.
    """
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise ValueError(f"not a readable PDF: {e}") from e
    with doc:
        if not doc.is_pdf:
            raise ValueError("not a PDF")
        if doc.needs_pass:
            raise ValueError("PDF is password-protected")
        if doc.page_count == 0:
            raise ValueError("PDF has no pages")
        return int(doc.page_count)


def is_textbook(pdf: Path, page_count: int) -> bool:
    """Decide whether a PDF is a long book that should not be rendered page by page.

    A PDF counts when it has at least `LONG_PDF_MIN_PAGES` pages and most of a
    sample of its pages carry a real text layer. A long scan has no text layer,
    so it is rendered in full: its images are the only way to read it.

    Args:
        pdf (Path): PDF to inspect.
        page_count (int): Number of pages in the PDF.

    Returns:
        bool: True if only the first pages should be rendered.
    """
    if page_count < config.LONG_PDF_MIN_PAGES:
        return False
    sample = sorted({page_count * i // 5 for i in range(5)})
    with pymupdf.open(pdf) as doc:
        with_text = sum(
            len("".join(str(doc[n].get_text()).split())) >= config.TEXT_LAYER_MIN_CHARS for n in sample
        )
    return with_text * 2 > len(sample)


def check_pptx(path: Path) -> int:
    """Validate a PowerPoint deck and count its slides.

    Args:
        path (Path): File to check.

    Returns:
        int: Number of slides.

    Raises:
        ValueError: If the file can't be opened as a deck or has no slides.
    """
    try:
        slides = len(Presentation(str(path)).slides)
    except Exception as e:
        raise ValueError(f"not a readable PowerPoint file: {e}") from e
    if slides == 0:
        raise ValueError("deck has no slides")
    return slides


def check_image(path: Path) -> int:
    """Validate that a file is a readable image.

    Args:
        path (Path): File to check.

    Returns:
        int: Number of pages, always 1.

    Raises:
        ValueError: If the file can't be opened as an image.
    """
    try:
        with Image.open(path) as im:
            im.verify()
    except Exception as e:
        raise ValueError(f"not a readable image: {e}") from e
    return 1


def ingest_file(conn: sqlite3.Connection, paths: Paths, src: Path) -> IngestResult:
    """Ingest one PDF or image into the archive.

    Files inside `inbox/` are removed once ingested (duplicates are moved to
    `inbox/duplicates/`). Files elsewhere are copied and left in place.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        src (Path): PDF or image to ingest.

    Returns:
        IngestResult: What happened to the file.
    """
    src = src.resolve()
    consume = paths.inbox in src.parents  # only inbox files are moved/removed
    suffix = src.suffix.lower()
    is_pdf = suffix == config.PDF_SUFFIX
    is_word = suffix in config.WORD_SUFFIXES
    is_pptx = suffix == config.PPTX_SUFFIX

    if suffix not in config.INGEST_SUFFIXES:
        return IngestResult(src, "error", error=f"unsupported file type: {suffix or '(none)'}")
    try:
        if is_word:
            page_count = 1  # a Word file is one unit of text; it has no pages to render
        elif is_pptx:
            page_count = check_pptx(src)
        elif is_pdf:
            page_count = check_pdf(src)
        else:
            page_count = check_image(src)
    except ValueError as e:
        return IngestResult(src, "error", error=str(e))

    sha = sha256_file(src)
    if conn.execute("SELECT 1 FROM raw_files WHERE sha256 = ?", (sha,)).fetchone():
        if consume:
            _move_unique(src, paths.duplicates)
        return IngestResult(src, "duplicate", sha256=sha)

    raw = paths.raw / f"{sha}{suffix}"
    if not raw.exists():
        tmp = raw.with_name(f"{raw.name}.tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(raw)

    if is_word:
        rendered = [RenderedPage(1, None, None, None)]
    elif is_pptx:
        rendered = [RenderedPage(n, None, None, None) for n in range(1, page_count + 1)]  # text only
    elif is_pdf:
        render_limit = config.LONG_PDF_RENDER_PAGES if is_textbook(raw, page_count) else None
        rendered = render_pdf(raw, sha, paths.archive, config.RENDER_DPI, config.THUMB_WIDTH, render_limit)
    else:
        rendered = render_image(raw, sha, paths.archive, config.THUMB_WIDTH)
    assert len(rendered) == page_count

    doc_id = str(ULID())
    ts = now()
    with conn:
        conn.execute(
            "INSERT INTO raw_files (sha256, raw_path, source_filename, scanned_at, page_count, ingested_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (sha, f"raw/{raw.name}", src.name, scanned_at(src), page_count, ts),
        )
        # Default split strategy: one raw file = one document. The filename stands in as the
        # title until extraction writes a better one, so text search has something to show.
        conn.execute(
            "INSERT INTO documents (doc_id, title, created_at) VALUES (?, ?, ?)",
            (doc_id, src.stem if is_word or is_pptx or is_pdf else None, ts),
        )
        for p in rendered:
            page_id = str(ULID())
            conn.execute(
                "INSERT INTO pages (page_id, raw_sha256, raw_page_no, doc_id, page_no,"
                " phash, image_path, thumb_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (page_id, sha, p.raw_page_no, doc_id, p.raw_page_no, p.phash, p.image_path, p.thumb_path),
            )
            conn.execute(
                "INSERT OR IGNORE INTO jobs (job_id, target_id, stage, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(ULID()), page_id, config.OCR_STAGE, config.OCR_STAGE_VERSION, ts),
            )

    if consume:
        src.unlink()
    return IngestResult(src, "ingested", sha256=sha, doc_id=doc_id, pages=page_count)


def inbox_files(paths: Paths) -> list[Path]:
    """List every ingestable file in the inbox, including ones in subfolders.

    Subfolders are walked so a dropped folder of documents is picked up, but the
    archive itself stays flat: the folder a file came from is not recorded. Images
    under `IMAGE_MIN_SIDE` pixels are left out, since they are icons rather than scans.

    Args:
        paths (Paths): Archive locations.

    Returns:
        list[Path]: Files to ingest, in path order.
    """
    return sorted(
        p
        for p in paths.inbox.rglob("*")
        if p.is_file()
        and p.suffix.lower() in config.INGEST_SUFFIXES
        and paths.duplicates not in p.parents
        and config.INBOX_SKIP_DIRS.isdisjoint(p.relative_to(paths.inbox).parts[:-1])
        and not p.name.startswith(config.INBOX_SKIP_PREFIXES)
        and not _is_tiny_image(p)
    )


def ingest_inbox(conn: sqlite3.Connection, paths: Paths) -> list[IngestResult]:
    return [ingest_file(conn, paths, p) for p in inbox_files(paths)]
