"""Find source-code projects under a folder, without reading or changing anything.

A project is a folder of code the user wrote. A folder with a build marker (`.git`,
`Makefile`, `package.json`, ...) is one project, subfolders included. Otherwise each
immediate subfolder that holds code is its own project, and code lying loose in the
folder itself forms a project named after it. Dependencies, build output and files
over `CODE_MAX_BYTES` are never listed (notebooks excepted: their outputs are dropped on read).
"""

import contextlib
import json
import os
import re
import shutil
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ulid import ULID

from attic import config
from attic.config import Paths
from attic.db import now
from attic.ingest import scanned_at, sha256_file


@dataclass(frozen=True)
class CodeProject:
    """One project and the source files that would be ingested for it.

    Attributes:
        name (str): The project folder's name.
        root (Path): The project folder.
        files (tuple[Path, ...]): Source files to ingest, in path order.
        too_large (int): Code files skipped for exceeding `CODE_MAX_BYTES`.
    """

    name: str
    root: Path
    files: tuple[Path, ...]
    too_large: int

    @property
    def languages(self) -> Counter[str]:
        """File counts by suffix, e.g. `Counter({".c": 12, ".h": 4})`."""
        return Counter(f.suffix.lower() for f in self.files)


# --- start private functions ---


def _walk_code(directory: Path, recurse: bool = True) -> tuple[list[Path], int]:
    """List the code files in a folder, pruning skipped folders as it goes.

    Args:
        directory (Path): Folder to walk.
        recurse (bool): False to look only at files directly inside `directory`.

    Returns:
        tuple[list[Path], int]: Source files in path order, and how many code files
            were skipped for size.
    """
    files: list[Path] = []
    too_large = 0
    for current, dirs, names in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in config.CODE_SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            path = Path(current) / name
            if name.startswith(config.INBOX_SKIP_PREFIXES) or path.suffix.lower() not in config.CODE_SUFFIXES:
                continue
            # A notebook's size is mostly embedded output, which is dropped on read, so it is exempt.
            if path.suffix.lower() != ".ipynb" and path.stat().st_size > config.CODE_MAX_BYTES:
                too_large += 1
            else:
                files.append(path)
        if not recurse:
            break
    return sorted(files), too_large


def _has_marker(directory: Path) -> bool:
    """Whether a folder holds a build marker such as `.git` or `Makefile`."""
    return any((directory / marker).exists() for marker in config.CODE_PROJECT_MARKERS)


def _project(name: str, root: Path, recurse: bool = True) -> CodeProject | None:
    """Build a project from a folder, or None if it holds no code."""
    files, too_large = _walk_code(root, recurse)
    return CodeProject(name, root, tuple(files), too_large) if files else None


def _course_tags(name: str) -> list[str]:
    """Tags for a project whose folder is named like `cse-5334`: the course and its subject.

    Args:
        name (str): Project folder name.

    Returns:
        list[str]: `project:<name>`, plus `course:cse-NNNN` and `course-title:<subject>`
            when the name carries a course number.
    """
    tags = [f"project:{name}"]
    if m := re.match(r"cse-?(\d{4})", name.lower()):
        tags.append(f"course:cse-{m.group(1)}")
        if title := config.COURSE_TITLES.get(m.group(1)):
            tags.append(f"course-title:{title}")
    return tags


def _prune_empty(root: Path) -> None:
    """Delete folders left empty under `root`, deepest first, and `root` itself if it ends up empty."""
    for current, _dirs, _names in os.walk(root, topdown=False):
        with contextlib.suppress(OSError):  # not empty: something was skipped and stays
            Path(current).rmdir()


# --- end private functions ---


def find_projects(root: Path) -> list[CodeProject]:
    """Find every code project under a folder.

    Args:
        root (Path): Folder to scan.

    Returns:
        list[CodeProject]: Projects with at least one source file, in name order.
    """
    if _has_marker(root):
        found = [_project(root.name, root)]
    else:
        found = [_project(root.name, root, recurse=False)]
        found += [
            _project(child.name, child)
            for child in sorted(root.iterdir())
            if child.is_dir() and child.name not in config.CODE_SKIP_DIRS and not child.name.startswith(".")
        ]
    return [project for project in found if project]


