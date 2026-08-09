# Reference

This is the canonical syntax and data reference for Agentic Autograder. Use
[Usage](usage.md) for operating decisions and [Architecture](architecture.md)
for implementation design. All paths and fields below describe the current
command parser and runtime models.

## Command syntax

Exactly one subcommand is required. Options may appear in any order after the
subcommand.

```text
autograder inspect --assignment PATH --out DIR [shared options]
autograder solve   --assignment PATH --out DIR [shared options] [solution options]
autograder rubric  --assignment PATH --out DIR [shared options] [solution options] [rubric options]
autograder grade   --assignment PATH --submissions PATH [PATH ...] --out DIR [shared options] [solution options] [rubric options] [grading options]
```

<!-- cli-subcommands:start -->
| Command | Last stage requested | Required command-specific input | Embedded outcome |
|---|---|---|---|
| `inspect` | Assignment analysis | None beyond the shared assignment and output paths | `AssignmentSpec` |
| `solve` | Solution generation or checking | None; `--solutions` is optional | `SolutionsManual` |
| `rubric` | Rubric generation or checking | None; solution and rubric inputs are optional | `Rubric` |
| `grade` | Submission mapping, transcription, and grading | One or more `--submissions` paths | Returns `list[StudentGrade]` when complete; after writing partial outputs, raises `PartialGradeFailure` whose `grades` and `failures` attributes expose available results |
<!-- cli-subcommands:end -->

Each later command includes every earlier stage. For example, `grade` creates
the assignment spec, solutions, and rubric when compatible saved artifacts do
not already exist. `-h` and `--help` are supplied automatically by `argparse`
for the root parser and each subcommand; they are intentionally excluded from
the public option inventory.

## CLI option inventory

`Parser default` is the value produced by `build_parser()` before conversion to
`RunConfig`; `null` means omitted. `Nargs` is the number of consumed values,
with `+` meaning one or more. `Action` is `store` for a value-consuming option
and `store_true` for a zero-value flag. `Choices` is `any` when the parser does
not restrict the tokens. `all` means every subcommand in the structured
command inventory, while `root` is reserved for root-parser options. There are
currently no public root options. Complete
scope/alias/destination/arity/action/choice/default rows are checked against
the live parser; automatic `argparse` help actions are explicitly excluded.

