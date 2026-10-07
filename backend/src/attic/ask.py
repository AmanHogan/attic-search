"""Answer a plain-English question from the archive.

RAG handles "what/where/who" questions; text-to-SQL handles "how much/how many/when".

1. Route: the model classifies the question as `rag` or `sql` and suggests filters.
2. RAG: hybrid search finds pages; the model answers only from them, citing [n].
3. SQL: the model writes one SELECT over the `documents_v` / `amounts_v` views.
   SQLite itself enforces read-only, views-only access; errors are sent back to
   the model to fix, and if it still fails the question falls back to RAG.
Every question is logged to `route_log`.
"""

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, get_args

import ollama
from pydantic import BaseModel

from attic import config
from attic.config import Paths
from attic.db import connect, now
from attic.extract import DocType
from attic.search import SearchHit, search

ROUTE_PROMPT = """You route questions about a personal archive of scanned paper documents \
(receipts, letters, notes, schoolwork, forms, certificates, records).

route:
- "sql" ONLY for questions that need arithmetic or ranking across many documents: how much \
(in total), how many, total, sum, average, the most/least expensive, when did I last, \
most recent, list all of X.
- "rag" for everything else, which is most questions: what a document says, who/what/where, \
finding or naming a particular document, and "do I have…", "did I ever…", "what was the name of…". \
When unsure, choose "rag".

doc_type: leave this null unless the question literally names a kind of document \
(e.g. "which receipts", "my certificates"). A topic like medical, school, or car is NOT a doc_type. \
When in doubt, null.
year: a four-digit year only if the question names one; otherwise null.
search_query: the question rewritten as short search keywords (drop filler words and the year)."""
"""System prompt for classifying a question and extracting filters."""

SQL_PROMPT = """You write one SQLite SELECT query to answer a question about a personal document archive. \
Today is {today}.

You may only use these two views:

documents_v: one row per document
  doc_id, title,
  doc_type (receipt|estimate|statement|letter|certificate|record|schoolwork|id_card|form|program
            |note|photo|other),
  date_start, date_end (YYYY-MM-DD text: when it was written; equal if exact; NULL if unknown),
  summary, caption (what a photo shows), sensitive (0/1),
  tags (comma-separated text), people (comma-separated text), page_count

amounts_v: one row per document that shows money
  doc_id, title,
  kind ('receipt' = you paid, 'estimate' = a quote for work, 'award' = money given to you such as
        a scholarship or reimbursement),
  party (who you paid, or who paid you), date (YYYY-MM-DD text),
  amount (grand total in dollars, REAL), odometer (INTEGER miles),
  service (vehicle documents only: oil change|inspection|repair|registration|other),
  category (exactly one of: car, medical, electronics, groceries, dining, shopping, housing, utilities,
            travel, parking, government, education, other),
  summary, tags (comma-separated text) — the same text as in documents_v, so an organization the
            document names can be found here even when `party` spells it differently

Rules:
- Return a single SELECT (or WITH ... SELECT). No other statements.
- Match text case-insensitively with LIKE and % wildcards (e.g. party LIKE '%jiffy%'), since names vary.
- An abbreviation in the question is almost never how a document spells the name. Expand it, \
then match a distinctive word of the expansion, OR-ed over party, summary, and tags inside \
parentheses. "UTA" means the University of Texas at Arlington, so match '%arlington%'. Never match \
an abbreviation on its own.
- Wrap a group of OR conditions in parentheses, so that AND filters still apply to all of them.
- Filter years with dates, e.g. date >= '2024-01-01' AND date < '2025-01-01'.
- Give result columns clear names (e.g. SELECT sum(amount) AS total_spent).
- Only filter on what the question asks for. Never add a date, type, or party filter it doesn't mention.
- To find documents about a topic, place, or person, search title, summary, tags, and people with LIKE. \
doc_type is only ever one of the fixed types listed above.
- "Last" / "most recent" means max(date) (or ORDER BY date DESC LIMIT 1); \
"first" / "earliest" means min(date).
- For a spending topic, filter category with = on the exact category name (e.g. category = 'car'). \
Spending, paid, bought, receipts, invoices, bills mean kind = 'receipt'; quotes, estimates, bids mean \
kind = 'estimate'; scholarships, awards, grants, reimbursements, money received mean kind = 'award'; \
if the question asks about several, don't filter on kind.
- To show which document a single result came from, include title, party, and date.

Examples:
Q: How much did UTA give me in scholarships?
SELECT title, party, date, amount FROM amounts_v WHERE kind = 'award' \
AND (party LIKE '%uta%' OR party LIKE '%arlington%' OR summary LIKE '%arlington%' \
OR tags LIKE '%arlington%')
Q: How much did I spend at Jiffy Lube in 2024?
SELECT round(sum(amount), 2) AS total_spent FROM amounts_v \
WHERE party LIKE '%jiffy%' AND kind = 'receipt' AND date >= '2024-01-01' AND date < '2025-01-01'
Q: How much have I spent on my car?
SELECT round(sum(amount), 2) AS total_spent FROM amounts_v WHERE category = 'car' AND kind = 'receipt'
Q: What was my most expensive car repair bill?
SELECT title, party, date, amount FROM amounts_v \
WHERE category = 'car' AND kind = 'receipt' ORDER BY amount DESC LIMIT 1
Q: What was the highest estimate I got?
SELECT title, party, date, amount FROM amounts_v WHERE kind = 'estimate' ORDER BY amount DESC LIMIT 1
Q: How much scholarship money did UTA give me?
SELECT title, party, date, amount FROM amounts_v WHERE kind = 'award' AND party LIKE '%texas at arlington%'
Q: How much have I received in scholarships?
SELECT round(sum(amount), 2) AS total_awarded FROM amounts_v WHERE kind = 'award'
Q: When did I last get an oil change?
SELECT max(date) AS last_oil_change FROM amounts_v WHERE service = 'oil change'
Q: When did I last see the dentist?
SELECT max(date_end) AS last_visit FROM documents_v \
WHERE title LIKE '%dent%' OR summary LIKE '%dent%' OR tags LIKE '%dent%'
Q: How many letters do I have from grandma?
SELECT count(*) AS letters FROM documents_v \
WHERE doc_type = 'letter' AND (people LIKE '%grandma%' OR summary LIKE '%grandma%')"""
"""System prompt for writing SQL; `{today}` is filled in at call time."""

