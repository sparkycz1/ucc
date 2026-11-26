"""Token accounting for the AI assistant, and the global spend limits.

The limits are **global** — one budget for the whole deployment, not one
per user. That's deliberate: what an operator actually wants to cap is the
bill, and a per-user cap doesn't bound the bill unless you also bound the
number of users.

"Day", "week", and "month" are **rolling windows** — the last 24 hours, 7
days, and 30 days — not calendar-aligned buckets. Rolling is both simpler
(one `created_at >= now - delta` query per window, no timezone or
week-start convention to pick) and strictly safer for the thing the setting
is for: a calendar-aligned daily cap resets to zero at midnight, so a
runaway at 23:55 can spend two full days' budget in ten minutes. A rolling
window can't do that.

`check_within_limits` is called **before** the provider request for a new
turn, never after. Checking afterwards would let a capped deployment
overshoot by one more (potentially expensive) call every single time the
limit is hit.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ai_provider import AiProviderKind
from app.db.models.ai_usage import AiUsageRecord
from app.db.models.app_settings import AppSettings

DAY_WINDOW = timedelta(hours=24)
WEEK_WINDOW = timedelta(days=7)
MONTH_WINDOW = timedelta(days=30)


class Purpose(enum.StrEnum):
    """What an AI call was for (`AiUsageRecord.purpose`)."""

    ASSESSMENT = "assessment"
    ASSISTANT = "assistant"


async def record_usage(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    provider_kind: AiProviderKind,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    purpose: Purpose | None = None,
    label: str | None = None,
    duration_ms: int | None = None,
    fallback: bool = False,
) -> None:
    """Write one usage row. Committed here rather than left to the caller —
    a turn that made a paid API call must be accounted for even if
    something later in the same request fails."""
    db.add(
        AiUsageRecord(
            user_id=user_id,
            provider_kind=provider_kind,
            model_id=model_id,
            input_tokens=max(input_tokens, 0),
            output_tokens=max(output_tokens, 0),
            purpose=purpose.value if purpose else None,
            label=label[:255] if label else None,
            duration_ms=duration_ms,
            fallback=fallback,
        )
    )
    await db.commit()


@dataclass(frozen=True)
class WindowUsage:
    calls: int
    input_tokens: int
    output_tokens: int

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class UsageSummary:
    """The token counter on Settings → AI."""

    # "24h" / "7d" / "30d" → totals.
    windows: dict[str, WindowUsage]
    # The last 30 days per "<provider>/<model>", most tokens first.
    by_model: list[tuple[str, WindowUsage]]
    # The last 30 days per purpose ("assessment", "assistant", "" = older rows).
    by_purpose: list[tuple[str, WindowUsage]]
    recent: list[AiUsageRecord]


_SUMS = (
    func.count(AiUsageRecord.id),
    func.coalesce(func.sum(AiUsageRecord.input_tokens), 0),
    func.coalesce(func.sum(AiUsageRecord.output_tokens), 0),
)


def _usage(row: Any) -> WindowUsage:
    return WindowUsage(int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))


async def usage_summary(db: AsyncSession, *, recent: int = 25) -> UsageSummary:
    now = datetime.now(UTC)
    windows = {}
    for name, window in (("24h", DAY_WINDOW), ("7d", WEEK_WINDOW), ("30d", MONTH_WINDOW)):
        row = (
            await db.execute(select(*_SUMS).where(AiUsageRecord.created_at >= now - window))
        ).one()
        windows[name] = _usage(row)
    since = now - MONTH_WINDOW
    by_model = [
        (f"{kind.value}/{model}", _usage(rest))
        for kind, model, *rest in (
            await db.execute(
                select(AiUsageRecord.provider_kind, AiUsageRecord.model_id, *_SUMS)
                .where(AiUsageRecord.created_at >= since)
                .group_by(AiUsageRecord.provider_kind, AiUsageRecord.model_id)
            )
        ).all()
    ]
    by_model.sort(key=lambda item: item[1].tokens, reverse=True)
    by_purpose = [
        (purpose or "", _usage(rest))
        for purpose, *rest in (
            await db.execute(
                select(AiUsageRecord.purpose, *_SUMS)
                .where(AiUsageRecord.created_at >= since)
                .group_by(AiUsageRecord.purpose)
            )
        ).all()
    ]
    by_purpose.sort(key=lambda item: item[1].tokens, reverse=True)
    rows = list(
        (
            await db.execute(
                select(AiUsageRecord).order_by(AiUsageRecord.created_at.desc()).limit(recent)
            )
        ).scalars()
    )
    return UsageSummary(windows, by_model, by_purpose, rows)


async def get_usage_totals(db: AsyncSession, since: datetime) -> int:
    """Total tokens (input + output, every provider and model) recorded at
    or after `since`."""
    result = await db.execute(
        select(
            func.coalesce(
                func.sum(AiUsageRecord.input_tokens + AiUsageRecord.output_tokens), 0
            )
        ).where(AiUsageRecord.created_at >= since)
    )
    return int(result.scalar_one() or 0)


async def check_within_limits(db: AsyncSession, app_settings: AppSettings) -> str | None:
    """Returns an error message if any configured limit is already met or
    exceeded, else `None`. An unset (NULL) limit means unlimited.

    ">= limit", not "> limit": once the budget is reached, the next call is
    refused rather than allowed to be the one that goes over.
    """
    now = datetime.now(UTC)
    windows: list[tuple[str, int | None, timedelta]] = [
        ("Daily", app_settings.ai_daily_token_limit, DAY_WINDOW),
        ("Weekly", app_settings.ai_weekly_token_limit, WEEK_WINDOW),
        ("Monthly", app_settings.ai_monthly_token_limit, MONTH_WINDOW),
    ]
    for label, limit, window in windows:
        if not limit:
            continue
        total = await get_usage_totals(db, now - window)
        if total >= limit:
            return (
                f"{label} AI token limit ({limit}) reached — "
                f"{total} tokens used in the last {_window_label(window)}. "
                "Ask an administrator to raise it on the Settings page, or wait."
            )
    return None


def _window_label(window: timedelta) -> str:
    if window == DAY_WINDOW:
        return "24 hours"
    return f"{window.days} days"
