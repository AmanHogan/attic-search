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

DOCX_SUFFIX = ".docx"
"""Modern Word format (zipped XML), read with `python-docx` on any platform."""

DOC_SUFFIX = ".doc"
"""Pre-2007 Word format (binary OLE), which only macOS `textutil` can read here."""

WORD_SUFFIXES = frozenset({DOCX_SUFFIX, DOC_SUFFIX})
"""Word files, read as text directly instead of being rendered and OCR'd."""

PPTX_SUFFIX = ".pptx"
"""PowerPoint decks, read as text only: one page per slide, nothing rendered."""

INGEST_SUFFIXES = IMAGE_SUFFIXES | WORD_SUFFIXES | {PDF_SUFFIX, PPTX_SUFFIX}
"""Every file type `attic ingest` accepts."""

LONG_PDF_MIN_PAGES = 20
"""A PDF this long with a text layer is a textbook: only its first pages are rendered."""

LONG_PDF_RENDER_PAGES = 3
"""How many leading pages of a textbook PDF get a PNG and thumbnail."""

CODE_SUFFIXES = frozenset(
    {
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".s",
        ".asm",
        ".cs",
        ".m",
        ".py",
        ".java",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".php",
        ".html",
        ".css",
        ".lpr",
        ".pas",
        ".sql",
        ".sh",
        ".md",
        ".ipynb",
    }
)
"""Source files `attic scan-code` picks up; compiled output and data files are left out."""

CODE_SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "vendor",
        "target",
        "build",
        "dist",
        "out",
        "bin",
        "obj",
        ".idea",
        ".vscode",
        ".ipynb_checkpoints",
        "__MACOSX",
        ".next",
        ".gradle",
    }
)
"""Folder names never entered inside a code project: dependencies, build output, editor state."""

CODE_MAX_BYTES = 200_000
"""Code files larger than this are skipped: they are almost always generated or data."""

CODE_PROJECT_MARKERS = frozenset(
    {".git", "Makefile", "makefile", "package.json", "pyproject.toml", "pom.xml", "CMakeLists.txt"}
)
"""A folder holding one of these is a single project, nested folders and all."""

PROJECT_MAX_CHARS = 6_000
"""Most characters of context the model sees when describing a code project."""

PROJECT_TREE_FILES = 80
"""Most file paths shown to the model when it describes a project."""

PROJECT_HEAD_FILES = 6
"""How many of a project's main source files the model sees the start of."""

PROJECT_HEAD_LINES = 40
"""Lines shown from the start of each main file."""

PROJECT_MAIN_NAMES = frozenset({"main", "index", "app", "server", "run", "__main__"})
"""File names (without extension) that are read first when describing a project."""

COURSE_TITLES = {
    "5331": "database systems",
    "5334": "data mining",
    "6363": "machine learning",
    "6367": "computer vision",
    "6369": "reinforcement learning",
}
"""UTA course numbers to subject, added as tags to a project whose folder is named `cse-NNNN`."""

IMAGE_MIN_SIDE = 300
"""Images smaller than this (shortest side, pixels) are skipped when sweeping the inbox: icons and sprites."""

INBOX_SKIP_PREFIXES = ("~$", ".")
"""Filename prefixes ignored in the inbox: Word lock files and dotfiles such as .DS_Store."""

INBOX_SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        "public",
        "static",
        "assets",
        "__pycache__",
        ".venv",
        "venv",
        "target",
    }
)
"""Folder names skipped anywhere under the inbox: code projects and their bundled images."""

TEXTUTIL = "/usr/bin/textutil"
"""macOS tool used only for legacy .doc, which no portable library reads."""

OCR_STAGE = "ocr"
"""Job stage name for per-page text extraction."""

OCR_STAGE_VERSION = 1
"""Version of the OCR stage; bump to re-run OCR on every page."""

OCR_LANGUAGES = ("en-US",)
"""Languages Apple Vision should expect, most likely first."""

TEXT_LAYER_MIN_CHARS = 20
"""Minimum non-whitespace characters for a PDF's own text layer to be used instead of OCR."""

EXTRACT_STAGE = "extract"
"""Job stage name for per-document metadata extraction."""