RAG_ANSWER_PROMPT = """You answer questions about the user's own scanned documents, using ONLY the numbered \
sources given. The text came from OCR and may contain errors.
- The documents belong to the user. On forms, the patient/customer "Name:" is usually the user, \
not the provider (the doctor, business, or signer).
- Cite the sources you use as [1], [2], etc.
- The sources are the best matches from the archive, so read them carefully before deciding: \
an answer phrased differently still counts (an event booklet names the event, a flyer names the place). \
Only say "I couldn't find that in your documents." when the answer is genuinely absent. Never invent one.
- Two kinds of question need different care:
  - Asked whether a DOCUMENT is in the archive ("do I have…", "can you locate…", "where is my…"), \
read the kind of document loosely. An immunization record, an insurance card, a prescription, and a \
provider's bill are all health records; a report card is a school record. If a source is one of \
those, answer yes and name it. Never say the archive holds nothing of a kind while a source is one.
  - Asked whether something HAPPENED ("did I volunteer…", "did I ever…", "did I go…"), answer yes \
only if a source says it did. A flyer, a program description, or an application is not proof that \
you took part: say which document is closest, what it shows, and that it does not record \
your part in it. Do not fall back to "I couldn't find that in your documents." when a source is \
about the subject asked about.
- Be brief: one to three sentences."""
"""System prompt for answering from retrieved pages."""

SQL_ANSWER_PROMPT = """You turn the result of a database query into a short answer to the user's question \
about their own documents. Use only the rows given. Amounts are in dollars. If there are no rows, \
say nothing matching was found. One or two sentences."""
"""System prompt for phrasing SQL results as an answer."""


class Route(BaseModel):
    """The model's routing decision for a question.

    Attributes:
        route (str): "rag" or "sql".
        doc_type (str | None): Suggested document-type filter.
        year (int | None): Suggested year filter.
        search_query (str): Question rewritten as search keywords.
    """

    route: Literal["rag", "sql"]
    doc_type: str | None
    year: int | None
    search_query: str


class SqlQuery(BaseModel):
    """The model's generated SQL.

    Attributes:
        sql (str): One SELECT statement.
    """

    sql: str


