"""Merge several documents into one, e.g. when each page of an essay was photographed separately.

Raw files and page images are never touched: a page only changes which
document it belongs to and its page number. The merged document's extracted
facts and search chunks are cleared, so the next `attic extract` and
`attic index` treat it as one new document.
"""

import sqlite3

from attic import config

# --- start private functions ---


def _find_document(conn: sqlite3.Connection, ref: str) -> str:
    """Resolve a document ID or an original filename to a document ID.

    Args:
        conn (sqlite3.Connection): Open archive database.
        ref (str): A doc_id, or a raw file's original name (e.g. "20261004_193706.jpg").

    Returns:
        str: The document ID.

    Raises:
        ValueError: If nothing matches.
    """
    if conn.execute("SELECT 1 FROM documents WHERE doc_id = ?", (ref,)).fetchone():
        return ref
    row = conn.execute(
        "SELECT p.doc_id FROM pages p JOIN raw_files r ON r.sha256 = p.raw_sha256"
        " WHERE r.source_filename = ? ORDER BY p.raw_page_no LIMIT 1",
        (ref,),
    ).fetchone()
    if not row:
        raise ValueError(f"no document or file named {ref!r}")
    return str(row[0])


# --- end private functions ---


def merge_documents(conn: sqlite3.Connection, refs: list[str]) -> tuple[str, int]:
    """Combine documents into the first one, keeping pages in the order given.

    Args:
        conn (sqlite3.Connection): Open archive database.
        refs (list[str]): Documents to merge, by doc_id or original filename, in page order.

    Returns:
        tuple[str, int]: The merged document's ID and its new page count.

    Raises:
        ValueError: If a reference doesn't match, fewer than two distinct documents are given.
    """
    doc_ids = list(dict.fromkeys(_find_document(conn, ref) for ref in refs))
    if len(doc_ids) < 2:
        raise ValueError("give at least two different documents to merge")
    target, others = doc_ids[0], doc_ids[1:]

    page_ids = [
        row[0]
        for doc_id in doc_ids
        for row in conn.execute("SELECT page_id FROM pages WHERE doc_id = ? ORDER BY page_no", (doc_id,))
    ]
    marks = ", ".join("?" * len(others))
    page_marks = ", ".join("?" * len(page_ids))
    with conn:
        # Two passes (negative, then positive) so UNIQUE(doc_id, page_no) never collides mid-update.
        for n, page_id in enumerate(page_ids, start=1):
            conn.execute("UPDATE pages SET doc_id = ?, page_no = ? WHERE page_id = ?", (target, -n, page_id))
        conn.execute("UPDATE pages SET page_no = -page_no WHERE doc_id = ?", (target,))
        conn.execute(f"UPDATE pages_fts SET doc_id = ? WHERE page_id IN ({page_marks})", [target, *page_ids])

        for table in ("tags", "people", "amounts", "chunks"):
            conn.execute(f"DELETE FROM {table} WHERE doc_id IN ({marks}, ?)", [*others, target])
        conn.execute(
            f"DELETE FROM jobs WHERE target_id IN ({marks}, ?) AND stage IN (?, ?)",
            [*others, target, config.EXTRACT_STAGE, config.INDEX_STAGE],
        )
        conn.execute(f"DELETE FROM documents WHERE doc_id IN ({marks})", others)
        conn.execute(
            "UPDATE documents SET title = NULL, doc_type = NULL, doc_date_start = NULL, doc_date_end = NULL,"
            " summary = NULL WHERE doc_id = ?",
            (target,),
        )
    return target, len(page_ids)