<!-- cli-options:start -->
| Commands | Options | Destination | Nargs | Action | Choices | Accepted value | Parser default | Effect and interaction |
|---|---|---|---|---|---|---|---|---|
| `all` | `--assignment`, `-a` | `assignment` | `1` | `store` | `any` | Existing supported file or directory path | `required` | Blank assignment. A directory contributes its supported files directly inside it. |
| `all` | `--out`, `-o` | `out` | `1` | `store` | `any` | Directory path | `required` | Run output. It must be disjoint from every input and either new, empty, or compatibly bound. |
| `all` | `--model` | `model` | `1` | `store` | `any` | OpenRouter model slug | `openrouter/auto-beta` | Selects the requested model and binds cached content. Use a fixed slug such as `openai/gpt-5.1` for reproducible or high-stakes runs. |
| `all` | `--api-key` | `api_key` | `1` | `store` | `any` | OpenRouter API key string | `null` | A nonempty option wins; otherwise `OPENROUTER_API_KEY` is read. The key is neither persisted nor part of run-binding identity. |
| `all` | `--max-workers` | `max_workers` | `1` | `store` | `any` | Positive integer | `4` | Maximum concurrent model tasks inside a parallel stage. Students themselves are processed sequentially. |
| `all` | `--max-tokens` | `max_tokens` | `1` | `store` | `any` | Positive integer | `null` | Raises both configured token limits. A value below either built-in limit has no effect. |
| `all` | `--reasoning-effort` | `reasoning_effort` | `1` | `store` | `none, minimal, low, medium, high, xhigh, max` | One listed effort | `null` | Optional reasoning preference. Omission uses the selected model's default. |
| `all` | `--allow-data-retention` | `allow_data_retention` | `0` | `store_true` | `any` | Flag with no value | `false` | Opts out of the default zero-data-retention routing requirement. |
| `all` | `--allow-data-collection` | `allow_data_collection` | `0` | `store_true` | `any` | Flag with no value | `false` | Opts out of the default denial of providers that collect or train on request data. |
| `all` | `--force` | `force` | `0` | `store_true` | `any` | Flag with no value | `false` | Rebuilds requested stages instead of loading saved artifacts. It cannot override a binding mismatch. |
| `all` | `--verbose`, `-v` | `verbose` | `0` | `store_true` | `any` | Flag with no value | `false` | Enables debug logs. Ordinary caught exceptions are re-raised after logging, so a traceback is shown. |
| `solve, rubric, grade` | `--solutions`, `-s` | `solutions` | `1` | `store` | `any` | Supported document or JSON path | `null` | Omission generates solutions. A supplied key is checked for coverage; JSON and documents have different matching behavior. |
| `solve, rubric, grade` | `--strict-solutions` | `strict_solutions` | `0` | `store_true` | `any` | Flag with no value | `false` | Stops on a missing or empty leaf answer instead of generating the gap. Unknown IDs remain warnings. |
| `solve, rubric, grade` | `--verify-provided-solutions` | `verify_provided_solutions` | `0` | `store_true` | `any` | Flag with no value | `false` | Independently evaluates supplied answers while first building the manual. Reused manuals are not rechecked. |
| `rubric, grade` | `--rubric`, `-r` | `rubric` | `1` | `store` | `any` | Supported document or JSON path | `null` | Omission generates a rubric. A supplied rubric is validated, completed unless strict, and normalized to authoritative points. |
| `rubric, grade` | `--rubric-prompt` | `rubric_prompt` | `1` | `store` | `any` | Text string | `null` | Steers generated rubric content and generated gaps. It does not alter fixed point totals or a complete supplied rubric. |
| `rubric, grade` | `--strict-rubric` | `strict_rubric` | `0` | `store_true` | `any` | Flag with no value | `false` | Stops when a leaf has no rubric entry instead of generating the missing entry. It does not waive point consistency. |
| `grade` | `--submissions`, `-S` | `submissions` | `+` | `store` | `any` | One or more file or directory paths | `required` | Discovers students from every supplied path. Argument order and natural file order determine each submission's page order. |
| `grade` | `--review-confidence` | `review_confidence` | `1` | `store` | `any` | Float from 0 through 1 inclusive | `0.6` | Queues a completed grade when grader confidence is strictly below the value. Scores are reused when only this changes. |
| `grade` | `--ocr-threshold` | `ocr_threshold` | `1` | `store` | `any` | Float from 0 through 1 inclusive | `0.5` | Queues nonzero-path work when transcript confidence is strictly below the value. Scores are reused when only this changes. |
<!-- cli-options:end -->

### Material option interactions

- CLI API-key resolution is `--api-key` when it is nonempty, then
  `OPENROUTER_API_KEY`; either value is copied into `RunConfig.api_key`. A
  missing key only produces a warning at startup because a fully cached command
  can finish without a client. If a missing or invalid stage needs a model
  request, lazy client construction reaches OpenRouter only when needed. With
  none available, the request error follows the
  stage's ordinary boundary: the command may stop, persist a per-item failure,
  or isolate one student. Cached failed-entry retries use a narrower guard:
  they require a truthy `RunConfig.api_key` without constructing the client.
  Programmatic callers that rely on SDK environment discovery should therefore
  populate the field explicitly when cached failures must be retried.
- `--max-tokens N` sets `max_tokens = max(32768, N)` and
  `big_max_tokens = max(32768, N)`. Programmatic callers can set the two fields
  independently.
- OpenRouter routing always permits fallbacks and requires parameter support.
  By default it also requires zero-data-retention endpoints and denies data
  collection. The two `--allow-data-*` flags relax only their named privacy
  requirements. One nonempty session ID is reused throughout each agent loop,
  making dynamic model/provider routing sticky for that conversation.
- Prompt caching is automatic at OpenRouter or the selected provider. There is
  no cache-marker or cache-disable option.
- `--verify-provided-solutions` affects only entries whose provenance begins
  `provided`. Generated gaps still follow the normal solver/evaluator loop.
  If an evaluator rejects a supplied answer it becomes unverified. If that
  evaluator task itself fails, the existing trust flag is retained and the
  failure is recorded as an issue.
- Review thresholds are routing settings, not scoring settings. They are
  reapplied whenever saved grades are read, and comparisons use `<`, not `<=`.
