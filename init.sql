CREATE DATABASE IF NOT EXISTS careernexus_db;
USE careernexus_db;
CREATE USER IF NOT EXISTS 'career_app_user'@'%' IDENTIFIED BY 'dbSecur3d';
GRANT SELECT, INSERT, UPDATE, DELETE ON careernexus_db.* TO 'career_app_user'@'%';
FLUSH PRIVILEGES;

-- ---------------------------------------------------------------------------
-- Core lookup + identity tables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS skills (
    id INT AUTO_INCREMENT PRIMARY KEY,
    skill_name VARCHAR(100) UNIQUE NOT NULL
);

-- Registered accounts. Sign-up requires an email + password and explicit
-- consent to sensitive-data collection (see consent_data_collection): the app
-- refuses to create an account without it. Older, resume-synthesised identities
-- (pre-login) carry password_hash='imported' and consent_data_collection=FALSE.
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    consent_data_collection BOOLEAN NOT NULL DEFAULT FALSE,
    consent_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Resume side (parsed by resume_parser)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_resumes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    parsed_data JSON,
    upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_skills (
    user_id INT,
    skill_id INT,
    PRIMARY KEY (user_id, skill_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
);

-- Normalised view of a parsed resume's work history. The full parsed document
-- is always stored as JSON in user_resumes.parsed_data; these tables make the
-- key sections queryable without unpacking JSON.
CREATE TABLE IF NOT EXISTS resume_experience (
    id INT AUTO_INCREMENT PRIMARY KEY,
    resume_id INT NOT NULL,
    company VARCHAR(255),
    title VARCHAR(255),
    location VARCHAR(255),
    start_date VARCHAR(20),
    end_date VARCHAR(20),
    is_current BOOLEAN DEFAULT FALSE,
    description TEXT,
    FOREIGN KEY (resume_id) REFERENCES user_resumes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resume_education (
    id INT AUTO_INCREMENT PRIMARY KEY,
    resume_id INT NOT NULL,
    institution VARCHAR(255),
    degree VARCHAR(255),
    field_of_study VARCHAR(255),
    start_date VARCHAR(20),
    end_date VARCHAR(20),
    is_current BOOLEAN DEFAULT FALSE,
    gpa VARCHAR(20),
    FOREIGN KEY (resume_id) REFERENCES user_resumes(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Job side (scraped by job_scraper via JobSpy)
-- ---------------------------------------------------------------------------
-- One row per scrape run. A run is normally kicked off by uploading a resume,
-- so resume_id/user_id link the jobs back to the person they were found for.
-- Both are nullable so an anonymous / ad-hoc scrape is still recordable.
CREATE TABLE IF NOT EXISTS job_searches (
    id INT AUTO_INCREMENT PRIMARY KEY,
    resume_id INT,
    user_id INT,
    search_terms VARCHAR(512),          -- comma-joined terms derived from the resume
    location VARCHAR(255),
    sites_searched VARCHAR(255),        -- comma-joined, e.g. "indeed,zip_recruiter"
    source VARCHAR(20) DEFAULT 'jobspy',-- 'jobspy' (live) or 'sample' (fallback)
    results_count INT DEFAULT 0,
    ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (resume_id) REFERENCES user_resumes(id) ON DELETE SET NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

-- Scraped job postings. Extends the original minimal jobs table with the
-- fields JobSpy returns so the data is fully queryable in DBeaver. A posting is
-- stored per-search (UNIQUE on search_id + source/external id) so each run keeps
-- its own self-contained result set, even when the same posting appears twice.
CREATE TABLE IF NOT EXISTS jobs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    search_id INT,
    source_site VARCHAR(50),            -- indeed, zip_recruiter, linkedin, ...
    external_id VARCHAR(255),           -- the job board's own id (or a url hash)
    title VARCHAR(255),
    company VARCHAR(255),
    location VARCHAR(255),
    job_type VARCHAR(100),              -- fulltime, contract, internship, ...
    is_remote BOOLEAN DEFAULT FALSE,
    salary_min DECIMAL(12, 2),
    salary_max DECIMAL(12, 2),
    salary_currency VARCHAR(10),
    salary_interval VARCHAR(20),        -- yearly, hourly, ...
    description LONGTEXT,
    link TEXT,                          -- job_url
    search_term VARCHAR(255),           -- which query surfaced this posting
    date_posted DATE,
    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    posted_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_job_per_search (search_id, source_site, external_id),
    FOREIGN KEY (search_id) REFERENCES job_searches(id) ON DELETE CASCADE
);

-- One row per completed follow-up questionnaire. The answers JSON is keyed by
-- question id (see job_matcher/questions.py) and is re-fed into the heuristic
-- ranking today; later it becomes part of the AI matcher's context. One plan
-- per search (re-answering overwrites).
CREATE TABLE IF NOT EXISTS career_plans (
    id INT AUTO_INCREMENT PRIMARY KEY,
    search_id INT NOT NULL,
    resume_id INT,
    answers JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_plan_per_search (search_id),
    FOREIGN KEY (search_id) REFERENCES job_searches(id) ON DELETE CASCADE,
    FOREIGN KEY (resume_id) REFERENCES user_resumes(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS job_skills (
    job_id INT,
    skill_id INT,
    PRIMARY KEY (job_id, skill_id),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
    FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Accounts: password resets + brute-force throttle
-- ---------------------------------------------------------------------------
-- Single-use, time-limited password-reset tokens. Only the SHA-256 hash of the
-- token is stored, so a database leak doesn't hand out working reset links.
CREATE TABLE IF NOT EXISTS password_resets (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    token_hash CHAR(64) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_reset_token (token_hash),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Login throttle: one row per identifier (email / client ip). fail_count is
-- reset on a successful login; locked_until stops further attempts after too
-- many failures. Keyed small so it stays cheap to check on every login.
CREATE TABLE IF NOT EXISTS auth_throttle (
    identifier VARCHAR(255) PRIMARY KEY,
    fail_count INT NOT NULL DEFAULT 0,
    locked_until TIMESTAMP NULL,
    last_attempt TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Saved jobs + application tracker
-- ---------------------------------------------------------------------------
-- A posting a user bookmarked, with its application status. The posting fields
-- are snapshotted (not a FK to jobs) so a saved job survives its search being
-- deleted. UNIQUE(user, dedup_key) keeps the same posting from being saved
-- twice; dedup_key is a normalised title|company|location hash.
CREATE TABLE IF NOT EXISTS saved_jobs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    dedup_key CHAR(40) NOT NULL,
    title VARCHAR(255),
    company VARCHAR(255),
    location VARCHAR(255),
    source_site VARCHAR(50),
    salary_display VARCHAR(120),
    is_remote BOOLEAN DEFAULT FALSE,
    job_url TEXT,
    description LONGTEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'interested',  -- interested/applied/interviewing/offer/rejected
    notes TEXT,
    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_saved_per_user (user_id, dedup_key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Background scrape queue (processed by the worker container)
-- ---------------------------------------------------------------------------
-- One row per queued/async scrape. The web app inserts a 'pending' row and
-- returns immediately; the worker claims it (pending -> running), runs the
-- scrape, stores the results as a normal job_searches row, and records the
-- resulting search_id here. saved_search_id is set when a scheduled alert
-- enqueued the run.
CREATE TABLE IF NOT EXISTS scrape_jobs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT,
    resume_id INT,
    saved_search_id INT,
    params JSON,                        -- keywords/location/work_type/country/sites
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending/running/done/error
    search_id INT,                      -- the job_searches row this produced
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP NULL,
    finished_at TIMESTAMP NULL,
    INDEX idx_scrape_status (status, id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (resume_id) REFERENCES user_resumes(id) ON DELETE CASCADE,
    FOREIGN KEY (search_id) REFERENCES job_searches(id) ON DELETE SET NULL
);

-- ---------------------------------------------------------------------------
-- Saved searches + scheduled alerts
-- ---------------------------------------------------------------------------
-- A recurring search the user wants re-run on a schedule. The worker's
-- scheduler finds rows whose next_run_at has passed, enqueues a scrape_jobs
-- row for each, and emails the user any postings not seen in the previous run.
CREATE TABLE IF NOT EXISTS saved_searches (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    resume_id INT NOT NULL,
    label VARCHAR(255),
    params JSON,                        -- same shape as scrape_jobs.params
    frequency VARCHAR(20) NOT NULL DEFAULT 'daily',  -- daily/weekly
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMP NULL,
    next_run_at TIMESTAMP NULL,
    last_search_id INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_saved_search_due (active, next_run_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (resume_id) REFERENCES user_resumes(id) ON DELETE CASCADE,
    FOREIGN KEY (last_search_id) REFERENCES job_searches(id) ON DELETE SET NULL
);
