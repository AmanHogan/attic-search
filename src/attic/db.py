"""SQLite connection and schema management."""

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 2
"""Current schema version, stored in SQLite's `PRAGMA user_version`."""

MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE raw_files ADD COLUMN raw_path TEXT NOT NULL DEFAULT ''",
        "UPDATE raw_files SET raw_path = 'raw/' || sha256 || '.pdf'",
    ],
}
"""SQL that upgrades a database to each version from the one before it."""


def connect(path: Path) -> sqlite3.Connection:
    """Open the archive database with foreign keys on and WAL journaling.

    Args:
        path (Path): Database file.

    Returns:
        sqlite3.Connection: Connection returning rows as `sqlite3.Row`.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> bool:
    """Create the schema if the database is empty, or migrate an older one.

    Args:
        conn (sqlite3.Connection): Open archive database.

    Returns:
        bool: True if the schema was created, False if it already existed.

    Raises:
        RuntimeError: If the database is from a newer version of attic.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == 0:
        schema = resources.files("attic").joinpath("schema.sql").read_text()
        conn.executescript(schema)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return True
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"database schema v{current} is newer than this code (v{SCHEMA_VERSION})")
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with conn:
            for sql in MIGRATIONS[version]:
                conn.execute(sql)
            conn.execute(f"PRAGMA user_version = {version}")
    return False


def now() -> str:
    """Return the current UTC time as an ISO-8601 string, to the second."""
    return datetime.now(UTC).isoformat(timespec="seconds")