- `--force` changes reuse behavior, not the identity of generated content. It
  rebuilds all stages reached by the command and gives up partial-resume
  savings; it does not make changed inputs or settings compatible.

## Accepted input formats and layouts

### Document formats

<!-- document-formats:start -->
| Suffix | Source constants | Kind | Multi-file behavior |
|---|---|---|---|
| `.pdf` | `SUPPORTED_EXTS` | Visual PDF; pages render on demand and an embedded text layer may be read | May be combined with other PDFs and raster images |
| `.png` | `SUPPORTED_EXTS`, `IMAGE_EXTS` | Raster image; one page per file | May be combined with PDFs and other raster images |
| `.jpg`, `.jpeg` | `SUPPORTED_EXTS`, `IMAGE_EXTS` | Raster image; one page per file | May be combined with PDFs and other raster images |
| `.md`, `.markdown` | `SUPPORTED_EXTS`, `TEXT_EXTS` | UTF-8 text chunked into pseudo-pages | Must be the only file in that document |
| `.tex` | `SUPPORTED_EXTS`, `TEXT_EXTS` | UTF-8 LaTeX source chunked into pseudo-pages; it is not compiled | Must be the only file in that document |
| `.json` | Teacher solution and rubric suffix branches | Structured teacher input only | Accepted for `--solutions` and `--rubric`, not as an assignment or submission document |
<!-- document-formats:end -->

An explicit unsupported file raises an ingestion error. When a directory is a
document or discovery root, only supported files directly inside the relevant
directory are selected; other files are ignored. Multi-file documents use
natural filename order, so `page2.jpg` precedes `page10.jpg`. PDF and image
pages may be mixed. A Markdown or LaTeX source cannot be combined with any
second file.

Raster EXIF orientation is applied. By default a source above 40,000,000
pixels is rejected before full decode, and each rendered page or crop is capped
at 3,400,000 pixels. Page numbers and region page fields are one-based. Region
boxes are `[x0, y0, x1, y1]` percentages from the page's top-left corner and
may record a clockwise view rotation of `0`, `90`, `180`, or `270` degrees.

### Assignment and teacher-material layouts

`--assignment` accepts one supported document file or a directory whose direct
supported files form one naturally ordered document. `--solutions` and
`--rubric` accept the same document layouts, or the JSON forms below. Teacher
document content is mapped to leaf problem IDs by a model. Structured JSON is
loaded directly by ID and does not receive document-content matching.

Every input must be separate from `--out`: neither path may equal, contain, or
be contained by the other. This rule applies to the assignment, teacher files,
submission arguments, and each discovered student file.

### Submission discovery

Each `--submissions` argument is expanded independently, then combined:

| Supplied path | Students discovered |
|---|---|
| Supported file | One student; ID is the file stem and the submission contains that file |
| Directory with supported files directly inside | Each direct file is a separate student; ID is its stem |
| Directory with immediate subdirectories | Each nonempty immediate subdirectory is one student; ID is the directory name and its direct supported files are combined |
| Directory containing both direct files and immediate subdirectories | Both rules apply, so direct files and nonempty subdirectories all become students |

Nested directories below the immediate student directory are not traversed.
Empty directories and unsupported files are ignored. Discovery raises an error
only when the combined result across all supplied `--submissions` paths is
empty; an empty directory is harmless when another argument yields a student.
Within a multi-file student submission, files are concatenated in natural
order and the ordered names and contents bind the run.

Student IDs are converted to safe artifact directory slugs by replacing unsafe
character runs with `_`, stripping leading and trailing underscores, then
stripping leading dots, and using `student` if nothing remains. On a collision,
discovery renames the later student ID itself with `_2`, `_3`, and so on until
its slug is unique. That suffixed ID appears in reports and models, and its safe
form names `students/<slug>/`.

## Teacher solution JSON

A JSON key may be a bare problem-ID object or an object with a `solutions`
member. Each value may be a scalar answer or an object. Object entries read
`reasoning` and `final_answer`; `answer` is accepted as a fallback name for
`final_answer`. Values are converted to strings. Other entry fields are
ignored, so fields such as `verified` cannot set trust directly.

| Location | Required | Meaning |
|---|---|---|
| Root object | Yes | Bare problem mapping, or wrapper containing `solutions` |
| `solutions.<problem_id>` | Yes for coverage | Scalar final answer, or entry object |
| `reasoning` | No | Worked reasoning; defaults to an empty string |
| `final_answer` | No at parse time | Final answer; falls back to `answer`, then empty string. Empty answers count as missing during coverage validation |
| `answer` | No | Alias used only when `final_answer` is absent |