def notebook_text(notebook: Path) -> str:
    """Read a Jupyter notebook as plain text, dropping outputs and metadata.

    Args:
        notebook (Path): `.ipynb` file.

    Returns:
        str: Code cells and markdown cells in order, separated by blank lines.
    """
    cells = json.loads(notebook.read_text()).get("cells", [])
    return "\n\n".join(
        "".join(cell.get("source", "")).strip()
        for cell in cells
        if cell.get("cell_type") in ("code", "markdown") and "".join(cell.get("source", "")).strip()
    )


@dataclass(frozen=True)
class ProjectIngest:
    """What ingesting one project did.

    Attributes:
        name (str): Project folder name.
        doc_id (str | None): The project's document; None if every file was already archived.
        added (int): Files copied into the archive.
        duplicates (int): Files already in the archive (same bytes), not added again.
        removed (int): Inbox files deleted after their archive copy was verified.
    """

    name: str
    doc_id: str | None
    added: int
    duplicates: int
    removed: int


def ingest_project(
    conn: sqlite3.Connection, paths: Paths, project: CodeProject, delete_source: bool = True
) -> ProjectIngest:
    """Copy a project's source files into the archive and record them as one document.

    Each file becomes a page of a `project` document, named by its path inside the project.
    A file whose bytes are already archived is not added again. Re-running on the same
    project name adds only new files to the existing document. An inbox file is deleted
    only after its archive copy exists with the same size, and files that were skipped
    (binaries, build output) are never touched.

    Args:
        conn (sqlite3.Connection): Open archive database.
        paths (Paths): Archive locations.
        project (CodeProject): Project from `find_projects`.
        delete_source (bool): Delete archived files from the inbox afterwards.

    Returns:
        ProjectIngest: Counts of what was added, skipped as duplicates, and removed.
    """
    tag = f"project:{project.name}"
    row = conn.execute("SELECT doc_id FROM tags WHERE tag = ?", (tag,)).fetchone()
    doc_id: str | None = row["doc_id"] if row else None
    page_no = (
        conn.execute("SELECT coalesce(max(page_no), 0) FROM pages WHERE doc_id = ?", (doc_id,)).fetchone()[0]
        if doc_id
        else 0
    )
    archived: list[Path] = []
    added = duplicates = 0
    ts = now()
    with conn:
        for src in project.files:
            sha = sha256_file(src)
            raw = paths.raw / f"{sha}{src.suffix.lower()}"
            known = conn.execute("SELECT 1 FROM raw_files WHERE sha256 = ?", (sha,)).fetchone()
            if not raw.exists():
                tmp = raw.with_name(f"{raw.name}.tmp")
                shutil.copyfile(src, tmp)
                tmp.replace(raw)
            if raw.stat().st_size != src.stat().st_size:
                continue  # the copy is not what we read; keep the original and skip it
            archived.append(src)
            if known:
                duplicates += 1
                continue
            if doc_id is None:
                doc_id = str(ULID())
                conn.execute(
                    "INSERT INTO documents (doc_id, title, doc_type, created_at) VALUES (?, ?, 'project', ?)",
                    (doc_id, project.name, ts),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO tags (doc_id, tag) VALUES (?, ?)",
                    [(doc_id, t) for t in _course_tags(project.name)],
                )
            page_no += 1
            page_id = str(ULID())
            conn.execute(
                "INSERT INTO raw_files"
                " (sha256, raw_path, source_filename, scanned_at, page_count, ingested_at)"
                " VALUES (?, ?, ?, ?, 1, ?)",
                (sha, f"raw/{raw.name}", str(src.relative_to(project.root)), scanned_at(src), ts),
            )
            conn.execute(
                "INSERT INTO pages (page_id, raw_sha256, raw_page_no, doc_id, page_no)"
                " VALUES (?, ?, 1, ?, ?)",
                (page_id, sha, doc_id, page_no),
            )
            conn.execute(
                "INSERT OR IGNORE INTO jobs (job_id, target_id, stage, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(ULID()), page_id, config.OCR_STAGE, config.OCR_STAGE_VERSION, ts),
            )
            added += 1
        if added and doc_id:
            # New text means the project description and its index entry are out of date.
            conn.execute(
                "UPDATE jobs SET status = 'pending', updated_at = ? WHERE target_id = ? AND stage IN (?, ?)",
                (ts, doc_id, config.EXTRACT_STAGE, config.INDEX_STAGE),
            )
    removed = 0
    if delete_source:
        for src in archived:
            src.unlink()
            removed += 1
        _prune_empty(project.root)
    return ProjectIngest(project.name, doc_id, added, duplicates, removed)
