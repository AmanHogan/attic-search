import re
import sqlite3
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from attic import config, extract, index
from attic.config import Paths, get_paths
from attic.db import connect, init_db
from attic.extract import DocFacts, run_extract
from attic.index import run_index
from attic.ingest import ingest_inbox
from attic.text import run_ocr


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = get_paths(tmp_path)
    p.ensure()
    return p


@pytest.fixture
def conn(paths: Paths) -> Iterator[sqlite3.Connection]:
    c = connect(paths.db)
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def make_pdf() -> Callable[..., Path]:
    """Factory that writes a simple text-only PDF for tests.

    Returns:
        Callable[..., Path]: `_make(path, pages=1, text="hello")`, which writes
            a PDF with one line of text per page and returns its path.
    """

    def _make(path: Path, pages: int = 1, text: str = "hello") -> Path:
        doc = pymupdf.open()
        for i in range(pages):
            doc.new_page().insert_text((72, 72), f"{text} page {i + 1}", fontsize=11)
        doc.save(path)
        doc.close()
        return path

    return _make


@pytest.fixture
def make_image() -> Callable[..., Path]:
    """Factory that writes a simple image for tests, optionally with EXIF rotation.

    Returns:
        Callable[..., Path]: `_make(path, size=(200, 100), orientation=None,
            text="hello", font_size=10)`, which writes black text on white and
            returns the path. `orientation` is the EXIF orientation tag
            (e.g. 6 = "rotate 90° clockwise to display").
    """

    def _make(
        path: Path,
        size: tuple[int, int] = (200, 100),
        orientation: int | None = None,
        text: str = "hello",
        font_size: int = 10,
    ) -> Path:
        img = Image.new("RGB", size, "white")
        font = ImageFont.load_default(size=font_size)
        ImageDraw.Draw(img).text((20, 20), text, fill="black", font=font)
        exif = Image.Exif()
        if orientation is not None:
            exif[0x0112] = orientation
        img.save(path, exif=exif)
        return path

    return _make


@pytest.fixture
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the embedding model with a deterministic bag-of-words stand-in.

    Each word is hashed into one of 256 slots, so texts that share words get
    similar vectors, which is enough to test ranking without Ollama.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest's patching helper.
    """

    def _embed(texts: list[str], prefix: str) -> np.ndarray:
        vectors = np.zeros((len(texts), 256), dtype=np.float32)
        for i, t in enumerate(texts):
            for word in re.findall(r"\w+", t.lower()):
                vectors[i, zlib.crc32(word.encode()) % 256] += 1
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.where(norms == 0, 1, norms)

    monkeypatch.setattr(index, "embed", _embed)


@pytest.fixture
def build_archive(
    paths: Paths,
    conn: sqlite3.Connection,
    make_pdf: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    fake_embed: None,
) -> Callable[..., None]:
    """Factory that runs documents through the whole pipeline: ingest, OCR, extract, index.

    Returns:
        Callable[..., None]: `_build(docs, with_index=True)`, where `docs` is a list of
            (filename, page text, DocFacts). The fake LLM returns each document's
            DocFacts; pass `with_index=False` to stop after extraction.
    """

    def _build(docs: list[tuple[str, str, DocFacts]], with_index: bool = True) -> None:
        answers = {text: facts for _, text, facts in docs}

        def fake_llm(text: str, scanned_at: str | None) -> DocFacts:
            return next(facts for key, facts in answers.items() if key in text)

        monkeypatch.setattr(extract, "_ask_llm", fake_llm)
        monkeypatch.setattr(extract, "_caption", lambda image: "a test photo")
        for filename, text, _ in docs:
            make_pdf(paths.inbox / filename, text=text)
        ingest_inbox(conn, paths)
        list(run_ocr(conn, paths))
        list(run_extract(conn, paths))
        if with_index:
            list(run_index(conn, paths))

    return _build


@pytest.fixture(autouse=True)
def no_min_image_side(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests use tiny images, so the sweep's small-image rule is off unless a test turns it on.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest's patching helper.
    """
    monkeypatch.setattr(config, "IMAGE_MIN_SIDE", 0)
