from pathlib import Path

from attic.config import find_home, get_paths


def test_find_home_walks_up_to_the_archive(tmp_path: Path) -> None:
    """Running from a subdirectory still finds the one archive above it.

    Args:
        tmp_path (Path): Temporary project root.
    """
    (tmp_path / "archive").mkdir()
    nested = tmp_path / "backend" / "src"
    nested.mkdir(parents=True)

    assert find_home(nested) == tmp_path
    assert find_home(tmp_path) == tmp_path


def test_find_home_falls_back_to_the_start(tmp_path: Path) -> None:
    assert find_home(tmp_path) == tmp_path


def test_explicit_home_wins(tmp_path: Path) -> None:
    (tmp_path / "archive").mkdir()

    paths = get_paths(tmp_path / "elsewhere")

    assert paths.home == (tmp_path / "elsewhere").resolve()
