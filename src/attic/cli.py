"""Command-line entry point: `attic <command>`."""

import argparse
import sqlite3
import sys
from pathlib import Path

from attic.config import Paths, get_paths
from attic.db import connect, init_db
from attic.ingest import ingest_file, ingest_inbox


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

    # Status command
    p_status = sub.add_parser("status", help="show archive counts")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    args.func(get_paths(args.home), args)


if __name__ == "__main__":
    main()
