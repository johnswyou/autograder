# How it works

## The 30-second explanation

Agentic Autograder does not pass a PDF filename to Claude and hope it finds the
right answer. Python opens the documents, renders PDF pages as JPEG images,
and sends those images in Anthropic Messages API requests. Claude first learns
the blank assignment's problem structure, then looks across every student page
to map each problem to the work that answers it. Once the location is known,
Python supplies sharper crops and Claude transcribes and grades the work.
Python validates the structured results, applies deterministic safeguards and
arithmetic, writes reports, and builds a review queue. The instructor reviews
the evidence and owns the final grade.

There is no standalone OCR engine. The same Claude visual-capability path does
the visual mapping, handwriting transcription, and rubric-based judgment. A
PDF text layer can help recover exact typeset assignment wording, but scanned
or handwritten pages are understood from rendered images.

## One answer from page to report

Follow Jordan's Problem 1(c), the same continued-answer case used by the
synthetic sample in [Getting started](getting-started.md). The repository sample
is typeset for repeatability. For this explanation, imagine Jordan wrote that
same calculation by hand on an appended page and submitted it as a scanned PDF.

### 1. Python ingests the documents

Python opens the blank assignment and Jordan's submission as page-oriented
documents. For a PDF, PyMuPDF renders a requested full page or rectangular
region into pixels, and the image pipeline encodes the result as JPEG. Python
then base64-encodes those JPEG bytes in Anthropic image content blocks whose
media type is `image/jpeg`.

Phone photos follow the same agent-facing image path, but Python crops their
existing pixels rather than re-rendering vectors. EXIF orientation is honored.
Markdown and LaTeX inputs use text chunks instead of images.

If a PDF has embedded text, Claude can ask Python for that text layer. This is
useful for exact typeset prompts; it is only an additional reading tool and is
not the primary path for handwriting.

### 2. Claude maps from full pages before anyone transcribes

Claude receives the known problem inventory from the blank assignment and
full-page views of Jordan's submission. It skims every page and matches work by
what it computes or argues, not by assuming that submission page 2 corresponds
to assignment page 2.

For Problem 1(c), Jordan wrote a continuation note on the expected page and the
calculation on an appended page. The mapping result records an
`answered_elsewhere` status and one or more percentage-based page regions in
reading order. The same mapping pass can record inserted pages, out-of-order
work, labels that disagree with content, and work that belongs to no known
problem.

Mapping and transcription are deliberately separate:

- Mapping answers **where is all the work for this problem?** using the whole
  submission and assignment structure.
- Transcription answers **what exactly is written in those regions?** without
  correcting the student's reasoning.

Separating them prevents a transcriber from treating the expected answer box as
proof that the answer is there. It also lets one answer include a margin, a
back page, or several regions while giving each transcription task focused
visual evidence.

### 3. Python makes sharper views when needed

After mapping, Python renders the recorded regions for the transcriber and
grader. A region from a vector PDF is re-rendered from the PDF at a higher
scale, up to the configured pixel limits; this can reveal small subscripts and
thin strokes that were hard to see in the full-page view. A photo region is
cropped and may be enlarged within an upscale limit. Claude can also request a
full-page high-detail view, another crop, or a 90-, 180-, or 270-degree rotated
view for sideways work.

Cropping concentrates available pixels and removes distracting neighboring
work. It cannot reconstruct a stroke that was never captured, refocus a blurry
photo, recover a clipped edge, or make faint marks reliable when the source has
insufficient detail. When zooming still leaves ambiguity, the correct result is
lower confidence, an `[illegible]` marker, and human review—not invented text.

### 4. Claude transcribes the located work

A fresh Claude task receives the Problem 1(c) prompt and its mapped crop or
crops. It produces a verbatim structured transcript: equations in LaTeX,
mistakes preserved, crossed-out work marked, diagrams described, and unreadable
spans labeled. It also returns a confidence score and quality notes.

The file and some user-facing fields retain the historical term “OCR,” but
`autograder/ocr.py` orchestrates Claude through the Anthropic Messages API; it
does not invoke Tesseract, PyMuPDF OCR, or another recognition engine.

### 5. Claude applies the rubric

A separate Claude grading task receives the fixed rubric criteria, official
solution, transcript confidence, mapper status, and a crop of the student's
actual work. The transcript is its primary evidence, but it can zoom back into
the source before deducting for a suspicious character. It can ask Python's
restricted numeric calculator to verify arithmetic instead of estimating it
mentally.