This is the canonical object form:

<!-- solution-json-example:start -->
```json
{
  "solutions": {
    "1": {
      "reasoning": "Using v = d / t gives 8 m / 2 s.",
      "final_answer": "4 m/s"
    }
  }
}
```
<!-- solution-json-example:end -->

The shorter `{"1": "4 m/s"}` is also valid. A non-object JSON root behaves
as an empty key. Unknown IDs are ignored with a warning. Supplied JSON entries
start as `verified: true` with `provided` provenance because JSON bypasses
content matching; that flag means accepted teacher input, not independent
mathematical verification. Use `--verify-provided-solutions` when an evaluator
check is required.

The generated `solutions_manual.json` wraps each accepted or generated entry
in a `Solution` with these fields:

| Field | Type and meaning |
|---|---|
| `assignment_title` | String copied from the assignment spec |
| `solutions` | Object keyed by leaf problem ID |
| `problem_id` | Leaf ID repeated inside each solution |
| `reasoning`, `final_answer` | Worked solution and concise answer |
| `method_summary` | Optional short method summary |
| `assumptions` | String list |
| `verified` | Whether this solution and all recorded prerequisite solutions are trusted |
| `unverified_dependencies` | Leaf IDs whose trust blocks this entry |
| `verifier_notes` | Optional rejection, mismatch, dependency, or agent-failure detail |
| `provenance` | `generated`, `provided`, or `provided_unverified` |
| `rounds` | One-based solver/evaluator round number reached for generated drafts. Supplied entries and solver-failure placeholders use the default `0`, so this is not a count of attempted API calls; values can reach `solution_max_rounds + 1` |

Missing answers are generated unless strict mode stops the command. Generated
answers receive one initial solver/evaluator attempt plus up to
`solution_max_rounds` regenerations. Trust failure propagates through declared
problem dependencies.

## Teacher rubric JSON

Rubric JSON is validated as the `Rubric` Pydantic model. Extra fields are
forbidden. Problem and criterion point fields are numeric and nonnegative;
`total_points` is a number or null without its own range constraint, and the
normalized output total is recomputed from accepted problem weights.

<!-- rubric-json-example:start -->
```json
{
  "title": "Minimal rubric",
  "total_points": 1,
  "problems": [
    {
      "problem_id": "1",
      "points": 1,
      "criteria": [
        {
          "id": "1.c1",
          "description": "Gives the correct result with appropriate work.",
          "points": 1
        }
      ],
      "grading_notes": null
    }
  ]
}
```
<!-- rubric-json-example:end -->

| Field | Required | Type and meaning |
|---|---|---|
| `title` | No | String; defaults to empty and is filled from the assignment when needed |
| `total_points` | No | Number or null; normalized output is recomputed from accepted problem weights |
| `problems` | No | Array of rubric-problem objects; defaults to empty |
| `problem_id` | Yes | Assignment leaf ID |
| `points` | Yes | Nonnegative problem weight |
| `criteria` | No | Array of criterion objects; defaults to empty and is replaced by one full-credit criterion when the weight is positive |
| `criteria[].id` | Yes | Criterion ID; whitespace is removed and duplicates are renamed globally |
| `criteria[].description` | Yes | Observable scoring condition |
| `criteria[].points` | Yes | Nonnegative criterion weight |
| `grading_notes` | No | String or null for tolerances, alternative methods, and policy guidance |

Unknown problem entries are warned about and dropped. Missing leaf entries are
generated unless `--strict-rubric` is set. After coverage, the runtime enforces
authoritative point values: explicit printed leaf weights win; with no printed
values or totals each leaf defaults to one point; an ambiguous partial or
aggregate allocation requires a complete teacher rubric. Conflicting leaf,
parent, or assignment totals stop the run. Criterion weights are proportionally
rescaled to the authoritative problem weight, empty positive criteria become
one full-credit criterion, duplicate IDs are renamed, and the total is
recomputed.

## Output tree and artifacts

The complete `grade` tree is shown below. Earlier commands create the prefix
through their requested stage. Files are atomically replaced one at a time,
but the directory is not a multi-file transaction.

