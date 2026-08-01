# Reader-first documentation rewrite implementation plan

**Goal:** Replace the two documentation monoliths with a progressive documentation set that gives a new user a correct mental model, a complete first-run path, task-oriented operating guidance, an exhaustive reference, and an execution-first contributor architecture guide.

**Scope:** Documentation, documentation tests, and package documentation metadata only. Do not change grading behavior.

## Global constraints

- The primary reader has not read the source code and has no prior mental model of the repository.
- A reader who completes the beginner path must be able to grade their own assignment, inspect the outputs, understand required human review, and find every user-specifiable CLI option and advanced `RunConfig` setting.
- Keep `docs/usage.md` and `docs/architecture.md` as stable paths, but replace their contents.
- Add `docs/README.md`, `docs/getting-started.md`, `docs/how-it-works.md`, and `docs/reference.md`.
- Give each fact one canonical home. Summaries elsewhere must link to that home rather than duplicating detailed rules.
- Lead with concrete behavior. Introduce terminology only after the reader has seen the pipeline or task it describes.
- State the actual visual path directly: PyMuPDF renders PDF pages or selected regions to JPEG; the JPEG is base64-encoded in an Anthropic image content block; Claude interprets and transcribes the image. There is no separate OCR engine such as Tesseract.
- Clearly separate Claude's judgments, Python's deterministic enforcement, and the instructor's required review.
- Preserve all current safety and correctness contracts, including privacy/API disclosure, model cost, generated-solution verification, rubric point allocation, blank versus not-found behavior, unavailable scores, review triggers, resumability/invalidation, prompt-injection treatment, and report escaping.
- Use one consistent synthetic assignment and output path in examples.
- Keep the first-run path linear. Move optional modes, rare edge cases, and exhaustive tables into usage/reference material.
- Diagrams are limited to relationships that prose does not explain as clearly: the end-to-end pipeline, one answer's lifecycle, and resume/invalidation behavior.
- Replace brittle exact-prose tests with tests of maintained navigation and mechanically checkable contracts. Do not lock incidental wording or heading order.
- Do not make live Anthropic calls during verification. The sample is synthetic and typeset; say explicitly that it is not a handwriting benchmark.

## Canonical document ownership

- `README.md`: value, intended use, human-oversight warning, shortest example, and routing.
- `docs/README.md`: documentation index and audience-specific reading paths.
- `docs/getting-started.md`: installation through first reviewed result.
- `docs/how-it-works.md`: concrete grading pipeline and responsibility boundaries.
- `docs/usage.md`: task-oriented operator workflows and decision guidance.
- `docs/reference.md`: exhaustive commands, options, defaults, formats, outputs, statuses, and configuration.
- `docs/architecture.md`: contributor implementation guide and module ownership.

## Task 1: Establish the documentation structure and maintainable test contract

**Files:**

- Modify: `tests/test_documentation.py`
- Modify: `README.md`
- Modify: `pyproject.toml`
- Create: `docs/README.md`
- Create: `docs/getting-started.md`
- Create: `docs/how-it-works.md`
- Create: `docs/reference.md`

**Work:**

1. Add focused tests for the required maintained-document set, portable relative navigation, valid local Markdown links/anchors, balanced code fences, and recognized Mermaid diagram declarations.
2. Run the focused test and capture the expected RED result before creating the missing files or links.
3. Remove exact-sentence, banned-word, and fixed-heading-order assertions that lock prose rather than user-visible contracts. Preserve implementation-level tests that do not depend on the documentation rewrite.
4. Create concise document shells with accurate audience, prerequisites, outcome, and cross-navigation statements so the structural tests pass. Do not pre-write later task content.
5. Rewrite the root README as a short entry point: value, oversight requirement, shortest sample command, key outputs, and routes to the documentation index.
6. Make `docs/README.md` the explicit index for new users, returning users, and contributors.
7. Change the `Documentation` project URL in `pyproject.toml` to `docs/README.md`.
8. Run the focused documentation tests, then the full offline suite.