@dataclass
class Answer:
    """The result of asking one question.

    Attributes:
        question (str): What was asked.
        route (str): Path that produced the answer: "rag" or "sql".
        text (str): The answer.
        sources (list[SearchHit]): Pages the answer is based on (RAG), numbered from 1.
        sql (str | None): The query that ran (SQL route).
        rows (list[dict[str, object]]): The query's result rows (SQL route).
        note (str | None): Anything worth telling the user, e.g. that SQL fell back to RAG.
    """

    question: str
    route: str
    text: str
    sources: list[SearchHit] = field(default_factory=list)
    sql: str | None = None
    rows: list[dict[str, object]] = field(default_factory=list)
    note: str | None = None


# --- start private functions ---


def _chat[T: BaseModel](system: str, user: str, schema: type[T]) -> T:
    """Ask the model a question whose answer must follow a JSON schema.

    Args:
        system (str): System prompt.
        user (str): User message.
        schema (type[T]): Pydantic model the answer must match.

    Returns:
        T: The validated answer.
    """
    response = ollama.chat(
        model=config.ASK_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        format=schema.model_json_schema(),
        options={"temperature": 0},
    )
    return schema.model_validate_json(response.message.content or "")


def _chat_text(system: str, user: str) -> str:
    """Ask the model for a free-text answer.

    Args:
        system (str): System prompt.
        user (str): User message.

    Returns:
        str: The model's answer, stripped.
    """
    response = ollama.chat(
        model=config.ASK_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        options={"temperature": 0},
    )
    return (response.message.content or "").strip()


def _route(question: str) -> Route:
    """Classify a question and drop any suggested filter that isn't valid.

    Args:
        question (str): The user's question.

    Returns:
        Route: Route with `doc_type` limited to known types and `year` to a plausible range.
    """
    route = _chat(ROUTE_PROMPT, question, Route)
    # The model guesses a type far too eagerly, and a wrong filter hides the right
    # document completely, so keep one only when the question actually says that word.
    named = route.doc_type is not None and route.doc_type.split("_")[0] in question.lower()
    if route.doc_type not in get_args(DocType) or not named:
        route.doc_type = None
    if route.year is not None and not 1900 <= route.year <= date.today().year + 1:
        route.year = None
    if not route.search_query.strip():
        route.search_query = question
    return route


def _rag(conn: sqlite3.Connection, question: str, route: Route) -> Answer:
    """Answer from the best-matching pages, with numbered citations.

    If filtered search finds nothing, it retries without filters before giving up;
    if there are still no pages, it answers "not found" without asking the model.

    Args:
        conn (sqlite3.Connection): Open archive database.
        question (str): The user's question.
        route (Route): Routing decision (search keywords and filters).

    Returns:
        Answer: Answer text plus the numbered source pages.
    """
    hits = search(conn, route.search_query, route.doc_type, route.year, limit=config.ASK_SOURCES)
    if not hits and (route.doc_type or route.year):
        hits = search(conn, route.search_query, limit=config.ASK_SOURCES)
    if not hits:
        return Answer(question, "rag", "I couldn't find that in your documents.")

    blocks = []
    for n, hit in enumerate(hits, start=1):
        text = conn.execute("SELECT text FROM pages_fts WHERE page_id = ?", (hit.page_id,)).fetchone()
        body = (text[0] if text else "")[: config.ASK_SOURCE_MAX_CHARS]
        label = f"{hit.title or 'Untitled'} ({hit.doc_type or 'unknown'}, {hit.date_start or 'undated'})"
        blocks.append(f"[{n}] {label}, page {hit.page_no}\n{body}")
    user = f"Question: {question}\n\nSources:\n\n" + "\n\n".join(blocks)
    return Answer(question, "rag", _chat_text(RAG_ANSWER_PROMPT, user), sources=hits)


def _check_sql(sql: str) -> str:
    """Reject anything but a single SELECT / WITH statement.

    This is a first line of defense with clear error messages; the SQLite
    authorizer in `_run_sql` is what actually enforces read-only, views-only access.

    Args:
        sql (str): Query from the model.

    Returns:
        str: The query, trimmed, without a trailing semicolon.

    Raises:
        ValueError: If it isn't exactly one SELECT or WITH statement.
    """
    cleaned = re.sub(r"^```(?:sql)?|```$", "", sql.strip()).strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", cleaned):
        raise ValueError("only a single SELECT (or WITH ... SELECT) statement is allowed")
    if not sqlite3.complete_statement(cleaned + ";") or ";" in cleaned:
        raise ValueError("only one statement is allowed")
    return cleaned


