# Career Nexus Resume Parser — Implementation Blueprint

This document consolidates every design decision made so far into a single
reference for implementation. It assumes `resume_schema.py` and
`intermediate_representation.py` already exist (output contract and
pipeline-internal representation, respectively) and covers everything
between "file on disk" and "validated `ParsedResume` object."

---

## 1. Scope & Constraints

- Inputs: PDF and DOCX resumes.
- Output: a `ParsedResume` object (see `resume_schema.py`), serializable to JSON.
- No AI/LLM dependency anywhere in this pipeline — pure parsing logic
  (regex, font/style heuristics, library-based extraction).
- English-only for v1. Section-keyword matching is exact-match against a
  dictionary (no fuzzy matching).
- Graceful degradation: a resume that can't be segmented still returns a
  valid `ParsedResume` with `raw_text` populated and
  `metadata.section_detection_status = "failed"` — never an exception.
- Python 3.10+ (uses `list[str]` / `dict[str, ...]` builtin generics).

---

## 2. Pipeline Overview

```
File (PDF/DOCX)
   │
   ▼
[1] Format detection & validation
   │
   ▼
[2] Extraction → ExtractedDocument (IDR: list of TextBlocks)
   │
   ▼
[3] Segmentation → blocks grouped by canonical section, per column
   │
   ▼
[4] Entry-boundary detection (experience/education/projects/volunteer)
   │
   ▼
[5] Field-level parsing → populates each schema model
   │
   ▼
[6] Normalization (dates, text cleanup) applied throughout [5]
   │
   ▼
ParsedResume (validated, JSON-serializable)
```

---

## 3. Output Schema — `resume_schema.py`

Already implemented. Key points to remember during implementation:

- Every section list (`experience`, `education`, etc.) is always present,
  even if empty — never omit a key.
- `raw_text` is always populated, regardless of `section_detection_status`.
- Dates are strings in `"YYYY"` or `"YYYY-MM"` format via the shared
  `DateRange` model (`start_date`, `end_date`, `is_current`).
- `additional_sections: dict[str, list[str]]` holds raw lines for any
  heading that didn't match the section dictionary, keyed by the resume's
  own heading text.
- No validators that raise — a field that can't be confidently extracted
  becomes `None`/empty plus a message in `metadata.warnings`.

---

## 4. Intermediate Representation — `intermediate_representation.py`

Already implemented. The extractor's entire job is to produce an
`ExtractedDocument`:

- `blocks: list[TextBlock]` — raw facts only (text, font_size, is_bold,
  is_italic, paragraph_style, is_list_item, indentation_level,
  is_table_cell, order_index, column_index, page_number).
- `column_count` — set by the extractor's column-detection pass.
- `order_index` is reading order **within a column**. Segmentation treats
  each column as an independent sequence.

---

## 5. Stage 1: Format Detection

- Check the file extension AND the magic bytes (`%PDF` for PDF;
  `PK\x03\x04` zip signature + presence of `word/document.xml` for DOCX —
  `python-docx` will raise on a bad file, so a try/except around opening
  it doubles as validation).
- Unsupported/corrupt files → raise a clear, specific exception at this
  stage (this is the one place where failing loudly is correct — there's
  no "partial" result possible for an unreadable file).

---

## 6. Stage 2: Extraction (PDF & DOCX → IDR)

### PDF (PyMuPDF / `fitz`)

- For each page, use `page.get_text("dict")` → iterate blocks → lines →
  spans. Each span gives text, font name, size, flag bits, and bounding
  box (`x0, y0, x1, y1`).
- **Bold/italic detection**: check both the flag bits *and* whether the
  font name contains "Bold"/"Italic" — flag bits are inconsistently set
  depending on the PDF generator, font name substrings are more reliable
  in practice.
- **Column detection**: cluster lines by `x0` start position across the
  page. If two or more distinct `x0` clusters exist with a significant gap
  between them (e.g. >50pt), treat as separate columns; assign
  `column_index` per cluster. Single cluster → `column_count = 1`.
- **Reading order**: within each column, sort by `y0` (top to bottom),
  then `x0` for ties, to produce `order_index`. Across multiple pages,
  continue each column's sequence onto the next page (column 0 of page 1
  → column 0 of page 2, etc.) — validate this assumption against real
  two-page resumes during testing.
