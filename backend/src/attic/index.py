"""Index documents for search: split page text into chunks and embed each one.

A document is queued once extraction is done, because every chunk is embedded
with a header built from the extracted metadata: type, date, title, tags, and
summary. That header lets a search like "eye doctor 2026" match a page whose
text only says "09-22-26", and lets a page of bare figures (a shot record's
table of dates) be found by what the document is actually about.

Embeddings are stored as float32 BLOBs in `chunks`; search compares a query
against all of them with numpy, which is fast enough for a personal archive.
"""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import ollama
from ulid import ULID

from attic import config
from attic.config import Paths
from attic.db import now


@dataclass(frozen=True)
class IndexResult:
    """Outcome of one index job.

    Attributes:
        doc_id (str): Document that was indexed.
        source_filename (str): Original name of the document's (first) raw file.
        status (str): "done" or "error".
        title (str | None): Document title, for display.
        chunks (int): Number of chunks stored.
        error (str | None): Why indexing failed; set only on error.
    """

    doc_id: str
    source_filename: str
    status: str
    title: str | None = None
    chunks: int = 0
    error: str | None = None


# --- start private functions ---


def _split(text: str, max_chars: int, overlap: int) -> list[str]:
    """Split text into chunks of about `max_chars`, breaking between lines.

    Each chunk after the first starts with the last few lines of the previous
    one (up to `overlap` characters), so text near a boundary appears whole in
    at least one chunk.

    Args:
        text (str): Page text.
        max_chars (int): Target maximum chunk length.
        overlap (int): Maximum characters carried over into the next chunk.

    Returns:
        list[str]: The chunks; a single chunk if the text is short enough.
    """
    if len(text) <= max_chars:
        return [text]

    lines = []
    for line in text.splitlines():
        while len(line) > max_chars:
            lines.append(line[:max_chars])
            line = line[max_chars:]
        lines.append(line)

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > max_chars:
            chunks.append("\n".join(current))
            tail: list[str] = []
            tail_size = 0
            for prev in reversed(current):
                if tail_size + len(prev) + 1 > overlap:
                    break
                tail.insert(0, prev)
                tail_size += len(prev) + 1
            current, size = tail, tail_size
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _header(
    doc_type: str | None,
    start: str | None,
    end: str | None,
    title: str | None,
    tags: str | None,
    summary: str | None,
) -> str:
    """Build the metadata lines put in front of every chunk of a document.

    The tags and summary are repeated on every chunk on purpose: a page that is
    only a table of numbers has nothing for a search to match otherwise.

    Args:
        doc_type (str | None): Extracted document type.
        start (str | None): Earliest document date.
        end (str | None): Latest document date.
        title (str | None): Extracted title.
        tags (str | None): Comma-separated tags.
        summary (str | None): One or two sentence summary.

    Returns:
        str: One bracketed line of type, date, and title, plus tag and summary lines when present.
    """
    if not start:
        when = "undated"
    elif start == end:
        when = start
    else:
        when = f"{start} to {end}"

    lines = [f"[{doc_type or 'unknown'} | {when} | {title or 'untitled'}]"]
    if tags:
        lines.append(f"Tags: {tags}")
    if summary:
        lines.append(f"Summary: {summary}")
    return "\n".join(lines)


def _enqueue(conn: sqlite3.Connection) -> None:
    """Queue an index job for every document whose pages all have text, if it has none yet.

    Text, not extraction, is the prerequisite: that way search works without running
    the LLM at all. Extraction still sets its document's index job back to pending, so
    a document is re-chunked with its title, tags, and summary once those exist.

    Args:
        conn (sqlite3.Connection): Open archive database.
    """
    ready = conn.execute(
        "SELECT d.doc_id AS target_id FROM documents d"
        " WHERE EXISTS (SELECT 1 FROM pages p WHERE p.doc_id = d.doc_id)"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM pages p WHERE p.doc_id = d.doc_id AND NOT EXISTS ("
        "     SELECT 1 FROM jobs o WHERE o.target_id = p.page_id AND o.stage = ?"
        "     AND o.version = ? AND o.status = 'done'))"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM jobs j WHERE j.target_id = d.doc_id AND j.stage = ? AND j.version = ?)",
        (config.OCR_STAGE, config.OCR_STAGE_VERSION, config.INDEX_STAGE, config.INDEX_STAGE_VERSION),
    ).fetchall()
    ts = now()
    with conn:
        for row in ready:
            conn.execute(
                "INSERT OR IGNORE INTO jobs (job_id, target_id, stage, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(ULID()), row["target_id"], config.INDEX_STAGE, config.INDEX_STAGE_VERSION, ts),
            )


