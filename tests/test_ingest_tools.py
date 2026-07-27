"""Offline tests for document ingestion and the agent toolkit."""

from __future__ import annotations

import math
from pathlib import Path

import pymupdf
import pytest

from autograder.config import safe_eval
from autograder.ingest import Document, IngestError, discover_submissions
from autograder.tools import ToolKit

# -- ingestion ---------------------------------------------------------------


def test_pdf_document(tiny_pdf: Path):
    doc = Document.from_path(tiny_pdf, "assignment")
    assert doc.kind == "pdf"
    assert doc.n_pages == 2
    assert doc.is_visual
    assert "2 + 2" in (doc.page_text(1) or "")

    jpg = doc.render_page(1, max_edge=600)
    assert jpg[:3] == b"\xff\xd8\xff"  # JPEG magic

    crop = doc.render_region(1, [5, 5, 60, 30], target_edge=800)
    assert crop[:3] == b"\xff\xd8\xff"
    doc.close()


def test_pdf_bad_page(tiny_pdf: Path):
    doc = Document.from_path(tiny_pdf, "assignment")
    with pytest.raises(IngestError):
        doc.render_page(99, max_edge=600)
    doc.close()


def test_text_document(tmp_path: Path):
    md = tmp_path / "hw.md"
    md.write_text("# HW\n\n" + ("problem text. " * 600), encoding="utf-8")
    doc = Document.from_path(md, "assignment")
    assert doc.kind == "text"
    assert not doc.is_visual
    assert doc.n_pages >= 2  # chunked
    assert any("problem text" in (doc.page_text(i) or "") for i in range(1, doc.n_pages + 1))
    with pytest.raises(IngestError):
        doc.render_page(1, max_edge=600)
    doc.close()


def test_image_document(tmp_path: Path):
    from PIL import Image

    img_path = tmp_path / "scan.png"
    Image.new("RGB", (1000, 1400), "white").save(img_path)
    doc = Document.from_path(img_path, "submission")
    assert doc.kind == "images"
    assert doc.n_pages == 1
    jpg = doc.render_region(1, [10, 10, 50, 50], target_edge=900)
    assert jpg[:3] == b"\xff\xd8\xff"
    doc.close()


def test_document_rejects_raster_above_source_pixel_limit(tmp_path: Path, monkeypatch):
    """Header dimensions must be rejected before EXIF can decode the image."""
    from PIL import Image

    import autograder.ingest as ingest

    image_path = tmp_path / "oversized.png"
    Image.new("RGB", (4, 5), "white").save(image_path)

    def fail_if_exif_transpose_runs(image: Image.Image) -> Image.Image:
        raise AssertionError("EXIF transpose ran before the source pixel check")

    monkeypatch.setattr(ingest.ImageOps, "exif_transpose", fail_if_exif_transpose_runs)

    with pytest.raises(
        IngestError,
        match=r"submission: oversized\.png has 20 pixels; limit is 19",
    ):
        Document.from_path(image_path, "submission", max_source_pixels=19)


