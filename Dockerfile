# Web UI image: serves the Career Nexus flow (parse -> search -> questions ->
# recommendations) and writes results to MariaDB.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# Application code.
COPY resume_parser ./resume_parser
COPY job_scraper ./job_scraper
COPY job_matcher ./job_matcher
COPY ai_client ./ai_client
COPY webapp ./webapp

EXPOSE 8000

# gunicorn serves webapp/app.py:app. Threads keep the UI responsive while a
# worker is busy scraping; the long timeout covers multi-board scrapes (a
# 4-query run across 3-4 boards can take a few minutes on a slow network).
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "300", "webapp.app:app"]
