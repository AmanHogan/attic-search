import sqlite3
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

from attic import ask as ask_module
from attic.ask import Route, SqlQuery, ask
from attic.config import Paths
from attic.extract import AmountFacts, DocFacts


def receipt(title: str, vendor: str, when: str, total: float) -> DocFacts:
    return DocFacts(
        doc_type="receipt",
        title=title,
        date_start=when,
        date_end=when,
        people=[],
        tags=["car"],
        summary="",
        sensitive=False,
        amount=AmountFacts(party=vendor, date=when, total=total, odometer=None, service=None, category="car"),
    )


DOCS = [
    (
        "oil1.pdf",
        "JIFFY LUBE oil change synthetic total 45.99",
        receipt("Oil change", "Jiffy Lube", "2024-03-05", 45.99),
    ),
    (
        "oil2.pdf",
        "JIFFY LUBE oil change conventional total 30.00",
        receipt("Oil change", "Jiffy Lube", "2024-09-10", 30.0),
    ),
    (
        "letter.pdf",
        "Dear grandma, thank you for the birthday card",
        DocFacts(
            doc_type="letter",
            title="Thank-you letter",
            date_start="2019-06-01",
            date_end="2019-06-01",
            people=["Grandma"],
            tags=["family"],
            summary="",
            sensitive=False,
            amount=None,
        ),
    ),
]
"""Two receipts and a letter, run through the full pipeline."""


class FakeModel:
    """Scripted stand-in for the LLM that records what it was asked.

    Attributes:
        route (Route): Returned for every routing call.
        sql (list[str]): Returned, in order, for each SQL-writing call.
        reply (str): Returned for every free-text call.
        prompts (list[str]): Every user message received, in order.
        text_calls (int): Number of free-text calls.
    """

    def __init__(self, route: Route, sql: list[str] | None = None, reply: str = "the answer") -> None:
        self.route = route
        self.sql = list(sql or [])
        self.reply = reply
        self.prompts: list[str] = []
        self.text_calls = 0

    def chat(self, system: str, user: str, schema: type[BaseModel]) -> Any:
        self.prompts.append(user)
        if schema is Route:
            return self.route
        return SqlQuery(sql=self.sql.pop(0))

    def chat_text(self, system: str, user: str) -> str:
        self.prompts.append(user)
        self.text_calls += 1
        return self.reply


@pytest.fixture
def archive(build_archive: Callable[..., None]) -> None:
    """The DOCS, ingested, OCR'd, extracted, and indexed.

    Args:
        build_archive (Callable[..., None]): Runs documents through the pipeline.
    """
    build_archive(DOCS)


def use_model(monkeypatch: pytest.MonkeyPatch, model: FakeModel) -> FakeModel:
    """Route all LLM calls in `attic.ask` to a scripted fake.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest's patching helper.
        model (FakeModel): The fake to use.

    Returns:
        FakeModel: The same fake, for inspecting afterwards.
    """
    monkeypatch.setattr(ask_module, "_chat", model.chat)
    monkeypatch.setattr(ask_module, "_chat_text", model.chat_text)
    return model


def rag(query: str, doc_type: str | None = None, year: int | None = None) -> Route:
    return Route(route="rag", doc_type=doc_type, year=year, search_query=query)


SQL_ROUTE = Route(route="sql", doc_type=None, year=None, search_query="oil change spending")
"""A routing decision that sends the question down the SQL path."""


