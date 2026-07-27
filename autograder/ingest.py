"""Document ingestion.

Everything the system reads — the blank assignment, student submissions,
teacher rubrics, answer keys — is normalized into a :class:`Document`:

* ``pdf``    -> pages rendered on demand with PyMuPDF; vector re-render at
               high DPI makes "zoom" genuinely sharper, and the embedded
               text layer (if any) is exposed via :meth:`page_text`.
* ``images`` -> one page per JPEG/PNG (phone photos). EXIF orientation is
               honored. Zoom crops pixels and upsamples within a cap.
* ``mixed``  -> any combination of PDFs and images (e.g. a scanned PDF plus
               a photo of an appended sheet, or two scanned PDFs); pages are
               concatenated in file order into one page-numbered document.
* ``text``   -> markdown / LaTeX source, chunked into pseudo-pages so the
               same page-oriented tooling works.

All region coordinates are percentages of the page (origin top-left), so
they are resolution independent — the contract that makes the agents'
``zoom`` tool work.
"""

from __future__ import annotations

import io
import math
import threading
from pathlib import Path

import pymupdf
from PIL import Image, ImageOps

from .config import IMAGE_EXTS, SUPPORTED_EXTS, TEXT_EXTS, natural_key, slugify

TEXT_CHUNK_CHARS = 3500
MIN_REGION_PCT = 1.5  # degenerate-region guard


class IngestError(RuntimeError):
    pass