```text
OUT/
├── run_binding.json
├── assignment_spec.json
├── solutions_manual.json
├── solutions_manual.md
├── rubric.json
├── rubric.md
├── summary.csv
├── review_queue.md
├── run_manifest.json
└── students/
    └── <safe-student-slug>/
        ├── mapping.json
        ├── transcripts.json
        ├── grades.json
        └── report.md
```

| Artifact | First command that writes it | Purpose and important top-level data |
|---|---|---|
| `run_binding.json` | Every command | Schema version, assignment SHA-256, cache-identity configuration, and incrementally bound teacher/submission input digests |
| `assignment_spec.json` | `inspect` | Assignment metadata and the hierarchical problem tree; every downstream object uses gradable leaf IDs |
| `solutions_manual.json` | `solve` | Typed solution state used for resume and grading |
| `solutions_manual.md` | `solve` | Human-readable worked solutions, provenance, trust, rounds, notes, and dependencies |
| `rubric.json` | `rubric` | Normalized typed scoring contract used for resume and grading |
| `rubric.md` | `rubric` | Human-readable criteria, point weights, and grading notes |
| `students/<slug>/mapping.json` | `grade` | Located work by problem, extra pages, unmatched work, layout notes, and mapper integrity flags |
| `students/<slug>/transcripts.json` | `grade` | Object with a `transcripts` mapping keyed by leaf ID |
| `students/<slug>/grades.json` | `grade` | Student totals, availability, per-problem criterion scores, confidence, review state, and failures |
| `students/<slug>/report.md` | `grade` | Human-readable student evidence and feedback; shows a final score or processed subtotal, never promotes an incomplete subtotal |
| `summary.csv` | `grade` | One row per discovered student, including failures |
| `review_queue.md` | `grade` | Sorted student/problem/reason rows requiring a human decision |
| `run_manifest.json` | Completed command or partial grade | Invocation audit record: versions, status, timestamps, selected configuration, input paths and hashes, roster, issues, and this invocation's API usage |

Unexpected startup or stage errors may occur before `run_manifest.json` can be
written. All JSON and generated Markdown inside `OUT` are pipeline-owned resume
data; do not edit them as teacher inputs.

### Assignment and region data

`assignment_spec.json` contains `title`, optional `course`, optional
`total_points`, `n_pages`, optional `general_instructions`, and `problems`.
Each recursive problem contains:

| Field | Meaning |
|---|---|
| `id`, `label`, `prompt` | Stable hierarchical ID, printed label, and this node's prompt text |
| `type` | `container`, `multiple_choice`, `true_false`, `numeric`, `symbolic`, `short_answer`, `free_response`, `proof`, `derivation`, `diagram`, `sketch_plot`, `table`, `code`, or `other` |
| `points` | Printed number or null |
| `pages` | One-based assignment pages |
| `answer_region` | Optional expected-answer `Region` |
| `figure_refs` | Referenced figure/table `Region` list |
| `depends_on` | Earlier problem IDs used by this node |
| `choices` | Multiple-choice strings or null |
| `answer_format`, `notes` | Optional expected form and notes |
| `children` | Nested problem nodes; only non-container nodes without children are gradable leaves |

A `Region` has a one-based `page`, four-number percentage `bbox`, and clockwise
`rotate`. Coordinates within two percentage points of the page boundary are
clamped; farther-out values are invalid. Reversed corners are reordered.

### Mapping, transcript, and grade data

| Model | Fields |
|---|---|
| `StudentMapping` | `page_count`; `problems` mapping; `extra_pages`; `out_of_order`; `unmatched_work`; `integrity_flags`; optional `overall_notes` |
| `ProblemLocation` | `status`; ordered `regions`; optional `label_seen`; optional `note` |
| `UnmatchedWork` | `region`; `description` |
| `Transcript` | `problem_id`; verbatim `text`; `confidence`; `illegible_spans`; optional `quality_notes`; `integrity_flags`; `processing_status`; optional `failure` |
| `StudentGrade` | `student_id`; nullable `total_awarded`; `total_possible`; `processed_awarded`; `processed_possible`; `score_complete`; OCR mean/min; `problems` mapping; `flags` |
| `ProblemGrade` | `problem_id`; work `status`; nullable `awarded`; `possible`; `criteria`; `feedback`; grader `confidence`; `needs_review`; optional `review_reason`; `integrity_flags`; optional `ocr_confidence`; optional `location_note`; `processing_status`; optional `failure`; saved `intrinsic_review_reasons` |
| `CriterionScore` | `criterion_id`; bounded `awarded`; rubric-derived `possible`; evidence `justification` |
| `ArtifactFailure` | Failing `stage`; `message`; `retryable` metadata flag. Current resume selectors do not consult that flag |

