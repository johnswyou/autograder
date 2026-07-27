"""Persistent identity binding for one output directory."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_BINDING_FILENAME = "run_binding.json"
_SCHEMA_VERSION: Literal[2] = 2


class RunBindingError(RuntimeError):
    """Raised when an output directory does not match the requested run."""


def ensure_disjoint_output(output: Path, inputs: list[Path]) -> None:
    resolved_output = Path(output).resolve()
    for raw_input in inputs:
        resolved_input = Path(raw_input).resolve()
        if (
            resolved_output == resolved_input
            or resolved_output in resolved_input.parents
            or resolved_input in resolved_output.parents
        ):
            raise RunBindingError(
                f"output path {resolved_output} overlaps input path "
                f"{resolved_input}; choose a separate --out directory"
            )


class RunBinding(BaseModel):
    """The intentionally narrow cache contract for an output directory."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    assignment_sha256: str
    config: dict[str, object]
    inputs: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class RunState:
    """A resolved output directory and its validated run binding."""

    output: Path
    binding: RunBinding

    @classmethod
    def open(
        cls,
        output: Path,
        assignment_sha256: str,
        config: dict[str, object],
    ) -> RunState:
        """Open ``output`` only when it is bound to this same run identity."""
        resolved_output = Path(output).resolve()
        if resolved_output.exists() and not resolved_output.is_dir():
            raise RunBindingError(
                f"output path {resolved_output} is not a directory; use a fresh --out directory"
            )
        resolved_output.mkdir(parents=True, exist_ok=True)
        binding_path = resolved_output / _BINDING_FILENAME
        expected = RunBinding(
            schema_version=_SCHEMA_VERSION,
            assignment_sha256=assignment_sha256,
            config=config,
        )

        if not binding_path.exists():
            if any(resolved_output.iterdir()):
                raise RunBindingError(
                    "output directory is not empty and has no run binding; "
                    "use a fresh --out directory"
                )
            atomic_write_text(binding_path, _binding_json(expected))
            return cls(output=resolved_output, binding=expected)

        actual = _load_binding(binding_path)
        if actual.assignment_sha256 != expected.assignment_sha256:
            raise RunBindingError(
                "output directory is bound to a different assignment; "
                "use a fresh --out directory"
            )
        if _canonical_json(actual.config) != _canonical_json(expected.config):
            raise RunBindingError(
                "output directory is bound to a different configuration "
                f"({_describe_config_change(actual.config, expected.config)}); "
                "use a fresh --out directory"
            )
        return cls(output=resolved_output, binding=actual)

    def bind_input(self, name: str, digest: str) -> None:
        """Persist the first digest supplied for an input name.

        A resumed run may repeat an identical value, but it can never silently
        replace the value that its artifacts were built against.
        """
        existing = self.binding.inputs.get(name)
        if existing is None:
            updated = self.binding.model_copy(
                update={"inputs": {**self.binding.inputs, name: digest}}
            )
            atomic_write_text(self.output / _BINDING_FILENAME, _binding_json(updated))
            self.binding.inputs[name] = digest
            return
        if existing != digest:
            raise RunBindingError(
                f"output directory is bound to a different {name} input; "
                "use a fresh --out directory"
            )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _describe_config_change(saved: dict[str, object], requested: dict[str, object]) -> str:
    """Name the settings that differ, so the operator does not have to guess.

    The binding records around twenty settings; reporting only that "the
    configuration differs" leaves no way to tell a deliberate model change from
    an accidentally carried-over one.

    A name the current release no longer records is reported differently. It
    means the directory predates a change in which settings bind a run, not
    that the operator asked for a different value, and "requested None" would
    send them looking for a flag they never set.
    """
    differences = []
    for name in sorted(set(saved) | set(requested)):
        if saved.get(name) == requested.get(name):
            continue
        if name not in requested:
            differences.append(
                f"{name}: recorded as {saved[name]!r} by an earlier release "
                "that bound this directory to it"
            )
        else:
            differences.append(
                f"{name}: saved {saved.get(name)!r}, requested {requested[name]!r}"
            )
    return "; ".join(differences) or "settings differ"


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Replace ``path`` only after every byte of ``payload`` is durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace ``path`` with UTF-8 encoded text."""
    atomic_write_bytes(path, text.encode("utf-8"))


def _binding_json(binding: RunBinding) -> str:
    return json.dumps(binding.model_dump(mode="json"), sort_keys=True) + "\n"


def _load_binding(path: Path) -> RunBinding:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunBindingError(
            f"could not read run binding; use a fresh --out directory: {exc}"
        ) from exc

    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        raise RunBindingError(
            "run binding has an unsupported schema version; use a fresh --out directory"
        )
    try:
        return RunBinding.model_validate(raw)
    except ValidationError as exc:
        raise RunBindingError(
            f"run binding is invalid; use a fresh --out directory: {exc}"
        ) from exc
