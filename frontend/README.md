# attic — frontend

Next.js 16 (App Router, Tailwind 4) UI for the archive. It talks to the Python API,
which must be running:

```sh
cd ../backend && uv run attic serve     # http://127.0.0.1:8000
npm run dev                             # http://localhost:3000
```

`/api/*` is rewritten to the Python API in [next.config.ts](next.config.ts), so the browser
sees one origin and page images can be used directly as `<img src="/api/pages/…/image">`.
Point `ATTIC_API` elsewhere if the API runs on another port.

## Pages

| Route            | What it does                                                               |
| ---------------- | -------------------------------------------------------------------------- |
| `/`              | Ask a question; shows the answer with its source pages, or the SQL it ran  |
| `/documents`     | Every document, filterable by type and year, sortable by date/title/amount |
| `/documents/:id` | Page image beside its OCR text, and an editor for every extracted field    |
| `/search`        | Keyword + meaning search over page text                                    |

Extraction is a local model's best guess, so the document page lets you correct any field.
Saving re-queues that document for indexing, so search stays in step.

## Checks

```sh
npx tsc --noEmit && npm run lint
```
