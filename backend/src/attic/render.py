"""Turn raw files into page images: PNG + WebP thumbnail + perceptual hash per page.

PDFs are rendered page by page. Images are already pages, so they are only
rotated upright and saved; they never go through a PDF.
"""

from dataclasses import dataclass
from pathlib import Path

import imagehash
import pymupdf
from PIL import Image, ImageOps


@dataclass(frozen=True)
class RenderedPage:
    """Output files and hash for one page.

    All three paths are None for a text-native file (.docx, .doc) and for pages of a
    long textbook PDF that were not rendered, which have text but no image.

    Attributes:
        raw_page_no (int): 1-based page number within the raw file (always 1 for images).
        image_path (str | None): Full-size PNG, relative to `archive/`.
        thumb_path (str | None): WebP thumbnail, relative to `archive/`.
        phash (str | None): Perceptual hash (hex) for near-duplicate detection.
    """

    raw_page_no: int
    image_path: str | None
    thumb_path: str | None
    phash: str | None


# --- start private functions ---


def _save_page(img: Image.Image, sha256: str, page_no: int, archive: Path, thumb_width: int) -> RenderedPage:
    """Save one page image, its thumbnail, and compute its perceptual hash.

    Args:
        img (Image.Image): Upright RGB page image.
        sha256 (str): Hash of the raw file; names the output directories.
        page_no (int): 1-based page number within the raw file.
        archive (Path): Archive root; output goes under `pages/` and `thumbs/`.
        thumb_width (int): Thumbnail width in pixels.

    Returns:
        RenderedPage: Paths and hash for the saved page.
    """
    name = f"p{page_no:03d}"
    page_dir = archive / "pages" / sha256
    thumb_dir = archive / "thumbs" / sha256
    page_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    img.save(page_dir / f"{name}.png", optimize=False)

    thumb = img.copy()
    thumb.thumbnail((thumb_width, thumb_width * 10))
    thumb.save(thumb_dir / f"{name}.webp", quality=80)

    return RenderedPage(
        raw_page_no=page_no,
        image_path=f"pages/{sha256}/{name}.png",
        thumb_path=f"thumbs/{sha256}/{name}.webp",
        phash=str(imagehash.phash(img)),
    )


# --- end private functions ---


def render_pdf(
    pdf: Path, sha256: str, archive: Path, dpi: int, thumb_width: int, max_pages: int | None = None
) -> list[RenderedPage]:
    """Render the pages of a PDF to PNGs and thumbnails.

    Existing output is overwritten, so re-running is safe. Pages past `max_pages`
    are not rendered; they come back with no image, like a text-native file's page.

    Args:
        pdf (Path): PDF to render.
        sha256 (str): Hash of the PDF; names the output directories.
        archive (Path): Archive root; output goes under `pages/` and `thumbs/`.
        dpi (int): Render resolution.
        thumb_width (int): Thumbnail width in pixels.
        max_pages (int | None): Render only this many leading pages; None renders all.

    Returns:
        list[RenderedPage]: One entry per page, in page order.
    """
    rendered = []
    with pymupdf.open(pdf) as doc:
        for n, page in enumerate(doc, start=1):
            if max_pages is not None and n > max_pages:
                rendered.append(RenderedPage(n, None, None, None))
                continue
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            rendered.append(_save_page(img, sha256, n, archive, thumb_width))
    return rendered


def render_image(image: Path, sha256: str, archive: Path, thumb_width: int) -> list[RenderedPage]:
    """Save a photo/image as a single page at its original resolution.

    Phones store rotation as EXIF metadata instead of rotating the pixels, so
    the image is rotated upright first; OCR needs text the right way up.

    Args:
        image (Path): Image to use as the page.
        sha256 (str): Hash of the image; names the output directories.
        archive (Path): Archive root; output goes under `pages/` and `thumbs/`.
        thumb_width (int): Thumbnail width in pixels.

    Returns:
        list[RenderedPage]: A single entry for the one page.
    """
    with Image.open(image) as im:
        upright = ImageOps.exif_transpose(im).convert("RGB")
    return [_save_page(upright, sha256, 1, archive, thumb_width)]
