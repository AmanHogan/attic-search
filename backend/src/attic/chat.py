"""Chat sessions: ask follow-up questions in a conversation, keep it, delete it.

A chat is an ordered list of user and assistant messages. A follow-up like "how much
was the second one?" can't be searched on its own, so before answering, the model
rewrites it into a standalone question using the last few turns. That question then
goes through the normal `ask` pipeline (route, then RAG or SQL), unchanged.

A message pair is stored only once the answer exists, so a failed question (the
model not running) leaves the chat as it was.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import ollama
from ulid import ULID

from attic import config
from attic.ask import Answer, ask
from attic.config import Paths
from attic.db import now

REWRITE_PROMPT = """You rewrite the user's latest message in a conversation about their personal \
document archive into one standalone question that makes sense without the conversation.
- Replace words like "it", "that one", "the second one", "they", "there" with what they refer to.
- Keep names, dates, amounts, and document titles from the conversation when they are meant.
- If the message already stands alone, return it unchanged.
- Return only the question, with no explanation and no quotation marks."""
"""System prompt for turning a follow-up into a standalone question."""


@dataclass(frozen=True)
class Chat:
    """A conversation.

    Attributes:
        chat_id (str): ULID.
        title (str): The first question, or `CHAT_DEFAULT_TITLE` before there is one.
        created_at (str): When it was started (ISO timestamp).
        updated_at (str): When its last message was added (ISO timestamp).
    """

    chat_id: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Message:
    """One turn in a chat.

    Attributes:
        message_id (str): ULID. Messages are read in insertion order (SQLite rowid): a question
            and its answer get IDs in the same millisecond, which ULIDs don't order.
        chat_id (str): Chat it belongs to.
        role (str): "user" or "assistant".
        text (str): What was asked, or the answer.
        created_at (str): When it was added (ISO timestamp).
        standalone (str | None): User only: the follow-up as rewritten for search,
            or None if it was used as written.
        route (str | None): Assistant only: "rag" or "sql".
        sql (str | None): Assistant only: the query that ran.
        note (str | None): Assistant only: e.g. that SQL fell back to search.
        sources (list[dict[str, Any]]): Assistant only: the cited pages.
        rows (list[dict[str, Any]]): Assistant only: the query's rows.
    """

    message_id: str
    chat_id: str
    role: str
    text: str
    created_at: str
    standalone: str | None = None
    route: str | None = None
    sql: str | None = None
    note: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)


# --- start private functions ---


def _chat_row(row: sqlite3.Row) -> Chat:
    """Build a `Chat` from a `chats` row."""
    return Chat(row["chat_id"], row["title"], row["created_at"], row["updated_at"])


def _message_row(row: sqlite3.Row) -> Message:
    """Build a `Message` from a `messages` row, decoding its JSON columns."""
    return Message(
        message_id=row["message_id"],
        chat_id=row["chat_id"],
        role=row["role"],
        text=row["text"],
        created_at=row["created_at"],
        standalone=row["standalone"],
        route=row["route"],
        sql=row["sql"],
        note=row["note"],
        sources=json.loads(row["sources_json"]) if row["sources_json"] else [],
        rows=json.loads(row["rows_json"]) if row["rows_json"] else [],
    )


def _history(conn: sqlite3.Connection, chat_id: str) -> list[tuple[str, str]]:
    """The last `CHAT_HISTORY_TURNS` question/answer pairs of a chat, oldest first.

    Args:
        conn (sqlite3.Connection): Open archive database.
        chat_id (str): Chat to read.

    Returns:
        list[tuple[str, str]]: (role, text) pairs, at most twice `CHAT_HISTORY_TURNS` long.
    """
    rows = conn.execute(
        "SELECT role, coalesce(standalone, text) AS text FROM messages"
        " WHERE chat_id = ? ORDER BY rowid DESC LIMIT ?",
        (chat_id, config.CHAT_HISTORY_TURNS * 2),
    ).fetchall()
    return [(row["role"], row["text"]) for row in reversed(rows)]


def _rewrite(question: str, history: list[tuple[str, str]]) -> str:
    """Rewrite a follow-up into a standalone question, using the conversation so far.

    Args:
        question (str): The user's latest message.
        history (list[tuple[str, str]]): Earlier (role, text) turns, oldest first.

    Returns:
        str: The standalone question; the original if the model returns nothing usable.
    """
    turns = "\n".join(f"{'User' if role == 'user' else 'Assistant'}: {text}" for role, text in history)
    response = ollama.chat(
        model=config.CHAT_REWRITE_MODEL,
        messages=[
            {"role": "system", "content": REWRITE_PROMPT},
            {"role": "user", "content": f"Conversation:\n{turns}\n\nLatest message: {question}"},
        ],
        options={"temperature": 0, "num_ctx": config.EXTRACT_NUM_CTX},
    )
    rewritten = (response.message.content or "").strip().strip('"').strip()
    return rewritten or question


def _title(question: str) -> str:
    """A chat title from its first question, cut at a word boundary to `CHAT_TITLE_CHARS`."""
    title = " ".join(question.split())
    if len(title) <= config.CHAT_TITLE_CHARS:
        return title
    return title[: config.CHAT_TITLE_CHARS].rsplit(" ", 1)[0] + "…"


# --- end private functions ---


def create_chat(conn: sqlite3.Connection, title: str | None = None) -> Chat:
    """Start a new, empty chat.

    Args:
        conn (sqlite3.Connection): Open archive database.
        title (str | None): Title; None for `CHAT_DEFAULT_TITLE` until the first question.

    Returns:
        Chat: The new chat.
    """
    ts = now()
    chat = Chat(str(ULID()), (title or "").strip() or config.CHAT_DEFAULT_TITLE, ts, ts)
    with conn:
        conn.execute(
            "INSERT INTO chats (chat_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (chat.chat_id, chat.title, chat.created_at, chat.updated_at),
        )
    return chat


def list_chats(conn: sqlite3.Connection) -> list[Chat]:
    """Every chat, most recently active first."""
    return [_chat_row(r) for r in conn.execute("SELECT * FROM chats ORDER BY updated_at DESC, chat_id DESC")]


def get_chat(conn: sqlite3.Connection, chat_id: str) -> tuple[Chat, list[Message]]:
    """One chat and all its messages, oldest first.

    Args:
        conn (sqlite3.Connection): Open archive database.
        chat_id (str): Chat to read.

    Returns:
        tuple[Chat, list[Message]]: The chat and its messages.

    Raises:
        KeyError: If there is no such chat.
    """
    row = conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
    if row is None:
        raise KeyError(chat_id)
    messages = conn.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY rowid", (chat_id,))
    return _chat_row(row), [_message_row(m) for m in messages]


def rename_chat(conn: sqlite3.Connection, chat_id: str, title: str) -> Chat:
    """Give a chat a new title.

    Args:
        conn (sqlite3.Connection): Open archive database.
        chat_id (str): Chat to rename.
        title (str): New title; blank means `CHAT_DEFAULT_TITLE`.

    Returns:
        Chat: The renamed chat.

    Raises:
        KeyError: If there is no such chat.
    """
    with conn:
        updated = conn.execute(
            "UPDATE chats SET title = ? WHERE chat_id = ?",
            (title.strip() or config.CHAT_DEFAULT_TITLE, chat_id),
        ).rowcount
    if not updated:
        raise KeyError(chat_id)
    return get_chat(conn, chat_id)[0]


def delete_chat(conn: sqlite3.Connection, chat_id: str) -> bool:
    """Delete a chat and its messages. The archive's documents are not touched.

    Args:
        conn (sqlite3.Connection): Open archive database.
        chat_id (str): Chat to delete.

    Returns:
        bool: True if it existed.
    """
    with conn:
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        return conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,)).rowcount > 0


def send_message(conn: sqlite3.Connection, paths: Paths, chat_id: str, text: str) -> tuple[Message, Message]:
    """Ask a question in a chat: rewrite it if it's a follow-up, answer it, and store both turns.

    The first question in a chat is used as written and becomes the chat's title if it
    still has the default one. Nothing is stored if answering fails.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        chat_id (str): Chat to add to.
        text (str): The user's message.

    Returns:
        tuple[Message, Message]: The stored user message and the assistant's answer.

    Raises:
        KeyError: If there is no such chat.
        ValueError: If the message is blank.
    """
    chat, _ = get_chat(conn, chat_id)
    question = text.strip()
    if not question:
        raise ValueError("message is empty")
    history = _history(conn, chat_id)
    standalone = _rewrite(question, history) if history else question
    answer: Answer = ask(conn, paths, standalone)

    ts = now()
    user = Message(
        str(ULID()), chat_id, "user", question, ts, standalone=standalone if standalone != question else None
    )
    reply = Message(
        str(ULID()),
        chat_id,
        "assistant",
        answer.text,
        ts,
        route=answer.route,
        sql=answer.sql,
        note=answer.note,
        sources=[hit.__dict__ for hit in answer.sources],
        rows=answer.rows,
    )
    title = _title(question) if chat.title == config.CHAT_DEFAULT_TITLE else chat.title
    with conn:
        conn.execute(
            "INSERT INTO messages (message_id, chat_id, role, text, standalone, created_at)"
            " VALUES (?, ?, 'user', ?, ?, ?)",
            (user.message_id, chat_id, user.text, user.standalone, ts),
        )
        conn.execute(
            "INSERT INTO messages (message_id, chat_id, role, text, route, sql, note,"
            " sources_json, rows_json, created_at) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?)",
            (
                reply.message_id,
                chat_id,
                reply.text,
                reply.route,
                reply.sql,
                reply.note,
                json.dumps(reply.sources, default=str),
                json.dumps(reply.rows, default=str),
                ts,
            ),
        )
        conn.execute("UPDATE chats SET title = ?, updated_at = ? WHERE chat_id = ?", (title, ts, chat_id))
    return user, reply
