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

## Web UI + database (Docker)

The whole stack runs with one command — a browser UI for dropping resumes in,
and a MariaDB database that the parsed results are written to:

```bash
docker compose up --build
```

Then open <http://localhost:8000>. Drop a PDF/DOCX onto the page and it will be:

1. parsed with `parse_resume()`,
2. stored in the database (full JSON in `user_resumes.parsed_data`, plus
   normalised rows in `users`, `skills`/`user_skills`, `resume_experience`,
   and `resume_education`), and
3. offered back as a downloadable JSON file (also re-downloadable later from the
   list on the home page, or directly at `/download/<id>`).

No extra setup is required: the `db` service initialises the schema from
[`init.sql`](init.sql) on first run, and the `web` service waits for the DB to
become healthy before serving. Connect a client like DBeaver to
`localhost:3306` (database `careernexus_db`) to browse the stored data.

| Service | Image / build      | Port  | Notes                                      |
| ------- | ------------------ | ----- | ------------------------------------------ |
| `web`   | `./Dockerfile`     | 8000  | Flask + gunicorn upload UI                 |
| `db`    | `mariadb:10.6`     | 3306  | schema from `init.sql`, data in a volume   |

Running the web app outside Docker (for development):

```bash
pip install -r requirements-web.txt
DB_HOST=127.0.0.1 python -m webapp.app    # http://localhost:8000
```

DB connection settings are read from the `DB_HOST`, `DB_PORT`, `DB_NAME`,
`DB_USER`, and `DB_PASSWORD` environment variables (defaults match
`docker-compose.yml`).

## Job Finder (job scraping)

A separate subsystem ([`job_scraper/`](job_scraper/)) turns a parsed resume into
job-board searches and pulls live postings via
[JobSpy](https://github.com/speedyapply/JobSpy). It shares nothing with the
parser except the parsed-resume dict, so it can evolve independently — and later
feed an AI matching step that ranks postings against the resume and suggests
certifications to close gaps.

Open <http://localhost:8000/jobs>, drop a resume, and it will:

1. parse the resume and pull **search terms** from it (current/recent job titles
   first, then top skills) — see [`job_scraper/queries.py`](job_scraper/queries.py),
2. scrape matching postings with JobSpy (Indeed + ZipRecruiter + Glassdoor by
   default), normalising each to the `jobs` table shape —
   [`job_scraper/scraper.py`](job_scraper/scraper.py),
3. store the run in **`job_searches`** and every posting in **`jobs`** (browsable
   in DBeaver, same database), and
4. write a JSON file to `job_results/jobs_<id>.json` pairing the search context
   with the postings — ready to hand to the AI matcher alongside the resume JSON
   (also downloadable from the results page or at `/jobs/download/<id>`).

If JobSpy is unavailable, the network is blocked, or a search returns nothing,
the page falls back to clearly-labelled **sample** postings so the demo still
works; the storage path is identical to a live run.

| Env var                 | Default        | Purpose                                            |
| ----------------------- | -------------- | -------------------------------------------------- |
| `JOB_SITES`             | `indeed,zip_recruiter,glassdoor` | Comma-separated boards (set to `indeed` for a faster demo) |
| `JOB_COUNTRY`           | `Canada`       | Which country's Indeed site to search              |
| `JOB_RESULTS_PER_QUERY` | `15`           | Postings requested per search term                 |
| `JOB_HOURS_OLD`         | `168`          | Only postings newer than this many hours           |
| `JOB_RESULTS_DIR`       | `./job_results`| Where the JSON result files are written            |

> **Note on `JOB_COUNTRY`:** JobSpy defaults Indeed to the US site, so a search
> for a Canadian location (e.g. "Calgary, Alberta") returns nothing unless this
> is set. It's `Canada` by default for this project; set it to `USA` (etc.) for
> other regions.

> **Note on boards:** Indeed, ZipRecruiter, and Glassdoor are the default trio —
> all proxy-free and good for Canada/US. The same posting can appear on more than
> one board (each is listed separately, tagged by `source_site`). Two boards are
> *not* enabled by default: **LinkedIn** rate-limits hard and needs rotating
> proxies, and **Google Jobs** needs a separate `google_search_term` query
> format. ZipRecruiter only covers the US/Canada.

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
- `metadata.extraction_confidence` is a transparent `0.0`–`1.0` estimate of how
  completely the resume's structure was recovered (a triage signal for
  downstream consumers, not a calibrated probability): half from the
  section-detection status, half from which core fields were extracted.
- Skills are **canonicalized**: common variants (`JS`/`Javascript`,
  `python 3`/`Python`, `nodejs`/`node.js`) collapse to one form so the same
  skill doesn't appear as multiple entries; compound names like `C++`,
  `CI/CD`, `TCP/IP` are preserved as-is.

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
