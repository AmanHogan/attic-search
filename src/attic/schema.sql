-- attic schema v2. Dates are ISO-8601 TEXT; booleans are INTEGER 0/1.
-- File paths are stored relative to the archive/ directory.

-- Immutable originals: a PDF or a photo. One file may hold several documents.
CREATE TABLE raw_files (
    sha256          TEXT PRIMARY KEY,
    raw_path        TEXT NOT NULL,    -- e.g. raw/<sha256>.pdf or raw/<sha256>.jpg
    source_filename TEXT NOT NULL,
    scanned_at      TEXT,
    page_count      INTEGER NOT NULL,
    ingested_at     TEXT NOT NULL
);

CREATE TABLE collections (
    collection_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT
);

-- One logical item: an ordered set of pages.
CREATE TABLE documents (
    doc_id         TEXT PRIMARY KEY,  -- ULID
    title          TEXT,
    doc_type       TEXT CHECK (doc_type IN
                       ('note', 'schoolwork', 'receipt', 'letter',
                        'keepsake', 'photo', 'form', 'other')),
    doc_date_start TEXT,              -- start = end when exact; both NULL when unknown
    doc_date_end   TEXT,
    collection_id  TEXT REFERENCES collections (collection_id),
    summary        TEXT,
    sensitive      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    CHECK (doc_date_start IS NULL OR doc_date_end IS NULL OR doc_date_start <= doc_date_end)
);

-- A page lives physically in a raw file and belongs logically to a document.
-- Splitting/merging documents only rewrites doc_id/page_no.
CREATE TABLE pages (
    page_id     TEXT PRIMARY KEY,     -- ULID
    raw_sha256  TEXT NOT NULL REFERENCES raw_files (sha256),
    raw_page_no INTEGER NOT NULL,
    doc_id      TEXT NOT NULL REFERENCES documents (doc_id),
    page_no     INTEGER NOT NULL,
    phash       TEXT,
    image_path  TEXT NOT NULL,
    thumb_path  TEXT NOT NULL,
    UNIQUE (raw_sha256, raw_page_no),
    UNIQUE (doc_id, page_no)
);
CREATE INDEX pages_phash ON pages (phash);

-- Every OCR result ever produced; never overwritten.
CREATE TABLE page_text (
    page_id    TEXT NOT NULL REFERENCES pages (page_id),
    engine     TEXT NOT NULL,
    version    INTEGER NOT NULL,
    confidence REAL,
    text_path  TEXT NOT NULL,
    is_active  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (page_id, engine, version)
);
CREATE UNIQUE INDEX page_text_one_active ON page_text (page_id) WHERE is_active = 1;

CREATE TABLE tags (
    doc_id TEXT NOT NULL REFERENCES documents (doc_id),
    tag    TEXT NOT NULL,
    PRIMARY KEY (doc_id, tag)
);

CREATE TABLE people (
    doc_id TEXT NOT NULL REFERENCES documents (doc_id),
    name   TEXT NOT NULL,
    PRIMARY KEY (doc_id, name)
);

CREATE TABLE receipts (
    doc_id       TEXT PRIMARY KEY REFERENCES documents (doc_id),
    vendor       TEXT,
    date         TEXT,
    amount_cents INTEGER,
    odometer     INTEGER,
    category     TEXT
);

CREATE TABLE chunks (
    chunk_id    TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES documents (doc_id),
    page_id     TEXT REFERENCES pages (page_id),
    text        TEXT NOT NULL,
    embed_model TEXT NOT NULL
);

-- One row per (target, stage, version): enqueueing twice is a no-op.
CREATE TABLE jobs (
    job_id     TEXT PRIMARY KEY,
    target_id  TEXT NOT NULL,         -- page_id or doc_id, depending on stage
    stage      TEXT NOT NULL,
    version    INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'running', 'done', 'error')),
    error      TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (target_id, stage, version)
);
CREATE INDEX jobs_pending ON jobs (stage, status);

CREATE TABLE route_log (
    ts           TEXT NOT NULL,
    query        TEXT NOT NULL,
    route        TEXT NOT NULL,
    filters_json TEXT,
    template     TEXT,
    correct      INTEGER
);

-- Keyword search over active page text; rebuilt from page_text.
CREATE VIRTUAL TABLE pages_fts USING fts5 (
    text,
    page_id UNINDEXED,
    doc_id UNINDEXED,
    tokenize = 'trigram'
);
