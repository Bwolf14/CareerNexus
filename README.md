# Career Nexus

Upload a resume → review the parsed profile → search live job boards → answer
a short questionnaire → get a ranked shortlist with reasoning, resume advice,
and certification gaps. Everything except the AI reasoning runs **today, with
no AI/LLM dependency** — the AI-powered pieces (tailored interview questions,
richer match reasoning) are clearly-labelled placeholders that slot in later.

Subsystems, each usable on its own:

| Package          | What it does                                                        |
| ---------------- | ------------------------------------------------------------------- |
| `resume_parser/` | PDF/DOCX → validated `ParsedResume` JSON (regex/layout heuristics)  |
| `job_scraper/`   | parsed resume → job-board queries → live postings via JobSpy        |
| `job_matcher/`   | heuristic ranking, certification-demand analysis, template questions, resume tips |
| `ai_client/`     | optional: talks to a self-hosted LLM (Ollama etc.) for AI questions + match analysis |
| `webapp/`        | the guided five-step Flask UI wiring it all together                |

The parser is the implementation of the design in
[`resume_parser_blueprint.md`](resume_parser_blueprint.md). Its
output (`schema.py`) is a fixed contract consumed by every downstream
component (profiling chatbot, compatibility scoring, gap analysis, resume
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

The whole stack runs with one command — the guided web flow plus a MariaDB
database everything is written to:

```bash
docker compose up --build
```

Then open <http://localhost:8000>. The flow is behind a login: **register** with
an email + password and tick the box consenting to the collection of your data
(the account can't be created without it). Once signed in, follow the five steps:

1. **Upload** — drop a PDF/DOCX resume; it's parsed with `parse_resume()` and
   stored against your account (full JSON in `user_resumes.parsed_data`, plus
   normalised rows in `users`, `skills`/`user_skills`, `resume_experience`,
   `resume_education`).
2. **Your profile** (`/profile/<id>`) — review the extracted contact info,
   experience timeline, education, skills, certifications, and parser
   warnings; tweak search options (keywords, location, work type, region,
   which job boards to hit).
3. **Job matches** (`/matches/<id>`) — every posting the scrape found, with a
   client-side filter, salary/remote badges, and a live/sample source badge.
4. **Follow-up questions** (`/questions/<id>`) — 4–8 questions built from the
   resume with templates today (the AI interviewer replaces this later):
   "tell me more about project X", "where do you see yourself in 5–10
   years?", pay range, work style, priorities. Every question is optional and
   answers are stored with the search (`career_plans` table, with a JSON-file
   fallback in `job_results/`).
5. **Your career plan** (`/recommendations/<id>`) — the top 5–10 postings
   ranked by a transparent heuristic (skill overlap, title fit, pay-range and
   work-style fit, recency) with per-job reasons and concerns, plus
   **resume-improvement tips** and a **certification-demand analysis**
   ("78% of your matched jobs mention CCNA — consider earning it").

Your previous sessions are listed on the home page (only your own — each account
sees just its own resumes and searches); each keeps its matches, questionnaire
answers, and career plan. The search context + postings are still written to
`job_results/jobs_<id>.json` on the server for the future AI matcher to consume.

No extra setup is required: the `db` service initialises the schema from
[`init.sql`](init.sql) on first run, and the `web` service waits for the DB to
become healthy before serving. Connect a client like DBeaver to
`localhost:3306` (database `careernexus_db`) to browse the stored data.

> **Upgrading an existing database:** the login/consent columns were added to
> `users` in [`init.sql`](init.sql), but `init.sql` only runs on a *fresh* data
> volume. If you already have a `db_data` volume from an earlier version, reset
> it with `docker compose down -v` before `up` to pick up the new schema
> (this wipes stored data). Registration still works without the reset — the
> app falls back gracefully — it just won't persist the consent timestamp.

> **Upgrading an existing checkout:** the schema now includes a
> `career_plans` table. `init.sql` only runs when the DB volume is first
> created, so pre-existing volumes need either `docker compose down -v`
> (wipes stored data) or a one-off
> `docker compose exec db mariadb -uroot -pCareerNexPass32 careernexus_db < init.sql`.
> Without it the app still works — questionnaire answers just fall back to
> JSON files in `job_results/`.

| Service | Image / build      | Port  | Notes                                      |
| ------- | ------------------ | ----- | ------------------------------------------ |
| `web`   | `./Dockerfile`     | 8000  | Flask + gunicorn guided flow (healthcheck on `/health`) |
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
parser except the parsed-resume dict, so it can evolve independently.

A search kicked off from the profile page will:

1. pull **search terms** from the resume (current/recent job titles first,
   then top skills, plus any user keywords) —
   see [`job_scraper/queries.py`](job_scraper/queries.py),
2. scrape matching postings with JobSpy (Indeed + ZipRecruiter + Glassdoor by
   default; LinkedIn can be ticked on per-search in the UI), normalising each
   to the `jobs` table shape — [`job_scraper/scraper.py`](job_scraper/scraper.py),
3. store the run in **`job_searches`** and every posting in **`jobs`** (browsable
   in DBeaver, same database), and
4. write a JSON file to `job_results/jobs_<id>.json` pairing the search context
   with the postings — ready to hand to the AI matcher alongside the resume JSON.

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

## Job matcher (the "AI" that isn't AI yet)

[`job_matcher/`](job_matcher/) powers the questionnaire and career-plan pages
deterministically, so the whole product works end-to-end before any model is
integrated:

- **`scoring.py`** — ranks postings 0–100 against the resume + answers: skill
  overlap (word-boundary matching, so `C++`/`A+` behave), title-token
  alignment, work-style fit, pay-range fit (hourly↔yearly normalised), and
  recency. Every pick carries human-readable `reasons` and `concerns`.
- **`certifications.py`** — a ~45-entry certification dictionary (IT-focused:
  CCNA/CCNP, CompTIA, AWS/Azure/GCP, CISSP…, plus trades, healthcare, finance,
  PM, food service, and more). Scans posting text, reports demand percentages,
  and separates *gaps* from certifications the resume already holds.
- **`questions.py`** — template-generates the 4–8 follow-up questions from the
  resume ("You listed the project **X** — tell me more", 5–10 year goals, pay
  range, work style, preferred skills). Same output shape the AI interviewer
  will produce later.
- **`resume_tips.py`** — actionable resume advice from the parsed structure:
  missing contact info/summary, thin or unquantified bullets, sparse skills,
  ATS-hostile formatting (from parser warnings), and the top certification gap.

When the AI step lands, it replaces the *content* of these outputs (reasons,
question wording, tips) — the plumbing and UI stay as-is.

## AI integration (optional, self-hosted)

Point Career Nexus at an **Ollama** server — typically a GPU box elsewhere on
the LAN — and two features switch from deterministic to model-generated:

- the **follow-up questionnaire** is written by the model after reading the
  resume *and* the matched postings (the structured pay/work-style/skills
  questions that feed the ranking are always kept), and
- the **career plan** gains per-job narrative analysis plus an overall
  market summary, layered on top of the heuristic match signals.

Setup is done from the UI: **AI settings** (`/settings`) takes the server
address, has a *Test connection* button that lists the models installed on
the server, and saves to `job_results/ai_settings.json`. Environment
variables provide boot-time defaults:

| Env var              | Example                       | Purpose                          |
| -------------------- | ----------------------------- | -------------------------------- |
| `AI_ENABLED`         | `1`                           | Turn the AI features on at boot  |
| `AI_BASE_URL`        | `http://192.168.1.50:11434`   | The Ollama/LM Studio/vLLM server |
| `AI_MODEL`           | `qwen3:32b`                   | Model tag (`ollama list`)        |
| `AI_CONNECT_TIMEOUT` | `4`                           | Seconds to detect a dead server  |
| `AI_READ_TIMEOUT`    | `180`                         | Seconds to wait for a generation |

The app speaks the OpenAI-compatible chat API, so anything exposing
`/v1/chat/completions` works (Ollama, LM Studio, vLLM, cloud endpoints).
Failures degrade gracefully: an unreachable or misbehaving server falls back
to template questions and heuristic-only ranking, with the reason shown in
the UI. Generated questionnaires are cached per search so answers always map
to the questions the user actually saw; analyses are cached and regenerate
when the answers or shortlist change (or via the *Regenerate* links).

**Full Windows + Ollama walkthrough** (firewall, `OLLAMA_HOST`, keep-alive,
troubleshooting): [`docs/OLLAMA_SETUP.md`](docs/OLLAMA_SETUP.md).

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
python -m pytest -q          # parser + matcher + web routes (no DB/network needed)
```

- **Parser tests** (`resume_parser/tests/`) drive the field parsers,
  segmenter, and normalizer with synthetic `TextBlock` lists, plus
  integration tests over synthesized representative resumes (single-column
  PDF, two-column/sidebar PDF, DOCX heading-styles, DOCX manual-bold, sidebar
  layout-table DOCX, a no-headers "failed" case, and a scanned-like empty
  PDF). See
  [`tests/sample_resumes/README.md`](resume_parser/tests/sample_resumes/README.md).
- **Matcher tests** (`job_matcher/tests/`) cover scoring/ranking, answer
  handling (pay range, work style), certification gap/held detection with
  word-boundary edge cases, question generation, and resume tips.
- **AI client tests** (`ai_client/tests/`) cover settings persistence and
  URL normalisation, tolerant JSON extraction (code fences, `<think>`
  blocks), and the question/analysis features against a faked model.
- **Web tests** (`webapp/tests/`) exercise the full five-step flow through
  Flask's test client with the DB and scraper stubbed — including the
  degraded paths (DB down, skipped questionnaire, legacy URL redirects) and
  the AI paths (settings page, cached AI questions, analysis fallback).

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
