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


FINANCIAL_RE = re.compile(
    r"\$\s?\d|\b(invoice|receipt|subtotal|total|balance|amount due|statement|bill|billing|payment|paid"
    r"|refund|tax|estimate|quote|award|scholarship|tuition|premium|deductible)\b",
    re.IGNORECASE,
)
"""Text that suggests money: in `--fast` mode such documents still go to the full model."""


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
    """Return the value if it's a real YYYY-MM-DD date no later than today, else None.

    A paper can't be written in the future, so a future date is the model inventing one.
    """
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.isoformat() if parsed <= date.today() else None


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
        fast (bool): Use the small text-only `EXTRACT_FAST_MODEL`, unless the text looks
            financial (`FINANCIAL_RE`): money and dates matter there, and the small model
            misses them, so those still go to `EXTRACT_MODEL`.

    Returns:
        tuple[DocFacts, str | None]: The model's facts, and a caption for a photo.
    """
    if fast and not FINANCIAL_RE.search(text):
        facts = _ask_llm(text, scanned_at, config.EXTRACT_FAST_MODEL)
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


def _money_second_pass(text: str, kind: str, model: str | None = None) -> MoneyFacts:
    """Ask the model for just the total, which it finds far more reliably than as one field among many.

    Award letters get their own prompt: the receipt wording ("grand total", "amount due", "not a
    single line item") makes the model return null for an amount written out in a sentence.

    Args:
        text (str): The document's OCR text.
        kind (str): Money kind from `_kind`, which picks the prompt.
        model (str | None): Ollama model to use; None for `EXTRACT_MODEL`.

    Returns:
        MoneyFacts: Party, date, and grand total.
    """
    response = ollama.chat(
        model=model or config.EXTRACT_MODEL,
        messages=[
            {"role": "system", "content": AWARD_MONEY_PROMPT if kind == "award" else MONEY_PROMPT},
            {"role": "user", "content": text},
        ],
        format=MoneyFacts.model_json_schema(),
        options={"temperature": 0, "num_ctx": config.EXTRACT_NUM_CTX},
    )
    return MoneyFacts.model_validate_json(response.message.content or "")


def _fill_missing_total(facts: DocFacts, text: str, model: str | None = None) -> None:
    """Run the money-only pass when the document shows a total the main pass didn't pick up.

    Args:
        facts (DocFacts): The model's answer, updated in place.
        text (str): The document's OCR text.
        model (str | None): Ollama model to use; None for `EXTRACT_MODEL`.
    """
    if facts.amount and facts.amount.total is not None:
        return
    if not re.search(r"\$\s?\d", text):
        return
    money = _money_second_pass(text, _kind(facts.title, text), model)
    if money.grand_total is None:
        return
    if facts.amount is None:
        facts.amount = AmountFacts(
            party=money.party,
            date=money.date,
            total=money.grand_total,
            odometer=None,
            service=None,
            category=None,
        )
    else:
        facts.amount.total = money.grand_total
        facts.amount.party = facts.amount.party or money.party
        facts.amount.date = facts.amount.date or money.date


def _ask_llm(text: str, scanned_at: str | None, model: str | None = None) -> DocFacts:
    """Ask the local model for a document's metadata, forced into the `DocFacts` schema.

    Args:
        text (str): The document's OCR text.
        scanned_at (str | None): Scan date, so the model knows the latest possible date.
        model (str | None): Ollama model to use; None for `EXTRACT_MODEL`.

    Returns:
        DocFacts: The model's answer, validated against the schema.
    """
    response = ollama.chat(
        model=model or config.EXTRACT_MODEL,
        messages=[
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": f"Scan date: {scanned_at or 'unknown'}\n\nOCR text:\n{text}"},
        ],
        format=DocFacts.model_json_schema(),
        options={"temperature": 0, "num_ctx": config.EXTRACT_NUM_CTX},
    )
    return DocFacts.model_validate_json(response.message.content or "")


def _store_facts(
    conn: sqlite3.Connection, job_id: str, doc_id: str, facts: DocFacts, text: str, caption: str | None
) -> tuple[str | None, str | None]:
    """Clean the model's answer and write it to documents, tags, people, amounts; mark the job done.

    Everything is replaced, not merged, so re-running gives the same result.

    Args:
        conn (sqlite3.Connection): Open archive database.
        job_id (str): Extract job to mark done.
        doc_id (str): Document the facts describe.
        facts (DocFacts): The model's answer.
        text (str): The document's OCR text (used to tell awards and estimates from receipts).
        caption (str | None): Description of the photo, for image-like documents.

    Returns:
        tuple[str | None, str | None]: The stored (date_start, date_end).
    """
    start, end = _clean_range(facts.date_start, facts.date_end)
    people = _unique([_clean_name(p) for p in facts.people])
    tags = _unique([t.strip().lower() for t in facts.tags])
    # Small models sometimes call an invoice a "form", so keep money fields for any document
    # with a total, and for estimates even without one (so they at least get listed).
    amount = facts.amount
    kind = _kind(facts.title, text)
    if amount and amount.total is None and facts.doc_type != "receipt" and kind != "estimate":
        amount = None

    with conn:
        conn.execute(
            "UPDATE documents SET title = ?, doc_type = ?, doc_date_start = ?, doc_date_end = ?,"
            " summary = ?, caption = ?, sensitive = ? WHERE doc_id = ?",
            (
                facts.title.strip(),
                facts.doc_type,
                start,
                end,
                facts.summary.strip(),
                caption,
                facts.sensitive,
                doc_id,
            ),
        )
        conn.execute("DELETE FROM tags WHERE doc_id = ?", (doc_id,))
        conn.executemany("INSERT INTO tags (doc_id, tag) VALUES (?, ?)", [(doc_id, t) for t in tags])
        conn.execute("DELETE FROM people WHERE doc_id = ?", (doc_id,))
        conn.executemany("INSERT INTO people (doc_id, name) VALUES (?, ?)", [(doc_id, p) for p in people])
        conn.execute("DELETE FROM amounts WHERE doc_id = ?", (doc_id,))
        if amount:
            conn.execute(
                "INSERT INTO amounts (doc_id, kind, party, date, amount_cents, odometer, service, category)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_id,
                    kind,
                    amount.party,
                    _clean_date(amount.date),
                    round(amount.total * 100) if amount.total is not None else None,
                    amount.odometer,
                    amount.service,
                    amount.category,
                ),
            )
        conn.execute(
            "UPDATE jobs SET status = 'done', error = NULL, updated_at = ? WHERE job_id = ?",
            (now(), job_id),
        )
        # Chunks carry a [type | date | title] header, so new facts mean the document needs re-indexing.
        conn.execute(
            "UPDATE jobs SET status = 'pending', updated_at = ? WHERE target_id = ? AND stage = ?",
            (now(), doc_id, config.INDEX_STAGE),
        )
    return start, end


def _project_context(conn: sqlite3.Connection, paths: Paths, doc_id: str, name: str) -> str:
    """Build the text the model sees for a project: its name, file list, README, and main files.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        doc_id (str): Project document.
        name (str): Project folder name.

    Returns:
        str: The project's name, up to `PROJECT_TREE_FILES` file paths, its README, and the
            first `PROJECT_HEAD_LINES` lines of `PROJECT_HEAD_FILES` source files, cut to
            `PROJECT_MAX_CHARS`.
    """
    rows = conn.execute(
        "SELECT r.source_filename AS path, t.text_path FROM pages p"
        " JOIN raw_files r ON r.sha256 = p.raw_sha256"
        " JOIN page_text t ON t.page_id = p.page_id AND t.is_active = 1"
        " WHERE p.doc_id = ? ORDER BY p.page_no",
        (doc_id,),
    ).fetchall()
    texts = {row["path"]: (paths.archive / row["text_path"]).read_text() for row in rows}
    tree = "\n".join(sorted(texts)[: config.PROJECT_TREE_FILES])
    readme = next((t for p, t in texts.items() if Path(p).name.lower().startswith("readme")), "")
    code = [p for p in texts if not p.lower().endswith(".md")]
    main = sorted(code, key=lambda p: (Path(p).stem.lower() not in config.PROJECT_MAIN_NAMES, -len(texts[p])))
    heads = "\n\n".join(
        f"--- {p} ---\n" + "\n".join(texts[p].splitlines()[: config.PROJECT_HEAD_LINES])
        for p in main[: config.PROJECT_HEAD_FILES]
    )
    context = f"Project: {name}\n\nFiles:\n{tree}\n\nREADME:\n{readme[:1500]}\n\nMain files:\n{heads}"
    return context[: config.PROJECT_MAX_CHARS]


def _ask_project(context: str, model: str | None = None) -> ProjectFacts:
    """Ask the local model to describe a project, forced into the `ProjectFacts` schema.

    Args:
        context (str): Text from `_project_context`.
        model (str | None): Ollama model to use; None for `EXTRACT_MODEL`.

    Returns:
        ProjectFacts: The model's answer, validated against the schema.
    """
    response = ollama.chat(
        model=model or config.EXTRACT_MODEL,
        messages=[
            {"role": "system", "content": PROJECT_PROMPT},
            {"role": "user", "content": context},
        ],
        format=ProjectFacts.model_json_schema(),
        options={"temperature": 0, "num_ctx": config.EXTRACT_NUM_CTX},
    )
    return ProjectFacts.model_validate_json(response.message.content or "")


def _store_project(conn: sqlite3.Connection, job_id: str, doc_id: str, facts: ProjectFacts) -> None:
    """Write a project's description and mark the job done, keeping the tags ingest set.

    Tags with a colon (`project:`, `course:`, ...) were set by ingest from the folder name
    and are kept; the model's own tags are replaced on every run.

    Args:
        conn (sqlite3.Connection): Open archive database.
        job_id (str): Extract job to mark done.
        doc_id (str): Project document.
        facts (ProjectFacts): The model's answer.
    """
    tags = _unique([t.strip().lower() for t in facts.tags])
    with conn:
        conn.execute(
            "UPDATE documents SET title = ?, summary = ? WHERE doc_id = ?",
            (facts.title.strip(), facts.summary.strip(), doc_id),
        )
        conn.execute("DELETE FROM tags WHERE doc_id = ? AND instr(tag, ':') = 0", (doc_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO tags (doc_id, tag) VALUES (?, ?)", [(doc_id, t) for t in tags]
        )
        conn.execute(
            "UPDATE jobs SET status = 'done', error = NULL, updated_at = ? WHERE job_id = ?",
            (now(), job_id),
        )
        conn.execute(
            "UPDATE jobs SET status = 'pending', updated_at = ? WHERE target_id = ? AND stage = ?",
            (now(), doc_id, config.INDEX_STAGE),
        )


# --- end private functions ---


def run_extract(
    conn: sqlite3.Connection, paths: Paths, limit: int | None = None, fast: bool = False
) -> Iterator[ExtractResult]:
    """Queue ready documents, then extract metadata for each pending one, yielding as each finishes.

    Model calls run in `EXTRACT_WORKERS` threads so Ollama can serve several documents at
    once; every database read and write stays on the calling thread. Results arrive in the
    order they finish, not the order queued. Failures (model not running, invalid output)
    are recorded on the job and don't stop the run.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        limit (int | None): Process at most this many documents (skipped photos don't count);
            None for all.
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
        ).fetchall()
    )
    started_count = 0

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
            while (
                len(running) < config.EXTRACT_WORKERS * 2
                and (limit is None or started_count < limit)
                and (job := next(jobs, None))
            ):
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
                started_count += 1
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
