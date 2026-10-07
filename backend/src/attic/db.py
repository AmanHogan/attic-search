"""SQLite connection and schema management."""

import re
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 10
"""Current schema version, stored in SQLite's `PRAGMA user_version`."""

_VIEW_STATEMENTS = [
    stmt.strip()[stmt.strip().index("CREATE VIEW") :]
    for stmt in resources.files("attic").joinpath("schema.sql").read_text().split(";")
    if "CREATE VIEW" in stmt
]
"""The CREATE VIEW statements from schema.sql; views are rebuilt from these after every migration."""

MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE raw_files ADD COLUMN raw_path TEXT NOT NULL DEFAULT ''",
        "UPDATE raw_files SET raw_path = 'raw/' || sha256 || '.pdf'",
    ],
    3: [
        "ALTER TABLE chunks ADD COLUMN embedding BLOB NOT NULL DEFAULT x''",
        "CREATE INDEX chunks_doc ON chunks (doc_id)",
    ],
    4: [
        "ALTER TABLE route_log ADD COLUMN sql TEXT",
        "ALTER TABLE route_log ADD COLUMN answer TEXT",
    ],
    5: [
        "ALTER TABLE receipts ADD COLUMN kind TEXT NOT NULL DEFAULT 'receipt'"
        " CHECK (kind IN ('receipt', 'estimate'))",
    ],
    # documents gains `caption` and a new doc_type list; receipts becomes amounts with
    # `party`, `service` and a new `award` kind. SQLite can't change a CHECK constraint in
    # place, so both tables are rebuilt. Money rows are dropped rather than copied: this
    # version re-runs extraction anyway, which refills them.
    6: [
        "ALTER TABLE documents RENAME TO documents_old",
        """CREATE TABLE documents (
            doc_id         TEXT PRIMARY KEY,
            title          TEXT,
            doc_type       TEXT CHECK (doc_type IN
                               ('receipt', 'estimate', 'statement', 'letter', 'certificate',
                                'record', 'schoolwork', 'id_card', 'form', 'program',
                                'note', 'photo', 'other')),
            doc_date_start TEXT,
            doc_date_end   TEXT,
            collection_id  TEXT REFERENCES collections (collection_id),
            summary        TEXT,
            caption        TEXT,
            sensitive      INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL,
            CHECK (doc_date_start IS NULL OR doc_date_end IS NULL
                   OR doc_date_start <= doc_date_end)
        )""",
        # Old types that no longer exist become NULL; re-running extract fills them in again.
        """INSERT INTO documents
             (doc_id, title, doc_type, doc_date_start, doc_date_end, collection_id,
              summary, sensitive, created_at)
           SELECT doc_id, title,
                  CASE doc_type WHEN 'keepsake' THEN NULL WHEN 'other' THEN 'other'
                                WHEN NULL THEN NULL ELSE doc_type END,
                  doc_date_start, doc_date_end, collection_id, summary, sensitive, created_at
           FROM documents_old""",
        "DROP TABLE documents_old",
        "DROP TABLE receipts",
        """CREATE TABLE amounts (
            doc_id       TEXT PRIMARY KEY REFERENCES documents (doc_id),
            kind         TEXT NOT NULL DEFAULT 'receipt'
                             CHECK (kind IN ('receipt', 'estimate', 'award')),
            party        TEXT,
            date         TEXT,
            amount_cents INTEGER,
            odometer     INTEGER,
            service      TEXT,
            category     TEXT
        )""",
    ],
    # No table changes: amounts_v gained summary and tags, which _refresh_views picks up.
    7: [],
    # Text-native files (.docx, .doc) have no page image, so these columns became nullable.
    8: [
        """CREATE TABLE pages_v8 (
            page_id     TEXT PRIMARY KEY,
            raw_sha256  TEXT NOT NULL REFERENCES raw_files (sha256),
            raw_page_no INTEGER NOT NULL,
            doc_id      TEXT NOT NULL REFERENCES documents (doc_id),
            page_no     INTEGER NOT NULL,
            phash       TEXT,
            image_path  TEXT,
            thumb_path  TEXT,
            UNIQUE (raw_sha256, raw_page_no),
            UNIQUE (doc_id, page_no)
        )""",
        "INSERT INTO pages_v8 SELECT page_id, raw_sha256, raw_page_no, doc_id, page_no,"
        " phash, image_path, thumb_path FROM pages",
        "DROP TABLE pages",
        "ALTER TABLE pages_v8 RENAME TO pages",
        "CREATE INDEX pages_phash ON pages (phash)",
    ],
    # documents gains the `project` type, for a folder of source code. SQLite can't change a
    # CHECK constraint in place, so the table is rebuilt with every row copied across.
    9: [
        "ALTER TABLE documents RENAME TO documents_old",
        """CREATE TABLE documents (
            doc_id         TEXT PRIMARY KEY,
            title          TEXT,
            doc_type       TEXT CHECK (doc_type IN
                               ('receipt', 'estimate', 'statement', 'letter', 'certificate',
                                'record', 'schoolwork', 'id_card', 'form', 'program',
                                'note', 'photo', 'project', 'other')),
            doc_date_start TEXT,
            doc_date_end   TEXT,
            collection_id  TEXT REFERENCES collections (collection_id),
            summary        TEXT,
            caption        TEXT,
            sensitive      INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL,
            CHECK (doc_date_start IS NULL OR doc_date_end IS NULL
                   OR doc_date_start <= doc_date_end)
        )""",
        "INSERT INTO documents SELECT doc_id, title, doc_type, doc_date_start, doc_date_end,"
        " collection_id, summary, caption, sensitive, created_at FROM documents_old",
        "DROP TABLE documents_old",
    ],
    # Chat sessions in the app: new tables only, nothing existing changes.
    10: [
        """CREATE TABLE IF NOT EXISTS chats (
            chat_id    TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS messages (
            message_id   TEXT PRIMARY KEY,
            chat_id      TEXT NOT NULL REFERENCES chats (chat_id),
            role         TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            text         TEXT NOT NULL,
            standalone   TEXT,
            route        TEXT,
            sql          TEXT,
            note         TEXT,
            sources_json TEXT,
            rows_json    TEXT,
            created_at   TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat_id)",
    ],
}
"""SQL that upgrades the tables to each version from the one before it (views: see _refresh_views)."""


# --- start private functions ---


def _migrate(conn: sqlite3.Connection, current: int) -> None:
    """Apply each migration from `current` up to `SCHEMA_VERSION`, then rebuild the views.

    Foreign keys are turned off for the duration: a migration may rebuild a
    table that others reference, which SQLite would otherwise reject. They are
    checked again at the end, before anything is committed for good.

    Args:
        conn (sqlite3.Connection): Open archive database.
        current (int): The database's current schema version.

    Raises:
        RuntimeError: If the migrated database has broken references.
    """
    # Both pragmas are no-ops inside a transaction, so they are set outside one.
    conn.execute("PRAGMA foreign_keys = OFF")
    # Keeps `ALTER TABLE ... RENAME` from rewriting other tables' references to the old name.
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        for version in range(current + 1, SCHEMA_VERSION + 1):
            with conn:
                for sql in MIGRATIONS[version]:
                    conn.execute(sql)
                conn.execute(f"PRAGMA user_version = {version}")
        _refresh_views(conn)
        if broken := conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError(f"migration left broken references: {broken[:3]}")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def _refresh_views(conn: sqlite3.Connection) -> None:
    """Drop and recreate every view from schema.sql, so views always match the current tables.

    Views hold no data, so rebuilding them is always safe; it also means a
    changed view never needs its own migration step.

    Args:
        conn (sqlite3.Connection): Open archive database.
    """
    with conn:
        for statement in _VIEW_STATEMENTS:
            name = re.match(r"CREATE VIEW (\w+)", statement)
            assert name is not None
            conn.execute(f"DROP VIEW IF EXISTS {name.group(1)}")
            conn.execute(statement)


# --- end private functions ---


def connect(path: Path) -> sqlite3.Connection:
    """Open the archive database with foreign keys on and WAL journaling.

    Args:
        path (Path): Database file.

    Returns:
        sqlite3.Connection: Connection returning rows as `sqlite3.Row`.
    """
    # check_same_thread=False: the API hands one connection per request to a worker
    # thread, which may differ from the one that opened it. Each connection is still
    # only ever used by one request at a time.
    conn = sqlite3.connect(path, check_same_thread=False)
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
    if current == SCHEMA_VERSION:
        return False
    _migrate(conn, current)
    return False


def now() -> str:
    """Return the current UTC time as an ISO-8601 string, to the second."""
    return datetime.now(UTC).isoformat(timespec="seconds")
