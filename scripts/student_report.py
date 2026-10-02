#!/usr/bin/env python3
"""Build a student-facing grade report PDF from completed grading runs.

Collects one student's results from several ``autograder grade --out``
directories and turns them into a LaTeX report that reads as if a teacher wrote
it. The script owns everything deterministic: scores, problem labels, blank
items, validation, compiling and scrubbing. The prose (a comment per problem,
an overall comment per assignment, and a cover note) is written into ``TODO``
placeholders between ``skeleton`` and ``build``, by a person or an agent.

    skeleton  --runs DIR --student NAME [--assignment NAME ...] --workdir DIR
    check     --workdir DIR [--section SLUG]
    preview   --workdir DIR --section SLUG [--png]
    build     --workdir DIR [--dest DIR] [--png]

Standalone by design: it imports only the standard library, so it runs without
the project installed. Compiling needs ``pdflatex`` (TeX Live with the newpx
fonts) and poppler's ``pdftotext``, ``pdfinfo`` and ``pdftoppm``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

PLACEHOLDER = "TODO"
PLACEHOLDER_RE = re.compile(r"%\s*TODO\b")
SECTIONS_DIR = "sections"
BUILD_DIR = "build"

# Patterns that betray automated grading or AI-flavoured prose. They are hard
# errors in section sources and in the extracted PDF text.
FORBIDDEN: tuple[tuple[str, str, int], ...] = (
    (r"\bOCR\b", "OCR", 0),
    (r"transcri", "transcript", re.I),
    (r"\bA\.?I\b", "AI", 0),
    (r"\bLLM", "LLM", 0),
    (r"automat", "automated", re.I),
    (r"\bconfiden", "confidence", re.I),
    (r"criteri", "criterion", re.I),
    (r"rubric", "rubric", re.I),
    (r"grading notes", "grading notes", re.I),
    (r"official solution", "official solution", re.I),
    (r"scoring (guide|notes)", "scoring guide", re.I),
    (r"pipeline", "pipeline", re.I),
    (r"no work found", "no work found", re.I),
    (r"no response was found", "no response found", re.I),
    (r"\bthe student\b", "third-person 'the student'", re.I),
    (r"\bstudent(?:'|\\textquoteright\{\})s\b", "third-person 'student's'", re.I),
    (r"\bclaude\b|\bgpt\b|chatbot|language model", "AI name", re.I),
    (r"\\u00|\\u20", "unicode escape", 0),
    (r"---|\u2014", "em dash", 0),
    (r"\bc\d\b|\.c\d\b", "criterion id", 0),
    (r"needs[_ ]review", "needs review", re.I),
    (r"great job|delve|it'?s worth noting|crucial", "stock phrasing", re.I),
)
# Words that are usually grader vocabulary but can be legitimate physics prose.
SUSPICIOUS: tuple[tuple[str, str], ...] = (
    (r"\bmodel", "model"),
    (r"\breview", "review"),
    (r"\bstatus", "status"),
    (r"detect", "detect"),
    (r"\bscan", "scan"),
    (r"generat", "generate"),
    (r"\bflag", "flag"),
    (r"\bpage \d", "page reference"),
    (r"highlight", "highlight"),
)
# Rough tags for ordering assignments when none are named: homework first,
# assessments last, natural number order within each kind.
KIND_ORDER: tuple[tuple[str, int], ...] = (
    (r"homework|\bhw\b", 0),
    (r"quiz", 1),
    (r"\blab\b", 2),
    (r"practice", 3),
    (r"test|exam|midterm|final", 4),
)


# ---------------------------------------------------------------------------
# Small helpers


def fmt_points(value: float) -> str:
    """Render a point value the way a teacher writes it: 19, 2.5, 1.25."""
    return format(round(value, 4), "g")


def same(a: float, b: float) -> bool:
    """Equal as printed point values (fmt_points keeps four decimals)."""
    return math.isclose(a, b, abs_tol=5e-4)


def percent(awarded: float, possible: float) -> int:
    """Whole-number percentage, rounding halves up (Python's round() is banker's)."""
    return math.floor(awarded / possible * 100 + 0.5) if possible else 0


def latex_escape(text: str) -> str:
    """Escape text for LaTeX text mode."""
    replacements = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def natural_key(text: str) -> list[object]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text.lower())]


def assignment_sort_key(name: str) -> tuple[int, list[object]]:
    rank = next((r for pattern, r in KIND_ORDER if re.search(pattern, name, re.I)), len(KIND_ORDER))
    return rank, natural_key(name)


def normalize_assignment(name: str) -> str:
    """Canonical form used to match a requested name to a run directory.

    ``HW10``, ``hw 10`` and ``Homework 10`` all become ``homework10``; ``PT2``
    becomes ``practicetest2``.
    """
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    key = re.sub(r"^hw(?=\d)", "homework", key)
    return re.sub(r"^pt(?=\d)", "practicetest", key)


def covers_line(names: list[str]) -> str:
    """Join assignment names, sharing a repeated prefix: 'Homework 10, 11 and Practice Test 2'."""
    groups: list[tuple[str, list[str]]] = []
    for name in names:
        m = re.match(r"^(.*?)\s*(\d+)$", name)
        prefix, number = (m.group(1), m.group(2)) if m else (name, "")
        if number and groups and groups[-1][0] == prefix and groups[-1][1][-1]:
            groups[-1][1].append(number)
        else:
            groups.append((prefix, [number]))
    parts = [f"{prefix} {', '.join(nums)}" if nums[0] else prefix for prefix, nums in groups]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


# ---------------------------------------------------------------------------
# Locating the student and describing the assignment


def name_tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z]+", text.lower()))


def student_name_part(dirname: str) -> str:
    """The name portion of a student directory: text after the last ``_-_``, else all of it."""
    return dirname.rsplit("_-_", 1)[-1]


class AmbiguousStudent(ValueError):
    pass


def find_student_dir(run_dir: Path, student: str) -> Path | None:
    """Find the one student directory in a run whose name contains every token of ``student``.

    Matching on tokens tolerates the naming styles seen in practice:
    ``..._-_Avery_Stone``, ``STONE_AVERY`` and ``..._-_Ada_Avery_Nolan`` for "Ada Nolan".
    """
    wanted = name_tokens(student)
    if not wanted:
        raise ValueError("student name has no letters")
    students = run_dir / "students"
    if not students.is_dir():
        return None
    hits = sorted(d for d in students.iterdir() if (d / "grades.json").is_file()
                  and wanted <= name_tokens(student_name_part(d.name)))
    if len(hits) > 1:
        raise AmbiguousStudent(f"{run_dir.name}: '{student}' matches {', '.join(d.name for d in hits)}")
    return hits[0] if hits else None


def parse_course_term(course_dir_name: str) -> tuple[str, str]:
    """Split a course folder name such as ``Fall 2027 Intro Physics`` into course and term."""
    m = re.match(r"^(Spring|Summer|Fall|Autumn|Winter)\s+(\d{4})\s+(.+)$", course_dir_name.strip(), re.I)
    if m:
        return m.group(3), f"{m.group(1).title()} {m.group(2)}"
    return course_dir_name.strip(), ""


def topic_from_student_id(student_id: str) -> str:
    """The parenthesised topic in ids like '... - Homework 10 (Circular Motion) - Name'."""
    m = re.search(r"\(([^()]+)\)\s*-[^-]*$", student_id)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# The assignment spec: leaves, display labels, problem types


def spec_leaves(problems: object) -> dict[str, dict]:
    """Every gradable leaf of the spec tree, in order, mapped to its node.

    A leaf has no children and is not a container, which mirrors the pipeline's
    ``Problem.is_leaf`` and therefore the keys of ``grades.json``.
    """
    leaves: dict[str, dict] = {}

    def walk(nodes: object) -> None:
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            children = node.get("children") or []
            if children:
                walk(children)
            elif node.get("type") != "container":
                leaves[str(node.get("id"))] = node

    walk(problems)
    return leaves


def label_token(label: str) -> tuple[str, str] | None:
    """Classify one printed label: ('num', '13') for '13.', ('part', 'a') for 'A.' or '(a)'.

    Letters and roman numerals are both 'part': '(i)' renders the same either
    way. Group labels such as 'Questions 4-5' carry no token.
    """
    text = label.strip()
    m = re.fullmatch(r"(\d+)\.?", text)
    if m:
        return "num", m.group(1)
    m = re.fullmatch(r"\(?([A-Za-z]{1,4})[.)]", text)
    if m:
        return "part", m.group(1).lower()
    return None


def fallback_label(pid: str) -> str:
    """Derive a label from an id like '13a.ii' when the spec gives nothing usable."""
    m = re.fullmatch(r"(\d+)([a-z]?)((?:\.[a-z]+)*)", pid)
    if not m:
        return pid
    parts = ([m.group(2)] if m.group(2) else []) + [p for p in m.group(3).split(".") if p]
    return m.group(1) + "".join(f"({p})" for p in parts)


def display_labels(problems: object) -> dict[str, str]:
    """Human labels for every leaf: '1', '9(a)', '13(a)(ii)'.

    A number replaces whatever came before it (so '4.' under 'Questions 4-5'
    is just '4'); a part appends '(x)'.
    """
    labels: dict[str, str] = {}

    def walk(nodes: object, base: str, parts: tuple[str, ...]) -> None:
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            token = label_token(str(node.get("label") or ""))
            nb, np_ = base, parts
            if token and token[0] == "num":
                nb, np_ = token[1], ()
            elif token:
                np_ = (*parts, token[1])
            children = node.get("children") or []
            if children:
                walk(children, nb, np_)
            elif node.get("type") != "container":
                pid = str(node.get("id"))
                labels[pid] = nb + "".join(f"({p})" for p in np_) if nb and token else fallback_label(pid)

    walk(problems, "", ())
    return labels


def is_multiple_choice(node: dict) -> bool:
    return node.get("type") == "multiple_choice"


# ---------------------------------------------------------------------------
# Workdir manifest


@dataclass
class Assignment:
    slug: str
    name: str
    topic: str
    run_dir: str
    student_dir: str
    awarded: float
    possible: float

    @property
    def grades_path(self) -> Path:
        return Path(self.student_dir) / "grades.json"

    @property
    def section_file(self) -> str:
        return f"{SECTIONS_DIR}/{self.slug}.tex"


@dataclass
class Manifest:
    student: str
    course: str
    term: str
    runs: str
    assignments: list[Assignment] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def save(self, workdir: Path) -> None:
        (workdir / "manifest.json").write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, workdir: Path) -> Manifest:
        data = read_json(workdir / "manifest.json")
        data["assignments"] = [Assignment(**a) for a in data["assignments"]]
        return cls(**data)

    def by_slug(self, slug: str) -> Assignment:
        for a in self.assignments:
            if a.slug == slug:
                return a
        raise SystemExit(f"no section '{slug}'; known: {', '.join(a.slug for a in self.assignments)}")


# ---------------------------------------------------------------------------
# LaTeX templates

PREAMBLE = r"""\documentclass[11pt,letterpaper]{article}
\usepackage[T1]{fontenc}
\usepackage{amsmath}
\usepackage{newpxtext,newpxmath}
\usepackage[margin=1in,headheight=15pt]{geometry}
\usepackage{microtype}
\usepackage{xcolor}
\usepackage{booktabs,tabularx,array}
\usepackage{enumitem}
\usepackage{fancyhdr}
\usepackage{lastpage}
\usepackage{needspace}
\usepackage[hidelinks]{hyperref}

