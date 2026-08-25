# Agentic Autograder

[![Tests](https://github.com/johnswyou/autograder/actions/workflows/tests.yml/badge.svg)](https://github.com/johnswyou/autograder/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

Agentic Autograder grades handwritten physics and math assignments/exams/homework:
it locates answers, transcribes the work, applies a rubric, and creates
reports and a focused review queue.

> **Human oversight is strongly suggested.** Review every queued item, inspect a sample
> of results outside the queue, and approve grades before release.

After setup, a grading run looks like this:

```bash
autograder grade --assignment assignment.pdf --submissions submissions --out runs/first-run
```

Set `OPENROUTER_API_KEY` before a run that may call a model. The default
`openrouter/auto-beta` dynamically routes each agent; use a fixed OpenRouter
slug such as `qwen/qwen3.8-max` when reproducibility matters. Routing requires
zero data retention and denies provider data collection by default. The
explicit `--allow-data-retention` and `--allow-data-collection` flags relax
those protections for institutions whose policy permits it.

Start with `runs/first-run/summary.csv` for class totals,
`runs/first-run/review_queue.md` for items needing attention, and each
student report under `runs/first-run/students/`.

For installation, safe first-run guidance, operator workflows, command and
output reference, and contributor architecture, see the
[documentation index](docs/README.md).

## Test it out

Here is how to conduct a test grading run on a synthetic (i.e., made up) student submission. Here, we use Qwen3.8-Max. It assumes you have [uv](https://docs.astral.sh/uv/getting-started/installation/) installed.

```bash
git clone https://github.com/johnswyou/autograder.git
cd autograder
uv sync
uv run python examples/generate_sample.py
```

`uv sync` creates `.venv/` from the committed `uv.lock`, so you install the same
dependency versions continuous integration does, and it downloads a suitable
Python if your system has none.

Make sure your `OPENROUTER_API_KEY` is exported:

```bash
export OPENROUTER_API_KEY="<YOUR_API_KEY>"
```

Then, run this:

```bash
uv run autograder grade \
--model qwen/qwen3.8-max \
--assignment examples/sample/sample_assignment.pdf \
--submissions examples/sample/submissions \
--out runs/qwen38max-smoke \
--max-workers 1 \
--allow-data-retention \
--allow-data-collection
```

> Use both privacy opt-outs above only for synthetic data. OpenRouter’s current listing shows Qwen3.8-Max is not available through a ZDR (Zero Data Retention) endpoint, so the repo’s privacy defaults would otherwise reject it. For real student data, obtain institutional approval and initially omit `--allow-data-collection`; only add it if permitted and OpenRouter reports no eligible endpoint. See the current model endpoint (https://openrouter.ai/api/v1/models/qwen/qwen3.8-max/endpoints) and ZDR policy (https://openrouter.ai/docs/guides/features/zdr).

For real inputs, replace the paths and always choose a fresh `--out` directory:

```bash
uv run autograder grade \
--model qwen/qwen3.8-max \
--assignment path/to/assignment.pdf \
--submissions path/to/submissions \
--solutions path/to/solutions.pdf \
--rubric path/to/rubric.pdf \
--out runs/qwen38max-real \
--allow-data-retention
```

`--solutions` and `--rubric` are optional; omitting them causes the model to generate them. Add `--reasoning-effort max` only if you also want maximum reasoning effort (it is separate from “Max” in the model name Qwen3.8-Max and may increase cost).