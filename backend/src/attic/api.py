"""HTTP API over the archive, for the web frontend.

Read endpoints list and show documents; write endpoints correct what extraction
got wrong. Page images are served straight from `archive/`. Everything stays on
localhost: the archive is personal and there is no authentication.
"""

import sqlite3
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from attic import config
from attic.ask import ask
from attic.chat import create_chat, delete_chat, get_chat, list_chats, rename_chat, send_message
from attic.config import Paths, get_paths
from attic.db import connect, init_db
from attic.extract import DocType, SpendCategory, VehicleService
from attic.merge import merge_documents
from attic.search import search

AmountKind = Literal["receipt", "estimate", "award"]
"""Whether money on a document was paid, quoted, or received."""


class DocumentPatch(BaseModel):
    """Corrections to a document's extracted facts. Omitted fields are left alone.

    Attributes:
        title (str | None): New title.
        doc_type (DocType | None): New document type.
        date_start (str | None): New start date, YYYY-MM-DD.
        date_end (str | None): New end date, YYYY-MM-DD.
        summary (str | None): New summary.
        caption (str | None): New photo description.
        sensitive (bool | None): Whether to treat the document as sensitive.
        tags (list[str] | None): Replaces all tags.
        people (list[str] | None): Replaces all people.
    """

    title: str | None = None
    doc_type: DocType | None = None
    date_start: str | None = None
    date_end: str | None = None
    summary: str | None = None
    caption: str | None = None
    sensitive: bool | None = None
    tags: list[str] | None = None
    people: list[str] | None = None


class AmountPatch(BaseModel):
    """The money on a document. Sent whole: posting it replaces any existing row.

    Attributes:
        kind (AmountKind): Paid, quoted, or received.
        party (str | None): Who was paid, or who paid you.
        date (str | None): Date of the payment, quote, or award.
        amount (float | None): Grand total in dollars.
        odometer (int | None): Vehicle mileage.
        service (VehicleService | None): What vehicle work this was.
        category (SpendCategory | None): Spending category.
    """

    kind: AmountKind = "receipt"
    party: str | None = None
    date: str | None = None
    amount: float | None = None
    odometer: int | None = None
    service: VehicleService | None = None
    category: SpendCategory | None = None


class MergeRequest(BaseModel):
    """Documents to combine into one.

    Attributes:
        doc_ids (list[str]): Documents to merge, in page order; the first one is kept.
    """

    doc_ids: list[str] = Field(min_length=2)


class AskRequest(BaseModel):
    """A question for the archive.

    Attributes:
        question (str): The question, in plain English.
    """

    question: str = Field(min_length=1)


class ChatCreate(BaseModel):
    """A new chat.

    Attributes:
        title (str | None): Title; omitted means it is named after its first question.
    """

    title: str | None = None


class ChatRename(BaseModel):
    """A chat's new title.

    Attributes:
        title (str): The title.
    """

    title: str


class ChatMessageRequest(BaseModel):
    """A message in a chat.

    Attributes:
        text (str): The question or follow-up, in plain English.
    """

    text: str = Field(min_length=1)


app = FastAPI(title="attic", description="Local archive of scanned paper.")
# The Next.js dev server runs on another port, so the browser needs permission to call this one.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- start private functions ---


def _paths() -> Paths:
    return get_paths()


def _conn(paths: Annotated[Paths, Depends(_paths)]) -> Iterator[sqlite3.Connection]:
    """Open the archive database for one request.

    Args:
        paths (Paths): Archive locations.

    Yields:
        sqlite3.Connection: Open connection, closed when the request ends.

    Raises:
        HTTPException: 503 if there is no archive yet.
    """
    if not paths.db.exists():
        raise HTTPException(503, f"no archive at {paths.archive}; run `attic init` first")
    conn = connect(paths.db)
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(_conn)]
P = Annotated[Paths, Depends(_paths)]


def _rows(cursor: sqlite3.Cursor | Any) -> list[dict[str, Any]]:
    """Turn query results into plain dictionaries for JSON."""
    return [dict(row) for row in cursor]


