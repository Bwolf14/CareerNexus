"""Unit tests for the deterministic job_matcher package."""

from __future__ import annotations

from datetime import date

from job_matcher import (
    analyze_certifications,
    build_questions,
    build_resume_tips,
    score_jobs,
)
from job_matcher.questions import MAX_QUESTIONS, MIN_QUESTIONS


def make_resume(**overrides):
    resume = {
        "metadata": {
            "source_format": "pdf",
            "parser_version": "test",
            "section_detection_status": "success",
            "extraction_confidence": 1.0,
            "warnings": [],
        },
        "contact_info": {
            "name": "Alex Smith",
            "email": "alex@example.com",
            "phone": "555-0100",
            "location": "Calgary, AB",
            "links": {"linkedin": "linkedin.com/in/alex", "github": None,
                      "portfolio": None, "other": []},
        },
        "summary": "Network technician with homelab experience.",
        "experience": [
            {
                "company": "Acme Corp",
                "title": "Network Technician",
                "location": "Calgary, AB",
                "dates": {"start_date": "2022-01", "end_date": None, "is_current": True},
                "description": [
                    "Maintained switching for 3 offices and 400 users",
                    "Cut ticket backlog 40% in six months",
                ],
            }
        ],
        "education": [],
        "skills": {
            "raw": ["Networking", "Cisco IOS", "Python", "Linux", "VLANs", "Firewalls"],
            "categorized": {},
        },
        "certifications": [],
        "projects": [{"title": "Homelab VLAN segmentation", "description": None,
                      "technologies": [], "url": None,
                      "dates": {"start_date": None, "end_date": None, "is_current": False}}],
        "volunteer_experience": [],
        "additional_sections": {},
        "raw_text": "Alex Smith resume text",
    }
    resume.update(overrides)
    return resume


def make_job(**overrides):
    job = {
        "source_site": "indeed",
        "external_id": "j1",
        "title": "Network Technician",
        "company": "Northwind",
        "location": "Calgary, AB",
        "job_type": "fulltime",
        "is_remote": False,
        "salary_min": 70000.0,
        "salary_max": 90000.0,
        "salary_currency": "CAD",
        "salary_interval": "yearly",
        "salary_display": "$70,000–$90,000 / yearly",
        "description": "Looking for a network technician. Cisco IOS, VLANs, "
        "firewalls. CCNA required. Python scripting an asset.",
        "job_url": "https://example.com/j1",
        "date_posted": date.today().isoformat(),
        "search_term": "Network Technician",
    }
    job.update(overrides)
    return job


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------
def test_scoring_ranks_relevant_job_higher():
    resume = make_resume()
    relevant = make_job()
    unrelated = make_job(
        external_id="j2",
        title="Pastry Chef",
        description="Bake croissants and pastries daily.",
        salary_min=None, salary_max=None, salary_display=None,
    )
    ranked = score_jobs(resume, [unrelated, relevant])
    assert ranked[0]["job"]["external_id"] == "j1"
    assert ranked[0]["score"] > ranked[1]["score"]
    assert any("skills" in r.lower() for r in ranked[0]["reasons"])
    assert "Cisco IOS" in ranked[0]["matched_skills"]


def test_scoring_uses_answers_for_salary_and_work_style():
    resume = make_resume()
    low_pay = make_job(external_id="low", salary_min=30000.0, salary_max=35000.0)
    remote = make_job(external_id="remote", is_remote=True, location="Remote")
    answers = {
        "work_style": "Remote",
        "salary": {"min": "70000", "max": "95000", "interval": "yearly"},
    }
    ranked = score_jobs(resume, [low_pay, remote], answers)
    by_id = {s["job"]["external_id"]: s for s in ranked}
    assert by_id["remote"]["score"] > by_id["low"]["score"]
    assert any("below the range" in c for c in by_id["low"]["concerns"])
    assert any("Remote" in r for r in by_id["remote"]["reasons"])


