# Getting started

This guide takes an instructor from a fresh checkout to a reviewed synthetic
grading run. The commands assume a macOS or Linux shell and are run from the
repository root.

The example is deliberately typeset and contains no real student data. It lets
you check document ingestion and answer mapping before using handwritten work;
it does **not** measure handwriting or transcription quality.

## 1. Check Python

Agentic Autograder requires Python 3.10 or newer.

```bash
python --version
```

This reads the installed Python version and creates no files. Success is a
version such as `Python 3.10.14` or later.

## 2. Create an environment and install the command

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
autograder --help
```

These commands create `.venv/`, activate it for the current shell, install this
checkout and its dependencies, and display the command-line help. The installer
may also create editable-install metadata in the checkout. Success means the
last command exits normally and lists `inspect`, `solve`, `rubric`, and `grade`.

On Windows PowerShell, replace the activation command with
`.venv\Scripts\Activate.ps1`; the installation and success signal are the same.

## 3. Understand the data and cost boundary

Commands that need a model send the assignment, student submissions, and any
teacher solution or rubric material needed by that stage through OpenRouter and
incur API charges. Confirm that your institution permits this use before
processing student data. The output directory also contains names,
transcriptions, and grades, so protect it like the original submissions and do
not commit it to a public repository.

Set the key in the current shell without printing it:

```bash
export OPENROUTER_API_KEY="replace-with-your-key"
test -n "$OPENROUTER_API_KEY" && echo "OpenRouter API key is set"
```

This creates no project files. Success is the message `OpenRouter API key is
set`; it confirms only that the variable is nonempty. OpenRouter validates the
key on the first live call. The autograder does not write the key to its output
directory.

You may instead pass `--api-key` to each command, but an environment variable
keeps the key out of repeated autograder command lines and process listings.
Follow your institution's normal secret-handling rules for shell history.

The default model is the dynamic `openrouter/auto-beta` router. It can resolve
to different models or providers as availability changes. For reproducible or
high-stakes grading, choose a fixed OpenRouter slug, for example
`--model openai/gpt-5.1`. Each agent loop reuses one nonempty session ID so its
dynamic model/provider choice stays sticky across repair and tool turns.
OpenRouter and providers handle automatic prompt caching; there is no caching
switch or repository-specific cache marker.

Provider routing allows fallbacks and requires support for every requested
parameter. It requires zero data retention and denies provider data collection
by default. `--allow-data-retention` and `--allow-data-collection` are explicit
privacy opt-outs; use either only after institutional review.

## 4. Generate the synthetic assignment

```bash
python examples/generate_sample.py
```

This is local-only: it makes no OpenRouter request and creates:

```text
examples/sample/sample_assignment.pdf
examples/sample/submissions/jordan_lee.pdf
```

Success is output beginning `Created a synthetic, typeset demo` followed by
those assignment and submissions paths. Jordan's four-page submission includes
an inserted blank page, work continued on an extra page, one omitted answer,
and an answer written under the wrong problem number.

If you only wanted to prepare inputs without sending anything to OpenRouter,
stop here. The next command is the first paid API call.

## 5. Inspect how the assignment was understood

```bash
autograder inspect \
    --assignment examples/sample/sample_assignment.pdf \
    --out runs/sample-demo
