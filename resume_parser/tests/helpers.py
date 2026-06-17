"""Shared test helpers."""

from __future__ import annotations

from resume_parser.intermediate_representation import TextBlock


def block(text: str, order: int, **kwargs) -> TextBlock:
    """Concise TextBlock factory for synthetic unit-test inputs."""
    return TextBlock(text=text, order_index=order, **kwargs)