def _authorize(action: int, arg1: str | None, arg2: str | None, db: str | None, source: str | None) -> int:
    """SQLite authorizer: allow only reading the allowed views (and tables read through them).

    Args:
        action (int): What SQLite is about to do (an SQLITE_* action code).
        arg1 (str | None): For reads, the table name.
        arg2 (str | None): For reads, the column name; for functions, the function name.
        db (str | None): Database name ("main").
        source (str | None): The view or trigger responsible for the access, if any.

    Returns:
        int: `sqlite3.SQLITE_OK` to allow, `sqlite3.SQLITE_DENY` to refuse.
    """
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and (arg1 in config.SQL_VIEWS or source in config.SQL_VIEWS):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _run_sql(db_path: Path, sql: str) -> list[dict[str, object]]:
    """Run a generated query on its own read-only, views-only, time-limited connection.

    Args:
        db_path (Path): Archive database file.
        sql (str): Query that passed `_check_sql`.

    Returns:
        list[dict[str, object]]: Up to `SQL_MAX_ROWS` rows, as column -> value.

    Raises:
        sqlite3.Error: If the query is invalid, not allowed, or too slow.
    """
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA query_only = ON")
        deadline = time.monotonic() + config.SQL_TIMEOUT_SECONDS
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        conn.set_authorizer(_authorize)
        cursor = conn.execute(sql)
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchmany(config.SQL_MAX_ROWS)]
    finally:
        conn.close()


def _sql(paths: Paths, question: str) -> Answer:
    """Answer by generating and running SQL, letting the model fix errors a few times.

    Args:
        paths (Paths): Archive locations.
        question (str): The user's question.

    Returns:
        Answer: Answer text plus the SQL and its rows.

    Raises:
        RuntimeError: If no working query was produced after `SQL_RETRIES` fixes.
    """
    system = SQL_PROMPT.format(today=date.today().isoformat())
    user = f"Question: {question}"
    sql = ""
    last_error = ""
    for _ in range(config.SQL_RETRIES + 1):
        if last_error:
            user = (
                f"{user}\n\nYour previous query:\n{sql}\nfailed with: {last_error}\nWrite a corrected query."
            )
        sql = _chat(system, user, SqlQuery).sql
        try:
            sql = _check_sql(sql)
            rows = _run_sql(paths.db, sql)
            break
        except (ValueError, sqlite3.Error) as e:
            last_error = str(e)
    else:
        raise RuntimeError(f"couldn't write a working query ({last_error})")

    result = json.dumps(rows, default=str)
    text = _chat_text(SQL_ANSWER_PROMPT, f"Question: {question}\n\nQuery:\n{sql}\n\nRows:\n{result}")
    return Answer(question, "sql", text, sql=sql, rows=rows)


def _log(conn: sqlite3.Connection, answer: Answer, route: Route) -> None:
    """Record a question, how it was answered, and the answer in `route_log`.

    Args:
        conn (sqlite3.Connection): Open archive database.
        answer (Answer): The final answer.
        route (Route): The routing decision (its filters are logged).
    """
    filters = json.dumps({"doc_type": route.doc_type, "year": route.year, "search_query": route.search_query})
    with conn:
        conn.execute(
            "INSERT INTO route_log (ts, query, route, filters_json, sql, answer) VALUES (?, ?, ?, ?, ?, ?)",
            (now(), answer.question, answer.route, filters, answer.sql, answer.text),
        )


# --- end private functions ---


def ask(conn: sqlite3.Connection, paths: Paths, question: str) -> Answer:
    """Answer a natural-language question from the archive.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        question (str): The user's question.

    Returns:
        Answer: The answer, with its sources (RAG) or SQL and rows (SQL).
    """
    route = _route(question)
    if route.route == "sql":
        try:
            answer = _sql(paths, question)
        except RuntimeError as e:
            answer = _rag(conn, question, route)
            answer.note = f"SQL didn't work, so this answer comes from search instead: {e}"
    else:
        answer = _rag(conn, question, route)
    _log(conn, answer, route)
    return answer
