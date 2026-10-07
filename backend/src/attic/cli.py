"""Command-line entry point: `attic <command>`."""

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import get_args

from attic.ask import ask
from attic.code import find_projects, ingest_project
from attic.config import Paths, get_paths
from attic.db import connect, init_db
from attic.extract import DocType, run_extract
from attic.index import run_index
from attic.ingest import ingest_file, ingest_inbox
from attic.merge import merge_documents
from attic.search import search
from attic.text import requeue_word, run_ocr


def open_archive(paths: Paths) -> sqlite3.Connection:
    """Connect to an existing archive, exiting with a message if there isn't one.

    Args:
        paths (Paths): Archive locations.

    Returns:
        sqlite3.Connection: Open archive database.
    """
    if not paths.db.exists():
        sys.exit(f"no archive at {paths.archive}; run `attic init` first")
    conn = connect(paths.db)
    init_db(conn)
    return conn


def cmd_init(paths: Paths, args: argparse.Namespace) -> None:
    paths.ensure()
    conn = connect(paths.db)
    created = init_db(conn)
    print(f"{'created' if created else 'already exists'}: {paths.archive}")
    print(f"drop scans into: {paths.inbox}")


def cmd_ingest(paths: Paths, args: argparse.Namespace) -> None:
    """Ingest the given files, or everything in the inbox, and print a line per file.

    Exits with status 1 if any file failed.

    Args:
        paths (Paths): Archive locations.
        args (argparse.Namespace): Parsed arguments; `args.files` is the optional file list.
    """
    conn = open_archive(paths)
    paths.ensure()
    results = (
        [ingest_file(conn, paths, Path(p)) for p in args.files] if args.files else ingest_inbox(conn, paths)
    )
    if not results:
        print("nothing to ingest")
    for r in results:
        if r.status == "ingested":
            detail = f"{r.pages} page(s), doc {r.doc_id}"
        else:
            detail = r.error or f"same as {(r.sha256 or '')[:12]}"
        print(f"{r.status:9} {r.source.name}  {detail}")
    if any(r.status == "error" for r in results):
        sys.exit(1)


def cmd_ocr(paths: Paths, args: argparse.Namespace) -> None:
    """Extract text for every pending page and print a line per page as it finishes.

    Exits with status 1 if any page failed.

    Args:
        paths (Paths): Archive locations.
        args (argparse.Namespace): Parsed arguments; `redo_word` re-reads every Word file first.
    """
    conn = open_archive(paths)
    if args.redo_word:
        print(f"requeued {requeue_word(conn)} Word pages")
    failed = False
    done = 0
    for r in run_ocr(conn, paths):
        label = f"{r.source_filename} p{r.page_no}"
        if r.status == "done":
            done += 1
            print(f"done   {r.engine:8} conf {r.confidence:.2f}  {r.chars:6} chars  {label}")
        else:
            failed = True
            print(f"error  {label}  {r.error}")
    if not done and not failed:
        print("nothing to OCR")
    if failed:
        sys.exit(1)


def cmd_scan_code(paths: Paths, args: argparse.Namespace) -> None:
    """List the code projects under a folder and what would be ingested; changes nothing.

    Args:
        paths (Paths): Archive locations (unused).
        args (argparse.Namespace): Parsed arguments; `folder` is the folder to scan.
    """
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        sys.exit(f"not a folder: {folder}")
    projects = find_projects(folder)
    for p in projects:
        mix = ", ".join(f"{n} {ext}" for ext, n in p.languages.most_common(4))
        big = f"  ({p.too_large} too large)" if p.too_large else ""
        print(f"{p.name:40} {len(p.files):5} files  {mix}{big}")
    print(f"{len(projects)} projects, {sum(len(p.files) for p in projects)} files")


def cmd_ingest_code(paths: Paths, args: argparse.Namespace) -> None:
    """Copy code projects into the archive, deleting the inbox files once safely archived.

    Args:
        paths (Paths): Archive locations.
        args (argparse.Namespace): Parsed arguments; `folder` is scanned, `project` limits it
            to named projects, and `keep` leaves the source files in place.
    """
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        sys.exit(f"not a folder: {folder}")
    conn = open_archive(paths)
    wanted = set(args.project)
    projects = [p for p in find_projects(folder) if not wanted or p.name in wanted]
    if missing := wanted - {p.name for p in projects}:
        sys.exit(f"no such project: {', '.join(sorted(missing))}")
    for p in projects:
        r = ingest_project(conn, paths, p, delete_source=not args.keep)
        print(f"{r.name:40} added {r.added:4}  duplicate {r.duplicates:3}  removed {r.removed:4}")
    print(f"{len(projects)} projects")


