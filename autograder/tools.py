"""Tools the agents can call, plus helpers to build message content.

The central design idea: agents never receive a fixed, lossy view of a
document. They get an initial set of moderately sized page images and can
then *pull* what they need — re-render a page at high detail, zoom into a
region (vector PDFs re-render at up to ~650 DPI, photos crop + upsample),
rotate sideways phone photos, or read the embedded text layer. A safe
``compute`` calculator is included so numeric verification never relies on
mental arithmetic.
"""

from __future__ import annotations

import base64
from typing import Any

from .config import RunConfig, safe_eval
from .ingest import Document, IngestError

Block = dict[str, Any]


def text_block(t: str) -> Block:
    return {"type": "text", "text": t}


def image_block(jpeg_bytes: bytes) -> Block:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpeg_bytes).decode("ascii"),
        },
    }


def page_blocks(doc: Document, page: int, edge: int, caption: str | None = None) -> list[Block]:
    cap = caption or f"[{doc.label} — page {page} of {doc.n_pages}]"
    if doc.is_visual:
        return [text_block(cap), image_block(doc.render_page(page, edge))]
    return [text_block(cap + "\n" + (doc.page_text(page) or "(empty)"))]


def inline_pages(doc: Document, cap: int, edge: int) -> list[Block]:
    """First-message embedding of up to ``cap`` pages, with a note about the rest."""
    blocks: list[Block] = []
    n = min(doc.n_pages, cap)
    for page in range(1, n + 1):
        blocks.extend(page_blocks(doc, page, edge))
    if doc.n_pages > n:
        blocks.append(
            text_block(
                f"[NOTE: pages {n + 1}-{doc.n_pages} of the {doc.label} are not shown above. "
                f"Use the view_page tool to fetch them — you MUST inspect every page.]"
            )
        )
    return blocks


