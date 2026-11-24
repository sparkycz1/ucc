"""Every fuzz target (`fuzz/targets.py`) run over a fixed seed corpus plus a
few hundred deterministic pseudo-random inputs — so a contract a target
checks can't regress unnoticed between the coverage-guided runs in
`.github/workflows/fuzz.yml`, and so the targets run on every OS (Atheris
itself is Linux-only). An input the fuzzer once crashed on belongs in
`_SEEDS` once it's fixed."""

from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from fuzz.targets import TARGETS

_TOKENS = [
    "\n", "\t", " ", ":", "=", ",", ";", "|", "/", "\\", "[", "]", "{", "}", '"', "'",
    "-", ".", "0", "1", "99999", "-1", "nan", "inf", "%", "@", "http://", "https://",
    "//", "?", "#", "::", "\x00", "\x7f", "é", chr(0x2028), "null", "true", "[]", "{}",
    "- ", "&a", "*a", "!!python/object", "---", "*", "*/5", "fabric:", "site:",
    '"id":', '"reportedState":', '"controllers":', '"hardware":', '"autoUpdate":',
    '"schedule":', '"devices":', '"mac":', '"siteId":', '"hostId":', '"meta":',
]

# Inputs that once broke a target, kept so they can't come back.
_SEEDS: list[bytes] = [
    b"",
    b"\x00" * 12,
    # Open-redirect tricks a plain startswith("/") check let through.
    b"/\\evil.example",
    b"/\t/evil.example",
    b"//evil.example",
    # A Site Manager reply with every field the wrong type.
    b'{"id": "h", "reportedState": [], "ipAddress": 5}',
    b'{"id": "h", "reportedState": {"hardware": "x", "controllers": {"a": 1},'
    + b' "firmwareUpdate": [], "autoUpdate": {"schedule": [], "includeApplications": "yes"}}}',
    b'{"id": "h", "reportedState": {"controllers": [{"name": 1}, 2, null],'
    + b' "autoUpdate": {"schedule": {"frequency": 1, "day": "x", "hour": 99}}}}',
    b'{"siteId": 1, "hostId": [], "meta": "x"}',
    b'{"hostId": 1, "devices": {"mac": "a"}}',
    b'{"hostId": "h", "devices": [{"mac": 5}, {"mac": ["a"]}, "x"]}',
    # JSON / YAML nested far too deep.
    b"[" * 5000,
    b"- " * 2000 + b"x",
    # A YAML alias bomb ("billion laughs").
    b"a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]\n",
    b"*a [&a x]",
    b"fabric:not-a-uuid",
    b"site:",
    b"61 * * * *",
]


def _inputs(name: str, count: int = 300) -> list[bytes]:
    rng = random.Random(name)  # noqa: S311 - reproducible test inputs, not secrets
    inputs = list(_SEEDS)
    for _ in range(count):
        if rng.random() < 0.2:
            inputs.append(rng.randbytes(rng.randint(0, 120)))
        else:
            text = "".join(rng.choice(_TOKENS) for _ in range(rng.randint(0, 40)))
            inputs.append(text.encode("utf-8", "surrogatepass"))
    return inputs


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_fuzz_target_holds_its_contract(name: str) -> None:
    target: Callable[[bytes], None] = TARGETS[name]
    for data in _inputs(name):
        target(data)


def test_the_fuzz_workflow_runs_every_target() -> None:
    workflow = yaml.safe_load(
        (Path(__file__).parent.parent / ".github" / "workflows" / "fuzz.yml").read_text(
            encoding="utf-8"
        )
    )
    assert sorted(workflow["jobs"]["fuzz"]["strategy"]["matrix"]["target"]) == sorted(TARGETS)
