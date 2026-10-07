"""Extract document metadata with a local LLM: type, date range, title, people, tags, receipt fields.

A document is queued once every one of its pages has active text. The model
(via Ollama) returns JSON forced into the `DocFacts` schema; Python then cleans
up what small models get wrong (bad dates, "Last, First" names, stray casing)
before anything is written.
"""

import io
import re
import sqlite3
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import ollama
from PIL import Image
from pydantic import BaseModel
from ulid import ULID

from attic import config
from attic.config import Paths
from attic.db import now

EXTRACT_PROMPT = """You catalog scanned personal paper documents. You get the OCR text of one document \
(it may contain OCR errors) and return its metadata as JSON.

doc_type, pick exactly one:
- receipt: proof of a purchase or payment that shows an amount paid (store receipts, invoices, service bills).
- estimate: a quote, estimate, or bid for work not yet paid for.
- statement: a bank, credit card, benefits, or utility statement, or a privacy/account notice.
- letter: correspondence addressed to someone (including congratulation and acceptance letters).
- certificate: a diploma, award, scholarship certificate, or membership certificate.
- record: an official record of facts about you (shot records, test scores, transcripts, \
enrollment verifications, inspection reports).
- schoolwork: homework, essays, term papers, tests, class notes.
- id_card: an insurance, membership, or identification card.
- form: a pre-printed document with filled-in fields (prescriptions, applications, tax forms).
- program: an event booklet, schedule, flyer, or brochure.
- note: handwritten or informal notes.
- photo: a photograph with little or no text.
- other: anything else.

Other fields:
- title: short descriptive title.
- date_start/date_end: when the document was written, as YYYY-MM-DD. Exact date: start = end. \
Only month/year known: the whole range. Unknown: null. Dates in the text are US format (MM-DD-YY). \
The document was written on or before its scan date.
- people: full names of individual people mentioned, as "First Last". Not businesses.
- tags: 3-6 short lowercase topic tags.
- summary: 1-2 sentences.
- sensitive: true if the document is medical (doctors, prescriptions, diagnoses), tax, financial account, \
or identity related. Otherwise false.
- amount: fill whenever the document shows a sum of money that was paid, quoted, or awarded to you; \
otherwise null.
  - party: the other side's name: the business paid, or the organization that awarded you money. \
Never a street address or store number.
  - total: the grand total as a number. For an award, the amount awarded (if it is per year and also \
gives a total over several years, use the total).
  - odometer: only if vehicle mileage appears.
  - service: for vehicle documents only, one of oil change, inspection, repair, registration, other.
  - category: exactly one of car, medical, electronics, groceries, dining, shopping, housing, \
utilities, travel, parking, government, education, other. Anything about a vehicle \
(repairs, oil changes, parts, inspections, registration) is "car"; scholarships and tuition \
are "education"."""
"""System prompt telling the model what each output field means."""


CAPTION_PROMPT = """Describe this photograph in one sentence, for someone searching their own archive \
later. Say who or what is in it, roughly how many people, what they are doing, and the setting. \
Do not guess names or dates."""
"""System prompt for captioning a page that has little or no text."""


PROJECT_PROMPT = """You are describing a folder of source code the user wrote, so they can find it \
later. You are given the project's name, its file list, its README if any, and the start of its main \
files. Return:
- title: a short descriptive title for what the project is or does (not just the folder name)
- summary: 2-3 sentences: what it does, the main language, and anything notable (a course assignment, \
a game, a web app)
- tags: 3-8 short lowercase tags: the languages, the topic, and the kind of project"""
"""System prompt for describing a code project from its files."""


PaperType = Literal[
    "receipt",
    "estimate",
    "statement",
    "letter",
    "certificate",
    "record",
    "schoolwork",
    "id_card",
    "form",
    "program",
    "note",
    "photo",
    "other",
]
"""The types the model may pick for a scanned or written document."""

DocType = Literal[PaperType, "project"]
"""Every stored document type (matches the CHECK constraint in schema.sql); `project` is set by ingest."""


