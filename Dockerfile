# Web UI image: serves the resume-parser upload UI and writes results to MariaDB.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# Application code.
COPY resume_parser ./resume_parser
COPY webapp ./webapp

EXPOSE 8000

# gunicorn serves webapp/app.py:app. The app waits for the DB on the first
# request via the route handlers; a longer timeout covers large-PDF parsing.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "webapp.app:app"]