**Acceptance:** The new information architecture exists, navigation is valid, the root README no longer tries to be the entire manual, and the test suite does not require incidental prose.

## Task 2: Write the beginner path and concrete system explanation

**Files:**

- Replace: `docs/getting-started.md`
- Replace: `docs/how-it-works.md`
- Modify if needed: `docs/README.md`, `README.md`, `tests/test_documentation.py`

**Work:**

1. Write a linear getting-started guide covering installation, API key configuration, privacy/cost warning, synthetic sample generation, `inspect`, checking `assignment_spec.json`, `grade`, locating outputs, reviewing results, and substituting real assignment paths.
2. Follow every command with what happened, which files appeared, and what success looks like.
3. State that grading the sample performs paid Anthropic API calls and that the sample tests document ingestion/mapping rather than handwriting quality.
4. Write the 30-second system explanation first in `how-it-works.md`, then follow one handwritten answer through ingestion, full-page mapping, optional crop/rotation, transcription, rubric grading, deterministic validation/aggregation, persistence, and human review.
5. Explain exact implementation responsibility: Claude handles visual interpretation, mapping, transcription, and rubric-based judgment; Python handles validation, limits, arithmetic/tool safety, caching, aggregation, output escaping, and report generation; the instructor owns final approval.
6. Explain why mapping and transcription are separate, what cropping improves and cannot recover, and why no standalone OCR step exists.
7. Explain clean blank, not found, unavailable/failed, low-confidence, and review-required outcomes without burying the distinctions in edge-case prose.
8. Verify every shown command against `autograder --help` and the relevant subcommand help. Run the sample generator into a temporary directory and confirm its documented output paths; do not invoke the live model.

**Acceptance:** A newcomer can accurately explain how the repository makes Claude understand a PDF page and can reach the point immediately before a live grading call without reading source code.

## Task 3: Rewrite the task-oriented usage guide

**Files:**

- Replace: `docs/usage.md`
- Modify if needed: `docs/README.md`, `docs/getting-started.md`, `tests/test_documentation.py`

**Work:**

1. Organize the guide around operator jobs: plan the run, prepare inputs, inspect the assignment, choose solutions, choose a rubric, grade submissions, review results, resume safely, change inputs/configuration, manage cost/concurrency, protect student data, and troubleshoot.
2. Use decision tables for supplied versus generated solutions, whether verification is needed, when a rubric is required, output-directory reuse, and zero versus unavailable/review-required results.
3. Explain solution coverage validation versus independent correctness verification, prerequisite verification propagation, and the human-review consequence of unverified solutions.
4. Explain rubric point-source precedence, strict-rubric scope, criterion rescaling, and conflicts that stop a run.
5. Explain submission roster binding, multi-file submissions, supported standalone text documents, raster input limits, and stable student identity expectations.
6. Explain resumability, compatible extension of a run, cache-relevant changes, generated-file integrity boundaries, `--force`, and the need for a new output directory when required.
7. Explain per-problem versus whole-student failures, processed subtotals versus final totals, review queue behavior, and safe release workflow.
8. Keep exhaustive option/default tables out of this guide; link each workflow to the relevant reference section.

**Acceptance:** An operator can choose the correct workflow and understand its consequences without reading architecture internals or scanning an option catalog.

## Task 4: Build the exhaustive syntax and data reference

**Files:**

- Replace: `docs/reference.md`
- Modify: `tests/test_documentation.py`
- Modify if needed: `docs/README.md`, `docs/usage.md`

**Work:**

