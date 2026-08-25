# Usage

Use this guide when you understand the basic pipeline and need to make a real
grading decision. If the stages are unfamiliar, read [How it works](how-it-works.md)
first. For [exact command syntax](reference.md#command-syntax),
[accepted JSON shapes](reference.md#teacher-solution-json), defaults, and
artifact fields, use the Reference.

The autograder produces recommendations and review evidence. It does not
release grades. You remain responsible for the final decision.

## Plan the run before spending money

**Choose the assignment, roster, grading policy, model settings, and output
directory before the first command.** The output directory is bound to most of
those choices; changing one later usually means starting a new directory.

Use one of four commands according to how far you are ready to proceed:

- `inspect` saves the assignment structure only. Start here for a real
  assignment so a bad problem inventory does not flow into paid downstream
  work.
- `solve` also creates or checks the solutions manual. Use it when solutions
  need instructor approval before rubric work.
- `rubric` also creates or checks scoring criteria. Use it when the rubric
  needs approval before student data is processed.
- `grade` runs every required stage and requires submissions. It can extend a
  compatible earlier run in the same output directory.

For example, the synthetic assignment used in [Getting started](getting-started.md)
can be inspected and then extended to a full run:

```bash
autograder inspect \
    --assignment examples/sample/sample_assignment.pdf \
    --out runs/sample-demo

autograder grade \
    --assignment examples/sample/sample_assignment.pdf \
    --submissions examples/sample/submissions \
    --out runs/sample-demo
```

The second command reuses the saved assignment structure because its input and
settings match. A completed repeat can reuse every requested result without an
API key or another model call.

Keep `--out` separate from every input. It must not equal, contain, or be
contained by the assignment, solutions, rubric, or any submission path. In
particular, never put it inside the submissions directory. An existing
nonempty directory without a supported `run_binding.json` cannot be adopted.

Commands that make model calls send content through OpenRouter to the selected
provider and incur API charges.
Confirm the data policy and cost boundary before using real student work; see
[Protect student data at the point of use](#protect-student-data-at-the-point-of-use).

## Prepare inputs and preserve student identity

**Arrange files so each discovered submission corresponds to exactly one
student.** The filename or directory name becomes the student ID; the program
does not infer identity from page contents. Changing those names later changes
the roster and requires a new output directory.

The assignment, solutions, rubric, and submissions accept PDF, PNG, JPEG,
Markdown, and LaTeX sources as applicable. A blank assignment may be one file
or a directory of supported files. Use the copy that contains the questions
and printed point values, not an answer key or a completed submission.

Submission discovery follows these rules:

- A supplied file is one student, identified by the file stem.
- Supported files directly inside a supplied directory are separate students.
- Each immediate subdirectory is one student; its supported files are combined
  into a multi-file submission.
- If a directory contains both supported files and student subdirectories,
  both kinds become students. Remove stray files before running.
- Files for one student are combined in natural filename order, so `page2.jpg`
  precedes `page10.jpg`. PDFs and images may be mixed.
- A Markdown or LaTeX submission must be that student's only file. Convert it
  to PDF before combining it with another file.

IDs that would map to the same safe output folder receive suffixes such as
`_2`. Avoid relying on that recovery rule: make roster names unique and stable
before grading, and compare the discovered names printed by the command with
your authoritative roster.

Each standalone raster source is limited to 40,000,000 pixels and is rejected
from its header before full decode or EXIF handling. Resize an oversized photo
before grading. EXIF orientation is honored for accepted images. The separate
3,400,000-pixel cap applies to each page view or crop sent to a model; zoom can
expose existing detail but cannot recover detail absent from a blurry scan.

## Inspect the assignment before grading

**Run `inspect`, then approve `assignment_spec.json` before creating solutions
or grading students.** Every later artifact is keyed to the lowest-level
problem IDs found here, so an incorrect hierarchy can attach solutions,
criteria, work, and scores to the wrong problem.

`inspect` rejects an obviously incomplete inventory before saving it: a spec
leaving more than a third of the document's pages with no problem on them, or
whose printed leaf values cannot be reconciled with the printed total, is
returned to the model with the specific gap named, and the command fails if the
model cannot close it. Page coverage is not checked for Markdown and LaTeX
sources, whose pages are chunk boundaries rather than printed pages. The check
catches abandonment, not subtle omissions, so it does not replace your reading
of the spec.

```bash
autograder inspect \
    --assignment examples/sample/sample_assignment.pdf \
    --out runs/sample-demo
```

Check that every gradable problem and subproblem appears once, the hierarchy
and prompts are correct, expected answer areas and figures are sensible,
dependencies reflect phrases such as “using part (a),” and every printed point
value was read correctly. If the structure is wrong, improve the source and
start with a new output directory. Do not edit `assignment_spec.json` and
continue: generated files are pipeline-owned resume data, not teacher inputs.

## Choose and approve solutions

**Decide whether the source of truth is your key or a generated manual, and
whether supplied answers need an independent check.** Coverage validation and
correctness verification are different operations.

| Decision | What the autograder does | Consequence before grading |
|---|---|---|
| Omit `--solutions` | Generates each answer, then asks a separate evaluator to re-derive and check it. A rejected answer can be regenerated, for up to three solver/evaluator attempts with the standard configuration. | Review `solutions_manual.md`. Any answer still unverified sends grades that depend on it to human review. |
| Supply a document or JSON key without independent verification | Matches entries to assignment problems, checks coverage and nonempty answers, and for document keys checks that the requested quantity and given values appear to match. | A matched supplied entry can be marked `verified` without its mathematics having been independently checked. Treat that flag as coverage trust, review the key yourself, and do not describe it as a correctness check. |
| Supply a key with `--verify-provided-solutions` | Performs the same matching and asks independent evaluator agents to check the supplied entries. | A rejected answer becomes unverified and sends dependent grades to review. A successfully checked answer still depends on verified prerequisites. |
| Supply an incomplete key | Generates missing or empty entries through the normal solver/evaluator process and records the coverage gap. Unknown IDs are ignored with a warning. | Instructor and generated entries can coexist. Use `--strict-solutions` if a gap must stop the run instead. |

For a supplied document, content matching is model-assisted: a stale entry that
answers a different quantity or uses different given values is marked
unverified. JSON keys are loaded by problem ID and do not receive that document
matching pass. In either form, inspect the resulting manual rather than
assuming input structure proves correctness. See the
[teacher solution JSON reference](reference.md#teacher-solution-json) for the
accepted forms and fields.

Solution trust propagates through prerequisites. An evaluator may accept part
(b)'s work, but if part (a) is an unverified prerequisite, part (b) is also
saved as unverified. Solvers see verified prerequisite results as official and
unverified drafts only as advisory context. Grades that consult either
unverified solution are queued for a person.

There is one important failure boundary for supplied-answer verification. If
an evaluator rejects an answer, the answer becomes unverified. If the evaluator
itself fails, the supplied entry keeps its previous matching-based trust level;
that infrastructure failure alone does not mark it unverified or queue
dependent grades. The saved manual has no separate “check unavailable” state,
and reuse does not retry that check. The command prints the count and
`run_manifest.json` records the issue. Review those answers manually, or fix the
cause and repeat with a new output directory.

Always read `solutions_manual.md`, including provenance, verifier notes, and
unverified prerequisite lists, before approving a rubric or grades.

## Choose and approve the rubric

**Resolve the point source first, then decide whether missing rubric content
may be generated.** The program divides a printed total evenly when the
assignment prices a question but not its parts; it never overrides a printed
value, and it never reconciles two printed values that disagree.

| What the blank assignment establishes | Required action | Consequence |
|---|---|---|
| Every lowest-level problem has a printed value | You may omit `--rubric` or provide one with matching per-problem weights. | Printed leaf values are authoritative. A conflicting supplied weight stops the run. |
| No point values and no assignment or parent total appear anywhere | You may omit `--rubric`. | Each lowest-level problem receives 1 point. |
| A parent or assignment total is printed but its lowest-level problems are not | You may omit `--rubric`; supply one to set the weights yourself. | The total is split evenly among the parts it covers, and the split is logged before solutions are generated. A supplied weight takes precedence. |
| Lowest-level problems fall outside every printed total, or their enclosing total is already spent | You may omit `--rubric`; supply one to weight them yourself. | Each receives 1 point, and any printed total spanning them is no longer checked, because it demonstrably does not cover them. |
| A complete point allocation conflicts with a printed leaf, parent, or assignment total | Correct the assignment source or teacher rubric. | The run stops; neither generated criteria nor strict mode overrides the conflict. |

If you provide no rubric, the program generates criteria from the assignment
and solutions. A rubric document is matched to problems by content; rubric JSON
is checked directly. `--rubric-prompt` steers generated criteria and generated
gaps—for example, toward method credit—but never changes fixed point totals and
has no effect when a supplied rubric is already complete.

`--strict-rubric` has a narrow scope: it stops when any lowest-level problem is
missing an entry. Without it, missing entries are generated and marked
`[auto-generated]`. Strict mode does not make conflicting point totals valid,
and it does not turn criterion subtotals into a new source of problem weights.

After problem weights are accepted, the program enforces them
deterministically. Criteria that sum to a different amount are rescaled
proportionally to the authoritative problem weight; an empty criterion list is
replaced by one full-credit criterion; duplicate criterion IDs are renamed;
unknown problem entries are dropped; and the rubric total is recomputed. These
repairs are reflected in `rubric.md`. Coverage gaps and discrepancies detected
before normalization also appear as issues in `run_manifest.json`, but not
every normalization repair creates a manifest issue. Review the resulting
`rubric.md`, not only the source rubric.

## Grade the prepared roster

**Run `grade` only after the assignment, solutions, rubric, roster, and privacy
decision are ready.** A successful command means artifacts were written; it
does not mean every grade is safe to release.

```bash
autograder grade \
    --assignment examples/sample/sample_assignment.pdf \
    --submissions examples/sample/submissions \
    --out runs/sample-demo
```

Add the external solution and rubric inputs you approved when applicable. Use
the [CLI option inventory](reference.md#cli-option-inventory) for exact flags
and command scopes.

For each student, the mapper searches all pages by problem content rather than
assuming the blank assignment's page positions. It can associate inserted or
appended pages, continued answers, out-of-order work, and mislabeled work with
the intended problem. A separate transcription pass preserves mistakes and
marks unreadable spans; the grader then applies every rubric criterion. Model
tasks within a stage may run concurrently, but students are processed one at a
time.

The normal completion code is `0`. An interrupted command returns `130` and can
be resumed with the identical command. If some student work remains incomplete,
the program still writes all available reports, summary rows, review items,
manifest data, and returns `2`. Other startup or stage errors return `1`; add
verbose logging when the one-line error is not enough. Exact behavior is in
the [process exit status reference](reference.md#process-exit-status).

## Review results and decide what may be released

**Start with the class summary and review queue, resolve every queued item,
then spot-check results that were not queued.** The queue routes uncertainty;
it is not an approval system, and a score can be present while still requiring
a human decision.

Read artifacts in this order:

1. `summary.csv` shows roster-level totals, per-problem awards, review counts,
   flags, completeness, and failures.
2. `review_queue.md` names every student/problem pair requiring a person and
   gives the reason.
3. `students/<id>/report.md` shows criterion scores, evidence, feedback,
   location status, and the transcript for that student.
4. When evidence is unclear, compare `mapping.json`, `transcripts.json`, and
   `grades.json` with the original submission. Also consult
   `solutions_manual.md` and `rubric.md` before changing a grade.

Use the following distinctions when deciding an outcome:

| Saved result | Score consequence | Review consequence |
|---|---|---|
| `answered`, `answered_elsewhere`, `partial`, or `mislabeled` | The located attempt is transcribed and graded against the rubric. | Queued only when another trigger applies; mislabeled work is graded under the problem its content answers. |
| Clean `blank` with no region | Deterministic zero, with no grader call. | The blank status alone is not queued. Integrity signals still queue it. |
| `not_found` | Provisional deterministic zero, with no grader call. | Always queued so a person confirms no work was missed; any unattributed work is included as context. |
| `blank` or `not_found` with a region | Deterministic zero; the region says where the mapper looked and does not turn the result into a gradeable attempt. | Always queued so a person confirms the region is empty. |
| `illegible_candidate` | Processing continues and a score may be present. | Always queued. Do not treat the score as approval of an unreadable answer. |
| `mapping_error`, failed transcription, or failed grading | No award is available for that problem; it is not converted to zero. | Always queued. The student's final total is unavailable until retry or human resolution. |
| Low transcript or grader confidence | A completed score remains present. | Queued when below the current threshold; low confidence never silently becomes zero. |
| Any completed result marked `needs_review` | The score usually remains present. | A person must decide because of confidence, uncertainty, integrity signals, an unverified solution, illegibility, or deterministic validation. |

The default review comparisons are grader confidence below `0.60` and
transcript confidence below `0.50`. Other intrinsic triggers include an
unverified official solution, mapper/transcriber/grader integrity concerns, a
grader's explicit uncertainty, and an omitted rubric criterion. The program
fills an omitted criterion with zero and queues it, clamps every criterion
award to its valid range, and recomputes totals in code rather than trusting a
model-provided total. Deterministic `blank` and `not_found` paths do not consult
a solution, so an unverified solution alone does not queue them.

A per-problem failure preserves successful sibling problems. The report shows
an unavailable problem and a processed subtotal, but no final total;
`summary.csv` leaves `total_awarded` and `percent` blank rather than presenting
the subtotal as a final grade. A failure that prevents an entire student from
being processed creates a `failed` summary row with blank score fields, while
the rest of the class continues.

Before release, resolve every unavailable or queued result against the source,
inspect a representative sample of unqueued results, record any human
adjustments outside the generated directory, and approve the final grades
yourself. Never interpret an empty review queue as a guarantee of correctness.

## Resume an interrupted or partially failed run

**Repeat the identical command without `--force` to resume.** Eligible saved
stages and completed students are reused, while failed solution-agent,
transcription, and grading entries are retried when an API key is available.
Successful sibling results are retained.

```bash
autograder grade \
    --assignment examples/sample/sample_assignment.pdf \
    --submissions examples/sample/submissions \
    --out runs/sample-demo
```

The reusable records are the assignment structure, solutions, rubric, and
each student's mapping, transcripts, and grades. Invalid saved records may be
rebuilt. If a solution-agent failure is repaired, every transitively dependent
solution is regenerated. When that changes the manual, the rubric, grades,
reports, summary, review queue, and manifest are rebuilt; mappings and
transcripts remain reusable. A solution that exhausted its evaluator retries
and was saved as unverified is a completed result, not a retryable agent
failure; review it manually or deliberately rebuild the run.

Treat everything inside `--out` as read-only. A hand edit may be consumed as
pipeline state, overwritten on resume, or make the cache internally
inconsistent. Change an external teacher input and use a new output directory
instead. Files are replaced atomically one at a time, but the directory is not
a transaction: do not run two processes against the same output directory.

## Decide whether an output directory can be reused

**Reuse a directory only for the same grading identity or a compatible
extension of it.** `run_binding.json` fingerprints the assignment,
cache-relevant settings, teacher materials, rubric instructions, and each
student's ordered files as that student is encountered.

| Planned action | Reuse the same `--out`? | Consequence |
|---|---|---|
| Continue from `inspect` to `solve`, `rubric`, or `grade` with the same choices | Yes | Earlier compatible stages are reused and later input bindings are added. |
| Repeat an identical command after interruption or partial failure | Yes | Completed work is reused and eligible failures are retried. |
| Change only review-confidence thresholds | Yes | Scores are reused; review flags, reports, summary, and queue are recalculated from the new thresholds without a model call. Intrinsic review reasons remain. |
| Change worker count, prompt-caching choice, API key source, verbosity, or merely add/remove `--force` | Yes | These choices do not redefine saved content, although `--force` controls whether it is reused on that invocation. |
| Change the assignment, any recorded submission file or its order/name, the answer key, rubric, or rubric prompt | No | Use a new directory; a binding mismatch stops the command. |
| Change model, thinking, effort, token limit, solution retry policy, strict-solutions, strict-rubric, or supplied-solution verification policy | No | These affect generated content or trust and require a new directory. |
| Add or remove a student from the roster | No | The binding is per encountered student, not a complete-roster lock. Class files may be rewritten for the new roster and an old student directory may remain, so always start a new directory. |
| Reuse an older directory with an unsupported binding schema or settings recorded under an obsolete binding policy | No | Start fresh rather than trying to reinterpret old trust or review behavior. |

The [RunConfig reference](reference.md#runconfig-reference) is authoritative
for every cache-relevant setting, including programmatic configuration not
exposed as a CLI flag.

Use `--force` only when the inputs and bound settings are unchanged but every
stage requested by this command should be rebuilt—for example, after upgrading
the program and deliberately accepting the extra cost. It does not override a
binding mismatch or adopt different inputs. It also discards the benefit of a
partly completed run, so do not add it merely to resume.

## Change an answer key, rubric, assignment, or roster

**Edit the source outside the output directory and choose a new `--out`.** This
preserves the original audit trail and prevents artifacts produced under two
grading policies from being mixed.

For example, a revised key should produce a separate run:

```bash
autograder grade \
    --assignment examples/sample/sample_assignment.pdf \
    --submissions examples/sample/submissions \
    --solutions path/to/revised-key.json \
    --out runs/sample-demo-revised-key
```

Apply the same rule to rubric content or instructions, the assignment source,
any student's files, filenames or page order, and roster membership. Review
thresholds are the exception: because they change routing rather than scores,
you can rerun the same directory to ask which existing results a stricter or
looser threshold would flag.

## Choose a reasoning effort

**Treat `--reasoning-effort` as a run-wide model preference, not as a fixed
token budget or a portable quality level.** It is a shared option on every
subcommand and is written after the subcommand, like the other run options:

```bash
autograder grade \
    --assignment examples/sample/sample_assignment.pdf \
    --submissions examples/sample/submissions \
    --out runs/sample-demo-high-effort \
    --model openai/gpt-5.1 \
    --reasoning-effort high
```

The accepted values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
and `max`. These labels tell a compatible model how much internal reasoning to
allocate. They do not promise a particular number of reasoning tokens or the
same behavior across models.

Omission and `none` deliberately have different meanings:

| Configuration | What the client sends | Consequence |
|---|---|---|
| Omit `--reasoning-effort` | No reasoning-effort field | The selected model or provider uses its default. This also avoids adding reasoning-effort support as a routing requirement. |
| `--reasoning-effort none` | An explicit `none` preference | Ask a compatible model to disable reasoning. This is not the same as accepting its default. |
| `--reasoning-effort LEVEL` | The selected label on every model request | Ask for that relative effort. OpenRouter may translate it to a provider-native control or the nearest level the model supports. |

The parser checks only that the label is in the list above. It cannot guarantee
that a selected model/provider combination will honor that exact level.
OpenRouter [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
is configured to require support for supplied parameters and to allow provider
fallbacks. Supplying an effort can therefore narrow the eligible endpoints; a
request fails if no endpoint also satisfies the model's image and tool
requirements and the run's privacy policy. OpenRouter's
[reasoning controls](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
may normalize a level for an eligible provider, and some model-specific
combinations may still be rejected. In particular, do not assume that `xhigh`
or `max` is portable merely because the autograder accepts the spelling.

The default `openrouter/auto-beta` model is dynamic: separate agent sessions can
resolve to different models or providers, each with its own default and effort
scale. A session remains sticky within one agent loop, but a complete grading
run contains many independent loops. For more reproducible or high-stakes
grading, choose a fixed model slug, confirm that model's supported reasoning
controls, and set an explicit effort only after testing it on representative
work.

The setting applies to every uncached model call made by the requested command,
including every follow-up turn after a tool call. Because `grade` includes all
earlier stages when their artifacts are unavailable, one flag can affect
assignment analysis, solution generation and evaluation, teacher-material
interpretation, rubric work, submission mapping, transcription, and grading.
It does not affect deterministic Python validation, score aggregation, or
report generation. There is no per-stage reasoning-effort option.

This run-wide scope can multiply cost and latency: generated solutions use a
separate evaluator and may be regenerated, while many transcription and grading
tasks can run for one assignment. Higher effort can help with difficult visual
or rubric judgments but may produce more output tokens and take longer; lower
or disabled reasoning can be cheaper but may reduce quality. OpenRouter counts
reasoning tokens as output tokens and bills them at the applicable output rate.
Benchmark a representative subset rather than assuming the highest value is
best.

`--reasoning-effort` is independent of `--max-tokens`. The latter controls the
agent output ceilings; selecting a higher effort neither raises those ceilings
nor reserves a reasoning-token budget. A reasoning-heavy request can still hit
the output limit. Likewise, “Max” in a model name is part of that model's name
and has no connection to `--reasoning-effort max`.

Reasoning effort is part of the output directory's run-binding identity. Use
the identical value when continuing from `inspect` to `solve`, `rubric`, or
`grade` in the same `--out`. Changing it requires a fresh output directory, and
`--force` cannot override that mismatch. Omission and an explicit value such as
`medium` remain distinct identities even if the selected model currently
defaults to that value. Reusing an identical cached stage makes no model call,
so the setting has no new or retroactive effect on that artifact.

For audit purposes, `run_manifest.json` records the requested effort, reported
reasoning-token usage and cost, and the resolved models and providers for the
current command invocation. It does not record a provider-normalized effective
effort, expose the model's reasoning text, or combine usage from earlier
resumptions. A `reasoning` field in `solutions_manual.json` is instead the
model's submitted worked explanation for an official solution; it is not the
provider reasoning stream.

## Choose a provider sort

**Treat `--provider-sort` as a ranking among the providers a request could
already have used, not as a way to pick one.** It is a shared option on every
subcommand and is written after the subcommand:

```bash
autograder grade \
    --assignment examples/sample/sample_assignment.pdf \
    --submissions examples/sample/submissions \
    --out runs/sample-demo-fast \
    --provider-sort throughput
```

The accepted values are `price`, `throughput`, `latency`, and `exacto`. The
first three rank the eligible endpoints by cost, by generation speed, and by
recent response latency. `exacto` is OpenRouter's own accuracy-oriented
ranking; confirm what it selects on the
[provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
page before relying on it for graded work.

Eligibility is decided before the ranking is applied. The selected model, the
requirement that a provider support every supplied parameter, and the run's
privacy policy all narrow the endpoint set first; the sort only orders what
survives. With `--reasoning-effort` set and zero data retention required, that
set can already be a single endpoint, in which case the option changes nothing.

| Configuration | What the client sends | Consequence |
|---|---|---|
| Omit `--provider-sort` | No `sort` field | OpenRouter balances load across the eligible endpoints. |
| `--provider-sort STRATEGY` | The chosen ranking on every model request | Endpoints are tried in that order, and load balancing is disabled for the run. |

Each agent loop sends a sticky session ID, so the provider chosen for its first
turn serves the rest of that loop. A grading run contains many independent
loops, and the default `openrouter/auto-beta` model resolves separately in each
one. The ranking therefore steers many first choices rather than pinning one
provider to the whole run.

Unlike reasoning effort, a provider sort is not part of the output directory's
run-binding identity. You can change it and keep the same `--out` directory,
where a changed model or reasoning effort requires a fresh one.

The binding records settings that change what a saved artifact means. The
privacy options qualify, because they are guarantees about who was allowed to
see the data. A ranking is not such a guarantee: it only orders endpoints the
model requirements and the privacy policy have already admitted, and the
binding never pinned the endpoint that served any given request. Rerunning with
a different ranking cannot alter existing work either, because a stage that
finds a compatible saved artifact loads it without calling the model.

That freedom has an audit cost. A directory whose stages were built under
different rankings keeps no record of which stage used which, and
`run_manifest.json` describes only the command that wrote it last. Run each
ranking into its own `--out` directory whenever you may need to say what
produced a particular grade.

Sorting by throughput or latency can route to more expensive endpoints than the
balanced default would have chosen. `run_manifest.json` records the requested
sort alongside the providers actually reached and the cost for that command
invocation, so compare a representative run before adopting a ranking for a
whole roster.

## Pin a provider

**Use `--provider` when a run must stay on endpoints you know can serve it.**
Where `--provider-sort` ranks the eligible endpoints, `--provider` decides
which ones are eligible at all. It takes OpenRouter provider slugs, repeated or
comma-separated, and is a shared option on every subcommand:

```bash
autograder grade \
    --assignment examples/sample/sample_assignment.pdf \
    --submissions examples/sample/submissions \
    --out runs/sample-demo-pinned \
    --provider google-ai-studio
```

Fallbacks stay enabled inside the allowlist, so the router may still try a
second endpoint belonging to an allowed provider. Everything else that narrows
the endpoint set — the model, the parameter requirement, the privacy policy —
still applies first, and an allowlist that admits no endpoint fails the request
rather than silently widening.

The option exists because provider endpoints for one model are not
interchangeable. Every agent loop here inspects pages, so tool results carry
images, and an endpoint that cannot accept an image inside a tool result cannot
run this program at all. When such an endpoint serves the first turn of a loop,
the second turn fails with a provider error naming neither the image nor the
endpoint. Look up the slugs a model offers at
`https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints`, and pin the one
you verified.

Unlike a sort, an allowlist is part of the run-binding identity. It decides
which companies were permitted to see the submissions, which is the same kind
of guarantee as the privacy options, so changing it requires a fresh `--out`
directory. `run_manifest.json` records the providers each command actually
reached, which is how you confirm a pin did what you intended.

## Manage cost, latency, and concurrency

**Choose model quality and the reasoning setting described above before binding
the output directory, then control concurrency to fit your API limits.** Model,
reasoning effort, privacy routing, and output-token changes require a fresh
directory; worker count and provider sort do not.

Cost is concentrated as follows:

- Assignment analysis happens once for a compatible run.
- Generated solutions are relatively expensive because a separate evaluator
  checks each draft and may trigger regeneration.
- Rubric creation happens once for a compatible run.
- Mapping happens once per student. Transcription and grading run per located
  or attempted problem; clean zero paths avoid grader calls.
- Students run sequentially. Independent problem tasks within solution,
  transcription, and grading stages use up to the configured worker count.

More workers can reduce wall time but increase simultaneous requests and rate
pressure; it does not reduce total logical work. The selected model must support
image input, tool use, and any requested reasoning controls.

Automatic prompt caching is handled by OpenRouter and the selected provider
and can reduce repeated input-token cost within multi-turn agents. Each agent
uses a sticky session ID and retains a bounded number of tool
images and can request an evicted crop again. `run_manifest.json` separates API
calls, prompt, completion, reasoning, cached-prompt, cache-write, and cost
fields for the current command invocation only. It also separates the requested
model from resolved models and providers; it does not combine usage from
earlier resumptions.

## Protect student data at the point of use

**Confirm institutional permission before the first live call, and secure both
inputs and outputs for the life of the run.** Assignment pages, student
submissions, and relevant teacher materials are sent through OpenRouter whenever
a required stage calls the model. The output directory contains student IDs,
source paths, transcriptions, grades, and review evidence. Keep it out of public
repositories and broadly shared folders, apply your normal retention rules,
and restrict access like the original submissions.

Prefer `OPENROUTER_API_KEY` in the process environment over repeating a key on
the command line, subject to your institution's secret-handling policy. The key
is not written into run artifacts. A cached-only command needs no key.

Student-authored directions are untrusted data, not instructions. Mapping,
transcription, and grading agents are instructed to ignore such directions,
record integrity concerns, and continue; every recorded concern requires human
review. Inspect the original submission before acting on an integrity flag.

Generated reports escape untrusted Markdown/HTML, render unusual control
characters visibly, and neutralize formula-like text cells in CSV while
leaving numeric score cells numeric. The agent calculator accepts restricted
arithmetic, not names, imports, attribute access, or arbitrary code, and limits
expensive operations. These safeguards reduce document and spreadsheet risk;
they do not make the output public or eliminate the need for human review.

If you distribute or deploy the project, also confirm that your use of
PyMuPDF fits its AGPL or commercial licensing terms.

## Troubleshoot by protecting the audit trail

**Diagnose from the command message, `run_manifest.json`, summary, and stage
artifacts before rerunning.** Resume only when the grading identity is
unchanged; otherwise use a new output directory.

- **No API key:** Cached results can still be read. Set `OPENROUTER_API_KEY` or
  provide a key if a model call or failed-item retry is required.
- **No gradable problems:** Confirm that the blank assignment is readable and
  contains the questions. Improve the source and start with a new output
  directory.
- **No submissions:** Check paths, supported extensions, empty student
  subdirectories, and the discovery layout in
  [Prepare inputs and preserve student identity](#prepare-inputs-and-preserve-student-identity).
- **Unsupported or mixed text input:** Convert unsupported files to PDF, PNG,
  JPEG, Markdown, or LaTeX. Keep a standalone Markdown/LaTeX submission alone,
  or convert it before combining pages.
- **Output belongs to different inputs or settings:** Read the named binding
  difference and choose a new output directory. `--force` cannot merge grading
  identities.
- **A student has `run_status=failed`:** Read the summary failure, log, and
  manifest. Correct an input problem when necessary, then repeat the identical
  command if the binding still matches.
- **Final total or percent is blank:** At least one problem is unavailable.
  Inspect the student report and review queue; the displayed processed subtotal
  is not a final grade.
- **`mapping_error`:** The mapper claimed work without a usable location or
  omitted a problem. Inspect the submission and start a new output directory
  to map it again; ordinary resume reuses a valid saved mapping.
- **Nearly everything is queued:** Look for an unverified prerequisite
  solution or submission-wide integrity concern in `solutions_manual.md`, the
  review reasons, and student flags.
- **Low transcript confidence:** Compare the transcript with the original and
  obtain a clearer scan when possible. Zoom cannot restore missing source
  detail.
- **Unexpectedly low scores:** Compare criterion evidence with `rubric.md`.
  If the policy is wrong, revise the external rubric or rubric prompt and use a
  new output directory.
- **Slow or expensive run:** Check the invocation's usage in the manifest,
  model/reasoning choices, number of located answers, solution retries, and API
  rate pressure. Choose new model-producing settings in a new directory; tune
  worker count on the existing identity if only concurrency should change.

Current limitations remain part of the release decision: confidence is a model
self-assessment, grades are model judgments, the workflow is designed for
physics and mathematics, there is no custom report viewer or gradebook
integration, and large classes are sequential by student. Return to the
[documentation index](README.md) for the other reader paths.