```

The selected model receives rendered pages of the blank assignment and returns its problem
structure. Python validates and saves the result. This paid call creates:

```text
runs/sample-demo/assignment_spec.json
runs/sample-demo/run_binding.json
runs/sample-demo/run_manifest.json
```

Success means exit code `0`, a summary naming the assignment and its gradable
problems, and the line `Assignment structure written to
runs/sample-demo/assignment_spec.json`.

Format the saved structure for inspection:

```bash
python -m json.tool runs/sample-demo/assignment_spec.json
```

This reads the JSON and prints it without changing or creating files. Success
means formatted JSON appears with no parse error. Before continuing, confirm
that it represents:

- a two-page, 20-point assignment;
- Problem 1 with parts (a), (b), and (c), worth 8 points together;
- Problem 2 with parts (a) and (b), worth 6 points together; and
- the 6-point multiple-choice Problem 3.

Also check that the prompts, printed points, page references, and answer regions
are sensible. If the structure is wrong, improve the source document and start
again with a new `--out` directory. Do not grade against a structure you know is
wrong.

## 6. Grade the sample

This command performs paid OpenRouter API calls. It sends the synthetic
assignment and submission content to the selected model, generates or checks a solutions
manual and rubric, maps Jordan's work, transcribes it, and grades it:

```bash
autograder grade \
    --assignment examples/sample/sample_assignment.pdf \
    --submissions examples/sample/submissions \
    --out runs/sample-demo
```

Because the assignment, settings, and output path match the inspection step,
the saved assignment structure is reused. The command adds these main files:

```text
runs/sample-demo/solutions_manual.json
runs/sample-demo/solutions_manual.md
runs/sample-demo/rubric.json
runs/sample-demo/rubric.md
runs/sample-demo/students/jordan_lee/mapping.json
runs/sample-demo/students/jordan_lee/transcripts.json
runs/sample-demo/students/jordan_lee/grades.json
runs/sample-demo/students/jordan_lee/report.md
runs/sample-demo/summary.csv
runs/sample-demo/review_queue.md
```

It also updates `run_binding.json` and `run_manifest.json`. Success means exit
code `0`, a line beginning `Graded 1 student(s).`, Jordan's score, and printed
paths for `summary.csv` and `review_queue.md`. A successful run may still have
items that require human review; that is an expected safety outcome, not a
failed command.

## 7. Locate and review the results

```bash
find runs/sample-demo -maxdepth 3 -type f | sort
```

This lists the run artifacts and creates no files. Success means the list
includes `summary.csv`, `review_queue.md`, and
`students/jordan_lee/report.md`, together with the JSON stage artifacts shown
above.

Review the run in this order:

1. Open `summary.csv` for the class total and the count of items needing review.
2. Open `review_queue.md` and decide every listed item; a low-confidence or
   failed result is not safe to release without that decision.
3. Open `students/jordan_lee/report.md` for per-criterion scores, feedback, and
   the saved transcript.
4. Spot-check `students/jordan_lee/mapping.json`. The synthetic case is designed
   to exercise the inserted page, continued answer, omitted answer, and
   mislabeled answer described above.
5. Inspect some results that were not queued, then approve the final grades
   yourself. The generated files are recommendations and evidence, not an
   automatic release decision.

The sample demonstrates ingestion and mapping with clean typeset content. A
successful sample run says nothing about how well a particular scan or style of
handwriting will transcribe.

## 8. Substitute a real assignment

Keep the same two-step pattern, but use a fresh output directory and your real
paths:

```bash
autograder inspect \
    --assignment path/to/kinematics-quiz.pdf \
    --out runs/kinematics-quiz

autograder grade \
    --assignment path/to/kinematics-quiz.pdf \
    --submissions path/to/submissions \
    --out runs/kinematics-quiz
```

The first command makes paid calls and creates the three inspection files under
`runs/kinematics-quiz/`; success is the assignment-structure message. Review
`assignment_spec.json` before running the second command. The second command
makes additional paid calls and creates solutions, rubric, mapping,
transcription, grade, report, summary, and review-queue artifacts; success is
the graded-student summary and exit code `0`.

Never place `--out` inside the assignment or submissions paths. When inputs or
model-producing settings change, choose another output directory. For optional
answer keys, rubrics, resume behavior, and all flags, continue to the
[Usage guide](usage.md) and [Reference](reference.md).

Run bindings use schema 3. Output directories created by an earlier transport
or schema are rejected; choose a fresh `--out` directory rather than mixing
their saved artifacts with a current OpenRouter run.

To understand what happens between the PDF and the report, read
[How it works](how-it-works.md). Return to the
[documentation index](README.md) for all reader paths.