@pytest.mark.usefixtures("archive")
def test_rag_answers_from_numbered_sources(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAG sends the top pages to the model as numbered sources and returns them with the answer.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM.
    """
    model = use_model(monkeypatch, FakeModel(rag("grandma birthday"), reply="She thanked grandma [1]."))

    answer = ask(conn, paths, "what did I write to grandma?")

    assert answer.route == "rag" and answer.text == "She thanked grandma [1]."
    assert answer.sources[0].title == "Thank-you letter"
    assert "[1] Thank-you letter" in model.prompts[-1]
    assert "Dear grandma" in model.prompts[-1]


def test_rag_with_empty_archive_skips_model(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing to search, RAG says so without calling the model.

    (Meaning search ranks every page, so in a non-empty archive there are always
    sources; the model is then instructed to say when they don't hold the answer.)

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized, empty test database.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM.
    """
    model = use_model(monkeypatch, FakeModel(rag("zz", doc_type="photo")))

    answer = ask(conn, paths, "zz")

    assert answer.text == "I couldn't find that in your documents."
    assert model.text_calls == 0


@pytest.mark.usefixtures("archive")
def test_rag_drops_filters_when_nothing_matches(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(monkeypatch, FakeModel(rag("grandma", year=1990)))

    answer = ask(conn, paths, "letter to grandma in 1990")

    assert answer.sources[0].title == "Thank-you letter"


def test_invalid_route_filters_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    use_model(monkeypatch, FakeModel(Route(route="rag", doc_type="banana", year=3000, search_query="  ")))

    route = ask_module._route("anything")

    assert (route.doc_type, route.year, route.search_query) == (None, None, "anything")


@pytest.mark.usefixtures("archive")
def test_sql_answers_and_logs(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SQL route runs the model's query over the views and logs the question.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database.
        monkeypatch (pytest.MonkeyPatch): Replaces the LLM.
    """
    query = "SELECT round(sum(amount), 2) AS total FROM amounts_v WHERE party LIKE '%jiffy%'"
    model = use_model(monkeypatch, FakeModel(SQL_ROUTE, sql=[query], reply="You spent $75.99."))

    answer = ask(conn, paths, "how much did I spend at Jiffy Lube?")

    assert answer.route == "sql" and answer.text == "You spent $75.99."
    assert answer.rows == [{"total": 75.99}]
    assert '"total": 75.99' in model.prompts[-1]
    log = conn.execute("SELECT route, sql, answer FROM route_log").fetchone()
    assert tuple(log) == ("sql", query, "You spent $75.99.")


@pytest.mark.usefixtures("archive")
def test_sql_error_is_sent_back_for_a_fix(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = "SELECT count(*) AS n FROM amounts_v"
    model = use_model(monkeypatch, FakeModel(SQL_ROUTE, sql=["SELECT nope FROM amounts_v", good]))

    answer = ask(conn, paths, "how many receipts?")

    assert answer.sql == good and answer.rows == [{"n": 2}]
    assert "failed with: no such column: nope" in model.prompts[2]


@pytest.mark.usefixtures("archive")
def test_sql_falls_back_to_rag(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_model(monkeypatch, FakeModel(SQL_ROUTE, sql=["DELETE FROM documents"] * 3))

    answer = ask(conn, paths, "how much on oil changes?")

    assert answer.route == "rag"
    assert answer.note is not None and "SQL didn't work" in answer.note
    assert answer.sources[0].title == "Oil change"


@pytest.mark.parametrize(
    "sql",
    ["DELETE FROM documents", "SELECT 1; DROP TABLE jobs", "PRAGMA table_info(jobs)", "ATTACH 'x.db' AS x"],
)
def test_check_sql_rejects(sql: str) -> None:
    with pytest.raises(ValueError):
        ask_module._check_sql(sql)


def test_check_sql_accepts_fenced_select() -> None:
    assert ask_module._check_sql("```sql\nSELECT 1;\n```") == "SELECT 1"


@pytest.mark.usefixtures("archive")
@pytest.mark.parametrize("sql", ["SELECT * FROM jobs", "SELECT * FROM documents", "SELECT * FROM amounts"])
def test_run_sql_blocks_tables_outside_views(paths: Paths, sql: str) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="prohibited"):
        ask_module._run_sql(paths.db, sql)


@pytest.mark.usefixtures("archive")
def test_run_sql_reads_views(paths: Paths) -> None:
    rows = ask_module._run_sql(
        paths.db, "SELECT doc_type, count(*) AS n FROM documents_v GROUP BY 1 ORDER BY 1"
    )

    assert rows == [{"doc_type": "letter", "n": 1}, {"doc_type": "receipt", "n": 2}]
