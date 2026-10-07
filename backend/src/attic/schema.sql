-- attic schema v9. Dates are ISO-8601 TEXT; booleans are INTEGER 0/1.
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
                       ('receipt', 'estimate', 'statement', 'letter', 'certificate',
                        'record', 'schoolwork', 'id_card', 'form', 'program',
                        'note', 'photo', 'project', 'other')),
    doc_date_start TEXT,              -- start = end when exact; both NULL when unknown
    doc_date_end   TEXT,
    collection_id  TEXT REFERENCES collections (collection_id),
    summary        TEXT,
    caption        TEXT,              -- what a photo shows, for pages with little or no text
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
    image_path  TEXT,                 -- NULL for text-native files (.docx, .doc)
    thumb_path  TEXT,
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

-- Money on a document: paid (receipt), quoted (estimate), or received (award).
CREATE TABLE amounts (
    doc_id       TEXT PRIMARY KEY REFERENCES documents (doc_id),
    kind         TEXT NOT NULL DEFAULT 'receipt'
                     CHECK (kind IN ('receipt', 'estimate', 'award')),
    party        TEXT,            -- who was paid, or who paid you
    date         TEXT,
    amount_cents INTEGER,
    odometer     INTEGER,         -- vehicle mileage, if printed
    service      TEXT,            -- for vehicles: oil change, inspection, repair, registration, other
    category     TEXT             -- one of the fixed categories in extract.SpendCategory
);

-- Searchable pieces of page text (one per page unless the page is long), with their embedding.
CREATE TABLE chunks (
    chunk_id    TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES documents (doc_id),
    page_id     TEXT REFERENCES pages (page_id),
    text        TEXT NOT NULL,    -- includes the [type | date | title] header that was embedded
    embed_model TEXT NOT NULL,
    embedding   BLOB NOT NULL     -- float32 vector, L2-normalized
);
CREATE INDEX chunks_doc ON chunks (doc_id);

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

-- Every `attic ask`: how it was routed and what was answered.
CREATE TABLE route_log (
    ts           TEXT NOT NULL,
    query        TEXT NOT NULL,
    route        TEXT NOT NULL,     -- 'rag' or 'sql' (the path that produced the answer)
    filters_json TEXT,
    template     TEXT,              -- unused (from an earlier design)
    correct      INTEGER,           -- for manual grading; NULL until graded
    sql          TEXT,
    answer       TEXT
);

-- Read-only views that text-to-SQL is allowed to query (and nothing else).
CREATE VIEW documents_v AS
SELECT
    d.doc_id,
    d.title,
    d.doc_type,
    d.doc_date_start AS date_start,
    d.doc_date_end   AS date_end,
    d.summary,
    d.caption,
    d.sensitive,
    (SELECT group_concat(t.tag, ', ') FROM tags t WHERE t.doc_id = d.doc_id)     AS tags,
    (SELECT group_concat(p.name, ', ') FROM people p WHERE p.doc_id = d.doc_id)  AS people,
    (SELECT count(*) FROM pages pg WHERE pg.doc_id = d.doc_id)                   AS page_count
FROM documents d;

CREATE VIEW amounts_v AS
SELECT
    a.doc_id,
    d.title,
    a.kind,
    a.party,
    coalesce(a.date, d.doc_date_start) AS date,
    a.amount_cents / 100.0 AS amount,
    a.odometer,
    a.service,
    a.category,
    d.summary,
    (SELECT group_concat(t.tag, ', ') FROM tags t WHERE t.doc_id = a.doc_id) AS tags
FROM amounts a JOIN documents d ON d.doc_id = a.doc_id;

-- Keyword search over active page text; rebuilt from page_text.
CREATE VIRTUAL TABLE pages_fts USING fts5 (
    text,
    page_id UNINDEXED,
    doc_id UNINDEXED,
    tokenize = 'trigram'
);
