"""
Exceptions for the Career Nexus Resume Parser.

Per the blueprint (§1, §5): the pipeline degrades gracefully almost
everywhere — a resume that can't be segmented still returns a valid
``ParsedResume``. The *one* place where failing loudly is correct is
format detection: there is no meaningful "partial" result for a file we
cannot read at all, so an unreadable / unsupported file raises here.
"""

from __future__ import annotations


class ResumeParserError(Exception):
    """Base class for all resume-parser errors."""


class UnsupportedFormatError(ResumeParserError):
    """The file is neither a PDF nor a DOCX (by extension and magic bytes)."""


class CorruptFileError(ResumeParserError):
    """The file claims to be a supported format but cannot be opened/read."""
