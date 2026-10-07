"""Hybrid search: keyword (SQLite FTS5) and meaning (embeddings), merged with reciprocal rank fusion.

Keyword search is good at exact words, names, and numbers; meaning search
finds pages about the same thing even when the words differ. Each produces a
ranked list of pages; reciprocal rank fusion (RRF) gives every page
1 / (RRF_K + rank) points from each list it appears in, so pages that rank
well in both come out on top.
"""

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from attic import config, index


@dataclass(frozen=True)
class SearchHit:
    """One matching page.

    Attributes:
        doc_id (str): Document the page belongs to.
        page_id (str): The matching page.
        page_no (int): 1-based page number within the document.
        title (str | None): Document title, if extracted.
        doc_type (str | None): Document type, if extracted.
        date_start (str | None): Earliest date the document was written.
        date_end (str | None): Latest date the document was written.
        source_filename (str): Original name of the raw file.
        image_path (str | None): Page image, relative to `archive/`;
            None for a text-native file, which has no image.
        snippet (str): Short excerpt of the page text around the first matching word.
        score (float): Combined RRF score; higher is better.
        matched_by (str): "keyword", "meaning", or "both".
    """

    doc_id: str
    page_id: str
    page_no: int
    title: str | None
    doc_type: str | None
    date_start: str | None
    date_end: str | None
    source_filename: str
    image_path: str | None
    snippet: str
    score: float
    matched_by: str


# --- start private functions ---


def _terms(query: str) -> list[str]:
    """Lowercased words of 3+ characters (FTS5's trigram index can't match shorter ones), deduplicated."""
    return list(dict.fromkeys(w for w in re.findall(r"\w+", query.lower()) if len(w) >= 3))


def _filters(doc_type: str | None, year: int | None) -> tuple[str, list[object]]:
    """Build the SQL condition (on documents `d`) for the optional type and year filters.

    Args:
        doc_type (str | None): Only this document type.
        year (int | None): Only documents whose date range overlaps this year.

    Returns:
        tuple[str, list[object]]: SQL condition and its `?` parameters.
    """
    clauses: list[str] = ["1 = 1"]
    params: list[object] = []
    if doc_type:
        clauses.append("d.doc_type = ?")
        params.append(doc_type)
    if year:
        clauses.append("d.doc_date_start <= ? AND d.doc_date_end >= ?")
        params += [f"{year}-12-31", f"{year}-01-01"]
    return " AND ".join(clauses), params


def _keyword_ranks(conn: sqlite3.Connection, query: str, where: str, params: list[object]) -> list[str]:
    """Rank pages by FTS5 keyword match, best first.

    Each query word is quoted, so punctuation in the query can't break FTS5
    syntax, and words are OR'ed so a page doesn't need every word to match.

    Args:
        conn (sqlite3.Connection): Open archive database.
        query (str): The user's search text.
        where (str): Filter condition from `_filters`.
        params (list[object]): Parameters for `where`.

    Returns:
        list[str]: Page IDs, best match first.
    """
    terms = _terms(query)
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    rows = conn.execute(
        "SELECT f.page_id FROM pages_fts f JOIN documents d ON d.doc_id = f.doc_id"
        f" WHERE pages_fts MATCH ? AND {where} ORDER BY f.rank LIMIT ?",
        [match, *params, config.SEARCH_CANDIDATES],
    ).fetchall()
    return [row["page_id"] for row in rows]


