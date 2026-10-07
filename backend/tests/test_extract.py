import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from attic import config, extract
from attic.config import Paths
from attic.extract import AmountFacts, DocFacts, MoneyFacts, run_extract
from attic.ingest import ingest_inbox
from attic.text import run_ocr


def facts(**overrides: Any) -> DocFacts:
    """Build a model answer with sensible defaults, overriding any fields given.

    Args:
        **overrides (Any): DocFacts fields to replace.

    Returns:
        DocFacts: A fake model answer.
    """
    base: dict[str, Any] = {
        "doc_type": "receipt",
        "title": " Oil change ",
        "date_start": "2024-03-05",
        "date_end": "2024-03-05",
        "people": ["Hogan, Aman", "Aman Hogan", ""],
        "tags": ["Car", "car", "oil change "],
        "summary": "Oil change at Jiffy Lube.",
        "sensitive": False,
        "amount": AmountFacts(
            party="Jiffy Lube",
            date="2024-03-05",
            total=45.99,
            odometer=81234,
            service="oil change",
            category="car",
        ),
    }
    return DocFacts(**(base | overrides))


@pytest.fixture
def ocr_done(paths: Paths, conn: sqlite3.Connection, make_pdf: Callable[..., Path]) -> None:
    """One ingested, OCR'd document, ready for extraction.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        make_pdf (Callable[..., Path]): Test PDF factory.
    """
    make_pdf(paths.inbox / "receipt.pdf", text="JIFFY LUBE total $45.99 odometer 81234")
    ingest_inbox(conn, paths)
    list(run_ocr(conn, paths))


def use_answer(monkeypatch: pytest.MonkeyPatch, answer: DocFacts) -> list[str]:
    """Replace the LLM call with a fixed answer; returns the list of texts it was sent.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest's patching helper.
        answer (DocFacts): What the fake model returns.

    Returns:
        list[str]: Filled with each document text the fake model receives.
    """
    sent: list[str] = []

    def fake(text: str, scanned_at: str | None) -> DocFacts:
        sent.append(text)
        return answer

    monkeypatch.setattr(extract, "_ask_llm", fake)
    monkeypatch.setattr(extract, "_caption", lambda image: "a test photo")
    monkeypatch.setattr(
        extract, "_money_second_pass", lambda text: MoneyFacts(party=None, date=None, grand_total=None)
    )
    return sent


