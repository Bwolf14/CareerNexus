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

> **Upgrading an existing database:** [`init.sql`](init.sql) only runs on a
> *fresh* data volume, and this version adds several tables and columns
> (consent + `first_name`/`last_name`/`is_admin` on `users`, `app_settings`,
> `password_resets`, `auth_throttle`, `saved_jobs`, `scrape_jobs`,
> `saved_searches`) plus `career_plans`. If you already have a `db_data` volume
> from an earlier version, either reset it with `docker compose down -v` (wipes
> stored data) or replay the schema once with
> `docker compose exec db mariadb -uroot -pCareerNexPass32 careernexus_db < init.sql`.
> The app degrades gracefully without the new tables, but the new features
> (tracker, alerts, background scrapes) need them.

| Service  | Image / build  | Port  | Notes                                      |
| -------- | -------------- | ----- | ------------------------------------------ |
| `web`    | `./Dockerfile`        | 8000  | Flask + gunicorn guided flow (healthcheck on `/health`) |
| `admin`  | `./Dockerfile`        | 8001  | separate admin portal (login, users, AI/email settings) |
| `worker` | `./Dockerfile`        | —     | background scrape queue + scheduled job alerts |
| `ollama` | `ollama/ollama:latest`| —     | bundled local model engine (CPU); models in a volume |
| `db`     | `mariadb:10.6`        | 3306  | schema from `init.sql`, data in a volume   |

### Watching logs (real time, over SSH)

All services log to stdout, so from an SSH session on the host:

```bash
cd ~/CareerNexusFork
docker compose logs -f                 # everything, live
docker compose logs -f web admin worker   # just the app services
docker compose logs -f worker          # scrape queue + job alerts only
```

`LOG_LEVEL=DEBUG` (env) turns up verbosity. The worker prints each scrape/alert
it runs; the web/admin apps print AI slot selection, email sends, and errors.

Running the web app outside Docker (for development):

```bash
pip install -r requirements-web.txt
DB_HOST=127.0.0.1 SCRAPE_ASYNC=0 python -m webapp.app    # http://localhost:8000
DB_HOST=127.0.0.1 python -m webapp.admin_app             # http://localhost:8001 (admin)
DB_HOST=127.0.0.1 python -m webapp.worker                # (separate shell) queue + alerts
```

DB connection settings are read from the `DB_HOST`, `DB_PORT`, `DB_NAME`,
`DB_USER`, and `DB_PASSWORD` environment variables (defaults match
`docker-compose.yml`). Outside Docker you can set `SCRAPE_ASYNC=0` to scrape
inline in the web request and skip running the worker.

### Admin portal (separate port)

A separate admin app runs on **<http://localhost:8001>** (the `admin` service).
Default login **`admin` / `admin`**, bootstrapped on first start (change the
`ADMIN_USERNAME` / `ADMIN_PASSWORD` env vars — and promote a real account —
before exposing it). From the portal an admin can:

* see the **total number of registered users** (plus resume/search counts),
* **browse and search all accounts** by name or email, and **promote/demote
  admins** (a promoted user logs into the portal with their own email + password),
* configure the **AI (Ollama)** connection and the **email / SMTP** settings —
  these moved out of the user UI entirely and now live only here (stored in the
  `app_settings` table + AI settings file, read by the web app and worker).

> Because the portal and the user app share a host, they use separate session
> cookies; keep port 8001 firewalled to trusted machines.

### Accounts, tracker, and alerts

* **Accounts** — register with **first name, last name**, email + password (a
  required consent checkbox gates account creation). Forgotten passwords are
  reset via a one-time link; repeated failed logins are rate-limited. Manage or
  delete your account (and export all your data as JSON) at `/account`.
* **Job application tracker** (`/saved`) — hit **Save** on any posting (from
  the matches page or the career plan) to bookmark it. Phases sit on the left
  (*Interested → Applied → Interviewing → Offer / Rejected*); pick one and its
  list slides in. Clicking a job opens its full detail page.
* **Job + company detail pages** (`/job/…`, `/tracker/job/…`) — everything from
  the posting (pay, location, remote, description) instantly, plus a company
  background section that fills in after load: a non-AI **Wikipedia** summary
  when a reliable page exists, an optional AI overview grounded only in the
  collected data, other stored postings from the same company, and a one-click
  live board search for more of that company's jobs.
* **Career plan layout** — top 3 picks on the left (the #1 pick highlighted in
  orange) with compact "why we picked this" lines; a slide-out panel shows the
  full ranked list; resume-alteration suggestions on the right; certifications
  and further tips full-width below.
* **Named resumes** — every upload requires a name, shown everywhere resumes
  are listed, with a slide-out preview of the parsed contents.
* **Light mode** — the ◐ button in the nav toggles light/dark (persists per
  browser).
* **SMS + Discord alerts** — users add their own phone number and/or Discord
  webhook on the account page; an admin configures the Twilio credentials in
  the admin portal (Settings → SMS). Job alerts then fan out to email + SMS +
  Discord.
* **Specific alert criteria** — a saved search can be narrowed to a company
  and/or a title substring (e.g. only *Google* postings containing
  *engineer*), so alerts fire for exactly what you're watching instead of
  every new posting.