- **`indentation_level`**: bucket `x0` relative to the column's minimum
  `x0` (e.g. level 0 = within a few points of the column's left edge,
  level 1 = one bullet-indent further right, etc.).
- **`is_list_item`**: line text starts with a common bullet glyph (`•`,
  `◦`, `▪`, `‣`, `-`, `*`, `→`) — strip the glyph from `text` and set the
  flag.
- **Tables**: run `pdfplumber`'s `extract_tables()` separately to get table
  bounding regions; spans falling inside those regions get
  `is_table_cell=True`. Flatten in row-major order.
- **Scanned PDFs**: if total extracted text across the whole document is
  near-empty, this is a scanned/image PDF — out of scope for v1 (see §14).
  Return `section_detection_status="failed"` with a warning rather than
  attempting OCR.

### DOCX (`python-docx`)

- Iterate `document.paragraphs` plus cells from `document.tables`.
- **`paragraph_style`**: `paragraph.style.name` (e.g. `"Heading 1"`,
  `"Normal"`, `"List Bullet"`) — this is the single strongest signal DOCX
  gives you that PDF doesn't.
- **`is_bold`/`is_italic`**: check the paragraph's runs; if the first/
  majority run has `run.bold`/`run.italic` set, use that.
- **`font_size`**: `run.font.size.pt` if explicitly set on the run; `None`
  if it's inherited from the style (don't try to resolve the style's
  default — leave it `None` and let the segmenter's baseline computation
  handle missing values).
- **`is_list_item`**: style name contains "List" (List Bullet, List
  Number, List Paragraph), or inspect `paragraph._p.pPr.numPr` via the
  underlying XML for numbering properties.