def _meaning_ranks(conn: sqlite3.Connection, query: str, where: str, params: list[object]) -> list[str]:
    """Rank pages by embedding similarity to the query, best first.

    A page's score is its best-matching chunk. Only chunks made with the
    current embedding model are compared.

    Args:
        conn (sqlite3.Connection): Open archive database.
        query (str): The user's search text.
        where (str): Filter condition from `_filters`.
        params (list[object]): Parameters for `where`.

    Returns:
        list[str]: Page IDs, most similar first.
    """
    rows = conn.execute(
        "SELECT c.page_id, c.embedding FROM chunks c JOIN documents d ON d.doc_id = c.doc_id"
        f" WHERE c.embed_model = ? AND {where}",
        [config.EMBED_MODEL, *params],
    ).fetchall()
    if not rows:
        return []
    matrix = np.vstack([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows])
    similarity = matrix @ index.embed([query], config.EMBED_QUERY_PREFIX)[0]

    ranked: list[str] = []
    for i in np.argsort(-similarity):
        page_id = rows[i]["page_id"]
        if page_id not in ranked:
            ranked.append(page_id)
        if len(ranked) == config.SEARCH_CANDIDATES:
            break
    return ranked


def _snippet(text: str, query: str, width: int = 160) -> str:
    """Cut a short, single-line excerpt of the text around the first query word found.

    Args:
        text (str): Full page text.
        query (str): The user's search text.
        width (int): Approximate excerpt length in characters.

    Returns:
        str: The excerpt, with "…" where text was cut off.
    """
    flat = " ".join(text.split())
    lower = flat.lower()
    hits = [i for t in _terms(query) if (i := lower.find(t)) >= 0]
    start = max(0, min(hits) - width // 3) if hits else 0
    end = min(len(flat), start + width)
    return ("…" if start > 0 else "") + flat[start:end] + ("…" if end < len(flat) else "")


# --- end private functions ---


def search(
    conn: sqlite3.Connection,
    query: str,
    doc_type: str | None = None,
    year: int | None = None,
    limit: int = 10,
) -> list[SearchHit]:
    """Find the pages that best match a query, using keyword and meaning search together.

    Args:
        conn (sqlite3.Connection): Open archive database.
        query (str): What to search for, in plain words.
        doc_type (str | None): Only return documents of this type.
        year (int | None): Only return documents dated in this year (undated documents are excluded).
        limit (int): Maximum number of pages to return.

    Returns:
        list[SearchHit]: Matching pages, best first.
    """
    where, params = _filters(doc_type, year)
    keyword = _keyword_ranks(conn, query, where, params)
    meaning = _meaning_ranks(conn, query, where, params)

    scores: dict[str, float] = defaultdict(float)
    for ranks in (keyword, meaning):
        for rank, page_id in enumerate(ranks[: config.RRF_DEPTH], start=1):
            scores[page_id] += 1 / (config.RRF_K + rank)
    top = sorted(scores, key=lambda page_id: -scores[page_id])[:limit]
    if not top:
        return []

    marks = ", ".join("?" * len(top))
    rows = conn.execute(
        "SELECT p.page_id, p.doc_id, p.page_no, p.image_path, d.title, d.doc_type,"
        " d.doc_date_start, d.doc_date_end, r.source_filename, f.text"
        " FROM pages p JOIN documents d ON d.doc_id = p.doc_id"
        " JOIN raw_files r ON r.sha256 = p.raw_sha256"
        " LEFT JOIN pages_fts f ON f.page_id = p.page_id"
        f" WHERE p.page_id IN ({marks})",
        top,
    ).fetchall()
    by_page = {row["page_id"]: row for row in rows}

    keyword_set, meaning_set = set(keyword), set(meaning)
    hits = []
    for page_id in top:
        row = by_page[page_id]
        in_kw, in_mean = page_id in keyword_set, page_id in meaning_set
        hits.append(
            SearchHit(
                doc_id=row["doc_id"],
                page_id=page_id,
                page_no=row["page_no"],
                title=row["title"],
                doc_type=row["doc_type"],
                date_start=row["doc_date_start"],
                date_end=row["doc_date_end"],
                source_filename=row["source_filename"],
                image_path=row["image_path"],
                snippet=_snippet(row["text"] or "", query),
                score=scores[page_id],
                matched_by="both" if in_kw and in_mean else "keyword" if in_kw else "meaning",
            )
        )
    return hits
