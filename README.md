# Career Nexus Resume Parser

Parse **PDF** and **DOCX** resumes into a validated, JSON-serializable
`ParsedResume` object — pure parsing logic (regex, font/style heuristics,
library-based extraction), **no AI/LLM dependency**.

This is the implementation of the design in
[`resume_parser_blueprint.md`](resume_parser_blueprint.md). The parser's
output (`schema.py`) is a fixed contract consumed by downstream Career Nexus
components (profiling chatbot, compatibility scoring, gap analysis, resume
guidance, cover-letter assistance).

## Install

```bash
pip install -r requirements.txt          # runtime
pip install -r requirements-dev.txt      # + pytest, for development
```

Requires Python 3.10+.

## Usage

```python
from resume_parser import parse_resume

resume = parse_resume("alex_smith_resume.pdf")
print(resume.contact_info.name)
print(resume.model_dump_json(indent=2))   # JSON-serializable output
```

Command line:

```bash
python -m resume_parser path/to/resume.pdf            # JSON to stdout
python -m resume_parser path/to/resume.docx -o out.json
```

## Output shape

`parse_resume` always returns a valid `ParsedResume`. Key guarantees
(blueprint §3):

- Every section list (`experience`, `education`, …) is **always present**,
  even when empty.
- `raw_text` is **always populated** — the guaranteed fallback, and the only
  reliable field when section detection fails.
- Dates are strings (`"YYYY"` / `"YYYY-MM"`); `is_current` is a separate flag.
- Low-confidence / missing fields become `None`/empty plus a message in
  `metadata.warnings` — the parser **never raises** on a readable file.
- `metadata.section_detection_status` is `"success"`, `"partial"`, or
  `"failed"`.

The one place that fails loudly is **format detection**: an unsupported or
corrupt file raises `UnsupportedFormatError` / `CorruptFileError`, because
there is no meaningful partial result for a file we can't read.

## Pipeline

```
File (PDF/DOCX)
  └─[1] detect_format        extension + magic bytes            parser.py
  └─[2] extract              -> ExtractedDocument (IDR)         extractors/
  └─[3] segment              blocks -> canonical sections       segmenter.py
  └─[4] entry boundaries     experience/education/projects/vol  field_parsers/common.py
  └─[5] field parsing        -> schema models                   field_parsers/
  └─[6] normalization        dates + text cleanup (throughout)  normalizer.py
ParsedResume (validated, JSON-serializable)
```

### Module layout

```
resume_parser/
├── schema.py                      # output contract (ParsedResume)
├── intermediate_representation.py # IDR: column-aware list of TextBlocks
├── config/section_dictionary.yaml # canonical section -> header synonyms
├── extractors/                    # pdf_extractor.py, docx_extractor.py
├── segmenter.py                   # headings, sections, columns, status
├── field_parsers/                 # contact/experience/education/skills/...
├── normalizer.py                  # date parsing + text cleanup
└── parser.py                      # parse_resume() entry point
```

The section dictionary is plain config: adding a header synonym is a
one-line change, no code edit.

## Tests

```bash
python -m pytest resume_parser/tests/ -q
```

- **Unit tests** drive the field parsers, segmenter, and normalizer with
  synthetic `TextBlock` lists — no real files needed.
- **Integration tests** synthesize representative resumes (single-column PDF,
  two-column/sidebar PDF, DOCX heading-styles, DOCX manual-bold, sidebar
  layout-table DOCX, a no-headers "failed" case, and a scanned-like empty
  PDF), assert schema validity, and spot-check key fields. See
  [`tests/sample_resumes/README.md`](resume_parser/tests/sample_resumes/README.md).

## Known limitations (v2 backlog)

Carried over from the blueprint (§14), and honest about what a rule-based
parser can't guarantee:

- **Scanned/image PDFs**: no extractable text -> `"failed"` status, no OCR
  in v1.
- **Title vs. company ordering**: heuristic + North-American default; the
  assignment is flagged in `metadata.warnings` whenever it was a guess.
- **Heavy table-based layouts**: PDF/DOCX table flattening is heuristic;
  full-page table templates may segment poorly.
- **Location extraction**: regex-pattern based (no gazetteer) — misses
  unusual city names or non-"City, Province/State" formats.
- **English-only**: section dictionary and month-name parsing assume English.
- **Multi-page column continuity**: each column's reading order is continued
  across pages; templates that switch layout between pages may misorder.