Completed transcripts cannot have failures. Failed transcripts have empty text
and zero confidence. Completed problem grades have an award and no failure;
failed grades have a failure, null award, and no criteria. These invariants are
validated when cached JSON is loaded.

### Summary and manifest fields

`summary.csv` columns are, in order:

```text
student_id,total_awarded,total_possible,percent,<one column per leaf ID>,n_needs_review,ocr_min,flags,run_status,failure
```

Per-student `run_status` is `complete`, `incomplete`, or `failed`. Incomplete
rows retain `total_possible` and any available leaf awards but leave
`total_awarded` and `percent` blank. A whole-student failure row leaves every
score field blank. Text cells that could be spreadsheet formulas are prefixed
with an apostrophe; numeric score cells remain numeric.

`run_manifest.json` records `tool`, `tool_version`, `run_status`, UTC start and
finish times, Python version, model, selected `config`, hashed `inputs`, hashed
`submissions`, `issues`, and `usage`. Its run status is `complete` or
`partial_failure`. Usage counts only the current invocation: API calls, input
tokens, cache-creation input tokens, cache-read input tokens, and output tokens.
The API key is never included.

## Statuses and score availability

### Work statuses

| `WorkStatus` | Meaning | Score and review behavior |
|---|---|---|
| `answered` | Work found where expected | Transcribed and graded; queued only if another trigger applies |
| `answered_elsewhere` | Work found on an extra or unexpected page | Transcribed and graded; queued only if another trigger applies |
| `partial` | Attempt clearly started but incomplete | Transcribed and graded; queued only if another trigger applies |
| `mislabeled` | Content answers this problem despite the student's different label | Transcribed and graded under the content-matched problem; queued only if another trigger applies |
| `blank` | Answer space observed empty and no work found elsewhere | Deterministic zero without a grader call; a clean no-region blank is not queued solely for status |
| `illegible_candidate` | Work exists but the mapper doubts readability | Processing continues and may produce a score; always queued |
| `not_found` | No attributable work located | Deterministic provisional zero without a grader call; always queued for confirmation |
| `mapping_error` | Mapper omitted the problem or claimed work without a valid region | Transcription and grading become failed/unavailable; always queued |

For `blank` or `not_found`, a supplied region records where the mapper looked.
The zero remains, but the result is queued to confirm the space is empty.
Unattributed work adds context to a `not_found` review reason.

### Processing and run statuses

`ProcessingStatus.complete` means a transcript or problem grade is available.
`ProcessingStatus.failed` means the corresponding `ArtifactFailure` is present
and the item is selected for retry on a compatible cached rerun, regardless of
the current `retryable` metadata value. Successful sibling problem results
remain reusable.

A student score is final only when every problem grade is complete:

- `score_complete` is true exactly when `total_awarded` is not null.
- `processed_awarded / processed_possible` is the subtotal of completed
  problems. It is evidence for recovery, not a final grade.
- `total_possible` includes all problem weights even when a problem failed.
- A failed problem has `awarded: null`, not zero. Reports show the subtotal and
  `summary.csv` leaves the final total and percent blank.
- A failure before any `StudentGrade` can be assembled creates a separate
  `StudentFailure` and a `failed` summary row.

Generated solution trust is separate from processing status. A rejected or
dependency-blocked solution can be a completed but `verified: false` result;
dependent nonzero-path grades are queued. A solution-agent failure is saved as
an unverified placeholder and is retryable. A generated answer that exhausted
all evaluator rounds is completed and unverified, not retryable merely because
it was rejected.

## Human review triggers

`needs_review` routes evidence to `review_queue.md`; it does not release,
approve, or normally remove a completed score.