\definecolor{pen}{HTML}{B22222}
\definecolor{ink}{HTML}{1F2A44}
\definecolor{soft}{HTML}{5B6475}
\definecolor{rulegray}{HTML}{C9CED6}

\newcommand{\studentname}{@@STUDENT@@}
\newcommand{\coursename}{@@COURSE@@}
\newcommand{\termname}{@@TERM@@}

\hypersetup{
  pdftitle={\coursename{} Grade Report: \studentname},
  pdfauthor={}, pdfsubject={Graded work and feedback}, pdfkeywords={},
  pdfcreator={}, pdfproducer={}
}

\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\small\color{soft}\coursename@@TERMSEP@@}
\fancyhead[R]{\small\color{soft}\studentname}
\fancyfoot[C]{\small\color{soft}Page \thepage\ of \pageref{LastPage}}
\renewcommand{\headrulewidth}{0.4pt}
\renewcommand{\headrule}{\hbox to\headwidth{\color{rulegray}\leaders\hrule height \headrulewidth\hfill}}

\setlength{\parindent}{0pt}
\setlength{\parskip}{4pt}

% Score as written in the margin by the grader.
\newcommand{\score}[2]{\textcolor{pen}{\bfseries #1\,/\,#2}}

% \assignment{Homework 10}{Circular Motion}{awarded}{possible}
\newcommand{\assignment}[4]{%
  \clearpage
  \phantomsection\addcontentsline{toc}{section}{#1}%
  {\color{ink}\noindent{\LARGE\bfseries #1}\par\smallskip
   \noindent{\large #2}\hfill{\Large\score{#3}{#4}}\par}%
  \vspace{2pt}{\color{ink}\hrule height 0.8pt}\vspace{8pt}%
}

% Overall comment at the top of an assignment.
\newenvironment{overall}{%
  \par\noindent\textbf{\color{ink}Overall.}\ \ignorespaces
}{\par\vspace{6pt}{\color{rulegray}\hrule}\vspace{4pt}}

% Group heading inside an assignment (e.g. Multiple Choice).
\newcommand{\groupheading}[1]{%
  \par\needspace{5\baselineskip}\vspace{10pt}%
  \noindent{\color{ink}\large\bfseries #1}\par\vspace{2pt}}

% \begin{problem}{rawid}{display label}{awarded}{possible} ... \end{problem}
% The raw id is not printed; it ties the entry back to grades.json.
\newenvironment{problem}[4]{%
  \par\needspace{4\baselineskip}\addvspace{9pt}%
  \noindent{\bfseries\color{ink}Problem #2}\hfill\score{#3}{#4}\par\nopagebreak[4]\vspace{1pt}%
  \ignorespaces
}{\par}

% One-line entry for an item with no work submitted.
\newcommand{\blankprob}[3]{%
  \par\addvspace{2pt}%
  \noindent{\bfseries\color{ink}Problem #2}\quad{\itshape\color{soft}No work submitted.}\hfill\score{0}{#3}\par}

% Point-by-point breakdown for multi-point items.
\newenvironment{breakdown}{%
  \begin{itemize}[leftmargin=4.2em,labelwidth=3.6em,labelsep=0.6em,itemsep=1pt,topsep=3pt,parsep=0pt,align=left]%
}{\end{itemize}}
\newcommand{\pt}[2]{\item[\textcolor{pen}{\small\bfseries #1/#2}]}
"""

COVER_NOTE_SKELETON = r"""\noindent{\color{ink}\large\bfseries Comments}\par\vspace{4pt}

@@FIRSTNAME@@,

% TODO cover note: two or three paragraphs to the student. Say what the report
% contains, name the strengths and the recurring weak spots across ALL the
% assignments (drawn from the overall comments), and what to work on first.
"""


def render_preamble(m: Manifest) -> str:
    return (PREAMBLE.replace("@@STUDENT@@", latex_escape(m.student))
            .replace("@@COURSE@@", latex_escape(m.course))
            .replace("@@TERM@@", latex_escape(m.term))
            .replace("@@TERMSEP@@", r"\ \textperiodcentered\ \termname" if m.term else ""))


def render_section_skeleton(a: Assignment, grades: dict, spec: dict) -> str:
    leaves = spec_leaves(spec.get("problems"))
    labels = display_labels(spec.get("problems"))
    lines = [
        f"% {a.name}: replace each placeholder comment with prose. Keep the macros and numbers as they are.",
        rf"\assignment{{{latex_escape(a.name)}}}{{{latex_escape(a.topic)}}}"
        rf"{{{fmt_points(a.awarded)}}}{{{fmt_points(a.possible)}}}",
        "",
        r"\begin{overall}",
        f"% {PLACEHOLDER} overall comment",
        r"\end{overall}",
    ]
    problems = grades["problems"]
    kinds = {pid: is_multiple_choice(leaves.get(pid, {})) for pid in problems}
    show_groups = len(set(kinds.values())) > 1
    current: bool | None = None
    for pid, p in problems.items():
        if show_groups and kinds[pid] != current:
            current = kinds[pid]
            lines += ["", rf"\groupheading{{{'Multiple Choice' if current else 'Free Response'}}}"]
        label = latex_escape(labels.get(pid) or fallback_label(pid))
        awarded, possible = fmt_points(p["awarded"]), fmt_points(p["possible"])
        lines.append("")
        if p.get("status") == "blank" and not p["awarded"]:
            lines.append(rf"\blankprob{{{pid}}}{{{label}}}{{{possible}}}")
            continue
        lines += [rf"\begin{{problem}}{{{pid}}}{{{label}}}{{{awarded}}}{{{possible}}}",
                  f"% {PLACEHOLDER} comment"]
        criteria = [c for c in p.get("criteria", []) if c.get("possible")]
        if p["possible"] > 1 and len(criteria) > 1 and _criteria_match(p, criteria):
            lines.append(r"\begin{breakdown}")
            lines += [rf"\pt{{{fmt_points(c['awarded'])}}}{{{fmt_points(c['possible'])}}} % {PLACEHOLDER}"
                      for c in criteria]
            lines.append(r"\end{breakdown}")
        lines.append(r"\end{problem}")
    return "\n".join(lines) + "\n"


def _criteria_match(problem: dict, criteria: list[dict]) -> bool:
    return (same(sum(c["awarded"] for c in criteria), problem["awarded"])
            and same(sum(c["possible"] for c in criteria), problem["possible"]))


def section_header(text: str) -> tuple[str, str, str, str] | None:
    m = re.search(r"\\assignment\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}", strip_comments(text))
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


def render_main(m: Manifest, workdir: Path) -> str:
    rows = []
    for a in m.assignments:
        header = section_header((workdir / a.section_file).read_text(encoding="utf-8"))
        topic = header[1] if header else latex_escape(a.topic)
        rows.append(rf"{latex_escape(a.name)} & {topic} & {fmt_points(a.awarded)} / {fmt_points(a.possible)}"
                    rf" & {percent(a.awarded, a.possible)}\% \\")
    title_line = r"\coursename, \termname" if m.term else r"\coursename"
    sections = "\n".join(rf"\input{{{a.section_file[:-4]}}}" for a in m.assignments)
    return rf"""\input{{preamble}}

\begin{{document}}

\thispagestyle{{fancy}}
{{\color{{ink}}
\noindent{{\Huge\bfseries Grade Report}}\par\vspace{{4pt}}
\noindent{{\Large {title_line}}}\par\vspace{{10pt}}
}}
{{\color{{ink}}\hrule height 0.8pt}}\vspace{{10pt}}

\noindent\begin{{tabularx}}{{\textwidth}}{{@{{}}lX@{{}}}}
\textbf{{Student}} & \studentname \\
\textbf{{Covers}} & {latex_escape(covers_line([a.name for a in m.assignments]))} \\
\end{{tabularx}}

\vspace{{14pt}}
\noindent{{\color{{ink}}\large\bfseries Summary of Scores}}\par\vspace{{6pt}}

\noindent\begin{{tabularx}}{{\textwidth}}{{@{{}}l X r r@{{}}}}
\toprule
\textbf{{Assignment}} & \textbf{{Topic}} & \textbf{{Score}} & \textbf{{Percent}} \\
\midrule
{chr(10).join(rows)}
\bottomrule
\end{{tabularx}}

\vspace{{16pt}}
\input{{cover_note}}

{sections}

\end{{document}}
"""


# ---------------------------------------------------------------------------
# skeleton


def resolve_assignments(runs: Path, requested: list[str]) -> tuple[list[Path], list[str]]:
    run_dirs = [d for d in runs.iterdir() if (d / "students").is_dir()]
    if not requested:
        return sorted(run_dirs, key=lambda d: assignment_sort_key(d.name)), []
    by_key = {normalize_assignment(d.name): d for d in run_dirs}
    found, missing = [], []
    for name in requested:
        d = by_key.get(normalize_assignment(name))
        if d is None:
            missing.append(name)
        else:
            found.append(d)
    return found, missing


def teacher_notes(m: Manifest, details: dict[str, tuple[dict, dict]]) -> str:
    """Items the teacher should look at before the report goes out. Never shown to the student."""
    out = [f"# Teacher notes: {m.student}", "",
           "Internal checklist produced by `scripts/student_report.py skeleton`. Not part of the PDF.", ""]
    if m.missing:
        out += ["## Not found", ""]
        out += [f"- {name}: no run directory, or no folder for this student in it." for name in m.missing]
        out.append("")
    for a in m.assignments:
        grades, rubric = details[a.slug]
        notes_by_id = {str(r.get("problem_id")): str(r.get("grading_notes") or "")
                       for r in rubric.get("problems", []) if isinstance(r, dict)}
        items: list[str] = []
        if not grades.get("score_complete", True):
            items.append("**Score is incomplete:** some problems were not graded. Do not release this section.")
        for pid, p in grades["problems"].items():
            if p.get("processing_status", "complete") != "complete" or p.get("failure"):
                items.append(f"**{pid}: not graded** ({p.get('failure') or p.get('processing_status')}).")
            if p.get("needs_review"):
                items.append(f"{pid} ({fmt_points(p['awarded'])}/{fmt_points(p['possible'])}) was queued for "
                             f"review: {p.get('review_reason') or 'no reason recorded'}")
            if p.get("integrity_flags"):
                items.append(f"{pid} integrity flags: {', '.join(map(str, p['integrity_flags']))}")
            single = len([c for c in p.get("criteria", []) if c.get("possible")]) == 1
            if (p.get("status") == "answered" and p["awarded"] == 0 and p["possible"] > 0 and single
                    and re.search(r"\b0\.5\b|\bhalf\b", notes_by_id.get(pid, ""), re.I)):
                items.append(f"{pid} scored 0/{fmt_points(p['possible'])} as one criterion, but its notes describe "
                             "half-point parts. Check whether one part earned credit.")
        blank = sum(p["possible"] for p in grades["problems"].values() if p.get("status") == "blank")
        if a.possible and blank / a.possible >= 0.5:
            items.append(f"{fmt_points(blank)} of {fmt_points(a.possible)} points are on items recorded as blank. "
                         "Confirm against the submission before sending.")
        out += [f"## {a.name} ({fmt_points(a.awarded)}/{fmt_points(a.possible)})", ""]
        out += [f"- {item}" for item in items] if items else ["- Nothing flagged."]
        out.append("")
    out += ["## Added by the writers", "",
            "- (Record answer-key disagreements, misprinted questions, or suspected misreads here.)", ""]
    return "\n".join(out)


def source_digest(a: Assignment, grades: dict, spec: dict, solutions: dict) -> str:
    """Everything a writer needs for one assignment, problem by problem, in one file."""
    leaves = spec_leaves(spec.get("problems"))
    labels = display_labels(spec.get("problems"))
    run = Path(a.run_dir)
    out = [f"# Source material: {a.name}", "",
           f"- grades.json: `{a.grades_path}`",
           f"- report.md: `{Path(a.student_dir) / 'report.md'}`",
           f"- answer key: `{run / 'solutions_manual.md'}`",
           f"- assignment spec: `{run / 'assignment_spec.json'}`", ""]
    for pid, p in grades["problems"].items():
        node = leaves.get(pid, {})
        key = solutions.get(pid, {}) if isinstance(solutions.get(pid), dict) else {}
        out.append(f"## {pid} (Problem {labels.get(pid, pid)}): {fmt_points(p['awarded'])}/"
                   f"{fmt_points(p['possible'])}, {p.get('status')}, {node.get('type', '?')}")
        if node.get("prompt"):
            prompt = " ".join(str(node["prompt"]).split())
            out.append(f"- Prompt: {prompt[:600]}{'...' if len(prompt) > 600 else ''}")
        if node.get("choices"):
            out.append(f"- Choices: {' | '.join(map(str, node['choices']))}")
        if key.get("final_answer"):
            out.append(f"- Answer key: {key['final_answer']}")
        if p.get("location_note"):
            out.append(f"- What was marked: {p['location_note']}")
        for c in p.get("criteria", []):
            out.append(f"- Point {fmt_points(c['awarded'])}/{fmt_points(c['possible'])}: {c.get('justification', '')}")
        if p.get("feedback"):
            out.append(f"- Grader's comment: {p['feedback']}")
        out.append("")
    return "\n".join(out)


def cmd_skeleton(args: argparse.Namespace) -> int:
    runs = Path(args.runs).resolve()
    workdir = Path(args.workdir).resolve()
    if (workdir / "manifest.json").exists() and not args.force:
        raise SystemExit(f"{workdir} already holds a report; pass --force to overwrite its skeleton")
    run_dirs, missing = resolve_assignments(runs, args.assignment)
    course, term = parse_course_term(runs.parent.name)
    m = Manifest(student=args.student, course=args.course or course, term=args.term if args.term is not None else term,
                 runs=str(runs), missing=list(missing))
    details: dict[str, tuple[dict, dict]] = {}
    sources: dict[str, tuple[dict, dict, dict]] = {}
    for run in run_dirs:
        student_dir = find_student_dir(run, args.student)
        if student_dir is None:
            m.missing.append(run.name)
            continue
        grades = read_json(student_dir / "grades.json")
        spec = read_json(run / "assignment_spec.json")
        rubric = read_json(run / "rubric.json") if (run / "rubric.json").is_file() else {}
        sol_path = run / "solutions_manual.json"
        solutions = read_json(sol_path).get("solutions", {}) if sol_path.is_file() else {}
        a = Assignment(slug=slugify(run.name), name=run.name, topic=topic_from_student_id(grades.get("student_id", "")),
                       run_dir=str(run), student_dir=str(student_dir),
                       awarded=float(grades["total_awarded"]), possible=float(grades["total_possible"]))
        m.assignments.append(a)
        details[a.slug] = (grades, rubric)
        sources[a.slug] = (grades, spec, solutions)
    if not m.assignments:
        raise SystemExit(f"no graded work found for '{args.student}' under {runs}")

    (workdir / SECTIONS_DIR).mkdir(parents=True, exist_ok=True)
    (workdir / "sources").mkdir(exist_ok=True)
    m.save(workdir)
    (workdir / "preamble.tex").write_text(render_preamble(m), encoding="utf-8")
    first = latex_escape(args.student.split()[0]) if args.student.split() else ""
    (workdir / "cover_note.tex").write_text(COVER_NOTE_SKELETON.replace("@@FIRSTNAME@@", first), encoding="utf-8")
    for a in m.assignments:
        grades, spec, solutions = sources[a.slug]
        (workdir / a.section_file).write_text(render_section_skeleton(a, grades, spec), encoding="utf-8")
        (workdir / "sources" / f"{a.slug}.md").write_text(source_digest(a, grades, spec, solutions), encoding="utf-8")
    (workdir / "teacher_notes.md").write_text(teacher_notes(m, details), encoding="utf-8")

    print(f"Report skeleton for {m.student} in {workdir}")
    for a in m.assignments:
        print(f"  {a.slug:<28} {a.name}: {fmt_points(a.awarded)}/{fmt_points(a.possible)}"
              f"  ({len(read_json(a.grades_path)['problems'])} problems)")
    for name in m.missing:
        print(f"  MISSING  {name}")
    print(f"Teacher notes: {workdir / 'teacher_notes.md'}")
    return 0


# ---------------------------------------------------------------------------
# check


def strip_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", text)


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def scan_prose(text: str, findings: Findings, where: str) -> None:
    """Forbidden vocabulary, leftover placeholders and stray characters in a .tex source."""
    for match in PLACEHOLDER_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        findings.errors.append(f"{where}:{line}: unfilled {PLACEHOLDER} placeholder")
    body = strip_comments(text)
    for pattern, label, flags in FORBIDDEN:
        for match in re.finditer(pattern, body, flags):
            line = body.count("\n", 0, match.start()) + 1
            snippet = body[max(0, match.start() - 30): match.end() + 30].replace("\n", " ")
            findings.errors.append(f"{where}:{line}: forbidden ({label}): ...{snippet}...")
    for pattern, label in SUSPICIOUS:
        hits = len(re.findall(pattern, body, re.I))
        if hits:
            findings.warnings.append(f"{where}: check wording ({label}) x{hits}")
    for i, ch in enumerate(text):
        if ord(ch) > 127 and not (ord(ch) <= 0xFF and ch.isalpha()):
            findings.errors.append(f"{where}:{text.count(chr(10), 0, i) + 1}: write {ch!r} as LaTeX")
            break
    no_math = re.sub(r"\$[^$]*\$|\\\[.*?\\\]", "", body, flags=re.S)
    if re.search(r"(?<!\\)[<>]", no_math):
        findings.warnings.append(f"{where}: bare < or > in text mode (renders as inverted punctuation)")
    if '"' in no_math:
        findings.errors.append(f"{where}: straight double quote; write ``...'' instead")


ENTRY = re.compile(
    r"\\begin\{problem\}\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}(.*?)\\end\{problem\}"
    r"|\\blankprob\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}",
    re.S,
)


def check_section(text: str, grades: dict, where: str) -> Findings:
    """Validate one filled section against its grades.json: every problem once, in order, exact points."""
    f = Findings()
    body = strip_comments(text)

    def num(s: str) -> float:
        try:
            return float(s.strip())
        except ValueError:
            f.errors.append(f"{where}: '{s}' is not a number")
            return math.nan

    header = section_header(text)
    if header is None:
        f.errors.append(f"{where}: missing \\assignment header")
    elif not (same(num(header[2]), grades["total_awarded"]) and same(num(header[3]), grades["total_possible"])):
        f.errors.append(f"{where}: total {header[2]}/{header[3]} != grades.json "
                        f"{fmt_points(grades['total_awarded'])}/{fmt_points(grades['total_possible'])}")
    overall = re.search(r"\\begin\{overall\}(.*?)\\end\{overall\}", body, re.S)
    if body.count(r"\begin{overall}") != 1 or not overall or len(overall.group(1).split()) < 15:
        f.errors.append(f"{where}: needs exactly one overall comment of at least a few sentences")

    entries = []
    for mm in ENTRY.finditer(body):
        if mm.group(1) is not None:
            entries.append(("problem", mm.group(1), num(mm.group(3)), num(mm.group(4)), mm.group(5)))
        else:
            entries.append(("blank", mm.group(6), 0.0, num(mm.group(8)), ""))
    ids = [e[1] for e in entries]
    expected = list(grades["problems"])
    if ids != expected:
        missing = [k for k in expected if k not in ids]
        extra = [k for k in ids if k not in expected]
        dup = sorted({k for k in ids if ids.count(k) > 1})
        detail = f"missing={missing} extra={extra} duplicated={dup}" if missing or extra or dup else "order differs"
        f.errors.append(f"{where}: problems do not match grades.json: {detail}")

    for kind, pid, awarded, possible, inner in entries:
        p = grades["problems"].get(pid)
        if p is None:
            continue
        if not (same(awarded, p["awarded"]) and same(possible, p["possible"])):
            f.errors.append(f"{where}: {pid} shows {fmt_points(awarded)}/{fmt_points(possible)}, grades.json has "
                            f"{fmt_points(p['awarded'])}/{fmt_points(p['possible'])}")
        if kind == "blank" and p.get("status") != "blank":
            f.errors.append(f"{where}: {pid} is marked blank but has an answer")
        if kind == "problem" and len(re.sub(r"\\begin\{breakdown\}.*", "", inner, flags=re.S).split()) < 4:
            f.errors.append(f"{where}: {pid} has no comment")
        items = re.findall(r"\\pt\{([^}]*)\}\{([^}]*)\}(.*?)(?=\\pt\{|\\end\{breakdown\})", inner, re.S)
        if items:
            sa, sp = sum(num(a) for a, _, _ in items), sum(num(b) for _, b, _ in items)
            if not (same(sa, p["awarded"]) and same(sp, p["possible"])):
                f.errors.append(f"{where}: {pid} breakdown adds to {fmt_points(sa)}/{fmt_points(sp)}")
            if any(not t.strip() for _, _, t in items):
                f.errors.append(f"{where}: {pid} has a breakdown line with no text")
    scan_prose(text, f, where)
    return f


def run_checks(workdir: Path, m: Manifest, only: str | None = None) -> Findings:
    total = Findings()
    for a in m.assignments:
        if only and a.slug != only:
            continue
        path = workdir / a.section_file
        found = check_section(path.read_text(encoding="utf-8"), read_json(a.grades_path), a.section_file)
        total.errors += found.errors
        total.warnings += found.warnings
    if only is None:
        cover = (workdir / "cover_note.tex").read_text(encoding="utf-8")
        scan_prose(cover, total, "cover_note.tex")
        if len(strip_comments(cover).split()) < 40:
            total.errors.append("cover_note.tex: write the cover note")
    return total


def report(findings: Findings) -> int:
    for w in findings.warnings:
        print(f"WARN  {w}")
    for e in findings.errors:
        print(f"ERROR {e}")
    print(f"{len(findings.errors)} error(s), {len(findings.warnings)} warning(s)")
    return 1 if findings.errors else 0


def cmd_check(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    m = Manifest.load(workdir)
    if args.section:
        m.by_slug(args.section)
    return report(run_checks(workdir, m, args.section))


# ---------------------------------------------------------------------------
# preview / build


def require_tools(*tools: str) -> None:
    absent = [t for t in tools if shutil.which(t) is None]
    if absent:
        raise SystemExit(f"missing tools: {', '.join(absent)} (install TeX Live and poppler-utils)")


def compile_tex(workdir: Path, tex_relpath: str) -> tuple[Path | None, list[str]]:
    """Compile with pdflatex from the workdir, rerunning until references settle.

    Returns the PDF (None on failure) and the notable log lines.
    """
    out = workdir / BUILD_DIR
    out.mkdir(exist_ok=True)
    stem = Path(tex_relpath).stem
    log = out / f"{stem}.log"
    cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", f"-output-directory={BUILD_DIR}", tex_relpath]
    for _ in range(4):
        result = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, errors="replace")
        log_text = log.read_text(errors="replace") if log.exists() else result.stdout
        if result.returncode != 0:
            lines = log_text.splitlines()
            first_error = next((i for i, ln in enumerate(lines) if ln.startswith("!")), None)
            return None, lines[first_error: first_error + 8] if first_error is not None else lines[-20:]
        if "Rerun to get" not in log_text and "undefined references" not in log_text:
            break
    notable = sorted({ln.strip() for ln in log_text.splitlines()
                      if re.search(r"Overfull \\hbox|Undefined control|Missing character", ln)})
    return out / f"{stem}.pdf", notable


def render_pngs(pdf: Path, dest: Path, dpi: int = 80) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob(f"{pdf.stem}-*.png"):
        old.unlink()
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", str(pdf), str(dest / pdf.stem)], check=True)
    return sorted(dest.glob(f"{pdf.stem}-*.png"))


def cmd_preview(args: argparse.Namespace) -> int:
    require_tools("pdflatex")
    workdir = Path(args.workdir).resolve()
    m = Manifest.load(workdir)
    a = m.by_slug(args.section)
    (workdir / "preamble.tex").write_text(render_preamble(m), encoding="utf-8")
    rel = f"{BUILD_DIR}/preview_{a.slug}.tex"
    (workdir / BUILD_DIR).mkdir(exist_ok=True)
    (workdir / rel).write_text(
        f"\\input{{preamble}}\n\\begin{{document}}\n\\input{{{a.section_file[:-4]}}}\n\\end{{document}}\n",
        encoding="utf-8")
    pdf, notable = compile_tex(workdir, rel)
    for line in notable:
        print(line)
    if pdf is None:
        print("COMPILE FAILED")
        return 1
    print(f"OK {pdf}")
    if args.png:
        require_tools("pdftoppm")
        for png in render_pngs(pdf, workdir / BUILD_DIR / "png"):
            print(png)
    return 0


def scrub_pdf(pdf: Path) -> Findings:
    """Re-run the forbidden-vocabulary scan on the text a reader actually sees, plus metadata."""
    f = Findings()
    text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True,
                          check=True).stdout
    for pattern, label, flags in FORBIDDEN:
        for match in re.finditer(pattern, text, flags):
            snippet = text[max(0, match.start() - 30): match.end() + 30].replace("\n", " ")
            f.errors.append(f"PDF text: forbidden ({label}): ...{snippet}...")
    if PLACEHOLDER in text:
        f.errors.append(f"PDF text: contains {PLACEHOLDER}")
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    for key in ("Author", "Creator", "Producer", "Keywords"):
        m = re.search(rf"^{key}:[ \t]*(.*)$", info, re.M)
        if m and m.group(1).strip():
            f.errors.append(f"PDF metadata {key} is '{m.group(1).strip()}'")
    return f


def cmd_build(args: argparse.Namespace) -> int:
    require_tools("pdflatex", "pdftotext", "pdfinfo")
    workdir = Path(args.workdir).resolve()
    m = Manifest.load(workdir)
    if report(run_checks(workdir, m)):
        print("Fix the errors above before building.")
        return 1
    (workdir / "preamble.tex").write_text(render_preamble(m), encoding="utf-8")
    (workdir / "main.tex").write_text(render_main(m, workdir), encoding="utf-8")
    pdf, notable = compile_tex(workdir, "main.tex")
    for line in notable:
        print(line)
    if pdf is None:
        print("COMPILE FAILED")
        return 1
    if report(scrub_pdf(pdf)):
        return 1
    dest = Path(args.dest).resolve() if args.dest else Path(m.runs).parent / "feedback" / m.student
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / f"{m.student} - {m.course} Grade Report.pdf"
    shutil.copyfile(pdf, final)
    source = dest / "source"
    (source / SECTIONS_DIR).mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "preamble.tex", "main.tex", "cover_note.tex", "teacher_notes.md"):
        shutil.copyfile(workdir / name, source / name)
    for a in m.assignments:
        shutil.copyfile(workdir / a.section_file, source / a.section_file)
    pages = re.search(r"^Pages:\s*(\d+)", subprocess.run(["pdfinfo", str(final)], capture_output=True,
                                                          text=True, check=True).stdout, re.M)
    print(f"PDF: {final} ({pages.group(1) if pages else '?'} pages)")
    print(f"Source and teacher notes: {source}")
    if args.png:
        require_tools("pdftoppm")
        for png in render_pngs(pdf, workdir / BUILD_DIR / "png"):
            print(png)
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sk = sub.add_parser("skeleton", help="collect a student's runs and write placeholders to fill")
    sk.add_argument("--runs", required=True, help="the course's runs/ directory")
    sk.add_argument("--student", required=True, help="student name as it should appear, e.g. 'Avery Stone'")
    sk.add_argument("--assignment", action="append", default=[],
                    help="run directory name (repeatable, order kept; HW10/PT2 shorthand works). Default: all.")
    sk.add_argument("--workdir", required=True, help="directory for the report sources")
    sk.add_argument("--course", help="override the course name parsed from the runs/ parent folder")
    sk.add_argument("--term", help="override the term parsed from the runs/ parent folder ('' for none)")
    sk.add_argument("--force", action="store_true", help="overwrite an existing skeleton")
    sk.set_defaults(func=cmd_skeleton)

    ck = sub.add_parser("check", help="validate filled sections against grades.json")
    ck.add_argument("--workdir", required=True)
    ck.add_argument("--section", help="check one section slug only (skips the cover note)")
    ck.set_defaults(func=cmd_check)

    pv = sub.add_parser("preview", help="compile one section on its own")
    pv.add_argument("--workdir", required=True)
    pv.add_argument("--section", required=True)
    pv.add_argument("--png", action="store_true", help="also render PNG pages for a visual check")
    pv.set_defaults(func=cmd_preview)

    bd = sub.add_parser("build", help="check, compile, scrub and deliver the PDF")
    bd.add_argument("--workdir", required=True)
    bd.add_argument("--dest", help="output folder (default: <course>/feedback/<student>/)")
    bd.add_argument("--png", action="store_true", help="also render PNG pages for a visual check")
    bd.set_defaults(func=cmd_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except AmbiguousStudent as exc:
        print(f"ERROR {exc}. Use a fuller name.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
