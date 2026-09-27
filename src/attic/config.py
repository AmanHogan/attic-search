"""Filesystem layout and tunable settings."""

import os
from dataclasses import dataclass
from pathlib import Path

RENDER_DPI = 300
"""Resolution (dots per inch) for rendering PDF pages to PNG."""

THUMB_WIDTH = 400
"""Width in pixels of generated page thumbnails."""

PDF_SUFFIX = ".pdf"
"""File extension for PDFs, which are rendered page by page."""

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"})
"""File extensions for photos/images, which are used directly as a single page."""

INGEST_SUFFIXES = IMAGE_SUFFIXES | {PDF_SUFFIX}
"""Every file extension `attic ingest` picks up from the inbox."""

OCR_STAGE = "ocr"
"""Job stage name for per-page text extraction."""

OCR_STAGE_VERSION = 1
"""Version of the OCR stage; bump to re-run OCR on every page."""


@dataclass(frozen=True)
class Paths:
    """All archive locations, computed once from one home directory.

    Build with `get_paths()`, not the constructor directly, so every field
    stays consistent with `home`.

    Attributes:
        home (Path): Root directory containing `archive/` and `inbox/`.
        archive (Path): `home/archive`.
        raw (Path): Immutable original PDFs.
        pages (Path): Rendered page images.
        derived (Path): Per-page OCR/text output.
        thumbs (Path): Page thumbnails.
        db (Path): SQLite database file.
        inbox (Path): Where scans are dropped for ingest.
        duplicates (Path): Where re-dropped duplicates are moved.
    """

    home: Path
    archive: Path
    raw: Path
    pages: Path
    derived: Path
    thumbs: Path
    db: Path
    inbox: Path
    duplicates: Path

    def ensure(self) -> None:
        """Create every archive and inbox directory that doesn't exist yet."""
        for d in (self.raw, self.pages, self.derived, self.thumbs, self.inbox, self.duplicates):
            d.mkdir(parents=True, exist_ok=True)


def get_paths(home: str | Path | None = None) -> Paths:
    """Resolve the archive home directory and every path derived from it.

    Args:
        home (str | Path | None): Explicit home directory. Falls back to
            `$ATTIC_HOME`, then the current directory.

    Returns:
        Paths: Archive locations rooted at the resolved home.
    """
    home_dir = Path(home or os.environ.get("ATTIC_HOME") or ".").resolve()
    archive = home_dir / "archive"
    inbox = home_dir / "inbox"
    return Paths(
        home=home_dir,
        archive=archive,
        raw=archive / "raw",
        pages=archive / "pages",
        derived=archive / "derived",
        thumbs=archive / "thumbs",
        db=archive / "archive.db",
        inbox=inbox,
        duplicates=inbox / "duplicates",
    )