SpendCategory = Literal[
    "car",
    "medical",
    "electronics",
    "groceries",
    "dining",
    "shopping",
    "housing",
    "utilities",
    "travel",
    "parking",
    "government",
    "education",
    "other",
]
"""The fixed set of spending categories, so SQL can filter on an exact value."""


VehicleService = Literal["oil change", "inspection", "repair", "registration", "other"]
"""What a vehicle document was for, so service history can be queried."""


MONEY_PROMPT = """This is the OCR text of one receipt, invoice, estimate, quote, or award letter. \
The text may have lost its table layout, so a label like "Grand Total" and its number can be on \
different lines. Return:
- party: the business that was paid, or the organization that awarded the money (never an address \
or store number)
- date: YYYY-MM-DD
- grand_total: the final total for the whole document as a number (the largest "total" / "grand total" \
/ "amount due" / amount awarded, not a subtotal, tax, or single line item). If an award gives both a \
yearly and a multi-year total, use the multi-year total. null if you genuinely cannot tell."""
"""System prompt for a second, money-only pass when the main extraction found no total."""


AWARD_MONEY_PROMPT = """This is the OCR text of a letter awarding money, such as a scholarship,
grant, or reimbursement. The amount is usually written in a sentence rather than in a table. Return:
- party: the institution or organization awarding the money, not the name of one of its
departments or offices (so a university, not its financial aid office)
- date: YYYY-MM-DD
- grand_total: the amount awarded, as a plain number. When the letter gives both a yearly amount
and a total over several years, use the multi-year total. Use null if the letter names no award
amount; never use a number that is a phone number, address, ID, or test score."""
"""Money-only prompt for award letters, whose amounts the receipt wording misses.

Kept wrapped on real newlines rather than joined with backslashes: the joined wording makes the
model answer null for the same letters this one reads correctly.
"""


class MoneyFacts(BaseModel):
    """Just the money on a document, asked for on its own when the main pass missed it.

    Attributes:
        party (str | None): Business paid, or organization that awarded the money.
        date (str | None): Date on the document, YYYY-MM-DD.
        grand_total (float | None): The document's final total.
    """

    party: str | None
    date: str | None
    grand_total: float | None


class AmountFacts(BaseModel):
    """Money on a document, as returned by the model.

    Whether it was paid, quoted, or awarded is decided by `_kind`, not the model.

    Attributes:
        party (str | None): Business paid, or organization that awarded the money.
        date (str | None): Date of the payment, quote, or award, YYYY-MM-DD.
        total (float | None): Grand total, in dollars.
        odometer (int | None): Vehicle mileage, if printed (e.g. on an oil change receipt).
        service (str | None): For vehicle documents, what the work was.
        category (str | None): One of the fixed spending categories.
    """

    party: str | None
    date: str | None
    total: float | None
    odometer: int | None
    service: VehicleService | None
    category: SpendCategory | None


class ProjectFacts(BaseModel):
    """A code project's description, as returned by the model. Also its JSON schema.

    Attributes:
        title (str): Short descriptive title.
        summary (str): Two or three sentences.
        tags (list[str]): Short lowercase tags: languages, topic, kind of project.
    """

    title: str
    summary: str
    tags: list[str]


class DocFacts(BaseModel):
    """Document metadata, as returned by the model. Also the JSON schema the model must follow.

    Attributes:
        doc_type (str): One of the fixed paper types in the schema.
        title (str): Short descriptive title.
        date_start (str | None): Earliest date it was written, YYYY-MM-DD.
        date_end (str | None): Latest date it was written, YYYY-MM-DD.
        people (list[str]): Individual people mentioned.
        tags (list[str]): Short topic tags.
        summary (str): One or two sentences.
        sensitive (bool): Medical, tax, financial account, or identity document.
        amount (AmountFacts | None): Filled only when the document shows a sum of money.
    """

    doc_type: PaperType
    title: str
    date_start: str | None
    date_end: str | None
    people: list[str]
    tags: list[str]
    summary: str
    sensitive: bool
    amount: AmountFacts | None