* **Bring-your-own internet AI** — a user can enter their own OpenAI or
  Anthropic API key (account page, behind a prominent privacy warning). When
  enabled, THEIR AI requests go to that provider instead of the local models,
  with a hidden pre-prompt disabling extended thinking for fast, high-quality
  answers. Resume data leaves the server when this is on — the user accepts
  responsibility explicitly.
* **Per-model timeouts** — connect + response timeouts are rows in the admin
  options matrix, so each model slot can have its own.
* **Tailor my resume** (`/tailor/...`) — per-posting advice on which of your
  skills to lead with and which keywords/gaps to address (AI-written when an
  Ollama server is configured, deterministic otherwise).
* **Job alerts** (`/alerts`) — save a search to re-run on a daily/weekly
  schedule; the worker emails you any postings that weren't in the previous run.
* **Background scrapes** — searches are queued and run by the `worker`
  container, so the upload/search request returns a progress page instantly
  instead of blocking for the scrape.
* **Cross-board dedup** — the same posting found on Indeed *and* Glassdoor is
  collapsed into one result (tagged "also on …").
* **OCR** — scanned/image PDFs (no embedded text) are run through Tesseract so
  they still parse; degrades to a `"failed"` status if the engine is absent.

### Email + scheduling env vars

| Env var         | Default                 | Purpose                                            |
| --------------- | ----------------------- | -------------------------------------------------- |
| `SCRAPE_ASYNC`  | `1`                     | Queue scrapes for the worker (`0` = run inline)    |
| `APP_BASE_URL`  | `http://localhost:8000` | Base URL for links in reset/alert emails           |
| `SMTP_HOST`     | *(unset)*               | Enable real SMTP; unset → emails printed to the log |
| `SMTP_PORT`     | `587`                   | SMTP port                                          |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | *(unset)* | SMTP auth (login attempted only when set)   |
| `SMTP_FROM`     | `no-reply@careernexus.local` | From: address                                 |
| `WORKER_POLL_SECONDS` | `5`               | How often the worker polls the scrape queue        |
| `WORKER_SCHEDULE_SECONDS` | `60`          | How often the worker checks for due job alerts     |

Without `SMTP_HOST`, password-reset links and alert emails are written to the
container log (`docker compose logs worker` / `web`) so the flows work in the
demo with no mail server.

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

## AI integration (optional, self-hosted, tiered)

Career Nexus talks to **Ollama** models via Ollama's native API. Three features
switch from deterministic to model-generated when a model is available: the
**follow-up questionnaire**, the **career-plan narrative analysis**, and the
per-posting **resume tailoring**. All of it is configured from the **admin
portal** (port 8001) — nothing AI-related is exposed to normal users.

**Tiered model slots.** You configure up to three slots — **primary →
secondary → tertiary**. Before *every* prompt the app connectivity-tests the
slots in order and uses the first that answers (with its model installed). If
none are enabled or reachable, it runs in **safe mode**: template questions,
heuristic-only ranking, deterministic tailoring — exactly as with no AI.

**Per-model options matrix.** Each slot has a matrix of options you toggle in
the admin UI (click the cell where a setting meets a model). Value settings
reveal a box. Every row explains itself and gives a recommendation:

| Setting | Effect |
| ------- | ------ |
| Thinking | **OFF by default** — sends `"think": false` so reasoning models answer directly (faster, cleaner JSON). |
| Temperature | Sampling randomness (default on at 0 for consistent output). |
| Keep model loaded | `keep_alive` — hold the model in RAM N minutes to avoid reload latency. |
| Max response tokens | `num_predict` — cap the reply length. |
| Context window | `num_ctx` — how much text the model reads at once (more RAM). |
| Top-p / Top-k / Seed / Stop | Advanced sampling controls. |

These map straight into the Ollama request (`think`, `keep_alive`, `options`).

**Local models.** The stack bundles an **`ollama`** container so models can run
on the server itself. In the admin portal's Local models section you can browse
a CPU-tuned catalog (with RAM/storage/performance notes), see detected host RAM
(and a warning if a model is too big), **download** a model with a live progress
bar (percent + speed), and **load** it into memory. Tick “Use bundled local
model” on one slot to route it to the local engine. A networked Ollama box
(e.g. a GPU PC on the LAN) still works too — each slot has its own address and a
*Test* button that lists that server's models.

Environment variables seed the **primary** slot at boot (everything else is set
in the admin portal):

| Env var            | Example                       | Purpose                              |
| ------------------ | ----------------------------- | ------------------------------------ |
| `AI_ENABLED`       | `1`                           | Enable the primary slot at boot      |
| `AI_BASE_URL`      | `http://192.168.1.50:11434`   | A networked Ollama server            |
| `AI_MODEL`         | `qwen2.5:3b`                  | Model tag (`ollama list`)            |
| `OLLAMA_LOCAL_URL` | `http://ollama:11434`         | The bundled local Ollama service     |

Questionnaires are cached per search (so answers map to the questions shown);
career-plan analyses are cached and regenerate when answers/shortlist change.
The plan page loads instantly with the heuristic shortlist and an **“AI
thinking…”** banner, then fills in the model's narrative in place (via a
background request) so you can browse while it works.

> **CPU-only note:** the default deployment has no GPU, so the local catalog
> favours small models (0.5B–3B). 7B+ models run but are slow on CPU — the
> catalog says so per model.

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
