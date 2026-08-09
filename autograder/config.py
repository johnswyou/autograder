"""Run configuration defaults and small shared utilities."""

from __future__ import annotations

import ast
import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_MODEL = "openrouter/auto-beta"
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)

SUPPORTED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".md", ".markdown", ".tex"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
TEXT_EXTS = {".md", ".markdown", ".tex"}


@dataclass
class RunConfig:
    model: str = DEFAULT_MODEL
    api_key: str | None = None

    # concurrency / agent loop
    max_workers: int = 4
    max_agent_turns: int = 480
    max_tokens: int = 32768
    big_max_tokens: int = 32768       # for agents whose submit payload can be large

    # imaging
    inline_page_cap: int = 12         # pages embedded directly in the first message
    inline_page_edge: int = 1100      # px long edge for inline / "normal" page views
    detail_page_edge: int = 1568      # px long edge for "high" detail page views
    zoom_target_edge: int = 1500      # px long edge for zoomed crops
    max_upscale: float = 4.0          # raster sources: never upscale beyond this
    max_source_pixels: int = 40_000_000  # reject larger raster sources before decode
    max_pixels: int = 3_400_000       # absolute pixel cap per rendered image

    # thresholds
    review_confidence: float = 0.60   # grading confidence below this -> human review
    ocr_review_threshold: float = 0.50  # transcript confidence below this -> human review

    # solutions generation
    solution_max_rounds: int = 2      # generator/evaluator regeneration rounds

    reasoning_effort: ReasoningEffort | None = None
    zero_data_retention: bool = True
    allow_data_collection: bool = False

    # cost controls for the agent loop
    max_tool_images: int = 20         # tool-result images kept per agent before evicting the oldest

    # policies
    strict_rubric: bool = False
    strict_solutions: bool = False
    verify_provided_solutions: bool = False

    force: bool = False
    verbose: bool = False

    def __post_init__(self) -> None:
        self.validate_limits()

    def validate_limits(self) -> None:
        """Range-check the fields the architecture guide invites callers to override.

        The CLI already validates the options it exposes; this covers the rest,
        so a programmatic caller gets an error at construction instead of an
        obscure failure part-way through a paid run.
        """
        positive_ints = (
            "max_workers", "max_agent_turns", "max_tokens", "big_max_tokens",
            "inline_page_cap", "inline_page_edge", "detail_page_edge",
            "zoom_target_edge", "max_source_pixels", "max_pixels",
        )
        for name in positive_ints:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.solution_max_rounds < 0:
            raise ValueError("solution_max_rounds must be zero or greater")
        if self.max_tool_images < 0:
            raise ValueError("max_tool_images must be zero or greater")
        if self.max_upscale < 1.0:
            raise ValueError("max_upscale must be at least 1.0")
        for name in ("review_confidence", "ocr_review_threshold"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.reasoning_effort is not None and self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"reasoning_effort must be one of: {', '.join(REASONING_EFFORTS)}"
            )

    def cache_identity(self) -> dict[str, object]:
        """Return the settings that determine compatible cached artifacts.

        ``force`` is deliberately absent: it selects whether saved results are
        *reused*, never what they *contain*. Including it would make ``--force``
        unusable for its stated purpose, because adding the flag to a directory
        built without it would be rejected as a settings change.

        ``review_confidence`` and ``ocr_review_threshold`` are absent for a
        related reason: they decide which finished results are *flagged*, not
        how any of them were produced. Their effect is recomputed on every read
        by ``grading.apply_review_thresholds``, so a directory graded under one
        pair of thresholds can be re-read under another.
        """
        return {
            "model": self.model,
            "max_agent_turns": self.max_agent_turns,
            "max_tokens": self.max_tokens,
            "big_max_tokens": self.big_max_tokens,
            "inline_page_cap": self.inline_page_cap,
            "inline_page_edge": self.inline_page_edge,
            "detail_page_edge": self.detail_page_edge,
            "zoom_target_edge": self.zoom_target_edge,
            "max_upscale": self.max_upscale,
            "max_source_pixels": self.max_source_pixels,
            "max_pixels": self.max_pixels,
            "solution_max_rounds": self.solution_max_rounds,
            "reasoning_effort": self.reasoning_effort,
            "zero_data_retention": self.zero_data_retention,
            "allow_data_collection": self.allow_data_collection,
            "max_tool_images": self.max_tool_images,
            "strict_rubric": self.strict_rubric,
            "strict_solutions": self.strict_solutions,
            "verify_provided_solutions": self.verify_provided_solutions,
        }


# ---------------------------------------------------------------------------
# Safe calculator used by the `compute` agent tool
# ---------------------------------------------------------------------------

_ALLOWED_FUNCS = {
    name: getattr(math, name)
    for name in (
        "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh",
        "tanh", "sqrt", "exp", "log", "log10", "log2", "fabs", "floor", "ceil",
        "hypot", "degrees", "radians", "factorial", "gamma", "comb", "perm",
        "copysign", "fmod", "pow",
    )
    if hasattr(math, name)
}
_ALLOWED_FUNCS["abs"] = abs
_ALLOWED_FUNCS["round"] = round
_ALLOWED_FUNCS["min"] = min
_ALLOWED_FUNCS["max"] = max
_ALLOWED_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Call, ast.Name,
    ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
    ast.FloorDiv, ast.USub, ast.UAdd,
)