@dataclass(frozen=True)
class ExtractResult:
    """Outcome of one extract job.

    Attributes:
        doc_id (str): Document that was processed.
        source_filename (str): Original name of the document's (first) raw file.
        status (str): "done", "error", or "skipped" (a photo in fast mode).
        doc_type (str | None): Stored document type; None on error.
        title (str | None): Stored title; None on error.
        date_start (str | None): Stored start date, if any.
        error (str | None): Why extraction failed; set only on error.
    """

    doc_id: str
    source_filename: str
    status: str
    doc_type: str | None = None
    title: str | None = None
    date_start: str | None = None
    error: str | None = None


# --- start private functions ---


def _clean_date(value: str | None) -> str | None:
    """Return the value if it's a real YYYY-MM-DD date, else None."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _clean_range(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    """Validate a date range: drop invalid dates, fill a missing end, and order start <= end.

    Args:
        start (str | None): Start date from the model.
        end (str | None): End date from the model.

    Returns:
        tuple[str | None, str | None]: Cleaned (start, end); both None if neither is valid.
    """
    start, end = _clean_date(start), _clean_date(end)
    start, end = start or end, end or start
    if start and end and start > end:
        start, end = end, start
    return start, end


def _clean_name(name: str) -> str:
    """Turn "Last, First" into "First Last" and trim whitespace."""
    parts = [p.strip() for p in name.split(",")]
    if len(parts) == 2 and all(parts):
        return f"{parts[1]} {parts[0]}"
    return name.strip()


def _unique(values: list[str]) -> list[str]:
    """Drop empty strings and duplicates, keeping first-seen order."""
    return list(dict.fromkeys(v for v in values if v))


def _kind(title: str, text: str) -> str:
    """Classify money on a document as "award", "estimate", or "receipt" from its wording.

    A fixed rule is used instead of the model, which was unreliable at this.
    Whole words only, so "est. Tax" doesn't make an invoice an estimate.

    Args:
        title (str): Document title from extraction.
        text (str): The document's OCR text.

    Returns:
        str: "award" (money given to you), "estimate" (a quote), or "receipt" (money you paid).
    """
    head = f"{title}\n{text[:600]}"
    if re.search(r"\b(scholarship|grant|award(ed)?|fellowship|stipend|reimbursement)\b", head, re.I):
        return "award"
    if re.search(r"\b(estimate|quote|quotation|bid)\b", head, re.I):
        return "estimate"
    return "receipt"


def _enqueue(conn: sqlite3.Connection) -> None:
    """Queue an extract job for every document whose pages all have active text.

    Documents that already have a job for the current version are skipped,
    so this is safe to call on every run.

    Args:
        conn (sqlite3.Connection): Open archive database.
    """
    ready = conn.execute(
        "SELECT d.doc_id FROM documents d"
        " WHERE EXISTS (SELECT 1 FROM pages p WHERE p.doc_id = d.doc_id)"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM pages p WHERE p.doc_id = d.doc_id AND NOT EXISTS ("
        "     SELECT 1 FROM page_text t WHERE t.page_id = p.page_id AND t.is_active = 1))"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM jobs j WHERE j.target_id = d.doc_id AND j.stage = ? AND j.version = ?)",
        (config.EXTRACT_STAGE, config.EXTRACT_STAGE_VERSION),
    ).fetchall()
    ts = now()
    with conn:
        for row in ready:
            conn.execute(
                "INSERT OR IGNORE INTO jobs (job_id, target_id, stage, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(ULID()), row["doc_id"], config.EXTRACT_STAGE, config.EXTRACT_STAGE_VERSION, ts),
            )


def _document_text(conn: sqlite3.Connection, paths: Paths, doc_id: str) -> str:
    """Join the active text of every page in a document, in page order.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        doc_id (str): Document to read.

    Returns:
        str: The document's text, with a `--- page N ---` line before each page
            when there is more than one, cut to `EXTRACT_MAX_CHARS`.
    """
    rows = conn.execute(
        "SELECT p.page_no, t.text_path FROM pages p"
        " JOIN page_text t ON t.page_id = p.page_id AND t.is_active = 1"
        " WHERE p.doc_id = ? ORDER BY p.page_no",
        (doc_id,),
    ).fetchall()
    texts = [(paths.archive / row["text_path"]).read_text().strip() for row in rows]
    if len(texts) > 1:
        texts = [f"--- page {row['page_no']} ---\n{t}" for row, t in zip(rows, texts, strict=True)]
    return "\n\n".join(texts)[: config.EXTRACT_MAX_CHARS]


def _caption(image: Path) -> str:
    """Describe a photograph, so a page with little text can still be found by searching.

    The image is shrunk to `CAPTION_MAX_SIDE` pixels first: a one-sentence description
    does not need a 12-megapixel photo, which would take the model most of a minute
    and overflow its context window.

    Args:
        image (Path): Page image.

    Returns:
        str: One sentence describing the photo.
    """
    with Image.open(image) as im:
        small = im.convert("RGB")
    small.thumbnail((config.CAPTION_MAX_SIDE, config.CAPTION_MAX_SIDE))
    jpeg = io.BytesIO()
    small.save(jpeg, "JPEG", quality=85)
    response = ollama.chat(
        model=config.EXTRACT_MODEL,
        messages=[{"role": "user", "content": CAPTION_PROMPT, "images": [jpeg.getvalue()]}],
        options={"temperature": 0, "num_ctx": config.EXTRACT_NUM_CTX},
    )
    return (response.message.content or "").strip()


def _first_image(conn: sqlite3.Connection, paths: Paths, doc_id: str) -> Path | None:
    """Find a document's first page image, for captioning.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        doc_id (str): Document to look up.

    Returns:
        Path | None: The image, or None for a document with no page image (a Word file).
    """
    row = conn.execute(
        "SELECT image_path FROM pages WHERE doc_id = ? ORDER BY page_no LIMIT 1", (doc_id,)
    ).fetchone()
    return paths.archive / row["image_path"] if row and row["image_path"] else None


def _describe(
    text: str, scanned_at: str | None, image: Path | None, single_page: bool, fast: bool = False
) -> tuple[DocFacts, str | None]:
    """Run every model call a document needs: its facts, a missing total, and a photo caption.

    Only real photos are captioned: a document the model calls a photo, or a one-page
    document with almost no text. A multi-page scan with sparse OCR is not, since a
    description of its first page says little and costs a whole extra model call.

    Touches no database, so it is safe to run from a worker thread.

    Args:
        text (str): The document's OCR text.
        scanned_at (str | None): Scan date, so the model knows the latest possible date.
        image (Path | None): First page image, or None if there is none.
        single_page (bool): Whether the document has exactly one page.
        fast (bool): Use the small text-only `EXTRACT_FAST_MODEL` and never caption.

    Returns:
        tuple[DocFacts, str | None]: The model's facts, and a caption for a photo.
    """
    if fast:
        facts = _ask_llm(text, scanned_at, config.EXTRACT_FAST_MODEL)
        _fill_missing_total(facts, text, config.EXTRACT_FAST_MODEL)
        return facts, None
    facts = _ask_llm(text, scanned_at)
    _fill_missing_total(facts, text)
    sparse = single_page and len(text.strip()) < config.CAPTION_MAX_CHARS
    if image is None or not (facts.doc_type == "photo" or sparse):
        return facts, None
    return facts, _caption(image)


def _start_job(
    pool: ThreadPoolExecutor, conn: sqlite3.Connection, paths: Paths, job: sqlite3.Row, fast: bool
) -> tuple[Future[Any], str] | None:
    """Read a job's inputs from the database and hand its model calls to the worker pool.

    Args:
        pool (ThreadPoolExecutor): Workers that run the model calls.
        conn (sqlite3.Connection): Open archive database (main thread only).
        paths (Paths): Archive locations.
        job (sqlite3.Row): Pending extract job (see `run_extract`).
        fast (bool): Use the small text-only model; photo-like documents are skipped.

    Returns:
        tuple[Future[Any], str] | None: The pending model result and the text it was given
            (kept for storing the result), or None if fast mode skips the document.
    """
    doc_id = job["doc_id"]
    if job["doc_type"] == "project":
        context = _project_context(conn, paths, doc_id, job["title"])
        if fast:
            return pool.submit(_ask_project, context, config.EXTRACT_FAST_MODEL), context
        return pool.submit(_ask_project, context), context
    text = _document_text(conn, paths, doc_id)
    pages = conn.execute("SELECT count(*) FROM pages WHERE doc_id = ?", (doc_id,)).fetchone()[0]
    image = _first_image(conn, paths, doc_id)
    if fast and image is not None and pages == 1 and len(text.strip()) < config.CAPTION_MAX_CHARS:
        return None  # a photo: the text-only model can't see it, so it waits for a normal run
    return pool.submit(_describe, text, job["scanned_at"], image, pages == 1, fast), text


# --- end private functions ---


def run_extract(conn: sqlite3.Connection, paths: Paths, limit: int | None = None) -> Iterator[ExtractResult]:
    """Queue ready documents, then extract metadata for each pending one, yielding as each finishes.

    Model calls run in `EXTRACT_WORKERS` threads so Ollama can serve several documents at
    once; every database read and write stays on the calling thread. Results arrive in the
    order they finish, not the order queued. Failures (model not running, invalid output)
    are recorded on the job and don't stop the run.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        limit (int | None): Process at most this many pending documents; None for all.
        fast (bool): Use the small text-only `EXTRACT_FAST_MODEL`; photo-like documents are
            skipped (status "skipped") and stay pending for a normal run.

    Yields:
        ExtractResult: Outcome for each document, as each finishes.
    """
    _enqueue(conn)
    jobs = iter(
        conn.execute(
            "SELECT j.job_id, j.target_id AS doc_id,"
            " (SELECT r.source_filename FROM pages p JOIN raw_files r ON r.sha256 = p.raw_sha256"
            "  WHERE p.doc_id = j.target_id ORDER BY p.page_no LIMIT 1) AS source_filename,"
            " (SELECT r.scanned_at FROM pages p JOIN raw_files r ON r.sha256 = p.raw_sha256"
            "  WHERE p.doc_id = j.target_id ORDER BY p.page_no LIMIT 1) AS scanned_at,"
            " (SELECT d.doc_type FROM documents d WHERE d.doc_id = j.target_id) AS doc_type,"
            " (SELECT d.title FROM documents d WHERE d.doc_id = j.target_id) AS title"
            " FROM jobs j WHERE j.stage = ? AND j.version = ? AND j.status = 'pending'"
            " ORDER BY j.job_id",
            (config.EXTRACT_STAGE, config.EXTRACT_STAGE_VERSION),
        ).fetchall()[:limit]
    )

    def fail(job: sqlite3.Row, error: Exception) -> ExtractResult:
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'error', error = ?, updated_at = ? WHERE job_id = ?",
                (str(error), now(), job["job_id"]),
            )
        name = job["title"] if job["doc_type"] == "project" else job["source_filename"]
        return ExtractResult(job["doc_id"], name, "error", error=str(error))

    running: dict[Future[Any], tuple[sqlite3.Row, str]] = {}
    with ThreadPoolExecutor(max_workers=config.EXTRACT_WORKERS) as pool:
        while True:
            # Keep a couple of jobs queued per worker, so a worker never waits on a database read.
            while len(running) < config.EXTRACT_WORKERS * 2 and (job := next(jobs, None)):
                try:
                    started = _start_job(pool, conn, paths, job, fast)
                except Exception as e:
                    yield fail(job, e)
                    continue
                if started is None:
                    yield ExtractResult(job["doc_id"], job["source_filename"], "skipped")
                    continue
                future, text = started
                running[future] = (job, text)
            if not running:
                return
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                job, text = running.pop(future)
                doc_id = job["doc_id"]
                try:
                    if job["doc_type"] == "project":
                        project = future.result()
                        _store_project(conn, job["job_id"], doc_id, project)
                        yield ExtractResult(
                            doc_id, job["title"], "done", "project", project.title.strip(), None
                        )
                    else:
                        facts, caption = future.result()
                        start, _ = _store_facts(conn, job["job_id"], doc_id, facts, text, caption)
                        yield ExtractResult(
                            doc_id, job["source_filename"], "done", facts.doc_type, facts.title.strip(), start
                        )
                except Exception as e:
                    yield fail(job, e)