def cmd_extract(paths: Paths, args: argparse.Namespace) -> None:
    """Extract metadata for every document whose text is ready, printing a line per document.

    Exits with status 1 if any document failed.

    Args:
        paths (Paths): Archive locations.
        args (argparse.Namespace): Parsed arguments; `limit` stops after that many documents,
            and `fast` uses the small text-only model.
    """
    conn = open_archive(paths)
    failed = False
    done = skipped = 0
    for r in run_extract(conn, paths, args.limit, args.fast):
        if r.status == "skipped":
            skipped += 1
        elif r.status == "done":
            done += 1
            print(
                f"done   {r.doc_type:10} {r.date_start or '(no date)':10}  {r.title}  [{r.source_filename}]"
            )
        else:
            failed = True
            print(f"error  {r.source_filename}  {r.error}")
    if skipped:
        print(f"skipped {skipped} photos (left pending for a run without --fast)")
    if not done and not failed and not skipped:
        print("nothing to extract")
    if failed:
        sys.exit(1)


def cmd_index(paths: Paths, args: argparse.Namespace) -> None:
    """Chunk and embed every extracted document not yet indexed, printing a line per document.

    Exits with status 1 if any document failed.

    Args:
        paths (Paths): Archive locations.
        args (argparse.Namespace): Parsed arguments (unused).
    """
    conn = open_archive(paths)
    failed = False
    done = 0
    for r in run_index(conn, paths):
        if r.status == "done":
            done += 1
            print(f"done   {r.chunks:3} chunk(s)  {r.title}  [{r.source_filename}]")
        else:
            failed = True
            print(f"error  {r.source_filename}  {r.error}")
    if not done and not failed:
        print("nothing to index")
    if failed:
        sys.exit(1)


def cmd_search(paths: Paths, args: argparse.Namespace) -> None:
    """Run a hybrid search and print the matching pages, best first.

    Args:
        paths (Paths): Archive locations.
        args (argparse.Namespace): Parsed arguments: `query` words, optional `type`, `year`, `limit`.
    """
    conn = open_archive(paths)
    hits = search(conn, " ".join(args.query), doc_type=args.type, year=args.year, limit=args.limit)
    if not hits:
        print("no matches")
    for n, h in enumerate(hits, start=1):
        when = h.date_start if h.date_start == h.date_end else f"{h.date_start} to {h.date_end}"
        print(
            f"{n:2}. {h.title or '(untitled)'}  ({h.doc_type or '?'}, {when or 'undated'}, page {h.page_no})"
        )
        print(f"    {h.snippet}")
        print(f"    [{h.source_filename}]  matched by {h.matched_by}, score {h.score:.4f}")


def cmd_ask(paths: Paths, args: argparse.Namespace) -> None:
    """Answer a question and print the answer with its sources or SQL.

    Args:
        paths (Paths): Archive locations.
        args (argparse.Namespace): Parsed arguments: `question` words.
    """
    conn = open_archive(paths)
    answer = ask(conn, paths, " ".join(args.question))
    print(answer.text)
    if answer.note:
        print(f"\n(note: {answer.note})")
    if answer.sources:
        print("\nSources:")
        for n, h in enumerate(answer.sources, start=1):
            when = h.date_start or "undated"
            print(f"  [{n}] {h.title or '(untitled)'} ({h.doc_type or '?'}, {when}, page {h.page_no})")
            # A text-native file has no page image, so name the file it came from instead.
            where = paths.archive / h.image_path if h.image_path else h.source_filename
            print(f"      {where}")
    if answer.sql:
        print(f"\nSQL ({len(answer.rows)} row(s)):\n  {answer.sql}")


def cmd_merge(paths: Paths, args: argparse.Namespace) -> None:
    """Merge documents into one, in the order given, and say what to run next.

    Args:
        paths (Paths): Archive locations.
        args (argparse.Namespace): Parsed arguments: `documents`, doc IDs or original filenames.
    """
    conn = open_archive(paths)
    try:
        doc_id, pages = merge_documents(conn, args.documents)
    except ValueError as e:
        sys.exit(f"error: {e}")
    print(f"merged into {doc_id}: {pages} page(s). Run `attic extract` then `attic index` to update it.")


def cmd_serve(paths: Paths, args: argparse.Namespace) -> None:
    """Start the web API, which the frontend talks to.

    Args:
        paths (Paths): Archive locations.
        args (argparse.Namespace): Parsed arguments: `host`, `port`, `reload`.
    """
    from attic.api import serve

    open_archive(paths).close()
    print(f"API on http://{args.host}:{args.port}  (archive: {paths.archive})")
    serve(args.host, args.port, args.reload)


