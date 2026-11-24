"""HTML from community.ui.com/Reddit turned into plain prompt text."""

from __future__ import annotations

import html
import re

_BLOCK = re.compile(r"</?(p|br|li|ul|ol|h[1-6]|div|tr)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = _BLOCK.sub("\n", value)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()