def test_scoring_hourly_rate_normalised():
    resume = make_resume()
    hourly = make_job(
        external_id="hourly", salary_min=40.0, salary_max=45.0,
        salary_interval="hourly",
    )
    answers = {"salary": {"min": "70000", "max": "95000", "interval": "yearly"}}
    ranked = score_jobs(resume, [hourly], answers)
    # 40*2080 = 83,200/yr — inside the range, so no "below range" concern.
    assert not ranked[0]["concerns"]


def test_scoring_returns_at_most_top_n():
    resume = make_resume()
    jobs = [make_job(external_id=f"j{i}") for i in range(25)]
    assert len(score_jobs(resume, jobs)) == 10
    assert len(score_jobs(resume, jobs, top_n=7)) == 7


# ---------------------------------------------------------------------------
# certifications
# ---------------------------------------------------------------------------
def test_cert_gap_detected_with_percentage():
    resume = make_resume()
    jobs = [make_job(external_id=f"j{i}") for i in range(3)]  # all mention CCNA
    jobs.append(make_job(external_id="none", description="No certs needed here."))
    analysis = analyze_certifications(resume, jobs)
    ccna = next(g for g in analysis["gaps"] if g["name"] == "Cisco CCNA")
    assert ccna["job_count"] == 3
    assert ccna["job_pct"] == 75
    assert "75%" in ccna["message"]


def test_cert_held_is_affirmed_not_a_gap():
    resume = make_resume(
        certifications=[{"name": "CCNA", "issuer": "Cisco",
                         "date_earned": None, "expiration_date": None}]
    )
    jobs = [make_job()]
    analysis = analyze_certifications(resume, jobs)
    assert not any(g["name"] == "Cisco CCNA" for g in analysis["gaps"])
    held = next(h for h in analysis["held"] if h["name"] == "Cisco CCNA")
    assert held["job_count"] == 1


def test_cert_boundaries_no_false_positive():
    resume = make_resume()
    # "scpa" must not match CPA; "accna" must not match CCNA.
    jobs = [make_job(description="Work with scpa and accna systems.")]
    analysis = analyze_certifications(resume, jobs)
    assert not analysis["gaps"]


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------
def test_questions_are_resume_specific_and_bounded():
    qs = build_questions(make_resume())
    assert MIN_QUESTIONS <= len(qs) <= MAX_QUESTIONS
    ids = [q["id"] for q in qs]
    assert len(ids) == len(set(ids))
    # Grounded in this resume:
    assert "recent_role" in ids and "project_detail" in ids
    prompts = " ".join(q["prompt"] for q in qs)
    assert "Network Technician" in prompts
    assert "Homelab VLAN segmentation" in prompts
    # The standard career questions are always present:
    for required in ("five_year", "salary", "work_style"):
        assert required in ids


def test_questions_for_sparse_resume_still_min_four():
    sparse = make_resume(experience=[], projects=[], skills={"raw": [], "categorized": {}})
    qs = build_questions(sparse)
    assert len(qs) >= MIN_QUESTIONS
    assert all(q["origin"] in ("standard", "aspiration") for q in qs)


# ---------------------------------------------------------------------------
# resume tips
# ---------------------------------------------------------------------------
def test_tips_flag_missing_contact_and_summary():
    resume = make_resume(summary=None)
    resume["contact_info"] = {"name": None, "email": None, "phone": None,
                              "location": None, "links": {}}
    tips = build_resume_tips(resume)
    titles = [t["title"] for t in tips]
    assert "Add missing contact details" in titles
    assert "Add a professional summary" in titles
    # sorted most severe first
    severities = [t["severity"] for t in tips]
    assert severities == sorted(severities, key={"high": 0, "medium": 1, "low": 2}.get)


def test_tips_quiet_on_strong_resume():
    tips = build_resume_tips(make_resume())
    assert all(t["severity"] != "high" for t in tips)


def test_tips_reference_top_cert_gap():
    resume = make_resume()
    jobs = [make_job(external_id=f"j{i}") for i in range(4)]
    analysis = analyze_certifications(resume, jobs)
    tips = build_resume_tips(resume, cert_analysis=analysis)
    assert any("certification gap" in t["title"].lower() for t in tips)