@pytest.mark.usefixtures("ocr_done")
def test_extract_fills_document(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model's answer is cleaned and written to documents, tags, people, and receipts.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM call.
    """
    sent = use_answer(monkeypatch, facts())

    [r] = list(run_extract(conn, paths))

    assert r.status == "done" and r.doc_type == "receipt" and r.title == "Oil change"
    assert "JIFFY LUBE" in sent[0]
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert (doc["doc_type"], doc["doc_date_start"], doc["doc_date_end"]) == (
        "receipt",
        "2024-03-05",
        "2024-03-05",
    )
    assert [t[0] for t in conn.execute("SELECT tag FROM tags ORDER BY tag")] == ["car", "oil change"]
    assert [p[0] for p in conn.execute("SELECT name FROM people")] == ["Aman Hogan"]
    receipt = conn.execute("SELECT party, amount_cents, odometer FROM amounts").fetchone()
    assert tuple(receipt) == ("Jiffy Lube", 4599, 81234)
    assert conn.execute("SELECT status FROM jobs WHERE stage = 'extract'").fetchone()[0] == "done"


def test_waits_for_ocr(
    paths: Paths, conn: sqlite3.Connection, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_pdf(paths.inbox / "a.pdf", text="not OCR'd yet, so not ready")
    ingest_inbox(conn, paths)
    use_answer(monkeypatch, facts())

    assert list(run_extract(conn, paths)) == []
    assert conn.execute("SELECT count(*) FROM jobs WHERE stage = 'extract'").fetchone()[0] == 0


@pytest.mark.usefixtures("ocr_done")
def test_rerun_is_noop(paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = use_answer(monkeypatch, facts())
    list(run_extract(conn, paths))

    assert list(run_extract(conn, paths)) == []
    assert len(sent) == 1


@pytest.mark.usefixtures("ocr_done")
@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2026-13-40", "2026-13-40", (None, None)),
        ("2024-03-05", None, ("2024-03-05", "2024-03-05")),
        ("2024-12-31", "2024-01-01", ("2024-01-01", "2024-12-31")),
        ("sometime", "2024-06-01", ("2024-06-01", "2024-06-01")),
    ],
)
def test_dates_are_cleaned(
    paths: Paths,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    start: str | None,
    end: str | None,
    expected: tuple[str | None, str | None],
) -> None:
    """Invalid dates are dropped, a missing end copies the start, and reversed ranges are fixed.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM call.
        start (str | None): date_start the fake model returns.
        end (str | None): date_end the fake model returns.
        expected (tuple[str | None, str | None]): Stored (start, end).
    """
    use_answer(monkeypatch, facts(date_start=start, date_end=end))

    list(run_extract(conn, paths))

    row = conn.execute("SELECT doc_date_start, doc_date_end FROM documents").fetchone()
    assert tuple(row) == expected


@pytest.mark.usefixtures("ocr_done")
def test_money_fields_without_total_ignored_for_non_receipts(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = AmountFacts(party="Clinic", date=None, total=None, odometer=None, service=None, category=None)
    use_answer(monkeypatch, facts(doc_type="form", amount=empty))

    list(run_extract(conn, paths))

    assert conn.execute("SELECT count(*) FROM amounts").fetchone()[0] == 0


@pytest.mark.usefixtures("ocr_done")
def test_invoice_typed_as_form_keeps_its_total(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_answer(monkeypatch, facts(doc_type="form"))

    list(run_extract(conn, paths))

    assert conn.execute("SELECT kind, amount_cents FROM amounts").fetchone()[:] == ("receipt", 4599)


@pytest.mark.usefixtures("ocr_done")
def test_estimate_on_a_form_is_stored(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair estimate keeps its total, as kind 'estimate', even though its doc_type is form.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM call.
    """
    estimate = AmountFacts(
        party="Body Shop", date=None, total=740.70, odometer=None, service=None, category="car"
    )
    use_answer(monkeypatch, facts(doc_type="form", title="Preliminary Estimate", amount=estimate))

    list(run_extract(conn, paths))

    row = conn.execute("SELECT kind, amount_cents, category FROM amounts").fetchone()
    assert tuple(row) == ("estimate", 74070, "car")


@pytest.mark.usefixtures("ocr_done")
def test_receipt_date_falls_back_to_document_date(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_date = AmountFacts(
        party="Jiffy Lube", date=None, total=45.99, odometer=None, service=None, category="car"
    )
    use_answer(monkeypatch, facts(amount=no_date))

    list(run_extract(conn, paths))

    assert conn.execute("SELECT date FROM amounts_v").fetchone()[0] == "2024-03-05"


@pytest.mark.usefixtures("ocr_done")
def test_reextract_marks_document_for_reindexing(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New facts change the chunk headers, so a done index job goes back to pending.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM call.
    """
    use_answer(monkeypatch, facts())
    list(run_extract(conn, paths))
    doc_id = conn.execute("SELECT doc_id FROM documents").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO jobs (job_id, target_id, stage, version, status, updated_at)"
            " VALUES ('j1', ?, 'index', 1, 'done', 'now')",
            (doc_id,),
        )
        conn.execute("UPDATE jobs SET status = 'pending' WHERE stage = 'extract'")

    list(run_extract(conn, paths))

    assert conn.execute("SELECT status FROM jobs WHERE stage = 'index'").fetchone()[0] == "pending"


@pytest.mark.usefixtures("ocr_done")
def test_llm_failure_is_recorded(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(text: str, scanned_at: str | None) -> DocFacts:
        raise ConnectionError("ollama not running")

    monkeypatch.setattr(extract, "_ask_llm", boom)
    monkeypatch.setattr(extract, "_caption", lambda image: "a test photo")

    [r] = list(run_extract(conn, paths))

    assert r.status == "error" and r.error == "ollama not running"
    job = conn.execute("SELECT status, error FROM jobs WHERE stage = 'extract'").fetchone()
    assert tuple(job) == ("error", "ollama not running")
    assert conn.execute("SELECT doc_type FROM documents").fetchone()[0] is None


@pytest.mark.parametrize(
    ("title", "text", "expected"),
    [
        ("Preliminary Estimate", "", "estimate"),
        ("Invoice", "QUOTE #123\nparts and labor", "estimate"),
        ("Invoice for RO #5", "Subtotal $1,911.59 + est. Tax $136.59", "receipt"),
        ("Oil Change Receipt", "Service Total: $58.88", "receipt"),
    ],
)
def test_kind_rule(title: str, text: str, expected: str) -> None:
    assert extract._kind(title, text) == expected


def test_short_word_file_is_not_captioned(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short .docx has no page image, so extraction finishes without a caption instead of crashing.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM call.
    """
    import docx

    word = docx.Document()
    word.add_paragraph("Short note.")
    word.save(str(paths.inbox / "note.docx"))
    ingest_inbox(conn, paths)
    list(run_ocr(conn, paths))
    use_answer(monkeypatch, facts(doc_type="note", amount=None))

    [r] = list(run_extract(conn, paths))

    assert r.status == "done"
    assert conn.execute("SELECT caption FROM documents").fetchone()[0] is None


def test_documents_are_extracted_in_parallel(
    paths: Paths, conn: sqlite3.Connection, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several documents are in the model at once, and every one is stored correctly.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        make_pdf (Callable[..., Path]): Test PDF factory.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM call.
    """
    import threading
    import time

    for i in range(6):
        make_pdf(paths.inbox / f"doc{i}.pdf", text=f"document number {i} " * 6)
    ingest_inbox(conn, paths)
    list(run_ocr(conn, paths))

    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def fake(text: str, scanned_at: str | None) -> DocFacts:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.1)
        with lock:
            state["now"] -= 1
        number = text.split("document number ")[1].split()[0]
        return facts(doc_type="note", title=f"Doc {number}", amount=None)

    monkeypatch.setattr(extract, "_ask_llm", fake)
    monkeypatch.setattr(extract, "_caption", lambda image: "a test photo")
    monkeypatch.setattr(config, "EXTRACT_WORKERS", 3)

    results = list(run_extract(conn, paths))

    assert len(results) == 6 and all(r.status == "done" for r in results)
    assert state["peak"] > 1
    assert sorted(r[0] for r in conn.execute("SELECT title FROM documents")) == [f"Doc {i}" for i in range(6)]
    assert (
        conn.execute("SELECT count(*) FROM jobs WHERE stage = 'extract' AND status = 'done'").fetchone()[0]
        == 6
    )


def test_limit_stops_after_that_many_documents(
    paths: Paths, conn: sqlite3.Connection, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a limit, the rest stay pending for the next run.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        make_pdf (Callable[..., Path]): Test PDF factory.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM call.
    """
    for i in range(4):
        make_pdf(paths.inbox / f"doc{i}.pdf", text=f"document number {i} " * 6)
    ingest_inbox(conn, paths)
    list(run_ocr(conn, paths))
    use_answer(monkeypatch, facts(doc_type="note", amount=None))

    assert len(list(run_extract(conn, paths, limit=3))) == 3
    assert len(list(run_extract(conn, paths))) == 1


def test_only_photos_and_lone_sparse_pages_are_captioned(
    paths: Paths, conn: sqlite3.Connection, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-page scan with sparse text is not captioned; a one-page sparse document is.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        make_pdf (Callable[..., Path]): Test PDF factory.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM calls.
    """
    make_pdf(paths.inbox / "one.pdf", pages=1, text="x")
    make_pdf(paths.inbox / "many.pdf", pages=3, text="x")
    ingest_inbox(conn, paths)
    list(run_ocr(conn, paths))
    captioned: list[str] = []

    def fake_caption(image: Path) -> str:
        captioned.append(image.parent.name)
        return "a test photo"

    use_answer(monkeypatch, facts(doc_type="note", amount=None))
    monkeypatch.setattr(extract, "_caption", fake_caption)

    list(run_extract(conn, paths))

    rows = {
        r["source_filename"]: r["caption"]
        for r in conn.execute(
            "SELECT r.source_filename, d.caption FROM documents d JOIN pages p ON p.doc_id = d.doc_id"
            " JOIN raw_files r ON r.sha256 = p.raw_sha256 WHERE p.page_no = 1"
        )
    }
    assert rows["one.pdf"] == "a test photo"
    assert rows["many.pdf"] is None
