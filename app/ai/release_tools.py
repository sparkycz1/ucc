"""The AI assistant's release tools — read-only, like every assistant tool
(`app.ai.tools` holds the catalogue and the permission checks; this module
holds the lookups).

- `list_release_ratings` — the Releases catalogue and every release with a
  pending update in the account's Fabrics, one line each.
- `get_release_rating` — one release's stored rating: level, reason,
  summary, issues, sources, and the account's consoles/devices it affects.
- `read_release_reports` — what community.ui.com (and Reddit / web search
  when configured) say about *any* version right now, so the assistant can
  judge a version ucc hasn't rated. Reads public sources only, writes
  nothing, and stores nothing.

Everything quoted from a forum is untrusted text; the assistant's system
prompt already treats tool output as data.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_settings import get_or_create_app_settings
from app.db.models.release import Release, ReleaseKind
from app.db.models.user import User
from app.releases.assess import build_prompt
from app.releases.products import APP_TITLES, version_key
from app.releases.service import (
    catalog,
    gather_evidence,
    model_names,
    product_label,
    visible_verdicts,
)
from app.releases.versions import normalize_version

# Bounded, like every tool result: the model gets enough to judge, not a
# whole forum.
MAX_REPORT_CHARS = 14000
MAX_LISTED_RELEASES = 60
MAX_AFFECTED = 30

RISK_MEANING = {
    1: "stable",
    2: "minor issues",
    3: "caution",
    4: "high risk",
    5: "broken",
}


def resolve_product(text: str) -> tuple[str | None, str]:
    """`"Protect"`, `"UniFi Network Application"`, `"network"` → (`app`, key);
    anything else (a device or console model) → (`None`, as typed)."""
    lowered = " ".join(text.lower().split())
    for key, title in APP_TITLES.items():
        if lowered in (key, title.lower()) or lowered == title.lower().removeprefix("unifi "):
            return ReleaseKind.APP.value, key
    for key in APP_TITLES:
        if key in lowered.split() or f"unifi {key}" in lowered:
            return ReleaseKind.APP.value, key
    return None, text.strip()


async def _visible_fabric_ids(db: AsyncSession, user: User) -> list[uuid.UUID]:
    from app.services.access_scope import fabrics_visible_to

    return [f.id for f in (await db.execute(await fabrics_visible_to(db, user))).scalars()]


def _compact(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


async def _model_code(db: AsyncSession, text: str) -> str:
    """ "UCG-Ultra" / "ucg ultra" → its synced model code ("UDRULT"); else
    `text` unchanged — releases are keyed on the code."""
    wanted = _compact(text)
    for code, name in (await model_names(db)).items():
        if wanted in (_compact(name), _compact(code)):
            return code
    return text


def _level(release: Release) -> str:
    level = release.effective_risk_level
    if level is None:
        return "not rated yet"
    source = "manual" if release.manual_risk_level is not None else "AI"
    return f"risk {level}/5 ({RISK_MEANING.get(level, '?')}, {source})"


def _line(release: Release, pending: dict[str, list[str]]) -> str:
    parts = [f"- {product_label(release)} {release.version}"]
    if release.stage:
        parts.append(f"[{release.stage}]")
    parts.append(_level(release))
    if release.short_reason:
        parts.append(f"— {release.short_reason}")
    if release.is_allowed:
        parts.append("(allowed by hand)")
    verdict_list = pending.get(str(release.id))
    if verdict_list:
        stops = verdict_list.count("stop")
        parts.append(
            f"; pending on {len(verdict_list)} of your consoles/devices"
            + (f", {stops} STOP" if stops else "")
        )
    return " ".join(parts)


async def list_release_ratings(db: AsyncSession, user: User, product: str | None) -> str:
    pending: dict[str, list[str]] = {}
    releases: dict[str, Release] = {}
    for _fabric, verdict in await visible_verdicts(db, user):
        if verdict.release is not None:
            releases[str(verdict.release.id)] = verdict.release
            pending.setdefault(str(verdict.release.id), []).append(verdict.verdict.value)
    for group in await catalog(db, await _visible_fabric_ids(db, user)):
        for release in group.releases:
            releases.setdefault(str(release.id), release)

    selected = list(releases.values())
    if product:
        kind, key = resolve_product(product)
        if kind is None:
            key = await _model_code(db, key)
        wanted = key.lower()
        selected = [
            r
            for r in selected
            if r.product.lower() == wanted or (kind is None and wanted in (r.title or "").lower())
        ]
    if not selected:
        return (
            f'No rated or pending releases match "{product}".'
            if product
            else "No releases are known yet."
        )
    selected.sort(key=lambda r: (product_label(r), version_key(r.version) or ()), reverse=True)
    app_settings = await get_or_create_app_settings(db)
    lines = [
        f"Acceptable risk level: {app_settings.max_acceptable_risk_level}/5 globally "
        "(a Fabric can override it). Scale: 1 stable, 2 minor issues, 3 caution, "
        "4 high risk, 5 broken."
    ]
    lines.extend(_line(r, pending) for r in selected[:MAX_LISTED_RELEASES])
    if len(selected) > MAX_LISTED_RELEASES:
        lines.append(f"(cut at {MAX_LISTED_RELEASES} — ask for one product)")
    return "\n".join(lines)


async def _find(db: AsyncSession, product: str, version: str) -> list[Release]:
    normalized = normalize_version(version) or version.strip()
    kind, key = resolve_product(product)
    if kind is None:
        key = await _model_code(db, key)
    query = select(Release).where(Release.version == normalized)
    if kind is not None:
        query = query.where(Release.kind == kind, Release.product == key)
    else:
        lowered = key.lower()
        query = query.where(
            (func.lower(Release.product) == lowered)
            | (func.lower(func.coalesce(Release.title, "")).contains(lowered))
        )
    return list((await db.execute(query.limit(5))).scalars().all())


async def get_release_rating(db: AsyncSession, user: User, product: str, version: str) -> str:
    found = await _find(db, product, version)
    if not found:
        return (
            f"ucc has no rating stored for {product} {version}. Use read_release_reports "
            "to read what the community says about it, and say clearly that the "
            "judgement is your own reading, not a stored ucc rating."
        )
    affected: dict[str, list[str]] = {}
    for fabric, verdict in await visible_verdicts(db, user, installed=True):
        if verdict.release is None:
            continue
        risk = verdict.risk
        context = f", risk here {risk.level}/5" if risk.level is not None else ""
        if risk.not_applicable:
            context += " (not applicable here: " + "; ".join(
                str(i.get("title")) for i in risk.not_applicable
            ) + ")"
        if verdict.target.source == "installed":
            line = (
                f"{fabric.name}: {verdict.target.label} runs it now"
                f"{' — ABOVE the acceptable level' if verdict.verdict == 'stop' else ''}{context}"
            )
        else:
            line = (
                f"{fabric.name}: {verdict.target.label} "
                f"({normalize_version(verdict.target.current_version) or '?'} → "
                f"{verdict.release.version}) {verdict.verdict.value.upper()}{context}"
            )
        affected.setdefault(str(verdict.release.id), []).append(line)
    out = []
    for release in found:
        lines = [
            f"{product_label(release)} {release.version}"
            + (f" [{release.stage}]" if release.stage else ""),
            f"Rating: {_level(release)}",
        ]
        if release.short_reason:
            lines.append(f"Why: {release.short_reason}")
        if release.manual_note:
            lines.append(f"Manual note ({release.manual_by}): {release.manual_note}")
        if release.is_allowed:
            lines.append(f"Allowed by hand by {release.allowed_by}: {release.allowed_note or ''}")
        if release.summary:
            lines.append(f"Summary: {release.summary}")
        if release.base_risk_level is not None and release.base_risk_level != release.risk_level:
            lines.append(
                f"Without the hardware-specific issues: risk {release.base_risk_level}/5 "
                "(each console/device below is judged by what it has)"
            )
        for issue in release.issues or []:
            if isinstance(issue, dict) and issue.get("title"):
                reports = issue.get("reports")
                count = f" [{reports} independent reports]" if isinstance(reports, int) else ""
                if isinstance(issue.get("severity"), int):
                    count += f" [severity {issue['severity']}/5]"
                if issue.get("affects"):
                    count += f" [affects: {', '.join(map(str, issue['affects']))}]"
                lines.append(
                    f"- issue: {issue['title']}{count} {issue.get('detail') or ''}".rstrip()
                )
        if release.assessed_at:
            lines.append(
                f"Assessed {release.assessed_at.date().isoformat()} by {release.assessed_by_model}"
            )
        for source in (release.sources or [])[:10]:
            if isinstance(source, dict) and source.get("url"):
                lines.append(
                    f"- source ({source.get('kind')}): {source.get('title')} {source['url']}"
                )
        if release.community_url and not release.sources:
            lines.append(f"- release notes: {release.community_url}")
        targets = affected.get(str(release.id), [])
        if targets:
            lines.append(f"In your Fabrics — pending updates and what runs it ({len(targets)}):")
            lines.extend(f"  {t}" for t in targets[:MAX_AFFECTED])
            if len(targets) > MAX_AFFECTED:
                lines.append(f"  (and {len(targets) - MAX_AFFECTED} more)")
        else:
            lines.append("Not pending on, or running on, any console or device in your Fabrics.")
        out.append("\n".join(lines))
    return "\n\n".join(out)


async def read_release_reports(
    db: AsyncSession, product: str, version: str, kind: str | None
) -> str:
    resolved_kind, key = resolve_product(product)
    if resolved_kind is None:
        key = await _model_code(db, key)
    chosen_kind = resolved_kind or (kind if kind in {k.value for k in ReleaseKind} else None)
    if chosen_kind is None:
        chosen_kind = ReleaseKind.FIRMWARE.value
    normalized = normalize_version(version)
    if normalized is None:
        return f'"{version}" is not a version number.'
    probe = Release(kind=chosen_kind, product=key, version=normalized)
    app_settings = await get_or_create_app_settings(db)
    evidence = await gather_evidence(app_settings, probe)
    if evidence.release is None and not evidence.items:
        errors = f" ({'; '.join(evidence.errors)})" if evidence.errors else ""
        return f"Nothing found about {product} {normalized} on the configured sources{errors}."
    label = evidence.release.title if evidence.release else product_label(probe)
    text = build_prompt(
        label=label,
        version=normalized,
        stage=evidence.release.stage if evidence.release else None,
        published_at=(evidence.release.created_at or "")[:10] if evidence.release else None,
        evidence=evidence,
        max_chars=MAX_REPORT_CHARS,
    )
    sources = "\n".join(
        f"- {s['kind']}: {s['title']} {s['url']}" for s in evidence.sources()[:12] if s.get("url")
    )
    return (
        text
        + "\n\nLinks:\n"
        + sources
        + "\n\nThis is raw community content, not a ucc rating. Judge it yourself and "
        "say so; cite the links."
    )