# Resource guards: expressions come (indirectly) from untrusted student work —
# the grader is instructed to punch the student's own arithmetic into compute —
# so a single `9**9**9` or `factorial(10**8)` must not stall the whole run.
_MAX_INT_BITS = 30_000    # intermediate integers beyond ~9000 decimal digits
_MAX_EXPONENT = 512
_INT_ARG_CAPS = {"factorial": 2_000, "comb": 10_000, "perm": 10_000}


def _guard_int(value):
    if isinstance(value, int) and value.bit_length() > _MAX_INT_BITS:
        raise ValueError("result too large")
    return value


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTS:
            return _ALLOWED_CONSTS[node.id]
        raise ValueError(f"unknown name: {node.id}")  # bare function name has no numeric value
    if isinstance(node, ast.UnaryOp):
        v = _eval_node(node.operand)
        return -v if isinstance(node.op, ast.USub) else +v
    if isinstance(node, ast.BinOp):
        left, right = _eval_node(node.left), _eval_node(node.right)
        op = node.op
        if isinstance(op, ast.Pow):
            if abs(right) > _MAX_EXPONENT:
                raise ValueError(f"exponent magnitude above {_MAX_EXPONENT} is not supported")
            if (isinstance(left, int) and isinstance(right, int) and right > 0
                    and abs(left) > 1 and abs(left).bit_length() * right > _MAX_INT_BITS):
                raise ValueError("result too large")
            return _guard_int(left ** right)
        if isinstance(op, ast.Add):
            return _guard_int(left + right)
        if isinstance(op, ast.Sub):
            return _guard_int(left - right)
        if isinstance(op, ast.Mult):
            return _guard_int(left * right)
        if isinstance(op, ast.Div):
            return left / right
        if isinstance(op, ast.Mod):
            return _guard_int(left % right)
        if isinstance(op, ast.FloorDiv):
            return _guard_int(left // right)
        raise ValueError(f"disallowed operator: {type(op).__name__}")
    if isinstance(node, ast.Call):
        args = [_eval_node(a) for a in node.args]
        fname = node.func.id  # pre-walk guarantees a whitelisted ast.Name
        cap = _INT_ARG_CAPS.get(fname)
        if cap is not None:
            for a in args:
                if abs(a) > cap:
                    raise ValueError(f"{fname}() argument above {cap} is not supported")
        return _guard_int(_ALLOWED_FUNCS[fname](*args))
    raise ValueError(f"disallowed construct: {type(node).__name__}")


def safe_eval(expression: str) -> float:
    """Evaluate a pure-arithmetic expression. Raises ValueError on anything else.

    Evaluation is a hand-rolled AST walk (no ``eval``) with magnitude guards, so a
    hostile or careless expression can neither execute code nor hang the process.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"disallowed construct: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                fname = getattr(getattr(node, "func", None), "id", "<expr>")
                raise ValueError(f"disallowed function: {fname}")
            if node.keywords:
                raise ValueError("keyword arguments are not allowed")
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_FUNCS and node.id not in _ALLOWED_CONSTS:
            raise ValueError(f"unknown name: {node.id}")
        if isinstance(node, ast.Constant) and (
                isinstance(node.value, bool) or not isinstance(node.value, (int, float))):
            raise ValueError("only numeric literals are allowed")

    try:
        result = _eval_node(tree)
        if not isinstance(result, (int, float)) or isinstance(result, bool):
            raise ValueError("expression did not evaluate to a number")
        return float(result)
    except ValueError:
        raise
    except (OverflowError, ZeroDivisionError, TypeError, MemoryError) as exc:
        raise ValueError(f"arithmetic error: {exc}") from exc


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

_NAT_RE = re.compile(r"(\d+)")


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in _NAT_RE.split(s)]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_path(path: Path) -> str:
    """SHA-256 of a file, or of a directory's (file name, file hash) pairs.

    Inputs like the assignment may legitimately be a directory of page images;
    hashing must not crash on them (the manifest fingerprints every input).

    A directory is hashed over exactly the files ``Document.from_path`` would
    read from it: its own entries with a supported extension, not nested ones
    and not unsupported ones. The two must agree. Hashing a wider set makes an
    untouched run look changed -- adding a note in a subdirectory, or leaving a
    ``.DS_Store`` behind, would reject the output directory as a different
    assignment even though every graded page is identical.
    """
    p = Path(path)
    if not p.is_dir():
        return sha256_file(p)
    h = hashlib.sha256()
    for f in sorted(p.iterdir(), key=lambda q: q.name):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS:
            h.update(f.name.encode("utf-8"))
            h.update(bytes.fromhex(sha256_file(f)))
    return h.hexdigest()


def short(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def slugify(s: str) -> str:
    """Directory-safe student id. Must never escape its parent: a submission file
    named ``...pdf`` has stem ``..``, which would otherwise resolve
    ``students/..`` to the run root."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")
    s = s.lstrip(".")  # forbids '.', '..', and hidden-file names
    return s or "student"
