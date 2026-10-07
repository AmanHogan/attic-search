import json
import sqlite3
from pathlib import Path

import pytest

from attic import extract
from attic.code import find_projects, ingest_project, notebook_text
from attic.config import Paths
from attic.extract import ProjectFacts, run_extract
from attic.text import run_ocr


def write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_subfolders_become_projects_and_loose_files_form_one(tmp_path: Path) -> None:
    write(tmp_path / "loose.c")
    write(tmp_path / "game" / "main.c")
    write(tmp_path / "game" / "notes.txt")
    write(tmp_path / "empty" / "readme.txt")

    projects = {p.name: p for p in find_projects(tmp_path)}

    assert set(projects) == {tmp_path.name, "game"}
    assert [f.name for f in projects["game"].files] == ["main.c"]


def test_marker_makes_one_project_with_nested_folders(tmp_path: Path) -> None:
    write(tmp_path / "Makefile")
    write(tmp_path / "src" / "a.c")
    write(tmp_path / "src" / "b.h")

    [project] = find_projects(tmp_path)

    assert project.name == tmp_path.name
    assert sorted(f.name for f in project.files) == [
        "a.c",
        "b.h",
    ]  # the Makefile marks the project, it is not ingested


def test_dependencies_and_big_files_are_skipped(tmp_path: Path) -> None:
    write(tmp_path / "app" / "main.js")
    write(tmp_path / "app" / "node_modules" / "lib" / "index.js")
    write(tmp_path / "app" / ".venv" / "site.py")
    write(tmp_path / "app" / "bundle.js", "x" * 300_000)

    [project] = find_projects(tmp_path)

    assert [f.name for f in project.files] == ["main.js"]
    assert project.too_large == 1


def test_notebook_text_drops_outputs_and_json(tmp_path: Path) -> None:
    nb = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Title"]},
            {"cell_type": "code", "source": ["x = 1\n", "print(x)"], "outputs": [{"text": "1"}]},
            {"cell_type": "raw", "source": ["ignored"]},
        ]
    }
    path = tmp_path / "a.ipynb"
    path.write_text(json.dumps(nb))

    assert notebook_text(path) == "# Title\n\nx = 1\nprint(x)"


def test_big_notebook_is_kept_because_outputs_are_dropped(tmp_path: Path) -> None:
    write(tmp_path / "ml" / "model.ipynb", "x" * 500_000)

    [project] = find_projects(tmp_path)

    assert [f.name for f in project.files] == ["model.ipynb"] and project.too_large == 0


def make_project(paths: Paths, name: str = "cse-5334") -> Path:
    root = paths.inbox / name
    write(root / "main.c", "int main(void) { return 0; }")
    write(root / "src" / "util.py", "def helper(): pass")
    write(root / "mandel", "binary")  # not source: never archived, never deleted
    return root


def test_ingest_project_copies_records_and_deletes(paths: Paths, conn: sqlite3.Connection) -> None:
    root = make_project(paths)
    [project] = find_projects(root.parent)

    result = ingest_project(conn, paths, project)

    assert (result.added, result.duplicates, result.removed) == (2, 0, 2)
    assert not (root / "main.c").exists() and not (root / "src").exists()
    assert (root / "mandel").exists()  # skipped files are left alone
    doc = conn.execute("SELECT title, doc_type FROM documents").fetchone()
    assert tuple(doc) == ("cse-5334", "project")
    assert sorted(r[0] for r in conn.execute("SELECT tag FROM tags")) == [
        "course-title:data mining",
        "course:cse-5334",
        "project:cse-5334",
    ]
    names = [r[0] for r in conn.execute("SELECT source_filename FROM raw_files ORDER BY source_filename")]
    assert names == ["main.c", "src/util.py"]
    assert all(p.stat().st_size for p in paths.raw.iterdir())


def test_ingest_project_keep_and_duplicates_and_rerun(paths: Paths, conn: sqlite3.Connection) -> None:
    root = make_project(paths)
    [project] = find_projects(root.parent)
    ingest_project(conn, paths, project, delete_source=False)
    assert (root / "main.c").exists()

    again = ingest_project(conn, paths, project, delete_source=False)  # same bytes: nothing new
    assert (again.added, again.duplicates) == (0, 2)

    write(root / "extra.c", "int extra;")
    [project] = find_projects(root.parent)
    third = ingest_project(conn, paths, project, delete_source=False)
    assert third.added == 1
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 3


def test_project_files_are_read_as_text_and_described(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(paths)
    nb = {"cells": [{"cell_type": "code", "source": ["x = 1"], "outputs": [{"text": "NOISE"}]}]}
    write(root / "model.ipynb", json.dumps(nb))
    [project] = find_projects(root.parent)
    ingest_project(conn, paths, project)
    list(run_ocr(conn, paths))
    assert conn.execute("SELECT count(*) FROM pages_fts WHERE pages_fts MATCH 'helper'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM pages_fts WHERE pages_fts MATCH 'NOISE'").fetchone()[0] == 0

    sent: list[str] = []

    def fake(context: str) -> ProjectFacts:
        sent.append(context)
        return ProjectFacts(
            title="Data mining labs", summary="C and Python coursework.", tags=["C", "python"]
        )

    monkeypatch.setattr(extract, "_ask_project", fake)
    [r] = list(run_extract(conn, paths))

    assert r.status == "done" and r.doc_type == "project"
    assert "main.c" in sent[0] and "util.py" in sent[0]
    tags = [t[0] for t in conn.execute("SELECT tag FROM tags ORDER BY tag")]
    assert "course:cse-5334" in tags and "c" in tags and "python" in tags
    assert conn.execute("SELECT title, doc_type FROM documents").fetchone()["title"] == "Data mining labs"
