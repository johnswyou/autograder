# Agentic Autograder

[![Tests](https://github.com/johnswyou/autograder/actions/workflows/tests.yml/badge.svg)](https://github.com/johnswyou/autograder/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

Agentic Autograder helps instructors review handwritten physics and math work:
it locates answers, transcribes the work, applies a rubric, and creates
reports and a focused review queue.

> **Human oversight is required.** Review every queued item, inspect a sample
> of results outside the queue, and approve grades before release.

After setup, a grading run looks like this:

```bash
autograder grade --assignment assignment.pdf --submissions submissions --out runs/first-run
```

Set `OPENROUTER_API_KEY` before a run that may call a model. The default
`openrouter/auto-beta` dynamically routes each agent; use a fixed OpenRouter
slug such as `openai/gpt-5.1` when reproducibility matters. Routing requires
zero data retention and denies provider data collection by default. The
explicit `--allow-data-retention` and `--allow-data-collection` flags relax
those protections for institutions whose policy permits it.

Start with `runs/first-run/summary.csv` for class totals,
`runs/first-run/review_queue.md` for items needing attention, and each
student report under `runs/first-run/students/`.

For installation, safe first-run guidance, operator workflows, command and
output reference, and contributor architecture, see the
[documentation index](docs/README.md).