def _encode_jpeg(img: Image.Image, max_bytes: int = 4_400_000) -> bytes:
    if img.mode != "RGB":
        img = img.convert("RGB")
    for quality in (88, 78, 65, 50):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data
    # last resort: shrink
    img = img.resize((max(1, img.width // 2), max(1, img.height // 2)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=60, optimize=True)
    return buf.getvalue()


def _cap_pixels(img: Image.Image, max_pixels: int) -> Image.Image:
    if img.width * img.height <= max_pixels:
        return img
    scale = (max_pixels / (img.width * img.height)) ** 0.5
    return img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.LANCZOS)


def _bounded_scale(
    width: float,
    height: float,
    desired_scale: float,
    max_pixels: int,
) -> float:
    """Largest positive scale no greater than desired_scale whose
    ceiling-rounded dimensions fit max_pixels."""
    def fits(scale: float) -> bool:
        return math.ceil(width * scale) * math.ceil(height * scale) <= max_pixels

    if fits(desired_scale):
        return desired_scale

    low, high = 0.0, desired_scale
    for _ in range(64):
        candidate = (low + high) / 2
        if fits(candidate):
            low = candidate
        else:
            high = candidate
    return low


def _view_bbox_to_page(x0: float, y0: float, x1: float, y1: float,
                       rotate: int) -> tuple[float, float, float, float]:
    """Map a percent bbox given in the rotate-``R`` *view* frame back to the page frame.

    ``view_page(rotate=R)`` shows the page rotated clockwise by R degrees and the agent
    reads coordinates off that view, but :meth:`render_region` crops the *unrotated* page
    — so a bbox picked from the rotated view must be un-rotated first, or the crop targets
    the wrong region. Corners are mapped individually and re-bounded to an axis-aligned box
    (90/270 swap the axes). ``rotate=0`` is the identity.
    """
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    r = rotate % 360
    if r == 90:
        m = [(v, 100.0 - u) for u, v in corners]
    elif r == 180:
        m = [(100.0 - u, 100.0 - v) for u, v in corners]
    elif r == 270:
        m = [(100.0 - v, u) for u, v in corners]
    else:
        m = corners
    xs = [p[0] for p in m]
    ys = [p[1] for p in m]
    return min(xs), min(ys), max(xs), max(ys)


def _chunk_text(source: str) -> list[str]:
    paragraphs = source.replace("\r\n", "\n").split("\n\n")
    chunks: list[str] = []
    cur = ""
    for para in paragraphs:
        candidate = (cur + "\n\n" + para) if cur else para
        if len(candidate) > TEXT_CHUNK_CHARS and cur:
            chunks.append(cur)
            cur = para
        else:
            cur = candidate
        # a single huge paragraph: hard split
        while len(cur) > TEXT_CHUNK_CHARS * 2:
            chunks.append(cur[: TEXT_CHUNK_CHARS * 2])
            cur = cur[TEXT_CHUNK_CHARS * 2 :]
    if cur.strip():
        chunks.append(cur)
    return chunks or [""]


class Document:
    """Page-oriented view over PDFs, images, a text source, or a PDF/image mix.

    A submission may span several files (two scanned PDFs, a PDF plus a photo
    of an appended sheet); their pages are concatenated in file order into one
    consecutively numbered document.

    Thread safety: parallel agents (transcribers, graders, solvers) share one
    Document across worker threads. MuPDF requires the embedding application
    to supply locks for multi-threaded use and PyMuPDF supplies none, so every
    page-backed operation here is serialized on a per-document lock. Rendering
    is milliseconds against the seconds-scale API calls it feeds, so the lock
    costs effectively nothing.
    """

    def __init__(
        self,
        kind: str,
        label: str,
        paths: list[Path],
        *,
        max_source_pixels: int = 40_000_000,
    ):
        self.kind = kind            # 'pdf' | 'images' | 'mixed' | 'text'
        self.label = label          # 'assignment' / 'submission' / ...
        self.paths = paths
        self._lock = threading.RLock()
        self._pdfs: list[pymupdf.Document] = []
        self._images: list[Image.Image] = []
        self._chunks: list[str] = []
        # visual page table: ('pdf', pdf_index, local_page) | ('img', image_index, 0)
        self._pages: list[tuple[str, int, int]] = []

        if kind == "text":
            source = paths[0].read_text(encoding="utf-8", errors="replace")
            self._chunks = _chunk_text(source)
            return
        if kind not in ("pdf", "images", "mixed"):
            raise IngestError(f"unknown document kind: {kind}")
        for p in paths:
            ext = p.suffix.lower()
            if ext == ".pdf":
                pdf = pymupdf.open(p)
                if pdf.page_count == 0:
                    pdf.close()
                    raise IngestError(f"{p}: PDF has no pages")
                idx = len(self._pdfs)
                self._pdfs.append(pdf)
                self._pages.extend(("pdf", idx, i) for i in range(pdf.page_count))
            elif ext in IMAGE_EXTS:
                img = Image.open(p)
                pixels = img.width * img.height
                if pixels > max_source_pixels:
                    img.close()
                    raise IngestError(
                        f"{label}: {p.name} has {pixels} pixels; limit is {max_source_pixels}"
                    )
                oriented = ImageOps.exif_transpose(img) or img
                self._images.append(oriented.convert("RGB"))
                self._pages.append(("img", len(self._images) - 1, 0))
            else:
                raise IngestError(f"{label}: {p.name} cannot be part of a visual document")
        if not self._pages:
            raise IngestError(f"{label}: no pages")

    # -- construction --------------------------------------------------------
    @classmethod
    def from_paths(
        cls,
        paths: list[Path],
        label: str,
        *,
        max_source_pixels: int = 40_000_000,
    ) -> Document:
        paths = sorted([Path(p) for p in paths], key=lambda p: natural_key(p.name))
        if not paths:
            raise IngestError(f"{label}: no input files")
        for p in paths:
            if not p.exists():
                raise IngestError(f"{label}: file not found: {p}")
            if p.suffix.lower() not in SUPPORTED_EXTS:
                raise IngestError(f"{label}: unsupported file type: {p} (supported: pdf, jpg, png, md, tex)")
        exts = {p.suffix.lower() for p in paths}
        if len(paths) == 1:
            ext = paths[0].suffix.lower()
            if ext == ".pdf":
                return cls("pdf", label, paths, max_source_pixels=max_source_pixels)
            if ext in TEXT_EXTS:
                return cls("text", label, paths, max_source_pixels=max_source_pixels)
            return cls("images", label, paths, max_source_pixels=max_source_pixels)
        if exts & TEXT_EXTS:
            raise IngestError(
                f"{label}: markdown/LaTeX sources cannot be combined with other files: "
                f"{[p.name for p in paths]}. Provide a single md/tex file."
            )
        if exts <= IMAGE_EXTS:
            return cls("images", label, paths, max_source_pixels=max_source_pixels)
        if exts == {".pdf"}:
            return cls("pdf", label, paths, max_source_pixels=max_source_pixels)
        # PDFs + images, pages in natural file order
        return cls("mixed", label, paths, max_source_pixels=max_source_pixels)

    @classmethod
    def from_path(
        cls,
        path: Path | str,
        label: str,
        *,
        max_source_pixels: int = 40_000_000,
    ) -> Document:
        path = Path(path)
        if path.is_dir():
            files = [p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]
            return cls.from_paths(files, label, max_source_pixels=max_source_pixels)
        return cls.from_paths([path], label, max_source_pixels=max_source_pixels)

    # -- basics ---------------------------------------------------------------
    @property
    def n_pages(self) -> int:
        return len(self._chunks) if self.kind == "text" else len(self._pages)

    @property
    def is_visual(self) -> bool:
        return self.kind != "text"

    def describe(self) -> str:
        names = ", ".join(p.name for p in self.paths[:4]) + ("…" if len(self.paths) > 4 else "")
        return f"{self.kind} document ({self.n_pages} page(s)) from {names}"

    def _check_page(self, page: int) -> int:
        if not isinstance(page, int) or page < 1 or page > self.n_pages:
            raise IngestError(f"page {page} out of range (1..{self.n_pages})")
        return page - 1

    # -- text -----------------------------------------------------------------
    def page_text(self, page: int) -> str | None:
        i = self._check_page(page)
        if self.kind == "text":
            return self._chunks[i]
        ref = self._pages[i]
        if ref[0] != "pdf":
            return None
        with self._lock:
            txt = self._pdfs[ref[1]][ref[2]].get_text("text").strip()
        return txt or None

    # -- rendering --------------------------------------------------------------
    def render_page(self, page: int, max_edge: int, rotate: int = 0, max_pixels: int = 3_400_000) -> bytes:
        """Render a full page to JPEG bytes with the long edge ~= max_edge px."""
        i = self._check_page(page)
        if self.kind == "text":
            raise IngestError("text documents have no visual pages; use read_text")
        ref = self._pages[i]
        with self._lock:
            if ref[0] == "pdf":
                p = self._pdfs[ref[1]][ref[2]]
                long_pts = max(p.rect.width, p.rect.height) or 1.0
                z = max(0.2, min(9.0, max_edge / long_pts))
                z = _bounded_scale(p.rect.width, p.rect.height, z, max_pixels)
                pix = p.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            else:
                img = self._images[ref[1]]
                long_px = max(img.size)
                desired_scale = max_edge / long_px if long_px > max_edge else 1.0
                scale = _bounded_scale(img.width, img.height, desired_scale, max_pixels)
                if abs(desired_scale - 1.0) > 0.05 or scale < desired_scale:
                    img = img.resize(
                        (math.ceil(img.width * scale), math.ceil(img.height * scale)),
                        Image.Resampling.LANCZOS,
                    )
            if rotate:
                img = img.rotate(-rotate, expand=True)
            img = _cap_pixels(img, max_pixels)
            return _encode_jpeg(img)

    def render_region(
        self,
        page: int,
        bbox_pct: list[float],
        target_edge: int,
        rotate: int = 0,
        max_upscale: float = 4.0,
        max_pixels: int = 3_400_000,
    ) -> bytes:
        """Render a page region ("zoom"). bbox is [x0,y0,x1,y1] in percent."""
        i = self._check_page(page)
        if self.kind == "text":
            raise IngestError("text documents cannot be zoomed; use read_text")
        x0, y0, x1, y1 = [max(0.0, min(100.0, float(v))) for v in bbox_pct]
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        # The bbox is expressed in the frame the caller is viewing (rotate applied);
        # un-rotate it into page coordinates before clipping, then rotate the crop.
        if rotate:
            x0, y0, x1, y1 = _view_bbox_to_page(x0, y0, x1, y1, rotate)
        if x1 - x0 < MIN_REGION_PCT:
            pad = (MIN_REGION_PCT - (x1 - x0)) / 2
            x0, x1 = max(0.0, x0 - pad), min(100.0, x1 + pad)
        if y1 - y0 < MIN_REGION_PCT:
            pad = (MIN_REGION_PCT - (y1 - y0)) / 2
            y0, y1 = max(0.0, y0 - pad), min(100.0, y1 + pad)

        ref = self._pages[i]
        with self._lock:
            if ref[0] == "pdf":
                p = self._pdfs[ref[1]][ref[2]]
                r = p.rect
                clip = pymupdf.Rect(
                    r.x0 + r.width * x0 / 100.0,
                    r.y0 + r.height * y0 / 100.0,
                    r.x0 + r.width * x1 / 100.0,
                    r.y0 + r.height * y1 / 100.0,
                )
                long_pts = max(clip.width, clip.height) or 1.0
                z = max(0.3, min(9.0, target_edge / long_pts))  # 9x of 72dpi = 648 dpi
                z = _bounded_scale(clip.width, clip.height, z, max_pixels)
                pix = p.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            else:
                base = self._images[ref[1]]
                px = (
                    int(base.width * x0 / 100.0),
                    int(base.height * y0 / 100.0),
                    max(int(base.width * x0 / 100.0) + 4, int(base.width * x1 / 100.0)),
                    max(int(base.height * y0 / 100.0) + 4, int(base.height * y1 / 100.0)),
                )
                img = base.crop(px)
                long_px = max(img.size) or 1
                desired_scale = min(target_edge / long_px, max_upscale)
                scale = _bounded_scale(img.width, img.height, desired_scale, max_pixels)
                if abs(desired_scale - 1.0) > 0.05 or scale < desired_scale:
                    img = img.resize(
                        (math.ceil(img.width * scale), math.ceil(img.height * scale)),
                        Image.Resampling.LANCZOS,
                    )
            if rotate:
                img = img.rotate(-rotate, expand=True)
            img = _cap_pixels(img, max_pixels)
            return _encode_jpeg(img)

    def close(self) -> None:
        with self._lock:
            for pdf in self._pdfs:
                pdf.close()
            self._pdfs = []
            self._images = []
            self._pages = []


# ---------------------------------------------------------------------------
# Submission discovery
# ---------------------------------------------------------------------------


def discover_submissions(paths: list[Path]) -> list[tuple[str, list[Path]]]:
    """Resolve CLI submission arguments into (student_id, files) pairs.

    * a file            -> one student (id = file stem)
    * a directory of subdirectories -> each subdirectory is a student
    * a directory of files          -> each file is a student
    """
    out: list[tuple[str, list[Path]]] = []
    seen: dict[str, int] = {}  # keyed by slug: ids that differ only in slug-hostile
                               # characters ('a b' vs 'a_b') share one artifact
                               # directory, so they must be deduplicated too

    def add(sid: str, files: list[Path]) -> None:
        sid = sid or "student"
        key = slugify(sid)
        if key in seen:
            n = seen[key]
            while True:
                n += 1
                candidate = f"{sid}_{n}"
                if slugify(candidate) not in seen:
                    break
            seen[key] = n
            sid, key = candidate, slugify(candidate)
        seen.setdefault(key, 1)
        out.append((sid, files))

    for raw in paths:
        p = Path(raw)
        if p.is_file():
            add(p.stem, [p])
            continue
        if not p.is_dir():
            raise IngestError(f"submission path not found: {p}")
        subdirs = sorted([d for d in p.iterdir() if d.is_dir()], key=lambda d: natural_key(d.name))
        files = sorted(
            [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS],
            key=lambda f: natural_key(f.name),
        )
        if subdirs:
            for d in subdirs:
                dfiles = sorted(
                    [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS],
                    key=lambda f: natural_key(f.name),
                )
                if dfiles:
                    add(d.name, dfiles)
            for f in files:
                add(f.stem, [f])
        else:
            for f in files:
                add(f.stem, [f])
    if not out:
        raise IngestError("no submissions found in the given path(s)")
    return out
