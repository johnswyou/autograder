"""Tests for binding an output directory to one immutable run identity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autograder.config import RunConfig
from autograder.run_state import RunBindingError, RunState, atomic_write_bytes


def test_atomic_write_replaces_complete_old_file(tmp_path: Path) -> None:
    path = tmp_path / "artifact.txt"
    path.write_bytes(b"old artifact")

    atomic_write_bytes(path, b"new complete artifact")

    assert path.read_bytes() == b"new complete artifact"


def _atomic_write_with_replace_failure(
    tmp_path: Path, monkeypatch,
) -> Path:
    import autograder.run_state as run_state

    path = tmp_path / "artifact.txt"
    path.write_bytes(b"old artifact")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replacement failed")

    monkeypatch.setattr(run_state.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        atomic_write_bytes(path, b"new complete artifact")

    return path


def test_atomic_write_replace_failure_preserves_old_file(
    tmp_path: Path, monkeypatch,
) -> None:
    path = _atomic_write_with_replace_failure(tmp_path, monkeypatch)

    assert path.read_bytes() == b"old artifact"


def test_atomic_write_cleans_temporary_file_on_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    _atomic_write_with_replace_failure(tmp_path, monkeypatch)

    assert list(tmp_path.glob(".artifact.*.tmp")) == []


def test_open_creates_binding_in_empty_output(tmp_path: Path) -> None:
    output = tmp_path / "output"
    config = RunConfig().cache_identity()

    state = RunState.open(output, "assignment-sha256", config)

    assert state.output == output.resolve()
    assert json.loads((output / "run_binding.json").read_text()) == {
        "assignment_sha256": "assignment-sha256",
        "config": config,
        "inputs": {},
        "schema_version": 3,
    }


def test_open_reuses_identical_binding(tmp_path: Path) -> None:
    output = tmp_path / "output"
    config = RunConfig().cache_identity()

    first = RunState.open(output, "assignment-sha256", config)
    second = RunState.open(output, "assignment-sha256", config)

    assert second.binding == first.binding


def test_bind_input_persists_first_value_and_rejects_a_different_value(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    state = RunState.open(output, "assignment-sha256", RunConfig().cache_identity())

    state.bind_input("solutions", "first")
    persisted = (output / "run_binding.json").read_text(encoding="utf-8")
    state.bind_input("solutions", "first")

    assert state.binding.inputs == {"solutions": "first"}
    assert (output / "run_binding.json").read_text(encoding="utf-8") == persisted
    with pytest.raises(RunBindingError, match="fresh --out"):
        state.bind_input("solutions", "changed")
    assert state.binding.inputs == {"solutions": "first"}
    assert (output / "run_binding.json").read_text(encoding="utf-8") == persisted


def test_open_rejects_nonempty_legacy_output(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "assignment.json").write_text("legacy cache")

    with pytest.raises(RunBindingError, match="fresh --out"):
        RunState.open(output, "assignment-sha256", RunConfig().cache_identity())


def test_open_rejects_changed_assignment(tmp_path: Path) -> None:
    output = tmp_path / "output"
    config = RunConfig().cache_identity()
    RunState.open(output, "first-assignment-sha256", config)

    with pytest.raises(RunBindingError, match="fresh --out"):
        RunState.open(output, "changed-assignment-sha256", config)


def test_open_rejects_changed_relevant_config(tmp_path: Path) -> None:
    output = tmp_path / "output"
    RunState.open(output, "assignment-sha256", RunConfig().cache_identity())

    with pytest.raises(RunBindingError, match="fresh --out"):
        RunState.open(
            output,
            "assignment-sha256",
            RunConfig(max_tokens=4096).cache_identity(),
        )


def test_open_rejects_json_type_change_in_relevant_config(tmp_path: Path) -> None:
    output = tmp_path / "output"
    config = RunConfig(strict_rubric=True).cache_identity()
    RunState.open(output, "assignment-sha256", config)
    binding_path = output / "run_binding.json"
    persisted = json.loads(binding_path.read_text())
    persisted["config"]["strict_rubric"] = 1
    binding_path.write_text(json.dumps(persisted, sort_keys=True) + "\n")

    with pytest.raises(RunBindingError, match="fresh --out"):
        RunState.open(output, "assignment-sha256", config)


@pytest.mark.parametrize("schema_version", [1, 2])
def test_open_rejects_unsupported_schema_version(tmp_path: Path, schema_version: int) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "run_binding.json").write_text(
        json.dumps(
            {
                "assignment_sha256": "assignment-sha256",
                "config": RunConfig().cache_identity(),
                "schema_version": schema_version,
            }
        )
    )

    with pytest.raises(RunBindingError, match="schema version"):
        RunState.open(output, "assignment-sha256", RunConfig().cache_identity())


def test_open_rejects_unknown_binding_fields(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "run_binding.json").write_text(
        json.dumps(
            {
                "assignment_sha256": "assignment-sha256",
                "config": RunConfig().cache_identity(),
                "schema_version": 3,
                "unexpected": "field",
            }
        )
    )

    with pytest.raises(RunBindingError, match="invalid"):
        RunState.open(output, "assignment-sha256", RunConfig().cache_identity())


def test_binding_mismatch_names_the_settings_that_changed(tmp_path: Path) -> None:
    """Twenty settings share one error, so it must say which one differs."""
    output = tmp_path / "run"
    RunState.open(output, "assignment-sha256", RunConfig().cache_identity())

    with pytest.raises(RunBindingError) as excinfo:
        RunState.open(
            output,
            "assignment-sha256",
            RunConfig(model="other-model", max_tokens=4096).cache_identity(),
        )

    message = str(excinfo.value)
    assert "model: saved 'openrouter/auto-beta', requested 'other-model'" in message
    assert "max_tokens: saved 32768, requested 4096" in message
    assert "reasoning_effort" not in message, "unchanged settings must not be listed"


def test_cache_identity_ignores_operational_flags() -> None:
    baseline = RunConfig()
    operational_change = RunConfig(
        api_key="different-key",
        max_workers=1,
        verbose=True,
        # `force` chooses whether saved results are reused, never what they
        # contain, so it must not bind the output directory.
        force=True,
    )

    assert operational_change.cache_identity() == baseline.cache_identity()
    assert RunConfig(model="different-model").cache_identity() != baseline.cache_identity()
    assert RunConfig(max_tokens=4096).cache_identity() != baseline.cache_identity()
    assert RunConfig(max_source_pixels=39_999_999).cache_identity() != baseline.cache_identity()


def test_directory_bound_by_an_earlier_release_is_rejected_not_reflagged(tmp_path: Path) -> None:
    """A directory written before the thresholds left the binding has grades
    with the old thresholds baked in and no record of why each was flagged.
    Reusing it would silently recompute review marks from nothing, so it must
    be refused — and the message must not read as though the operator passed a
    setting they never touched."""
    import json

    output = tmp_path / "run"
    output.mkdir()
    legacy = dict(RunConfig().cache_identity())
    legacy["review_confidence"] = 0.6
    legacy["ocr_review_threshold"] = 0.5
    (output / "run_binding.json").write_text(
        json.dumps({"schema_version": 3, "assignment_sha256": "abc",
                    "config": legacy, "inputs": {}}),
        encoding="utf-8",
    )

    with pytest.raises(RunBindingError) as excinfo:
        RunState.open(output, "abc", RunConfig().cache_identity())

    message = str(excinfo.value)
    assert "review_confidence: recorded as 0.6 by an earlier release" in message
    assert "requested None" not in message, "the operator never requested None"
    assert "fresh --out" in message


def test_cache_identity_ignores_the_review_thresholds() -> None:
    """The thresholds decide which finished results are flagged, not how any of
    them were produced. Binding the output directory to them would force a full
    re-grade to answer "who would this have flagged at 0.8?", so they are left
    out and re-applied on read instead."""
    baseline = RunConfig()

    assert RunConfig(review_confidence=0.9).cache_identity() == baseline.cache_identity()
    assert RunConfig(ocr_review_threshold=0.9).cache_identity() == baseline.cache_identity()


def test_bind_input_keeps_binding_unchanged_when_atomic_replacement_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    import autograder.run_state as run_state

    output = tmp_path / "output"
    state = RunState.open(output, "assignment-sha256", RunConfig().cache_identity())
    binding_path = output / "run_binding.json"
    persisted = binding_path.read_text(encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replacement failed")

    monkeypatch.setattr(run_state.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        state.bind_input("solutions", "generated")

    assert state.binding.inputs == {}
    assert binding_path.read_text(encoding="utf-8") == persisted
    assert list(output.glob(".run_binding.*.tmp")) == []