def test_pdf_page_scale_is_bounded_before_get_pixmap(tmp_path: Path, monkeypatch):
    """A PDF page must fit the render limit before MuPDF allocates its pixmap."""
    pdf_path = tmp_path / "assignment.pdf"
    _write_pdf(pdf_path, ["large page"])
    doc = Document.from_path(pdf_path, "assignment")
    requested: list[tuple[float, float, float]] = []
    original_get_pixmap = pymupdf.Page.get_pixmap

    def record_get_pixmap(page, *args, **kwargs):
        matrix = kwargs["matrix"]
        requested.append((page.rect.width, page.rect.height, matrix.a))
        return original_get_pixmap(page, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", record_get_pixmap)

    doc.render_page(1, max_edge=600, max_pixels=1_000)
    doc.close()

    assert requested
    assert all(
        math.ceil(width * scale) * math.ceil(height * scale) <= 1_000
        for width, height, scale in requested
    )


def test_pdf_region_scale_is_bounded_before_get_pixmap(tmp_path: Path, monkeypatch):
    """A PDF crop must fit the render limit before MuPDF allocates its pixmap."""
    pdf_path = tmp_path / "assignment.pdf"
    _write_pdf(pdf_path, ["large page"])
    doc = Document.from_path(pdf_path, "assignment")
    requested: list[tuple[float, float, float]] = []
    original_get_pixmap = pymupdf.Page.get_pixmap

    def record_get_pixmap(page, *args, **kwargs):
        matrix = kwargs["matrix"]
        clip = kwargs["clip"]
        requested.append((clip.width, clip.height, matrix.a))
        return original_get_pixmap(page, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", record_get_pixmap)

    doc.render_region(1, [0, 0, 50, 50], target_edge=600, max_pixels=1_000)
    doc.close()

    assert requested
    assert all(
        math.ceil(width * scale) * math.ceil(height * scale) <= 1_000
        for width, height, scale in requested
    )


def test_raster_page_resize_is_bounded_before_resize(tmp_path: Path, monkeypatch):
    """A raster page must fit the render limit before Pillow allocates a resize."""
    from PIL import Image

    image_path = tmp_path / "assignment.png"
    Image.new("RGB", (1_000, 1_400), "white").save(image_path)
    doc = Document.from_path(image_path, "assignment")
    requested: list[tuple[int, int]] = []
    original_resize = Image.Image.resize

    def record_resize(image, size, *args, **kwargs):
        requested.append(size)
        return original_resize(image, size, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", record_resize)

    doc.render_page(1, max_edge=600, max_pixels=1_000)
    doc.close()

    assert requested
    assert all(width * height <= 1_000 for width, height in requested)


def test_raster_region_resize_is_bounded_before_resize(tmp_path: Path, monkeypatch):
    """A raster crop must fit the render limit before Pillow allocates a resize."""
    from PIL import Image

    image_path = tmp_path / "assignment.png"
    Image.new("RGB", (1_000, 1_400), "white").save(image_path)
    doc = Document.from_path(image_path, "assignment")
    requested: list[tuple[int, int]] = []
    original_resize = Image.Image.resize

    def record_resize(image, size, *args, **kwargs):
        requested.append(size)
        return original_resize(image, size, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", record_resize)

    doc.render_region(1, [0, 0, 100, 100], target_edge=600, max_pixels=1_000)
    doc.close()

    assert requested
    assert all(width * height <= 1_000 for width, height in requested)


_QUAD = {(255, 0, 0): "TL", (0, 200, 0): "TR", (0, 0, 255): "BL", (255, 255, 0): "BR"}


def _quad_doc(tmp_path: Path):
    from PIL import Image

    from autograder.ingest import Document

    img = Image.new("RGB", (400, 400))
    px = img.load()
    for x in range(400):
        for y in range(400):
            if x < 200 and y < 200:
                px[x, y] = (255, 0, 0)      # TL red
            elif x >= 200 and y < 200:
                px[x, y] = (0, 200, 0)      # TR green
            elif x < 200 and y >= 200:
                px[x, y] = (0, 0, 255)      # BL blue
            else:
                px[x, y] = (255, 255, 0)    # BR yellow
    p = tmp_path / "quad.png"
    img.save(p)
    return Document.from_path(p, "assignment")


def _center_quadrant(jpeg_bytes: bytes) -> str:
    import io

    from PIL import Image

    im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    c = im.getpixel((im.width // 2, im.height // 2))
    nearest = min(_QUAD, key=lambda k: sum((a - b) ** 2 for a, b in zip(c, k, strict=True)))
    return _QUAD[nearest]


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_zoom_matches_rotated_view(tmp_path: Path, rotate: int):
    """What the agent sees in view_page(rotate=R) top-left must be what
    zoom([0,0,50,50], rotate=R) returns — same coordinate frame."""
    doc = _quad_doc(tmp_path)
    # Quadrant the agent sees at the TOP-LEFT of the rotated full-page view:
    view = doc.render_page(1, 800, rotate=rotate)
    import io

    from PIL import Image
    v = Image.open(io.BytesIO(view)).convert("RGB")
    seen = _center_quadrant_of(v, v.width // 4, v.height // 4)
    # Quadrant the zoom of that same top-left region actually returns:
    got = _center_quadrant(doc.render_region(1, [0, 0, 50, 50], 800, rotate=rotate))
    doc.close()
    assert got == seen, f"rotate={rotate}: agent saw {seen} at top-left but zoom returned {got}"


def _center_quadrant_of(im, x: int, y: int) -> str:
    c = im.getpixel((x, y))
    nearest = min(_QUAD, key=lambda k: sum((a - b) ** 2 for a, b in zip(c, k, strict=True)))
    return _QUAD[nearest]


def _write_pdf(path: Path, lines: list[str]) -> None:
    import pymupdf

    doc = pymupdf.open()
    for line in lines:
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 100), line, fontsize=12)
    doc.save(path)
    doc.close()


def test_multi_pdf_document(tmp_path: Path):
    """Two scanned PDFs for one student concatenate into one page sequence."""
    _write_pdf(tmp_path / "scan1.pdf", ["first pdf page one", "first pdf page two"])
    _write_pdf(tmp_path / "scan2.pdf", ["second pdf page one"])
    doc = Document.from_paths([tmp_path / "scan1.pdf", tmp_path / "scan2.pdf"], "s")
    assert doc.kind == "pdf" and doc.is_visual
    assert doc.n_pages == 3
    assert "first pdf page two" in (doc.page_text(2) or "")
    assert "second pdf page one" in (doc.page_text(3) or "")   # crosses the file boundary
    for page in (1, 2, 3):
        assert doc.render_page(page, max_edge=400)[:3] == b"\xff\xd8\xff"
    assert doc.render_region(3, [5, 5, 60, 30], target_edge=500)[:3] == b"\xff\xd8\xff"
    doc.close()


def test_mixed_pdf_and_photo_document(tmp_path: Path):
    """A PDF plus a photo of an appended sheet — the README's headline case."""
    from PIL import Image

    _write_pdf(tmp_path / "1_scan.pdf", ["main scan"])
    Image.new("RGB", (800, 1100), "white").save(tmp_path / "2_extra.png")
    doc = Document.from_paths([tmp_path / "1_scan.pdf", tmp_path / "2_extra.png"], "s")
    assert doc.kind == "mixed" and doc.is_visual
    assert doc.n_pages == 2
    assert "main scan" in (doc.page_text(1) or "")
    assert doc.page_text(2) is None                            # photos have no text layer
    assert doc.render_page(2, max_edge=400)[:3] == b"\xff\xd8\xff"
    assert doc.render_region(2, [10, 10, 50, 50], target_edge=400)[:3] == b"\xff\xd8\xff"
    doc.close()


def test_text_cannot_combine(tmp_path: Path):
    _write_pdf(tmp_path / "a.pdf", ["x"])
    (tmp_path / "b.md").write_text("# hw")
    with pytest.raises(IngestError, match="cannot be combined"):
        Document.from_paths([tmp_path / "a.pdf", tmp_path / "b.md"], "s")


def test_concurrent_rendering_is_serialized(tiny_pdf: Path):
    """Parallel agents share one Document; the per-document lock must keep
    concurrent renders correct (MuPDF requires embedder-provided locking)."""
    from concurrent.futures import ThreadPoolExecutor

    doc = Document.from_path(tiny_pdf, "s")

    def work(k: int) -> bytes:
        page = k % 2 + 1
        doc.page_text(page)
        return doc.render_region(page, [5.0, 5.0, 80.0, 60.0], 600)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, range(32)))
    doc.close()
    assert all(r[:3] == b"\xff\xd8\xff" for r in results)
    # same page + same bbox must give identical bytes — corruption would differ
    assert len({results[0], results[2], results[4]}) == 1


def test_discover_submissions(tmp_path: Path):
    # one file per student
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "alice.pdf").write_bytes(b"%PDF-1.4 fake")
    (flat / "bob.pdf").write_bytes(b"%PDF-1.4 fake")
    subs = discover_submissions([flat])
    assert [sid for sid, _ in subs] == ["alice", "bob"]

    # one directory per student with natural-sorted pages
    nested = tmp_path / "nested"
    (nested / "carol").mkdir(parents=True)
    for n in (10, 2, 1):
        (nested / "carol" / f"page{n}.png").write_bytes(b"fake")
    subs = discover_submissions([nested])
    assert subs[0][0] == "carol"
    assert [f.name for f in subs[0][1]] == ["page1.png", "page2.png", "page10.png"]


# -- toolkit -----------------------------------------------------------------


def test_toolkit_dispatch(tiny_pdf: Path, cfg):
    doc = Document.from_path(tiny_pdf, "assignment")
    kit = ToolKit({"assignment": doc}, cfg)

    specs = kit.specs(("view_page", "zoom", "read_text", "compute"))
    assert {s["name"] for s in specs} == {"view_page", "zoom", "read_text", "compute"}

    blocks, err = kit.dispatch("view_page", {"doc": "assignment", "page": 1})
    assert not err
    assert any(b.get("type") == "image" for b in blocks)

    blocks, err = kit.dispatch("zoom", {"doc": "assignment", "page": 1, "bbox": [0, 0, 50, 20]})
    assert not err
    assert any(b.get("type") == "image" for b in blocks)

    blocks, err = kit.dispatch("read_text", {"doc": "assignment", "page": 1})
    assert not err
    assert "2 + 2" in blocks[0]["text"]

    blocks, err = kit.dispatch("compute", {"expression": "sqrt(2) * 2"})
    assert not err
    assert "2.828" in blocks[0]["text"]

    # single-doc toolkits forgive a wrong doc name (fall back to the only doc)
    blocks, err = kit.dispatch("view_page", {"doc": "nope", "page": 1})
    assert not err

    # graceful errors, not exceptions
    kit2 = ToolKit({"assignment": doc, "submission": doc}, cfg)
    blocks, err = kit2.dispatch("view_page", {"doc": "nope", "page": 1})
    assert err and "must be one of" in blocks[0]["text"]
    blocks, err = kit.dispatch("view_page", {"doc": "assignment", "page": 99})
    assert err
    blocks, err = kit.dispatch("compute", {"expression": "__import__('os')"})
    assert err
    doc.close()


def test_safe_eval():
    assert safe_eval("2 + 2") == 4.0
    assert abs(safe_eval("sin(pi/2)") - 1.0) < 1e-12
    for bad in ("__import__('os')", "open('x')", "(1).__class__", "'a'+'b'", "lambda: 1"):
        with pytest.raises(ValueError):
            safe_eval(bad)


def test_safe_eval_resource_guards():
    """Student-controlled expressions must fail fast, never hang the run."""
    import time

    for bomb in ("9**9**9", "factorial(10**8)", "2**513", "factorial(2001)",
                 "comb(10**6, 2)", "(2**512)**512", "10**500 * 10**500 * 10**500 * 10**500"):
        t0 = time.monotonic()
        with pytest.raises(ValueError):
            safe_eval(bomb)
        assert time.monotonic() - t0 < 1.0, f"{bomb} was not rejected quickly"

    # legitimate coursework arithmetic still works
    assert safe_eval("2**10") == 1024.0
    assert safe_eval("factorial(20)") == 2432902008176640000.0
    assert safe_eval("comb(10, 3)") == 120.0
    assert abs(safe_eval("0.5 * 9.81 * 2.3**2") - 25.94745) < 1e-9
    for bad in ("1/0", "True", "sqrt(-1)"):
        with pytest.raises(ValueError):
            safe_eval(bad)


def test_slugify_never_escapes():
    from autograder.config import slugify

    assert slugify("..") == "student"
    assert slugify("...") == "student"
    assert slugify(".") == "student"
    assert slugify(".hidden") == "hidden"
    assert slugify("a/b") == "a_b"
    assert slugify("") == "student"
    for weird in ("..", "...", "../x", "..\\x"):
        s = slugify(weird)
        assert s and not s.startswith(".") and "/" not in s and "\\" not in s


def test_discover_submissions_unique_ids(tmp_path: Path):
    """Colliding ids (raw duplicates or slug collisions) must never share an
    artifact directory — that would grade one student against another's cache."""
    from autograder.config import slugify

    d = tmp_path / "collide"
    d.mkdir()
    for name in ("alice", "alice_2"):
        sd = d / name
        sd.mkdir()
        (sd / "p1.png").write_bytes(b"x")
    (d / "alice.pdf").write_bytes(b"%PDF fake")  # collides with dir 'alice'
    subs = discover_submissions([d])
    slugs = [slugify(sid) for sid, _ in subs]
    assert len(slugs) == 3
    assert len(set(slugs)) == 3, f"duplicate artifact dirs: {slugs}"

    d2 = tmp_path / "slugclash"
    d2.mkdir()
    (d2 / "a b.pdf").write_bytes(b"%PDF fake")
    (d2 / "a_b.pdf").write_bytes(b"%PDF fake")   # same slug as 'a b'
    subs = discover_submissions([d2])
    slugs = [slugify(sid) for sid, _ in subs]
    assert len(set(slugs)) == 2, f"slug collision not resolved: {slugs}"


def test_transcriber_crop_honors_region_rotate(tmp_path: Path, cfg):
    """A mapper region declared in a rotated view's frame must be cropped in that
    same frame by the transcriber (regression for the frame-contract gap)."""
    from autograder.models import ProblemLocation, Region, WorkStatus
    from autograder.ocr import transcribe_problem
    from tests.conftest import make_stub_client, tool_use

    doc = _quad_doc(tmp_path)
    spec_leaf_regions = [Region(page=1, bbox=[0, 0, 50, 50], rotate=90)]
    loc = ProblemLocation(status=WorkStatus.answered, regions=spec_leaf_regions)

    client = make_stub_client([
        [tool_use("submit_result", {"text": "work", "confidence": 0.9})],
    ])
    from autograder.models import AssignmentSpec, Problem, ProblemType
    spec = AssignmentSpec(title="Q", problems=[
        Problem(id="1", label="P1", prompt="p", type=ProblemType.numeric, pages=[1])])
    t = transcribe_problem(client, cfg, spec, doc, spec.find("1"), loc, None)
    assert t.problem_id == "1"

    # the crop embedded in the transcriber's first message must show what the
    # rotate=90 view has at its top-left: the page's bottom-left (blue) quadrant
    import base64
    content = client.calls[0]["messages"][0]["content"]
    imgs = [b for b in content if isinstance(b, dict) and b.get("type") == "image"]
    assert imgs, "no crop was embedded"
    got = _center_quadrant(base64.b64decode(imgs[0]["source"]["data"]))
    doc.close()
    assert got == "BL", f"crop used the wrong frame (got {got}, want BL)"