Claude decides how the evidence satisfies each rubric criterion, writes a
justification and feedback, estimates grading confidence, and can request human
review. That rubric-based judgment is model work; Python does not decide whether
Jordan's physics method deserves partial credit.

### 6. Python validates and aggregates

Claude returns typed structured data rather than prose that Python must guess
how to parse. Pydantic schemas reject malformed results and give Claude a chance
to repair them. Python then performs deterministic checks and processing,
including:

- rejecting invalid page coordinates and rotations;
- limiting image size, agent turns, tool images, and numeric computation;
- accepting only a restricted arithmetic expression language for calculations;
- dropping unknown or duplicate rubric criterion scores, clamping each award to
  its allowed range, and filling an omitted criterion with zero plus review;
- deriving confidence-threshold review flags;
- summing completed criterion and problem scores; and
- withholding the final total if any problem grade is unavailable.

These safeguards can validate shape, bounds, and arithmetic. They cannot prove
that Claude read the handwriting correctly or made the right academic judgment.

### 7. Python persists evidence and reports

Each completed stage is written as an inspectable artifact. For Jordan, that
includes `mapping.json`, `transcripts.json`, `grades.json`, and `report.md`.
Class-level output includes `summary.csv` and `review_queue.md`; the run also
records its inputs, configuration, issues, and API usage in
`run_manifest.json`.

Writes are atomic, so a process interruption does not intentionally leave a
half-written replacement. Compatible saved stages are reused on a repeated
command, while failed per-problem transcription or grading results are retried
when a key is available. Before report text reaches Markdown or CSV, Python
escapes untrusted student/model content and neutralizes spreadsheet formulas.

### 8. The instructor makes the final decision

The review queue focuses attention, but it is not a substitute for oversight.
The instructor must resolve every queued result, sample unqueued results,
correct errors in the grading workflow or downstream grade system as needed,
and approve grades before release.

## Who is responsible for what?

| Responsibility | Owner |
|---|---|
| Interpret assignment and submission page images | Claude |
| Map each problem to work across full pages | Claude |
| Transcribe handwriting and report confidence | Claude |
| Judge work against rubric criteria and write feedback | Claude |
| Render/crop/rotate pages and construct API image blocks | Python |
| Validate schemas, coordinates, score bounds, and limits | Python |
| Provide restricted arithmetic and tool safety | Python |
| Cache compatible artifacts and retry failed pieces | Python |
| Aggregate scores, escape output, and generate reports | Python |
| Decide whether the evidence is acceptable | Instructor |
| Approve and release the final grade | Instructor |

## Outcomes that must not be confused

These outcomes deliberately mean different things:

| Outcome | Score behavior | Review behavior |
|---|---|---|
| Clean `blank` | Deterministic zero. The mapper positively observed an empty answer space and found no work elsewhere. | Not queued solely for being blank. Integrity flags or another independent concern can still require review. |
| `not_found` | Deterministic zero. No work could be attributed to the problem, but that is less certain than a clean observed blank. | Always queued so a human confirms that work was not missed. |
| `blank` or `not_found` with a region | Deterministic zero. The region records where the mapper looked; it does not turn the outcome into a gradeable answer. | Queued so a human confirms the pointed-to space is really empty. |
| Unavailable / `failed` | No award exists for that problem. A mapping, transcription, or grading stage failed rather than concluding the answer was wrong. | Always queued. The student's final total is unavailable; only the processed subtotal is reported until retry or human resolution. |
| Low confidence | The completed score remains present. Low transcript confidence means the visual reading may be wrong; low grader confidence means the rubric judgment may be wrong. | Queued when confidence is below the configured threshold. Low confidence is not silently converted to zero. |
| `needs_review` | Usually preserves the completed score; a failed result remains unavailable. | This is a routing flag, not a new work status. It can be triggered by confidence, illegibility, model uncertainty, integrity signals, unverified solutions, or deterministic validation concerns. |

A clean `blank` zero is evidence that no answer was supplied. A `not_found`
zero is provisional evidence that no answer was located. An unavailable score
is evidence that the system could not finish. A review flag says a person must
make or confirm the decision; it does not by itself erase a completed score.

For the hands-on path, return to [Getting started](getting-started.md). For
operational decisions and exact options, use [Usage](usage.md) and
[Reference](reference.md). Return to the [documentation index](README.md) for
all reader paths.