def cmd_status(paths: Paths, args: argparse.Namespace) -> None:
    """Print counts of raw files, documents, pages, and jobs by stage and status.

    Args:
        paths (Paths): Archive locations.
        args (argparse.Namespace): Parsed arguments (unused).
    """
    conn = open_archive(paths)
    for label, sql in [
        ("raw files", "SELECT count(*) FROM raw_files"),
        ("documents", "SELECT count(*) FROM documents"),
        ("pages", "SELECT count(*) FROM pages"),
    ]:
        print(f"{label:12} {conn.execute(sql).fetchone()[0]}")
    for row in conn.execute(
        "SELECT stage, status, count(*) n FROM jobs GROUP BY stage, status ORDER BY stage, status"
    ):
        print(f"jobs         {row['stage']}/{row['status']}: {row['n']}")


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and run the chosen subcommand.

    Args:
        argv (list[str] | None): Arguments to parse; defaults to `sys.argv[1:]`.
    """

    # Parser setup
    parser = argparse.ArgumentParser(prog="attic", description="Local home archive.")
    parser.add_argument("--home", help="archive home directory (default: $ATTIC_HOME or cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    # Init command
    p_init = sub.add_parser("init", help="create archive/, inbox/ and the database")
    p_init.set_defaults(func=cmd_init)

    # Ingest command
    p_ingest = sub.add_parser("ingest", help="ingest PDFs and images from inbox/ (or the given files)")
    p_ingest.add_argument(
        "files", nargs="*", help="PDFs/images to ingest; files outside inbox/ are copied, not moved"
    )
    p_ingest.set_defaults(func=cmd_ingest)

    # OCR command
    p_ocr = sub.add_parser("ocr", help="extract text for every page still waiting for it")
    p_ocr.add_argument("--redo-word", action="store_true", help="re-read the text of every .doc/.docx first")
    p_ocr.set_defaults(func=cmd_ocr)

    # Scan-code command
    p_scan = sub.add_parser("scan-code", help="list the code projects under a folder (dry run)")
    p_scan.add_argument("folder", help="folder holding code projects")
    p_scan.set_defaults(func=cmd_scan_code)

    # Ingest-code command
    p_code = sub.add_parser("ingest-code", help="copy code projects into the archive (see scan-code)")
    p_code.add_argument("folder", help="folder holding code projects")
    p_code.add_argument("--project", action="append", default=[], help="only this project (repeatable)")
    p_code.add_argument("--keep", action="store_true", help="leave the source files where they are")
    p_code.set_defaults(func=cmd_ingest_code)

    # Extract command
    p_extract = sub.add_parser(
        "extract", help="use the local LLM to fill in each document's type, date, etc."
    )
    p_extract.add_argument(
        "--fast", action="store_true", help="use the small text-only model; photos wait for a normal run"
    )
    p_extract.add_argument("--limit", type=int, help="stop after this many documents (to let the Mac cool)")
    p_extract.set_defaults(func=cmd_extract)

    # Index command
    p_index = sub.add_parser("index", help="chunk and embed extracted documents for search")
    p_index.set_defaults(func=cmd_index)

    # Search command
    p_search = sub.add_parser("search", help="find pages by keyword and meaning")
    p_search.add_argument("query", nargs="+", help="what to search for")
    p_search.add_argument("--type", choices=get_args(DocType), help="only this document type")
    p_search.add_argument("--year", type=int, help="only documents dated in this year")
    p_search.add_argument("--limit", type=int, default=10, help="maximum results (default 10)")
    p_search.set_defaults(func=cmd_search)

    # Ask command
    p_ask = sub.add_parser("ask", help="ask a question in plain English")
    p_ask.add_argument("question", nargs="+", help="the question")
    p_ask.set_defaults(func=cmd_ask)

    # Merge command
    p_merge = sub.add_parser("merge", help="combine documents (e.g. separately photographed pages) into one")
    p_merge.add_argument("documents", nargs="+", help="doc IDs or original filenames, in page order")
    p_merge.set_defaults(func=cmd_merge)

    # Serve command
    p_serve = sub.add_parser("serve", help="start the web API for the frontend")
    p_serve.add_argument("--host", default="127.0.0.1", help="address to bind (default: localhost only)")
    p_serve.add_argument("--port", type=int, default=8000, help="port to listen on (default 8000)")
    p_serve.add_argument("--reload", action="store_true", help="restart when the code changes")
    p_serve.set_defaults(func=cmd_serve)

    # Status command
    p_status = sub.add_parser("status", help="show archive counts")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    args.func(get_paths(args.home), args)


if __name__ == "__main__":
    main()
