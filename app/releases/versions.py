"""Version strings as UniFi reports them, made comparable."""

from __future__ import annotations

import re

# Three or more dot-separated numbers, optionally one more: 5.5.51, 4.3.6,
# 10.0.160, 6.7.31.15618.
_DOTTED = re.compile(r"(?<![\d.])(\d+(?:\.\d+){2,3})(?![\d])")
_SHORT = re.compile(r"(?<![\d.])(\d+\.\d+)(?![\d.])")


def normalize_version(raw: str | None) -> str | None:
    """The plain dotted version inside `raw`, or `None` if there is none.

    Cameras report firmware like `UVC.SAV539g.v5.5.51.67.abcdef.231115`
    and switches `7.6.11.17272`, while the update and the community post
    say `5.5.51` / `7.6.11`; all of them normalize to the three-part form.
    A 3-part version is returned unchanged (stripped)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    match = _DOTTED.search(text)
    if match:
        # A 4th part is a build number (`7.6.11.17272`, `v5.5.45.67`);
        # releases are named by the first three everywhere UniFi shows them.
        return ".".join(match.group(1).split(".")[:3])
    match = _SHORT.search(text)
    if match:
        return match.group(1)
    return None
