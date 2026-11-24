"""Version strings as UniFi reports them, made comparable."""

from __future__ import annotations

# Versions come from Site Manager, community.ui.com and forum text, so the
# parser is a single linear scan — no regular expression whose backtracking
# a long run of digits could make slow (CodeQL "polynomial regular
# expression"). Inputs are also capped before scanning.
_MAX_INPUT = 512
_VERSION_CHARS = frozenset("0123456789.")


def _runs(text: str) -> list[str]:
    """Maximal runs of digits and dots, in order."""
    runs: list[str] = []
    start = -1
    for index, char in enumerate(text):
        if char in _VERSION_CHARS:
            if start < 0:
                start = index
        elif start >= 0:
            runs.append(text[start:index])
            start = -1
    if start >= 0:
        runs.append(text[start:])
    return runs


def _leading_numbers(run: str) -> list[str]:
    """The dot-separated numbers a run starts with, up to the first gap
    (`5.5.45.67.0` → all five, `1.2..3` → `1`, `2`)."""
    if run.startswith("."):
        return []
    numbers: list[str] = []
    for part in run.split("."):
        if not part:
            break
        numbers.append(part)
    return numbers


def normalize_version(raw: str | None) -> str | None:
    """The plain dotted version inside `raw`, or `None` if there is none.

    Cameras report firmware like `UVC.SAV539g.v5.5.51.67.abcdef.231115`
    and switches `7.6.11.17272`, while the update and the community post
    say `5.5.51` / `7.6.11`; all of them normalize to the three-part form
    (a 4th part is a build number, and releases are named by the first
    three everywhere UniFi shows them). The first run of three or more
    numbers wins; failing that, an exact two-part `1.4`."""
    if raw is None:
        return None
    text = raw.strip()[:_MAX_INPUT]
    if not text:
        return None
    runs = _runs(text)
    for run in runs:
        numbers = _leading_numbers(run)
        if len(numbers) >= 3:
            return ".".join(numbers[:3])
    for run in runs:
        numbers = _leading_numbers(run)
        if len(numbers) == 2 and run == ".".join(numbers):
            return run
    return None