| Trigger | Score behavior |
|---|---|
| Grader explicitly reports uncertainty | Completed rubric score remains |
| Grader confidence is below `review_confidence` | Completed rubric score remains; threshold reason is recalculated on read |
| Transcript confidence is below `ocr_review_threshold` on a non-blank/non-not-found path | Completed rubric score remains; threshold reason is recalculated on read |
| Mapper status is `illegible_candidate` | Processing continues; any completed score remains |
| Official solution is absent or unverified on a graded path | Completed score remains; clean deterministic-zero paths do not consult solution trust |
| Mapper, transcriber, or grader integrity flags exist | Completed score remains; submission-wide mapper flags affect every graded path |
| Grader omits a rubric criterion | Missing criterion is filled with zero, totals are recomputed, and the completed score remains |
| `not_found`, or no-work status with a region | Deterministic zero remains pending human confirmation |
| Mapping, transcription, or grading failure | Problem award is unavailable and the student's final total is withheld |
| Whole-student failure | No `StudentGrade`; summary and queue contain the student-level failure |

Awards outside criterion bounds are clamped and unknown or duplicate criterion
scores are dropped. These repairs do not by themselves add a review reason;
an omitted known criterion does.

## Run binding and cache behavior

`run_binding.json` is created before model work and has strict schema version
`2`:

| Field | Meaning |
|---|---|
| `schema_version` | Exact supported binding schema, currently `2` |
| `assignment_sha256` | Digest of the assignment file or direct supported files in its directory |
| `config` | Exact `RunConfig.cache_identity()` object described below |
| `inputs` | Incrementally recorded digests for `solutions`, `rubric`, `rubric_prompt`, and `submission:<slug>` |

An output path must be a directory. A new binding may be created only in an
empty directory. Existing bindings must have the current schema and exactly
matching assignment and cache-identity configuration. The first value recorded
for each teacher input, prompt, or student slug is permanent: a later mismatch
stops the command and requires a new output directory. Generated teacher
material is recorded as `generated`; an absent rubric prompt is `none`.
Submission digests include ordered filenames and contents.

The binding is incremental, not a complete-roster lock. A later command can
extend an `inspect` run with solution, rubric, or submission bindings. It can
also encounter a new student name, so adding or removing roster members is not
reliably rejected; use a new output directory for any roster change.

Without `--force`, a saved stage is reused only when its file exists and
validates against its Pydantic model. Missing or invalid files are rebuilt and
request the lazy client when they reach model work. Failed per-problem
solution-agent, transcription, and grading placeholders follow the separate
cached-retry guard: a truthy `RunConfig.api_key` retries them, while a falsey
field keeps them flagged. Successful sibling results are retained. Repaired
solutions invalidate dependent rubric and grade artifacts while mappings and
transcripts remain reusable.

Everything that binds the run appears as `yes` in the configuration tables
below. Worker count, API key, review thresholds, force, and
verbosity do not bind content. Threshold changes rederive review flags and
rewrite reports and class files without another grading call. `--force` skips
artifact reuse but is still subject to all binding checks.

## Process exit status

| Status | Condition | Output consequence |
|---|---|---|
| `0` | Help, or requested command completed without incomplete student results | Requested artifacts are available; human review may still be required |
| `1` | Startup, binding, ingestion, model, solution, rubric, or other ordinary error | Error is logged; the manifest may be absent if the pipeline did not finish |
| `2` | Invalid command syntax from `argparse`, or `grade` finished with at least one failed student or incomplete final score | Syntax errors do not run; partial grades still write all available reports, summary rows, queue items, and a `partial_failure` manifest |
| `130` | `KeyboardInterrupt` after parsing | Command reports that an identical invocation can resume from compatible artifacts |

By default ordinary exceptions produce a one-line log message. With
`--verbose`, ordinary caught exceptions are re-raised to show a traceback.
Parser errors happen before logging configuration. `PartialGradeFailure` and
interrupts retain their dedicated exit handling even in verbose mode.

## RunConfig reference

Programmatic callers construct `RunConfig` and pass it to `Pipeline`. The first
table covers fields backed by, or directly derived from, public CLI settings.
The second contains advanced integration controls with no dedicated flag. Every
dataclass field appears exactly once; field presence and defaults are checked
mechanically.

### Public and CLI-derived settings

