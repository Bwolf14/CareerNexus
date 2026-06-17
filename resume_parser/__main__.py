"""
Command-line interface:

    python -m resume_parser path/to/resume.pdf [--out result.json]

Prints the parsed resume as JSON to stdout (or to a file with --out).
Exits non-zero only for unreadable/unsupported files — a resume that merely
fails to segment still prints a valid object with status "failed".
"""

from __future__ import annotations

import argparse
import sys

from .exceptions import ResumeParserError
from .parser import parse_resume


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="resume_parser", description=__doc__)
    ap.add_argument("filepath", help="Path to a PDF or DOCX resume")
    ap.add_argument("--out", "-o", help="Write JSON here instead of stdout")
    ap.add_argument(
        "--indent", type=int, default=2, help="JSON indent (default: 2)"
    )
    args = ap.parse_args(argv)

    try:
        resume = parse_resume(args.filepath)
    except ResumeParserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = resume.model_dump_json(indent=args.indent)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