def _require_document(conn: sqlite3.Connection, doc_id: str) -> None:
    """Raise 404 unless the document exists.

    Args:
        conn (sqlite3.Connection): Open archive database.
        doc_id (str): Document to check.

    Raises:
        HTTPException: 404 if there is no such document.
    """
    if not conn.execute("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)).fetchone():
        raise HTTPException(404, f"no document {doc_id}")


def _page_file(conn: sqlite3.Connection, paths: Paths, page_id: str, column: str) -> FileResponse:
    """Serve a page's image or thumbnail from disk.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        page_id (str): Page to serve.
        column (str): "image_path" or "thumb_path".

    Returns:
        FileResponse: The image file.

    Raises:
        HTTPException: 404 if the page is unknown, has no image (a text-native
            file such as .docx), or its file is missing from disk.
    """
    row = conn.execute(f"SELECT {column} AS path FROM pages WHERE page_id = ?", (page_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"no page {page_id}")
    if row["path"] is None:
        raise HTTPException(404, f"page {page_id} has no image")
    path = paths.archive / row["path"]
    if not path.exists():
        raise HTTPException(404, f"missing file {row['path']}")
    return FileResponse(path)


# --- end private functions ---


@app.get("/api/stats")
def get_stats(conn: Db) -> dict[str, Any]:
    """Counts for the dashboard: documents, pages, pending work, and types by frequency."""

    def count(table: str) -> int:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    return {
        "documents": count("documents"),
        "pages": count("pages"),
        "raw_files": count("raw_files"),
        "jobs": _rows(
            conn.execute("SELECT stage, status, count(*) AS n FROM jobs GROUP BY 1, 2 ORDER BY 1, 2")
        ),
        "doc_types": _rows(
            conn.execute(
                "SELECT coalesce(doc_type, 'unextracted') AS doc_type, count(*) AS n"
                " FROM documents GROUP BY 1 ORDER BY n DESC"
            )
        ),
        "years": _rows(
            conn.execute(
                "SELECT substr(doc_date_start, 1, 4) AS year, count(*) AS n FROM documents"
                " WHERE doc_date_start IS NOT NULL GROUP BY 1 ORDER BY 1 DESC"
            )
        ),
    }


@app.get("/api/documents")
def list_documents(
    conn: Db,
    doc_type: str | None = None,
    year: int | None = None,
    has_amount: bool | None = None,
    sort: Literal["date", "title", "amount"] = "date",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """List documents with their money and page count, newest first by default.

    Args:
        conn (Db): Open archive database.
        doc_type (str | None): Only this document type.
        year (int | None): Only documents whose date range touches this year.
        has_amount (bool | None): True for only documents with money, False for only those without.
        sort (str): "date", "title", or "amount".
        limit (int): Maximum rows to return.
        offset (int): Rows to skip, for paging.

    Returns:
        dict[str, Any]: `total` matching documents and the requested page of `documents`.
    """
    where, params = ["1 = 1"], []
    if doc_type:
        where.append("d.doc_type = ?")
        params.append(doc_type)
    if year:
        where.append("d.doc_date_start <= ? AND d.doc_date_end >= ?")
        params += [f"{year}-12-31", f"{year}-01-01"]
    if has_amount is not None:
        where.append("a.doc_id IS NOT NULL" if has_amount else "a.doc_id IS NULL")
    clause = " AND ".join(where)
    order = {
        "date": "d.doc_date_start IS NULL, d.doc_date_start DESC",
        "title": "d.title COLLATE NOCASE",
        "amount": "a.amount_cents IS NULL, a.amount_cents DESC",
    }[sort]

    total = conn.execute(
        f"SELECT count(*) FROM documents d LEFT JOIN amounts a USING (doc_id) WHERE {clause}", params
    ).fetchone()[0]
    documents = _rows(
        conn.execute(
            "SELECT d.doc_id, d.title, d.doc_type, d.doc_date_start AS date_start,"
            " d.doc_date_end AS date_end, d.summary, d.caption, d.sensitive,"
            " a.kind, a.party, a.amount_cents / 100.0 AS amount, a.category, a.service, a.odometer,"
            " (SELECT count(*) FROM pages p WHERE p.doc_id = d.doc_id) AS page_count,"
            " (SELECT p.page_id FROM pages p WHERE p.doc_id = d.doc_id"
            "   AND p.image_path IS NOT NULL ORDER BY p.page_no LIMIT 1) AS first_page_id,"
            " (SELECT group_concat(t.tag, ', ') FROM tags t WHERE t.doc_id = d.doc_id) AS tags"
            " FROM documents d LEFT JOIN amounts a USING (doc_id)"
            f" WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
    )
    return {"total": total, "documents": documents}


@app.get("/api/documents/{doc_id}")
def get_document(conn: Db, paths: P, doc_id: str) -> dict[str, Any]:
    """One document in full: its facts, money, tags, people, and every page with its text.

    Args:
        conn (Db): Open archive database.
        paths (P): Archive locations.
        doc_id (str): Document to fetch.

    Returns:
        dict[str, Any]: The document, its `amount`, `tags`, `people`, and `pages`.

    Raises:
        HTTPException: 404 if there is no such document.
    """
    document = conn.execute(
        "SELECT doc_id, title, doc_type, doc_date_start AS date_start, doc_date_end AS date_end,"
        " summary, caption, sensitive, created_at FROM documents WHERE doc_id = ?",
        (doc_id,),
    ).fetchone()
    if not document:
        raise HTTPException(404, f"no document {doc_id}")

    pages = []
    for page in conn.execute(
        "SELECT p.page_id, p.page_no, r.source_filename, t.text_path, t.engine, t.confidence,"
        " p.image_path IS NOT NULL AS has_image"
        " FROM pages p JOIN raw_files r ON r.sha256 = p.raw_sha256"
        " LEFT JOIN page_text t ON t.page_id = p.page_id AND t.is_active = 1"
        " WHERE p.doc_id = ? ORDER BY p.page_no",
        (doc_id,),
    ):
        page = dict(page)
        text_path = page.pop("text_path", None)
        page["text"] = (paths.archive / text_path).read_text() if text_path else ""
        pages.append(page)

    amount = conn.execute(
        "SELECT kind, party, date, amount_cents / 100.0 AS amount, odometer, service, category"
        " FROM amounts WHERE doc_id = ?",
        (doc_id,),
    ).fetchone()
    return {
        **dict(document),
        "amount": dict(amount) if amount else None,
        "tags": [r[0] for r in conn.execute("SELECT tag FROM tags WHERE doc_id = ? ORDER BY tag", (doc_id,))],
        "people": [r[0] for r in conn.execute("SELECT name FROM people WHERE doc_id = ?", (doc_id,))],
        "pages": pages,
    }


@app.patch("/api/documents/{doc_id}")
def patch_document(conn: Db, doc_id: str, patch: DocumentPatch) -> dict[str, str]:
    """Correct a document's facts. Only the fields sent are changed.

    Args:
        conn (Db): Open archive database.
        doc_id (str): Document to correct.
        patch (DocumentPatch): The corrections.

    Returns:
        dict[str, str]: `{"status": "ok"}`.

    Raises:
        HTTPException: 404 if there is no such document.
    """
    _require_document(conn, doc_id)
    fields = patch.model_dump(exclude_unset=True)
    tags, people = fields.pop("tags", None), fields.pop("people", None)
    columns = {"date_start": "doc_date_start", "date_end": "doc_date_end"}

    with conn:
        if fields:
            assignments = ", ".join(f"{columns.get(k, k)} = ?" for k in fields)
            conn.execute(f"UPDATE documents SET {assignments} WHERE doc_id = ?", [*fields.values(), doc_id])
        if tags is not None:
            conn.execute("DELETE FROM tags WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO tags (doc_id, tag) VALUES (?, ?)",
                [(doc_id, t.strip().lower()) for t in tags if t.strip()],
            )
        if people is not None:
            conn.execute("DELETE FROM people WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO people (doc_id, name) VALUES (?, ?)",
                [(doc_id, p.strip()) for p in people if p.strip()],
            )
        # The chunk header carries type, date and title, so a correction means re-indexing.
        conn.execute(
            "UPDATE jobs SET status = 'pending' WHERE target_id = ? AND stage = ?",
            (doc_id, config.INDEX_STAGE),
        )
    return {"status": "ok"}


@app.put("/api/documents/{doc_id}/amount")
def put_amount(conn: Db, doc_id: str, amount: AmountPatch) -> dict[str, str]:
    """Set the money on a document, replacing whatever was there.

    Args:
        conn (Db): Open archive database.
        doc_id (str): Document to update.
        amount (AmountPatch): The money fields.

    Returns:
        dict[str, str]: `{"status": "ok"}`.

    Raises:
        HTTPException: 404 if there is no such document.
    """
    _require_document(conn, doc_id)
    cents = round(amount.amount * 100) if amount.amount is not None else None
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO amounts"
            " (doc_id, kind, party, date, amount_cents, odometer, service, category)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                amount.kind,
                amount.party,
                amount.date,
                cents,
                amount.odometer,
                amount.service,
                amount.category,
            ),
        )
    return {"status": "ok"}


@app.delete("/api/documents/{doc_id}/amount")
def delete_amount(conn: Db, doc_id: str) -> dict[str, str]:
    """Remove the money from a document (e.g. when it was picked up by mistake)."""
    _require_document(conn, doc_id)
    with conn:
        conn.execute("DELETE FROM amounts WHERE doc_id = ?", (doc_id,))
    return {"status": "ok"}


@app.delete("/api/documents/{doc_id}")
def delete_document(conn: Db, doc_id: str) -> dict[str, str]:
    """Remove a document and its pages from the archive, e.g. a duplicate scan.

    The original file and page images are kept on disk, so nothing is lost;
    only the database rows go.

    Args:
        conn (Db): Open archive database.
        doc_id (str): Document to remove.

    Returns:
        dict[str, str]: `{"status": "ok"}`.

    Raises:
        HTTPException: 404 if there is no such document.
    """
    _require_document(conn, doc_id)
    page_ids = [r[0] for r in conn.execute("SELECT page_id FROM pages WHERE doc_id = ?", (doc_id,))]
    marks = ", ".join("?" * len(page_ids))
    with conn:
        if page_ids:
            conn.execute(f"DELETE FROM pages_fts WHERE page_id IN ({marks})", page_ids)
            conn.execute(f"DELETE FROM page_text WHERE page_id IN ({marks})", page_ids)
            conn.execute(f"DELETE FROM jobs WHERE target_id IN ({marks})", page_ids)
        for table in ("tags", "people", "amounts", "chunks", "pages"):
            conn.execute(f"DELETE FROM {table} WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM jobs WHERE target_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
    return {"status": "ok"}


@app.post("/api/documents/merge")
def post_merge(conn: Db, request: MergeRequest) -> dict[str, Any]:
    """Combine documents into one, for pages of the same item photographed separately.

    Args:
        conn (Db): Open archive database.
        request (MergeRequest): The documents to merge, in page order.

    Returns:
        dict[str, Any]: The surviving `doc_id` and its `pages` count.

    Raises:
        HTTPException: 400 if the documents can't be merged.
    """
    try:
        doc_id, pages = merge_documents(conn, request.doc_ids)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"doc_id": doc_id, "pages": pages}


@app.get("/api/search")
def get_search(
    conn: Db,
    q: str,
    doc_type: str | None = None,
    year: int | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Find pages by keyword and meaning.

    Args:
        conn (Db): Open archive database.
        q (str): What to search for.
        doc_type (str | None): Only this document type.
        year (int | None): Only documents dated in this year.
        limit (int): Maximum results.

    Returns:
        dict[str, Any]: The matching `hits`, best first.
    """
    hits = search(conn, q, doc_type, year, limit)
    return {"hits": [hit.__dict__ for hit in hits]}


@app.post("/api/ask")
def post_ask(conn: Db, paths: P, request: AskRequest) -> dict[str, Any]:
    """Answer a question from the archive, via RAG or text-to-SQL.

    Args:
        conn (Db): Open archive database.
        paths (P): Archive locations.
        request (AskRequest): The question.

    Returns:
        dict[str, Any]: The answer, its sources, and any SQL that was run.

    Raises:
        HTTPException: 503 if the local model isn't reachable.
    """
    try:
        answer = ask(conn, paths, request.question)
    except Exception as e:
        raise HTTPException(503, f"could not reach the local model: {e}") from e
    return {
        "question": answer.question,
        "route": answer.route,
        "text": answer.text,
        "note": answer.note,
        "sql": answer.sql,
        "rows": answer.rows,
        "sources": [hit.__dict__ for hit in answer.sources],
    }


@app.get("/api/chats")
def get_chats(conn: Db) -> list[dict[str, Any]]:
    """Every chat, most recently active first."""
    return [asdict(chat) for chat in list_chats(conn)]


@app.post("/api/chats")
def post_chat(conn: Db, request: ChatCreate) -> dict[str, Any]:
    """Start a new, empty chat."""
    return asdict(create_chat(conn, request.title))


@app.get("/api/chats/{chat_id}")
def get_chat_messages(conn: Db, chat_id: str) -> dict[str, Any]:
    """One chat with all its messages, oldest first.

    Args:
        conn (Db): Open archive database.
        chat_id (str): Chat to read.

    Returns:
        dict[str, Any]: `{"chat": ..., "messages": [...]}`.

    Raises:
        HTTPException: 404 if there is no such chat.
    """
    try:
        chat, messages = get_chat(conn, chat_id)
    except KeyError as e:
        raise HTTPException(404, f"no chat {chat_id}") from e
    return {"chat": asdict(chat), "messages": [asdict(m) for m in messages]}


@app.patch("/api/chats/{chat_id}")
def patch_chat(conn: Db, chat_id: str, request: ChatRename) -> dict[str, Any]:
    """Rename a chat.

    Args:
        conn (Db): Open archive database.
        chat_id (str): Chat to rename.
        request (ChatRename): The new title.

    Returns:
        dict[str, Any]: The renamed chat.

    Raises:
        HTTPException: 404 if there is no such chat.
    """
    try:
        return asdict(rename_chat(conn, chat_id, request.title))
    except KeyError as e:
        raise HTTPException(404, f"no chat {chat_id}") from e


@app.delete("/api/chats/{chat_id}")
def delete_chat_messages(conn: Db, chat_id: str) -> dict[str, str]:
    """Delete a chat and its messages; documents are not touched.

    Args:
        conn (Db): Open archive database.
        chat_id (str): Chat to delete.

    Returns:
        dict[str, str]: `{"status": "ok"}`.

    Raises:
        HTTPException: 404 if there is no such chat.
    """
    if not delete_chat(conn, chat_id):
        raise HTTPException(404, f"no chat {chat_id}")
    return {"status": "ok"}


@app.post("/api/chats/{chat_id}/messages")
def post_chat_message(conn: Db, paths: P, chat_id: str, request: ChatMessageRequest) -> dict[str, Any]:
    """Ask a question or follow-up in a chat.

    Args:
        conn (Db): Open archive database.
        paths (P): Archive locations.
        chat_id (str): Chat to add to.
        request (ChatMessageRequest): The message.

    Returns:
        dict[str, Any]: `{"user": ..., "assistant": ...}`, the two stored messages.

    Raises:
        HTTPException: 404 if there is no such chat, 422 if the message is blank,
            503 if the local model isn't reachable.
    """
    try:
        user, reply = send_message(conn, paths, chat_id, request.text)
    except KeyError as e:
        raise HTTPException(404, f"no chat {chat_id}") from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:
        raise HTTPException(503, f"could not reach the local model: {e}") from e
    return {"user": asdict(user), "assistant": asdict(reply)}


@app.get("/api/pages/{page_id}/image")
def get_page_image(conn: Db, paths: P, page_id: str) -> FileResponse:
    """Serve a page's full-size image."""
    return _page_file(conn, paths, page_id, "image_path")


@app.get("/api/pages/{page_id}/thumb")
def get_page_thumb(conn: Db, paths: P, page_id: str) -> FileResponse:
    """Serve a page's thumbnail."""
    return _page_file(conn, paths, page_id, "thumb_path")


@app.get("/api/pages/{page_id}/pdf")
def open_page_pdf(conn: Db, page_id: str) -> RedirectResponse:
    """Send the browser to the original PDF, scrolled to this page.

    Args:
        conn (Db): Open archive database.
        page_id (str): Page to open.

    Returns:
        RedirectResponse: To `/api/raw/<file>#page=N`.

    Raises:
        HTTPException: 404 if the page is unknown or its file is not a PDF.
    """
    row = conn.execute(
        "SELECT r.raw_path, p.raw_page_no FROM pages p JOIN raw_files r ON r.sha256 = p.raw_sha256"
        " WHERE p.page_id = ?",
        (page_id,),
    ).fetchone()
    if not row or not row["raw_path"].endswith(config.PDF_SUFFIX):
        raise HTTPException(404, f"no PDF for page {page_id}")
    return RedirectResponse(f"/api/raw/{Path(row['raw_path']).name}#page={row['raw_page_no']}")


@app.get("/api/raw/{name}")
def get_raw_pdf(paths: P, name: str) -> FileResponse:
    """Serve an original PDF inline, so the browser's viewer opens it.

    Args:
        paths (P): Archive locations.
        name (str): File name inside `archive/raw/`.

    Returns:
        FileResponse: The PDF.

    Raises:
        HTTPException: 404 if the name is not a plain PDF file name or the file is missing.
    """
    path = paths.raw / name
    if Path(name).name != name or path.suffix != config.PDF_SUFFIX or not path.is_file():
        raise HTTPException(404, f"no file {name}")
    return FileResponse(path, media_type="application/pdf", content_disposition_type="inline")


@app.get("/api/meta")
def get_meta() -> dict[str, list[str]]:
    """The fixed value lists the UI offers in its dropdowns."""
    from typing import get_args

    return {
        "doc_types": list(get_args(DocType)),
        "categories": list(get_args(SpendCategory)),
        "services": list(get_args(VehicleService)),
        "amount_kinds": list(get_args(AmountKind)),
    }


def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Run the API server.

    Args:
        host (str): Address to bind; localhost only by default.
        port (int): Port to listen on.
        reload (bool): Restart automatically when the code changes.
    """
    import uvicorn

    uvicorn.run("attic.api:app" if reload else app, host=host, port=port, reload=reload)