<!-- runconfig-public:start -->
| Field | Default | Accepted override and relationship | Run binding |
|---|---|---|---|
| `model` | `openrouter/auto-beta` | OpenRouter model slug; `--model` | yes |
| `api_key` | `null` | String or null; CLI adds `OPENROUTER_API_KEY` fallback | no |
| `max_workers` | `4` | Positive integer; `--max-workers` | no |
| `max_tokens` | `32768` | Positive integer; standard agent output limit and one target of `--max-tokens` | yes |
| `big_max_tokens` | `32768` | Positive integer; large submit-payload limit and the other target of `--max-tokens` | yes |
| `review_confidence` | `0.6` | Float from 0 through 1; `--review-confidence` | no |
| `ocr_review_threshold` | `0.5` | Float from 0 through 1; `--ocr-threshold` | no |
| `reasoning_effort` | `null` | null, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`; `--reasoning-effort` | yes |
| `zero_data_retention` | `true` | Boolean; `--allow-data-retention` sets false | yes |
| `allow_data_collection` | `false` | Boolean; `--allow-data-collection` sets true | yes |
| `strict_rubric` | `false` | Boolean; `--strict-rubric` | yes |
| `strict_solutions` | `false` | Boolean; `--strict-solutions` | yes |
| `verify_provided_solutions` | `false` | Boolean; `--verify-provided-solutions` | yes |
| `force` | `false` | Boolean; `--force` | no |
| `verbose` | `false` | Boolean; `--verbose` or `-v` | no |
<!-- runconfig-public:end -->

### Advanced integration settings

<!-- runconfig-advanced:start -->
| Field | Default | Accepted override and relationship | Run binding |
|---|---|---|---|
| `max_agent_turns` | `480` | Positive integer; maximum turns for one agent task | yes |
| `inline_page_cap` | `12` | Positive integer; initial-message page limit | yes |
| `inline_page_edge` | `1100` | Positive integer pixels; normal initial and page-view long edge | yes |
| `detail_page_edge` | `1568` | Positive integer pixels; high-detail page-view long edge | yes |
| `zoom_target_edge` | `1500` | Positive integer pixels; target crop long edge | yes |
| `max_upscale` | `4.0` | Float at least 1; maximum raster-crop enlargement | yes |
| `max_source_pixels` | `40000000` | Positive integer; raster header rejection limit | yes |
| `max_pixels` | `3400000` | Positive integer; rendered-image pixel cap | yes |
| `solution_max_rounds` | `2` | Integer zero or greater; regeneration rounds after the initial solution attempt | yes |
| `max_tool_images` | `20` | Integer zero or greater; a positive value retains that many tool-result images before oldest-first eviction, while zero disables eviction | yes |
<!-- runconfig-advanced:end -->

Construction validates all listed ranges. The implementation does not perform
general runtime type coercion for dataclass callers, so integrations should
pass the stated Python types. `cache_identity()` contains exactly the fields
marked `yes`; it intentionally omits settings that change credentials,
execution mechanics, logging, or review routing without redefining saved
content.

The internal CLI conversion resolves the environment API key, raises both
token fields, maps both privacy opt-outs, and maps `ocr_threshold` to
`ocr_review_threshold`. It only changes strict and
verification booleans when their flags are present. Programmatic construction
does none of those name or inversion conversions.

## Glossary

| Term | Meaning |
|---|---|
| Assignment spec | Structured inventory of the blank assignment in `assignment_spec.json` |
| Artifact | A JSON, Markdown, or CSV file produced in the output directory |
| Cache identity | Configuration values that must match before saved content can belong to the same run |
| Criterion | One independently scored rubric condition |
| Gradable leaf | A non-container problem node with no children; the unit keyed across every downstream stage |
| Intrinsic review reason | Saved concern about the work or processing itself, independent of configurable confidence thresholds |
| Mapping | Per-student association from assignment leaf IDs to work statuses and page regions |
| OCR confidence | Historical field name for model transcription confidence; no standalone OCR engine is used |
| Processed subtotal | Sum over completed problem grades when one or more awards are unavailable; never a final total |
| Region | One-based page plus a percentage bounding box and view rotation |
| Review queue | Human-routing report assembled from unavailable and `needs_review` results |
| Run binding | Persistent guard that prevents one output directory from mixing recorded inputs or content-defining settings |
| Run manifest | Per-invocation audit record written after a complete or partially complete pipeline finish |
| Safe student slug | Filesystem-safe directory form of a discovered student ID |
| Solution trust | `verified` state of an official solution, including prerequisite trust; distinct from mere file coverage |
| Work status | Mapper's conclusion about where or whether a student's answer was found |

Return to the [documentation index](README.md).
