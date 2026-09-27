import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw

from attic.config import Paths, get_paths
from attic.db import connect, init_db


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
            doc.new_page().insert_text((72, 72), f"{text} page {i + 1}", fontsize=24)
        doc.save(path)
        doc.close()
        return path

    return _make


@pytest.fixture
def make_image() -> Callable[..., Path]:
    """Factory that writes a simple image for tests, optionally with EXIF rotation.

    Returns:
        Callable[..., Path]: `_make(path, size=(200, 100), orientation=None)`, which
            writes an image and returns its path. `orientation` is the EXIF
            orientation tag (e.g. 6 = "rotate 90° clockwise to display").
    """

    def _make(path: Path, size: tuple[int, int] = (200, 100), orientation: int | None = None) -> Path:
        img = Image.new("RGB", size, "white")
        ImageDraw.Draw(img).text((10, 10), "hello", fill="black")
        exif = Image.Exif()
        if orientation is not None:
            exif[0x0112] = orientation
        img.save(path, exif=exif)
        return path

    return _make