class ToolKit:
    """Binds one or more documents to the tool schemas + a dispatcher."""

    def __init__(self, docs: dict[str, Document], cfg: RunConfig):
        if not docs:
            raise ValueError("ToolKit needs at least one document")
        self.docs = docs
        self.cfg = cfg

    # -- schemas ---------------------------------------------------------------
    def specs(self, names: tuple[str, ...]) -> list[dict]:
        doc_keys = sorted(self.docs.keys())
        doc_prop = {
            "type": "string",
            "enum": doc_keys,
            "description": f"Which document to operate on: {', '.join(doc_keys)}.",
        }
        rotate_prop = {
            "type": "integer",
            "enum": [0, 90, 180, 270],
            "description": (
                "Clockwise rotation to apply before viewing (for sideways photos). Default 0. "
                "For zoom, give bbox in the coordinates of the rotated view you are reading from "
                "(same rotate value), not the original page."
            ),
        }
        all_specs = {
            "view_page": {
                "name": "view_page",
                "description": (
                    "Render a full page of a document as an image (or return the text of a "
                    "text-based document page). Use detail='high' when the normal view is too "
                    "small to read; use zoom for a specific region."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "doc": doc_prop,
                        "page": {"type": "integer", "description": "1-based page number."},
                        "detail": {"type": "string", "enum": ["normal", "high"],
                                   "description": "Render size. Default 'normal'."},
                        "rotate": rotate_prop,
                    },
                    "required": ["doc", "page"],
                },
            },
            "zoom": {
                "name": "zoom",
                "description": (
                    "Zoom into a rectangular region of a page and get a sharp, enlarged image. "
                    "Essential for small print, subscripts, dense handwriting, and figure details. "
                    "bbox is [x0, y0, x1, y1] in PERCENT of the page (0-100), origin at the TOP-LEFT. "
                    "Example: the bottom-left quadrant is [0, 50, 50, 100]. Zoom into smaller regions "
                    "for more magnification. PDF regions are re-rendered at high DPI (truly sharper); "
                    "photo regions are cropped and enlarged. If you pass rotate, the bbox is read in "
                    "that rotated view's frame (the orientation you see), not the original page."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "doc": doc_prop,
                        "page": {"type": "integer", "description": "1-based page number."},
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                            "description": "[x0, y0, x1, y1] percent coordinates, origin top-left.",
                        },
                        "rotate": rotate_prop,
                    },
                    "required": ["doc", "page", "bbox"],
                },
            },
            "read_text": {
                "name": "read_text",
                "description": (
                    "Return the embedded text layer of a PDF page (exact typeset wording — prefer "
                    "this over reading the image for typed assignment text), or the source text of "
                    "a markdown/LaTeX page. Returns an error for scanned pages/photos with no text layer."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"doc": doc_prop, "page": {"type": "integer"}},
                    "required": ["doc", "page"],
                },
            },
            "compute": {
                "name": "compute",
                "description": (
                    "Safely evaluate an arithmetic expression (math functions: sin, cos, tan, sqrt, "
                    "exp, log, log10, atan2, hypot, factorial, ...; constants pi, e). Use this for "
                    "every nontrivial numeric step instead of mental arithmetic. Example: "
                    "'0.5 * 9.81 * 2.3**2'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                },
            },
        }
        return [all_specs[n] for n in names if n in all_specs]

    # -- dispatch ----------------------------------------------------------------
    def dispatch(self, name: str, args: dict) -> tuple[list[Block], bool]:
        """Execute a tool call. Returns (content_blocks, is_error)."""
        try:
            if name == "compute":
                expr = str(args.get("expression", ""))
                value = safe_eval(expr)
                return [text_block(f"{expr} = {value:.10g}")], False

            doc = self._doc(args)
            page = int(args.get("page", 0))
            rotate = int(args.get("rotate", 0) or 0)
            if rotate not in (0, 90, 180, 270):
                raise IngestError("rotate must be one of 0, 90, 180, 270")

            if name == "read_text":
                txt = doc.page_text(page)
                if txt is None:
                    return [text_block(
                        f"ERROR: {doc.label} page {page} has no text layer (scanned/photo). "
                        "Use view_page or zoom to read it visually."
                    )], True
                return [text_block(f"[{doc.label} page {page} text layer]\n{txt}")], False

            if name == "view_page":
                if not doc.is_visual:
                    txt = doc.page_text(page) or "(empty)"
                    return [text_block(f"[{doc.label} page {page} (text document)]\n{txt}")], False
                detail = args.get("detail", "normal")
                edge = self.cfg.detail_page_edge if detail == "high" else self.cfg.inline_page_edge
                jpg = doc.render_page(page, edge, rotate=rotate, max_pixels=self.cfg.max_pixels)
                return [
                    text_block(f"[{doc.label} — page {page} of {doc.n_pages}, detail={detail}, rotate={rotate}]"),
                    image_block(jpg),
                ], False

            if name == "zoom":
                bbox = args.get("bbox")
                if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                    raise IngestError("bbox must be [x0, y0, x1, y1] in percent")
                if not doc.is_visual:
                    txt = doc.page_text(page) or "(empty)"
                    return [text_block(
                        f"[{doc.label} page {page} is a text document — zoom not applicable; "
                        f"full page text follows]\n{txt}"
                    )], False
                jpg = doc.render_region(
                    page, list(map(float, bbox)), self.cfg.zoom_target_edge,
                    rotate=rotate, max_upscale=self.cfg.max_upscale, max_pixels=self.cfg.max_pixels,
                )
                bb = ", ".join(f"{float(v):.1f}" for v in bbox)
                return [
                    text_block(f"[{doc.label} — page {page}, zoom bbox=[{bb}]%, rotate={rotate}]"),
                    image_block(jpg),
                ], False

            return [text_block(f"ERROR: unknown tool {name!r}")], True
        except (IngestError, ValueError) as exc:
            return [text_block(f"ERROR: {exc}")], True
        except Exception as exc:  # defensive: never crash the agent loop on a tool bug
            return [text_block(f"ERROR: tool {name} failed: {type(exc).__name__}: {exc}")], True

    def _doc(self, args: dict) -> Document:
        key = args.get("doc")
        if key not in self.docs:
            if len(self.docs) == 1:
                return next(iter(self.docs.values()))
            raise IngestError(f"doc must be one of {sorted(self.docs)}; got {key!r}")
        return self.docs[key]
