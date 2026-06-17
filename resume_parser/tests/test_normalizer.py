"""Unit tests for date normalization and text cleanup (blueprint §10)."""

from __future__ import annotations

import pytest

from resume_parser.normalizer import (
    canonical_skill_key,
    clean_text,
    normalize_date_token,
    normalize_skill,
    parse_date_range,
    parse_single_date,
    strip_leading_bullet,
)


# --------------------------------------------------------------------------
# skill canonicalization
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        # variants collapse to a canonical display form
        ("js", "JavaScript"),
        ("Javascript", "JavaScript"),
        ("java script", "JavaScript"),
        ("python", "Python"),
        ("Python 3", "Python"),
        ("python3", "Python"),
        ("nodejs", "Node.js"),
        ("node.js", "Node.js"),
        ("NODE JS", "Node.js"),
        ("postgres", "PostgreSQL"),
        ("PostgreSQL", "PostgreSQL"),
        ("k8s", "Kubernetes"),
        # compound / proper-cased names pass through untouched
        ("C++", "C++"),
        ("CI/CD", "CI/CD"),
        ("TCP/IP", "TCP/IP"),
        (".NET", ".NET"),
        # unknown skills keep their original casing, just cleaned up
        ("Photoshop", "Photoshop"),
        ("  Soldering ", "Soldering"),
        ("Forklift Certified.", "Forklift Certified"),
        ("", ""),
    ],
)
def test_normalize_skill(raw, expected):
    assert normalize_skill(raw) == expected


def test_canonical_skill_key_collapses_spacing_but_keeps_compounds():
    assert canonical_skill_key("Node.js") == canonical_skill_key("node js") == "nodejs"
    assert canonical_skill_key("C++") == "c++"
    assert canonical_skill_key("CI/CD") == "ci/cd"


@pytest.mark.parametrize(
    "token,expected",
    [
        ("2020", "2020"),
        ("Jan 2020", "2020-01"),
        ("January 2020", "2020-01"),
        ("Sept 2019", "2019-09"),
        ("Dec 2021", "2021-12"),
        ("03/2020", "2020-03"),
        ("01/15/2020", "2020-01"),
        ("13/2020", "2020"),          # invalid month -> year only
        ("Sept '22", "2022-09"),      # apostrophe-abbreviated year
        ("'22", "2022"),
        ("'98", "1998"),              # POSIX pivot: 69-99 -> 1900s
        ("not a date", None),
    ],
)
def test_normalize_date_token(token, expected):
    assert normalize_date_token(token) == expected


@pytest.mark.parametrize(
    "text,start,end,current",
    [
        ("Jan 2020 - Present", "2020-01", None, True),
        ("May 2019 – August 2021", "2019-05", "2021-08", False),
        ("2018 to 2022", "2018", "2022", False),
        ("2018—2022", "2018", "2022", False),
        ("03/2020 until 06/2022", "2020-03", "2022-06", False),
        ("2021 - Current", "2021", None, True),
        ("Ongoing since 2015", "2015", None, False),  # single date, no range sep
        ("2017", "2017", None, False),
        ("Sept '22 – Present", "2022-09", None, True),  # contracted year + present
    ],
)
def test_parse_date_range(text, start, end, current):
    dr, _ = parse_date_range(text)
    assert dr.start_date == start
    assert dr.end_date == end
    assert dr.is_current == current


def test_parse_date_range_removes_match_from_remainder():
    dr, remainder = parse_date_range("Acme Corp (2020 - 2022)")
    assert dr.start_date == "2020"
    assert "2020" not in remainder and "2022" not in remainder
    assert "Acme Corp" in remainder


def test_parse_date_range_no_date():
    dr, remainder = parse_date_range("Senior Engineer at Globex")
    assert dr.start_date is None and dr.end_date is None
    assert remainder == "Senior Engineer at Globex"


def test_parse_single_date():
    value, remainder = parse_single_date("Issued March 2021 by Org")
    assert value == "2021-03"
    assert "2021" not in remainder


def test_clean_text_collapses_whitespace_and_rejoins_hyphenation():
    assert clean_text("Did   some\twork\non   things") == "Did some work on things"
    assert clean_text("compu-\nter science") == "computer science"


def test_clean_text_trims_edge_bullets_but_keeps_inline_separators():
    # Leading/trailing decorative bullets are removed...
    assert clean_text("• Led a team") == "Led a team"
    assert clean_text("Led a team •") == "Led a team"
    # ...but an inline separator survives so segment-splitting can use it.
    assert "·" in clean_text("Engineer · Globex Inc.")


@pytest.mark.parametrize(
    "text,expected_text,expected_flag",
    [
        ("• Did a thing", "Did a thing", True),
        ("‣ Another", "Another", True),
        ("- Dash bullet", "Dash bullet", True),
        ("e-commerce work", "e-commerce work", False),  # hyphen word, not a bullet
        ("No bullet here", "No bullet here", False),
    ],
)
def test_strip_leading_bullet(text, expected_text, expected_flag):
    out_text, flag = strip_leading_bullet(text)
    assert out_text == expected_text
    assert flag == expected_flag
