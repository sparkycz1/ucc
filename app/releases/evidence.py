"""What an assessment is based on, and gathering it from every source."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Per item and in total — the prompt has to stay bounded however chatty a
# release thread is.
MAX_ITEM_CHARS = 1500
MAX_TOTAL_CHARS = 60000


class SourceError(Exception):
    """A source could not be read (HTTP error, unexpected response)."""


@dataclass(frozen=True)
class EvidenceItem:
    # "community" | "reddit" | "web"
    kind: str
    title: str
    url: str | None
    text: str
    # Upvotes/score where the source has one — a strongly upvoted
    # "bricked my UDM" matters more than one unanswered post.
    score: int | None = None
    created_at: str | None = None


@dataclass
class CommunityRelease:
    """The official release post matched on community.ui.com."""

    id: str
    title: str
    version: str
    stage: str | None
    url: str
    created_at: str | None
    notes: str


@dataclass
class Evidence:
    release: CommunityRelease | None = None
    items: list[EvidenceItem] = field(default_factory=list)
    # Per-source failures, shown with the assessment ("Reddit: HTTP 401").
    errors: list[str] = field(default_factory=list)

    def add(self, item: EvidenceItem) -> None:
        text = item.text.strip()
        if len(text) > MAX_ITEM_CHARS:
            text = text[:MAX_ITEM_CHARS] + "…"
        self.items.append(
            EvidenceItem(
                kind=item.kind,
                title=item.title[:300],
                url=item.url,
                text=text,
                score=item.score,
                created_at=item.created_at,
            )
        )

    def sources(self) -> list[dict[str, str | None]]:
        seen: set[str] = set()
        out: list[dict[str, str | None]] = []
        if self.release is not None:
            out.append({"kind": "community", "title": self.release.title, "url": self.release.url})
            seen.add(self.release.url)
        for item in self.items:
            if item.url and item.url not in seen:
                seen.add(item.url)
                out.append({"kind": item.kind, "title": item.title, "url": item.url})
        return out
