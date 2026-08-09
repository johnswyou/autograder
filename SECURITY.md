# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/johnswyou/autograder/security/advisories/new)
rather than opening a public issue.

Include the version or commit you tested, what you observed, and the steps to
reproduce it. Please do not include real student work in a report; a synthetic
example produced by `python examples/generate_sample.py` is enough to
demonstrate almost any issue, and keeps student data out of the report.

Expect an acknowledgement within about a week. This project is maintained by one
person in their spare time, so please allow reasonable time for a fix before
disclosing publicly.

## Scope

This is a command line tool that runs on your own computer. It has no server, no
network listener, and no multi-user trust boundary. Reports that assume a hosted
deployment do not apply.

Issues that are in scope include: the API key being written to disk or logged,
sanitization of untrusted input being bypassed, a path traversal that lets a
crafted submission write outside the output directory, and a dependency
vulnerability that this tool actually exercises.

## Handling your API key

The OpenRouter API key is read from the `OPENROUTER_API_KEY` environment variable
or the `--api-key` flag, and is held in memory for the duration of a run. It is
never written to the output directory and never logged. `run_manifest.json`
records the model and run settings, but not the key.

Prefer the environment variable over `--api-key`, since a command line argument
is visible to other processes on the machine and is usually saved in your shell
history.

## Handling student data

Grading sends assignment pages and student submissions through OpenRouter to
the selected model provider. By default, routing requires zero data retention
and denies providers that collect or train on request data. Only use
`--allow-data-retention` or `--allow-data-collection` after confirming that
your institution permits the relaxed policy.

The output directory records student names, transcribed handwriting, and the
path of every input file. Protect it the same way you protect the original
submissions. This repository's `.gitignore` excludes `GRADING/` and
`examples/sample/` so that grading work kept beside a checkout is not committed
by accident. If you fork this project, keep those entries.

Untrusted text taken from a submission is neutralized before it is written to
`summary.csv`, so that a value beginning with `=`, `+`, `-`, or `@` cannot be
run as a formula when the file is opened in a spreadsheet. Arithmetic that an
agent requests during grading is evaluated by a restricted parser that allows
only numeric literals, arithmetic operators, and a fixed set of math functions;
it cannot execute arbitrary code.
