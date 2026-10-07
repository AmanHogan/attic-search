import sqlite3
import warnings
from collections.abc import Iterator

import pytest

from attic import api, chat, config
from attic.ask import Answer
from attic.chat import create_chat, delete_chat, get_chat, list_chats, rename_chat, send_message
from attic.config import Paths

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # starlette's httpx deprecation notice
    from fastapi.testclient import TestClient


@pytest.fixture
def fake_models(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
    """Replace the answer pipeline and the follow-up rewrite with recorders.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest's patching helper.

    Returns:
        dict[str, list[object]]: What `ask` was asked, and what history each rewrite saw.
    """
    seen: dict[str, list[object]] = {"asked": [], "rewrites": []}

    def fake_ask(conn: sqlite3.Connection, paths: Paths, question: str) -> Answer:
        seen["asked"].append(question)
        return Answer(question, "sql", f"answer to: {question}", sql="SELECT 1", rows=[{"n": 1}])

    def fake_rewrite(question: str, history: list[tuple[str, str]]) -> str:
        seen["rewrites"].append(history)
        return f"{question} (about the oil change)"

    monkeypatch.setattr(chat, "ask", fake_ask)
    monkeypatch.setattr(chat, "_rewrite", fake_rewrite)
    return seen


def test_create_list_rename_delete(conn: sqlite3.Connection) -> None:
    first = create_chat(conn)
    second = create_chat(conn, "Car stuff")

    assert first.title == config.CHAT_DEFAULT_TITLE
    assert {c.chat_id for c in list_chats(conn)} == {first.chat_id, second.chat_id}
    assert rename_chat(conn, first.chat_id, "  Taxes  ").title == "Taxes"
    assert delete_chat(conn, second.chat_id) is True
    assert delete_chat(conn, second.chat_id) is False
    assert [c.chat_id for c in list_chats(conn)] == [first.chat_id]
    with pytest.raises(KeyError):
        get_chat(conn, second.chat_id)


def test_first_question_is_used_as_written_and_titles_the_chat(
    conn: sqlite3.Connection, paths: Paths, fake_models: dict[str, list[object]]
) -> None:
    c = create_chat(conn)

    user, reply = send_message(conn, paths, c.chat_id, "  How much did I spend on my car in 2024?  ")

    assert fake_models["asked"] == ["How much did I spend on my car in 2024?"]
    assert fake_models["rewrites"] == []
    assert user.standalone is None
    assert (reply.route, reply.sql, reply.rows) == ("sql", "SELECT 1", [{"n": 1}])
    stored, messages = get_chat(conn, c.chat_id)
    assert stored.title == "How much did I spend on my car in 2024?"
    assert [(m.role, m.text) for m in messages] == [
        ("user", "How much did I spend on my car in 2024?"),
        ("assistant", "answer to: How much did I spend on my car in 2024?"),
    ]


def test_follow_up_is_rewritten_with_the_conversation(
    conn: sqlite3.Connection, paths: Paths, fake_models: dict[str, list[object]]
) -> None:
    c = create_chat(conn)
    send_message(conn, paths, c.chat_id, "When was my last oil change?")

    user, _ = send_message(conn, paths, c.chat_id, "How much did it cost?")

    assert fake_models["rewrites"] == [
        [("user", "When was my last oil change?"), ("assistant", "answer to: When was my last oil change?")]
    ]
    assert fake_models["asked"][-1] == "How much did it cost? (about the oil change)"
    assert (
        user.text == "How much did it cost?"
        and user.standalone == "How much did it cost? (about the oil change)"
    )
    assert (
        get_chat(conn, c.chat_id)[0].title == "When was my last oil change?"
    )  # title stays the first question
    assert len(get_chat(conn, c.chat_id)[1]) == 4


def test_failed_answer_stores_nothing(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(conn: sqlite3.Connection, paths: Paths, question: str) -> Answer:
        raise ConnectionError("ollama is not running")

    monkeypatch.setattr(chat, "ask", down)
    c = create_chat(conn)

    with pytest.raises(ConnectionError):
        send_message(conn, paths, c.chat_id, "anything")

    chat_row, messages = get_chat(conn, c.chat_id)
    assert messages == [] and chat_row.title == config.CHAT_DEFAULT_TITLE


def test_long_first_question_is_cut_at_a_word(
    conn: sqlite3.Connection, paths: Paths, fake_models: dict[str, list[object]]
) -> None:
    c = create_chat(conn)
    send_message(conn, paths, c.chat_id, "word " * 40)

    title = get_chat(conn, c.chat_id)[0].title
    assert len(title) <= config.CHAT_TITLE_CHARS + 1 and title.endswith("…") and "  " not in title


@pytest.fixture
def client(paths: Paths, conn: sqlite3.Connection) -> Iterator[TestClient]:
    """The API, pointed at the test archive.

    Args:
        paths (Paths): Temporary archive locations.
        conn (sqlite3.Connection): Initialized test database (creates the archive).

    Yields:
        TestClient: Client for the app.
    """
    api.app.dependency_overrides[api._paths] = lambda: paths
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def test_chat_api_round_trip(client: TestClient, fake_models: dict[str, list[object]]) -> None:
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]

    sent = client.post(f"/api/chats/{chat_id}/messages", json={"text": "When was my last oil change?"})
    assert sent.status_code == 200
    assert sent.json()["assistant"]["text"] == "answer to: When was my last oil change?"

    listed = client.get("/api/chats").json()
    assert [c["title"] for c in listed] == ["When was my last oil change?"]
    assert len(client.get(f"/api/chats/{chat_id}").json()["messages"]) == 2
    assert client.patch(f"/api/chats/{chat_id}", json={"title": "Car"}).json()["title"] == "Car"
    assert client.delete(f"/api/chats/{chat_id}").status_code == 200
    assert client.get(f"/api/chats/{chat_id}").status_code == 404
    assert client.post(f"/api/chats/{chat_id}/messages", json={"text": "hi"}).status_code == 404