1. Document syntax for `inspect`, `solve`, `rubric`, and `grade`.
2. Document every long and short CLI option, command scope, accepted value/range, default, environment fallback, and consequential interaction. Derive the inventory from `autograder.cli.build_parser()` rather than the old docs.
3. Add a maintainable test that introspects the parser and checks that every public option is represented in the reference's structured option tables; exclude argparse's automatic help option explicitly.
4. Test documented defaults that can be checked mechanically against parser actions and `RunConfig`, without asserting prose.
5. Document accepted assignment/submission/solution/rubric formats and layouts, using the implementation constants as the source of truth.
6. Document the generated output tree, important JSON/CSV/Markdown files, score-availability semantics, statuses, review triggers, run binding/cache behavior, and command exit behavior.
7. Document structured solution and rubric JSON shapes with valid minimal examples.
8. Document advanced programmatic `RunConfig` fields, defaults, limits, and relationships not exposed directly by the CLI.
9. Add a compact glossary for terms used across the guides.

**Acceptance:** Every user-specifiable command argument and advanced configuration value has one findable canonical entry, and parser drift causes a focused documentation test failure.

## Task 5: Rewrite architecture around the actual runtime

**Files:**

- Replace: `docs/architecture.md`
- Modify if needed: `docs/how-it-works.md`, `docs/README.md`, `tests/test_documentation.py`

**Work:**

1. Begin with the CLI-to-`Pipeline` call path and an end-to-end stage diagram.
2. Trace the exact visual implementation through `Document.render_page`/`render_region`, JPEG creation, `image_block`, Anthropic message content, the shared agent loop, and structured Pydantic results.
3. Explain each stage in runtime order: assignment structure, solutions, rubric, student mapping, transcription, grading, aggregation/reporting.
4. Identify deterministic/model/human boundaries and the invariants checked at each boundary.
5. Explain typed models and status semantics, document/tool interfaces, prompt/tool image limits, retry/degradation behavior, per-student concurrency, and usage metering.
6. Explain persistence layout, atomic writes, cache identity, dependency invalidation, conditional reuse, run binding, and compatible command extension.
7. Explain security boundaries: student text as untrusted data, prompt-injection signaling, numeric-only calculation, Markdown escaping, CSV formula neutralization, and control-character rendering.
8. Provide a module ownership map and one supported programmatic integration example using the public pipeline entry point and explicit API-key handling.
9. Close with testing strategy and concise design rationale for mapping before transcription, structured outputs, and human review.

**Acceptance:** A contributor can trace one answer from input bytes to persisted grade, locate the owning module for every stage, and understand which guarantees are deterministic.

## Task 6: Integrate, deduplicate, and validate the full reader experience

**Files:**

- Review and modify as needed: `README.md`, `docs/README.md`, `docs/getting-started.md`, `docs/how-it-works.md`, `docs/usage.md`, `docs/reference.md`, `docs/architecture.md`, `tests/test_documentation.py`, `pyproject.toml`

**Work:**

1. Audit every contract from the former `usage.md`, `architecture.md`, root README, source configuration, parser, and existing documentation tests. Assign each retained fact a canonical destination and remove accidental duplication.
2. Check the complete new-user path for unexplained terms, forward references, missing prerequisites, and commands whose result is not described.
3. Check the returning-user path for findability of commands, flags, defaults, formats, outputs, statuses, failure behavior, and resume rules.
4. Check the contributor path for accurate source names and links, especially the PDF-to-JPEG-to-Anthropic image-message path.
5. Validate local links, anchors, fences, Mermaid declarations, CLI option coverage, parser/default consistency, sample generation, and all offline tests.
6. Confirm the following newcomer questions are answerable without source reading: value; what `grade` does; how Claude sees/transcribes PDFs; Claude/Python/human responsibilities; required files; output locations; zero versus unavailable; output reuse; all flags/defaults; and what data is sent externally.
7. Confirm no grading implementation file changed.

**Verification commands:**

```bash
.venv/bin/python -m pytest tests/test_documentation.py -q
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check tests/test_documentation.py
git diff --check
```

If Ruff is not installed in the worktree environment, install the repository-pinned version `ruff==0.16.0` before the final lint command.

**Acceptance:** The documentation is internally consistent, mechanically verifiable where appropriate, accurate to the implementation, and usable as both a beginner path and an experienced-user reference.
