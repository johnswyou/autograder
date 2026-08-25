"""Offline tests for OpenRouter usage metering, tool-image eviction, and run-input binding."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autograder.config import RunConfig
from autograder.llm import SUBMIT_TOOL_NAME, Usage, UsageMeter, _evict_stale_images, run_agent
from autograder.models import AssignmentSpec, Rubric, SolutionsManual
from autograder.report import write_manifest
from autograder.run_state import RunBindingError, RunState
from autograder.tools import text_block

from .conftest import make_stub_client, tool_use, turn
from .test_llm_models import Tiny, _task

# -- image eviction ---------------------------------------------------------------


def _image_block() -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,eA=="}}


def test_evict_stale_images_unit():
    messages = [
        {"role": "user", "content": [text_block("task"), _image_block()]},
        {"role": "assistant", "content": None},
        {"role": "tool", "tool_call_id": "a", "content": [_image_block(), _image_block()]},
        {"role": "assistant", "content": None},
        {"role": "tool", "tool_call_id": "b", "content": [_image_block(), _image_block()]},
    ]
    _evict_stale_images(messages, max_tool_images=1)
    assert messages[0]["content"][1]["type"] == "image_url"
    imgs = [b["type"] for b in messages[2]["content"]]
    assert imgs == ["text", "text"]  # oldest evicted
    imgs = [b["type"] for b in messages[4]["content"]]
    assert imgs == ["text", "image_url"]  # newest kept


def test_eviction_in_agent_loop(cfg: RunConfig, tiny_pdf: Path):
    from autograder.ingest import Document
    from autograder.tools import ToolKit

    cfg.max_tool_images = 1
    doc = Document.from_path(tiny_pdf, "assignment")
    client = make_stub_client(
        [
            [tool_use("view_page", {"doc": "assignment", "page": 1}, id="t1")],
            [tool_use("view_page", {"doc": "assignment", "page": 2}, id="t2")],
            [tool_use(SUBMIT_TOOL_NAME, {"answer": "done", "score": 1.0})],
        ]
    )
    out = run_agent(
        client,
        cfg,
        _task(
            toolkit=ToolKit({"assignment": doc}, cfg),
            tool_names=("view_page",),
        ),
        None,
    )
    doc.close()
    assert isinstance(out, Tiny)
    final_messages = client.calls[2].messages
    first_tr = final_messages[3]["content"]
    second_tr = final_messages[5]["content"]
    assert all(b.get("type") != "image_url" for b in first_tr)  # evicted
    assert "removed to conserve context" in first_tr[-1]["text"]
    assert any(b.get("type") == "image_url" for b in second_tr)  # newest kept


# -- metering ---------------------------------------------------------------------


def test_usage_meter_counts_cache_tokens(cfg: RunConfig):
    client = make_stub_client(
        [
            turn(
                tool_use(SUBMIT_TOOL_NAME, {"answer": "a", "score": 1}),
                usage=Usage(
                    prompt_tokens=10,
                    completion_tokens=5,
                    reasoning_tokens=2,
                    cached_prompt_tokens=7,
                    cache_write_tokens=3,
                    cost_usd=0.004,
                ),
            )
        ]
    )
    meter = UsageMeter()
    run_agent(client, cfg, _task(), meter)
    snap = meter.snapshot()
    assert snap["cache_write_tokens"] == 3
    assert snap["cached_prompt_tokens"] == 7
    assert snap["prompt_tokens"] == 10 and snap["completion_tokens"] == 5
    assert snap["reasoning_tokens"] == 2 and snap["cost_usd"] == pytest.approx(0.004)


# -- immutable run-input binding ---------------------------------------------------


def test_open_rejects_version_one_dependency_trust_cache(tmp_path: Path):
    output = tmp_path / "output"
    output.mkdir()
    config = RunConfig().cache_identity()
    (output / "run_binding.json").write_text(
        json.dumps(
            {
                "assignment_sha256": "assignment-sha256",
                "config": config,
                "inputs": {},
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RunBindingError, match="fresh --out"):
        RunState.open(output, "assignment-sha256", config)


def test_pipeline_rejects_changed_assignment_before_cache_reuse(
    tmp_path: Path,
    small_spec,
    tiny_pdf,
):
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    save_json(out / "assignment_spec.json", small_spec)
    pipe.assignment.close()

    tiny_pdf.write_bytes(tiny_pdf.read_bytes() + b"changed assignment")

    with pytest.raises(RunBindingError, match="fresh --out"):
        Pipeline(RunConfig(api_key=None), tiny_pdf, out)


def test_directory_hash_covers_exactly_the_files_that_are_ingested(tmp_path: Path):
    """A directory input is fingerprinted by ``sha256_path`` and read by
    ``Document.from_path``. If the two disagree on which files count, an
    untouched run looks changed: the hash moves, the binding check reports a
    different assignment, and a completed run cannot be resumed even though
    every graded page is identical."""
    from PIL import Image

    from autograder.config import sha256_path
    from autograder.ingest import Document

    pages = tmp_path / "pages"
    pages.mkdir()
    for name, shade in (("p1.png", 10), ("p2.png", 20)):
        Image.new("RGB", (40, 40), (shade, shade, shade)).save(pages / name)
    baseline = sha256_path(pages)

    doc = Document.from_path(pages, "assignment")
    ingested = {p.name for p in doc.paths}
    doc.close()
    assert ingested == {"p1.png", "p2.png"}

    # Neither of these is read as assignment content, so neither may move the hash.
    (pages / ".DS_Store").write_bytes(b"editor droppings")
    (pages / "scratch").mkdir()
    (pages / "scratch" / "notes.md").write_text("not part of the assignment", encoding="utf-8")
    assert sha256_path(pages) == baseline

    # A page that *is* read must still move it.
    Image.new("RGB", (40, 40), (99, 99, 99)).save(pages / "p2.png")
    assert sha256_path(pages) != baseline


def test_pipeline_rejects_changed_model_before_cache_reuse(
    tmp_path: Path,
    small_spec,
    tiny_pdf,
):
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key=None, model="model-one"), tiny_pdf, out)
    save_json(out / "assignment_spec.json", small_spec)
    pipe.assignment.close()

    with pytest.raises(RunBindingError, match="fresh --out"):
        Pipeline(RunConfig(api_key=None, model="model-two"), tiny_pdf, out)


def test_force_rebuilds_a_directory_created_without_it(
    tmp_path: Path,
    small_spec,
    tiny_pdf,
    monkeypatch,
):
    """``--force`` selects reuse, not artifact content, so it is not part of the
    binding: adding it to an existing directory must rebuild rather than be
    rejected as a settings change."""
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    cached = small_spec.model_copy(update={"title": "cached"})
    rebuilt = small_spec.model_copy(update={"title": "rebuilt"})
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    save_json(out / "assignment_spec.json", cached)
    pipe.assignment.close()

    forced = Pipeline(RunConfig(api_key=None, force=True), tiny_pdf, out)
    forced._client = object()
    monkeypatch.setattr("autograder.orchestrator.build_spec", lambda *args: rebuilt)

    result = forced.stage_spec()
    forced.assignment.close()

    assert result == rebuilt


def test_dropping_force_resumes_instead_of_being_rejected(
    tmp_path: Path,
    small_spec,
    tiny_pdf,
    monkeypatch,
):
    """The reverse direction: a directory built under ``--force`` stays reusable
    once the flag is dropped, so an interrupted forced run can resume."""
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key=None, force=True), tiny_pdf, out)
    save_json(out / "assignment_spec.json", small_spec)
    pipe.assignment.close()

    resumed = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    monkeypatch.setattr(
        "autograder.orchestrator.build_spec",
        lambda *args: (_ for _ in ()).throw(AssertionError("must reuse the saved spec")),
    )

    result = resumed.stage_spec()
    resumed.assignment.close()

    assert result == small_spec


def test_force_bound_reopen_rebuilds_cached_spec(
    tmp_path: Path,
    small_spec,
    tiny_pdf,
    monkeypatch,
):
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    cached = small_spec.model_copy(update={"title": "cached"})
    rebuilt = small_spec.model_copy(update={"title": "rebuilt"})
    pipe = Pipeline(RunConfig(api_key=None, force=True), tiny_pdf, out)
    save_json(out / "assignment_spec.json", cached)
    pipe.assignment.close()

    resumed = Pipeline(RunConfig(api_key=None, force=True), tiny_pdf, out)
    client = object()
    resumed._client = client
    calls = []

    def rebuild_spec(actual_client, *args):
        calls.append(actual_client)
        return rebuilt

    monkeypatch.setattr("autograder.orchestrator.build_spec", rebuild_spec)

    result = resumed.stage_spec()
    resumed.assignment.close()

    assert result == rebuilt
    assert calls == [client]
    assert AssignmentSpec.model_validate_json((out / "assignment_spec.json").read_text(encoding="utf-8")) == rebuilt


def test_stage_solutions_rejects_changed_key_before_loading_cache(
    tmp_path: Path,
    small_spec,
    tiny_pdf,
):
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    key = tmp_path / "key.md"
    key.write_text("answer: first", encoding="utf-8")
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    save_json(out / "solutions_manual.json", SolutionsManual())
    pipe.stage_solutions(small_spec, key)
    pipe.assignment.close()

    key.write_text("answer: changed", encoding="utf-8")
    resumed = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    client = make_stub_client([])
    resumed._client = client
    with pytest.raises(RunBindingError, match="fresh --out"):
        resumed.stage_solutions(small_spec, key)
    resumed.assignment.close()
    assert client.calls == []


@pytest.mark.parametrize("change", ["rubric", "prompt"])
def test_stage_rubric_rejects_changed_rubric_or_prompt_before_loading_cache(
    tmp_path: Path,
    small_spec,
    tiny_pdf,
    change: str,
):
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    rubric_path = tmp_path / "rubric.md"
    rubric_path.write_text("rubric: first", encoding="utf-8")
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    save_json(out / "rubric.json", Rubric())
    pipe.stage_rubric(small_spec, SolutionsManual(), rubric_path, "first prompt")
    pipe.assignment.close()

    if change == "rubric":
        rubric_path.write_text("rubric: changed", encoding="utf-8")
        steer = "first prompt"
    else:
        steer = "changed prompt"
    resumed = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    client = make_stub_client([])
    resumed._client = client
    with pytest.raises(RunBindingError, match="fresh --out"):
        resumed.stage_rubric(small_spec, SolutionsManual(), rubric_path, steer)
    resumed.assignment.close()
    assert client.calls == []


def test_run_grade_rejects_changed_submission_before_paid_stage(
    tmp_path: Path,
    tiny_pdf,
    monkeypatch,
):
    from autograder.orchestrator import Pipeline, _files_digest

    out = tmp_path / "run"
    submission = tmp_path / "alice.pdf"
    submission.write_bytes(tiny_pdf.read_bytes())
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    pipe.run_state.bind_input("submission:alice", _files_digest([submission]))
    pipe.assignment.close()

    submission.write_bytes(submission.read_bytes() + b"changed submission")
    resumed = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    client = make_stub_client([])
    resumed._client = client
    monkeypatch.setattr(
        resumed,
        "stage_spec",
        lambda: (_ for _ in ()).throw(AssertionError("stage_spec must not run")),
    )
    with pytest.raises(RunBindingError, match="fresh --out"):
        resumed.run_grade([submission], None, None, None)
    resumed.assignment.close()
    assert client.calls == []


def test_force_does_not_override_binding_mismatch(tmp_path: Path, small_spec, tiny_pdf):
    from autograder.config import sha256_path
    from autograder.orchestrator import Pipeline

    out = tmp_path / "run"
    key = tmp_path / "key.md"
    key.write_text("answer: first", encoding="utf-8")
    pipe = Pipeline(RunConfig(api_key=None, force=True), tiny_pdf, out)
    pipe.run_state.bind_input("solutions", sha256_path(key))
    pipe.assignment.close()

    key.write_text("answer: changed", encoding="utf-8")
    resumed = Pipeline(RunConfig(api_key=None, force=True), tiny_pdf, out)
    client = make_stub_client([])
    resumed._client = client
    with pytest.raises(RunBindingError, match="fresh --out"):
        resumed.stage_solutions(small_spec, key)
    resumed.assignment.close()
    assert client.calls == []


def test_identical_inputs_reuse_existing_artifact_without_api_key(
    tmp_path: Path,
    small_spec,
    tiny_pdf,
):
    from autograder.orchestrator import Pipeline
    from autograder.report import save_json

    out = tmp_path / "run"
    pipe = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    save_json(out / "assignment_spec.json", small_spec)
    pipe.assignment.close()

    resumed = Pipeline(RunConfig(api_key=None), tiny_pdf, out)
    assert resumed.stage_spec() == small_spec
    assert resumed._client is None
    resumed.assignment.close()


def _manifest_after(path: Path, provider_sort: str | None) -> dict:
    """Finish one invocation into ``path`` and return the manifest it wrote."""
    write_manifest(
        path,
        RunConfig(provider_sort=provider_sort),
        {},
        [],
        {"api_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
         "reasoning_tokens": 0, "cached_prompt_tokens": 0, "cache_write_tokens": 0,
         "cost_usd": 0.0, "resolved_models": [], "providers": []},
        datetime(2026, 7, 24, tzinfo=timezone.utc),
        [],
        "complete",
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_provider_sort_history_accumulates_every_ranking_a_directory_used(tmp_path: Path):
    path = tmp_path / "run_manifest.json"

    assert _manifest_after(path, None)["provider_sort_history"] == [None]
    assert _manifest_after(path, "throughput")["provider_sort_history"] == [None, "throughput"]

    # A directory that mixes rankings must not present itself as the product of
    # the latest one, which is the whole reason the field accumulates.
    final = _manifest_after(path, "price")
    assert final["provider_sort_history"] == [None, "throughput", "price"]
    assert final["config"]["provider_sort"] == "price"


def test_provider_sort_history_records_a_repeated_ranking_once(tmp_path: Path):
    path = tmp_path / "run_manifest.json"

    for _ in range(3):
        _manifest_after(path, "latency")
    assert _manifest_after(path, "latency")["provider_sort_history"] == ["latency"]


def test_provider_sort_history_survives_a_manifest_without_the_field(tmp_path: Path):
    path = tmp_path / "run_manifest.json"
    path.write_text(json.dumps({"tool": "agentic-autograder"}), encoding="utf-8")

    assert _manifest_after(path, "exacto")["provider_sort_history"] == ["exacto"]


def test_unreadable_manifest_warns_instead_of_failing_a_finished_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    path = tmp_path / "run_manifest.json"
    path.write_text("{ truncated", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="autograder.report"):
        manifest = _manifest_after(path, "price")

    assert manifest["provider_sort_history"] == ["price"]
    assert "could not read the provider-sort history" in caplog.text