- **`indentation_level`**: derive from `paragraph.paragraph_format.
  left_indent` (bucket into levels by dividing by a standard indent unit,
  e.g. 0.25") or from `numPr/ilvl` if it's a list item.
- **Columns**: true multi-column DOCX sections are rare for resumes; the
  more common case is a layout table (sidebar template). If a table spans
  most of the page width and has 2 cells per row, treat each cell column
  as a separate `column_index`. This is heuristic — flag as a known
  limitation (§14) and revisit if real samples show it's common.
- `page_number` is always `0` for DOCX.

---

## 7. Stage 3: Segmentation

1. **Compute body-text baseline**: the most common `font_size` across all
   blocks (ignoring `None`). If most blocks have `font_size=None` (common
   for DOCX with no explicit sizes), fall back to treating
   `paragraph_style == "Normal"` as the baseline indicator instead.

2. **Flag heading candidates**, per block:
   - DOCX: `paragraph_style` is one of Word's Heading/Title styles, **or**
   - Generic: `font_size` is meaningfully larger than baseline (e.g. ≥15%
     larger), **or** (`is_bold` and `text.isupper()` and word count ≤ 5)

3. **Match against the section dictionary** (`section_dictionary.yaml`,
   see §9 below): normalize candidate text (lowercase, strip punctuation/
   whitespace) and look up. A match assigns that heading — and every
   subsequent block in that column until the next recognized heading — to
   the corresponding canonical section.
   - If the matched canonical key is `contact_info` or `summary`, route
     those blocks to the contact/summary field-parsers instead of a list
     section.
   - If a heading candidate's normalized text has **no** dictionary match,
     create an `additional_sections[<original heading text>]` entry and
     assign subsequent blocks (until the next heading) to it as raw text
     lines.

4. **Header zone**: all blocks before the first recognized heading (in
   column 0). The block with the largest `font_size` in this zone is
   `contact_info.name`. Any remaining prose block with no list formatting
   becomes `summary` (if no explicit Summary/Objective heading was found
   elsewhere).

5. **Set `section_detection_status`**:
   - `"success"` — at least one of {experience, education} matched, plus
     at least one additional canonical section.
   - `"partial"` — some heading candidates were found and matched, but
     fewer than the above.
   - `"failed"` — zero heading candidates detected anywhere in the
     document. All section lists stay empty; `raw_text` is the only
     populated field besides `metadata`.

---

## 8. Stage 4: Entry-Boundary Detection

Applies to `experience`, `education`, `projects`, `volunteer_experience` —
sections that contain multiple repeated entries.

**Grouping algorithm** (per section, per column):

```
entries = []
current = []

for block in section_blocks:
    if is_entry_start(block, body_baseline) and current:
        entries.append(current)
        current = []
    current.append(block)

if current:
    entries.append(current)

def is_entry_start(block, body_baseline):
    at_body_size = (block.font_size is None
                    or abs(block.font_size - body_baseline) < TOLERANCE)
    return (
        block.indentation_level == 0
        and not block.is_list_item
        and at_body_size
        and (block.is_bold or DATE_RANGE_PATTERN.search(block.text))
    )
```

**Within each entry group**: the leading run of `indentation_level == 0`,
non-list blocks form the "header"; subsequent `is_list_item=True` blocks
become `description` (list of strings, one per bullet).

**Header parsing**:
1. Concatenate header blocks into one string.
2. Extract the date range first via regex (§10) — this is the most
   reliably-patterned piece — and remove it from the string. Feeds
   `dates: DateRange`.
3. Split the remainder on common separators (`—`, `-`, `|`, `,`, `(`/`)`,
   newline-joins) into 2–3 candidate segments.
4. Assign segments to `title`/`company` (experience) or
   `degree`/`institution`/`field_of_study` (education) using keyword
   lists:
   - Education: a small closed set of degree keywords (Bachelor, Diploma,
     Associate, Master, Certificate, B.Sc, B.A., Diploma, etc.) reliably
     identifies the `degree` segment. Institution names containing
     "University"/"College"/"Institute" help confirm `institution`;
     whatever's left is `field_of_study`.
   - Experience: **be honest that this is the weakest part of a purely
     rule-based parser.** Title-vs-company ordering varies by template
     with no universal convention. Use a keyword list of common job-title
     words (Technician, Engineer, Analyst, Manager, Specialist, Developer,
     Coordinator, Administrator, Labourer, etc.) and common company-name
     suffixes (Inc., LLC, Ltd., Corp., Co.) to bias the assignment; default
     to "first segment = title, second = company" (the more common North
     American convention) when neither heuristic matches. **Always log a
     `warnings` entry when the assignment was a default guess** rather
     than a keyword-confirmed match — this is exactly the kind of signal
     the resume-guidance feature can surface later ("we weren't confident
     about job title vs. company for entry 3").

---

## 9. Section Dictionary — `section_dictionary.yaml`

A standalone config file (see file alongside this document). Loaded once
at parser startup. Structure: canonical section name → list of
normalized-form synonyms. Adding a new synonym later is a one-line config
change, no code change.

---

## 10. Stage 5/6: Field-Level Parsing & Normalization

| Section | Approach |
|---|---|
| `contact_info` | Regex over header-zone text: email `[\w.+-]+@[\w-]+\.[\w.-]+`; phone allowing `(403) 555-0123`, `403-555-0123`, `+1 403 555 0123` formats; `linkedin\.com/in/\S+`, `github\.com/\S+` for links; location via a `City, Province/State` pattern (two title-case words/phrase + comma + 2-letter or full province/state name) — low confidence, log a warning if not found. |
| `summary` | Concatenate prose blocks in the matched Summary/Objective section (or header-zone fallback) into one string. |
| `experience` / `volunteer_experience` | Entry-boundary grouping (§8) → header parsing → `description` from bullet blocks. |
| `education` | Entry-boundary grouping (§8) → header parsing using degree keyword list. GPA via regex `GPA[:\s]*([\d.]+)(\s*/\s*[\d.]+)?` or `\d\.\d{1,2}\s*/\s*4\.0`. |
| `skills` | If the section is one long delimiter-separated block (`,`, `;`, `|`), split into `raw`. If lines follow `Category: item, item, item`, populate `categorized[Category]` *and* flatten into `raw`. If lines are one-skill-per-bullet, each is a `raw` entry. |
| `certifications` | One entry per line/block typically: `Name — Issuer (Date)` or `Name, Issuer, Date`. Extract a single date (not a range) via the date regex → `date_earned`. Look for `Expires`/`Valid until`/`Exp.` keyword phrases → `expiration_date`. Remainder split on separators for `name`/`issuer`. |
| `projects` | Entry-boundary grouping (§8), but header is often just a title with no date/company pattern — treat any block at `indentation_level == 0` after a list-item run as a new entry regardless of bold/date match. Lines starting with `Technologies:`/`Tech stack:`/`Built with:` → split into `technologies`. URL via `https?://\S+` or `github\.com/\S+`. |
| `additional_sections` | No parsing — raw line strings as extracted, keyed by original heading text. |

### Date Normalization (used throughout §8 and the table above)

- Patterns to match, in order: `Mon[a-z]* \d{4}` (e.g. "Jan 2020",
  "January 2020"), `\d{1,2}/\d{4}`, `\d{4}` (year only).
- Range separators: `-`, `–`, `—`, `to`, `until` (case-insensitive).
- `Present`/`Current`/`Now`/`Ongoing` (case-insensitive) → `is_current =
  True`, `end_date = None`.
- Use `python-dateutil`'s parser for the month-name component (handles
  abbreviations and full names), then format to `"YYYY-MM"`; if only a
  year was present, format as `"YYYY"`.
- Text cleanup applied to every extracted string before it lands in the
  schema: collapse repeated whitespace, strip stray bullet glyphs left
  over from PDF extraction, rejoin words split by line-break hyphenation
  (`compu-\nter` → `computer`).

---

## 11. Module Structure & Entry Point

```
resume_parser/
├── __init__.py
├── schema.py                      # resume_schema.py
├── intermediate_representation.py
├── config/
│   └── section_dictionary.yaml
├── extractors/
│   ├── __init__.py
│   ├── base.py                    # shared interface / Protocol
│   ├── pdf_extractor.py
│   └── docx_extractor.py
├── segmenter.py
├── field_parsers/
│   ├── __init__.py
│   ├── contact.py
│   ├── experience.py
│   ├── education.py
│   ├── skills.py
│   ├── certifications.py
│   ├── projects.py
│   └── volunteer.py
├── normalizer.py                  # date parsing, text cleanup
├── parser.py                      # top-level entry point
└── tests/
    ├── sample_resumes/
    └── test_parser.py
```

**Entry point** (`parser.py`):

```python
def parse_resume(filepath: str) -> ParsedResume:
    fmt = detect_format(filepath)               # §5
    doc = extract(filepath, fmt)                 # §6 → ExtractedDocument
    sections = segment(doc)                      # §7
    resume = build_parsed_resume(sections, doc)  # §8–10
    return resume
```

This is the single function CLI/API/import wrappers all call — no other
design decision depends on which of those you eventually build.

---

## 12. Dependencies (`requirements.txt`)

See file alongside this document. Core: `pydantic`, `pymupdf`,
`pdfplumber`, `python-docx`, `python-dateutil`, `pyyaml`.

---

## 13. Testing Strategy

- **Unit tests** (`field_parsers/`, `normalizer.py`): synthetic
  `TextBlock` lists as input — no real files needed. Cover date format
  variants, title/company ordering both ways, skills in all three layouts,
  GPA formats, etc.
- **Integration tests**: a small corpus (aim for ~10) of real/representative
  resumes covering: single-column PDF, two-column/sidebar PDF, DOCX using
  Word heading styles, DOCX using manual bold formatting only, and at
  least one resume with no detectable section headers (to exercise the
  `"failed"` status path). Assert schema validity + spot-check key fields
  per sample — don't assert exact output, since formatting varies.
- No AI evaluation needed at this layer — that's the next pipeline stage's
  job, and it consumes this parser's output as a fixed input.

---

## 14. Known Limitations & V2 Backlog

- **Scanned/image PDFs**: no extractable text → `"failed"` status, no OCR
  in v1. Add `pytesseract` fallback later if this proves common.
- **Heavy table-based layouts**: both PDF and DOCX table flattening are
  heuristic; templates that use tables for the *entire* layout (not just a
  sidebar or skills grid) may segment poorly.
- **Title vs. company ordering**: heuristic + default convention, not
  guaranteed — always check `metadata.warnings` for low-confidence
  assignments.
- **Location extraction**: regex-pattern based, no gazetteer — will miss
  unusual city names or non-"City, Province" formats.
- **English-only**: section dictionary and date-month-name parsing assume
  English. Adding a language means adding another dictionary file and
  passing a locale to `dateutil`/`dateparser` — the architecture doesn't
  need to change, just the config.
- **Multi-page column continuity**: the "continue each column's sequence
  across pages" assumption (§6) needs validation against real two-page
  resumes — if templates switch layout between pages, this could
  misorder content.