EXTRACT_STAGE_VERSION = 6
"""Version of the extract stage; bump to re-run extraction on every document."""

EXTRACT_MODEL = "qwen2.5vl:7b"
"""Ollama model that reads OCR text and returns document metadata."""

EXTRACT_WORKERS = 3
"""Documents sent to the model at once; Ollama needs OLLAMA_NUM_PARALLEL at least this to overlap them."""

EXTRACT_NUM_CTX = 4096
"""Context window (tokens) per extraction call; fits the 4000-character input, prompt and answer."""

CAPTION_MAX_SIDE = 1024
"""Photos are shrunk to this many pixels on the long side before captioning; this keeps the call fast."""

EXTRACT_FAST_MODEL = "qwen2.5:3b"
"""Small text-only model for `attic extract --fast`: about twice as fast, less accurate, no images."""

EXTRACT_MAX_CHARS = 4_000
"""Longest document text sent to the model; longer documents are cut off."""

CAPTION_MAX_CHARS = 120
"""Documents with less text than this are captioned by the vision model, so they can still be found."""

INDEX_STAGE = "index"
"""Job stage name for per-document chunking and embedding."""

INDEX_STAGE_VERSION = 2
"""Version of the index stage; bump to re-chunk and re-embed every document."""

EMBED_MODEL = "nomic-embed-text"
"""Ollama embedding model used for both documents and search queries."""

EMBED_DOC_PREFIX = "search_document: "
"""Prefix nomic-embed-text expects on text being stored for search."""

EMBED_QUERY_PREFIX = "search_query: "
"""Prefix nomic-embed-text expects on a search query."""

CHUNK_MAX_CHARS = 2_000
"""Longest chunk of page text; longer pages are split into several chunks."""

CHUNK_OVERLAP_CHARS = 200
"""Text repeated at the start of the next chunk so a sentence isn't cut in half."""

SEARCH_CANDIDATES = 50
"""How many pages each of keyword and meaning search contribute before merging."""

RRF_K = 60
"""Reciprocal rank fusion constant: higher values flatten the gap between top ranks."""

RRF_DEPTH = 10
"""How far down each ranking reciprocal rank fusion looks.

Keyword and meaning searches return lists of very different lengths: FTS5 finds
only what matches, while the embedding comparison always fills up to
`SEARCH_CANDIDATES`. Without a shared cutoff, a page that FTS5 matched on a
common substring scores from both lists and outranks the best semantic match,
which scores from one.
"""

ASK_MODEL = "qwen2.5vl:7b"
"""Ollama model that routes questions, writes SQL, and writes answers."""

ASK_SOURCES = 5
"""How many search results the model reads to answer a RAG question."""

ASK_SOURCE_MAX_CHARS = 3_000
"""Longest page text given to the model per source; longer pages are cut off."""

SQL_VIEWS = ("documents_v", "amounts_v")
"""The only views text-to-SQL may read (enforced by SQLite, not just the prompt)."""

SQL_MAX_ROWS = 50
"""Most result rows returned by a generated query (and shown to the model)."""

SQL_TIMEOUT_SECONDS = 5.0
"""A generated query running longer than this is cancelled."""

SQL_RETRIES = 2
"""How many times the model may fix a query that errored before falling back to RAG."""


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


def find_home(start: Path) -> Path:
    """Find the archive home by looking for an `archive/` directory, here or above.

    This lets `attic` be run from anywhere in the project (`backend/`, say) and
    still use the one archive at the project root.

    Args:
        start (Path): Directory to start looking from.

    Returns:
        Path: The nearest directory containing `archive/`, else `start` itself.
    """
    for directory in (start, *start.parents):
        if (directory / "archive").is_dir():
            return directory
    return start


def get_paths(home: str | Path | None = None) -> Paths:
    """Resolve the archive home directory and every path derived from it.

    Args:
        home (str | Path | None): Explicit home directory. Falls back to
            `$ATTIC_HOME`, then the nearest directory at or above the current
            one that contains `archive/`, then the current directory.

    Returns:
        Paths: Archive locations rooted at the resolved home.
    """
    explicit = home or os.environ.get("ATTIC_HOME")
    home_dir = Path(explicit).resolve() if explicit else find_home(Path.cwd())
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