def _chunk_document(conn: sqlite3.Connection, paths: Paths, job: sqlite3.Row) -> list[tuple[str, str]]:
    """Split every page of a document into header-prefixed chunks.

    A code project is embedded as one chunk (its description and file list); its files
    stay keyword-only. Pages of a long textbook PDF that were never rendered (no image)
    are left out: they stay searchable by keyword but are not embedded. Text-only
    files (Word, slides) are always embedded.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        job (sqlite3.Row): Index job joined with its document (see `run_index`).

    Returns:
        list[tuple[str, str]]: (page_id, chunk text) pairs, in page order. Blank pages are skipped.
    """
    header = _header(
        job["doc_type"],
        job["doc_date_start"],
        job["doc_date_end"],
        job["title"],
        job["tags"],
        job["summary"],
    )
    if job["doc_type"] == "project":
        files = conn.execute(
            "SELECT p.page_id, r.source_filename FROM pages p"
            " JOIN raw_files r ON r.sha256 = p.raw_sha256 WHERE p.doc_id = ? ORDER BY p.page_no",
            (job["doc_id"],),
        ).fetchall()
        listing = "\n".join(f["source_filename"] for f in files[: config.PROJECT_TREE_FILES])
        return [(files[0]["page_id"], f"{header}\nFiles:\n{listing}")] if files else []

    pages = conn.execute(
        "SELECT p.page_id, t.text_path FROM pages p"
        " JOIN page_text t ON t.page_id = p.page_id AND t.is_active = 1"
        " JOIN raw_files r ON r.sha256 = p.raw_sha256"
        " WHERE p.doc_id = ? AND (p.image_path IS NOT NULL OR r.raw_path NOT LIKE '%.pdf')"
        " ORDER BY p.page_no",
        (job["doc_id"],),
    ).fetchall()

    chunks = []
    for page in pages:
        text = (paths.archive / page["text_path"]).read_text().strip()
        if not text:
            continue
        for piece in _split(text, config.CHUNK_MAX_CHARS, config.CHUNK_OVERLAP_CHARS):
            chunks.append((page["page_id"], f"{header}\n{piece}"))
    return chunks


def _store_chunks(
    conn: sqlite3.Connection, job_id: str, doc_id: str, chunks: list[tuple[str, str]], vectors: np.ndarray
) -> None:
    """Replace a document's chunks and embeddings, and mark the job done, in one transaction.

    Args:
        conn (sqlite3.Connection): Open archive database.
        job_id (str): Index job to mark done.
        doc_id (str): Document the chunks belong to.
        chunks (list[tuple[str, str]]): (page_id, chunk text) pairs.
        vectors (np.ndarray): One embedding per chunk, same order.
    """
    with conn:
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.executemany(
            "INSERT INTO chunks (chunk_id, doc_id, page_id, text, embed_model, embedding)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (str(ULID()), doc_id, page_id, text, config.EMBED_MODEL, vector.tobytes())
                for (page_id, text), vector in zip(chunks, vectors, strict=True)
            ],
        )
        conn.execute(
            "UPDATE jobs SET status = 'done', error = NULL, updated_at = ? WHERE job_id = ?",
            (now(), job_id),
        )


# --- end private functions ---


def embed(texts: list[str], prefix: str) -> np.ndarray:
    """Embed texts with the local embedding model.

    Args:
        texts (list[str]): Texts to embed.
        prefix (str): `EMBED_DOC_PREFIX` for stored text, `EMBED_QUERY_PREFIX` for a query.

    Returns:
        np.ndarray: One L2-normalized float32 row per text; shape (len(texts), dimensions).
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    response = ollama.embed(model=config.EMBED_MODEL, input=[prefix + t for t in texts])
    vectors = np.array(response.embeddings, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


def run_index(conn: sqlite3.Connection, paths: Paths) -> Iterator[IndexResult]:
    """Queue extracted documents, then chunk and embed each pending one, yielding as each finishes.

    Failures (e.g. Ollama not running) are recorded on the job and don't stop the run.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.

    Yields:
        IndexResult: Outcome for each document, in the order the jobs were queued.
    """
    _enqueue(conn)
    jobs = conn.execute(
        "SELECT j.job_id, d.doc_id, d.doc_type, d.doc_date_start, d.doc_date_end, d.title,"
        " d.summary,"
        " (SELECT group_concat(t.tag, ', ') FROM tags t WHERE t.doc_id = d.doc_id) AS tags,"
        " (SELECT r.source_filename FROM pages p JOIN raw_files r ON r.sha256 = p.raw_sha256"
        "  WHERE p.doc_id = d.doc_id ORDER BY p.page_no LIMIT 1) AS source_filename"
        " FROM jobs j JOIN documents d ON d.doc_id = j.target_id"
        " WHERE j.stage = ? AND j.version = ? AND j.status = 'pending'"
        " ORDER BY j.job_id",
        (config.INDEX_STAGE, config.INDEX_STAGE_VERSION),
    ).fetchall()

    for job in jobs:
        doc_id, name = job["doc_id"], job["source_filename"]
        try:
            chunks = _chunk_document(conn, paths, job)
            vectors = embed([text for _, text in chunks], config.EMBED_DOC_PREFIX)
            _store_chunks(conn, job["job_id"], doc_id, chunks, vectors)
        except Exception as e:
            with conn:
                conn.execute(
                    "UPDATE jobs SET status = 'error', error = ?, updated_at = ? WHERE job_id = ?",
                    (str(e), now(), job["job_id"]),
                )
            yield IndexResult(doc_id, name, "error", error=str(e))
            continue
        yield IndexResult(doc_id, name, "done", job["title"], len(chunks))
