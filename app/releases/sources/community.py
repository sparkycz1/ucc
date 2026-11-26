"""community.ui.com — the official release post (notes, known issues) plus
its comment thread, and forum questions that mention the version.

Read anonymously through the same GraphQL endpoint the community.ui.com web
app itself uses (`https://community.svc.ui.com/`). It is not a documented
API, so every field is read defensively and a changed response is a
`SourceError`, never a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2

from app.db.models.release import ReleaseKind
from app.releases.evidence import CommunityRelease, Evidence, EvidenceItem, SourceError
from app.releases.products import search_term, title_matches, version_key
from app.releases.text import html_to_text
from app.releases.versions import normalize_version

GRAPHQL_URL = "https://community.svc.ui.com/"
SITE_URL = "https://community.ui.com"
USER_AGENT = "ucc (UniFi Control Center) release assessment"
TIMEOUT_SECONDS = 20.0

COMMENT_LIMIT = 40
QUESTION_LIMIT = 10

_RELEASES_QUERY = """
query ($searchTerm: String, $limit: Int) {
  releases(limit: $limit, searchTerm: $searchTerm, sortBy: LATEST) {
    items { id slug title version stage createdAt }
  }
}
"""

_CONTENT = "type ... on TextContent { content }"

_RELEASE_QUERY = f"""
query ($id: ID!, $limit: Int, $sortBy: CommentsSortBy) {{
  release(id: $id) {{
    id slug title version stage createdAt
    content {{ {_CONTENT} }}
    knownIssues {{ {_CONTENT} }}
    bugfixes {{ {_CONTENT} }}
    importantNotes {{ {_CONTENT} }}
    comments(limit: $limit, offset: 0, sortBy: $sortBy) {{
      items {{ id createdAt stats {{ score }} content {{ {_CONTENT} }} }}
    }}
  }}
}}
"""

_QUESTIONS_QUERY = """
query ($searchTerm: String!, $limit: Int) {
  questions(limit: $limit, searchTerm: $searchTerm, sortBy: ACTIVITY) {
    items { id slug title createdAt stats { score answers views } }
  }
}
"""


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _contents_text(value: object) -> str:
    parts = [html_to_text(_str(_dict(c).get("content"))) for c in _list(value)]
    return "\n".join(p for p in parts if p)


def release_url(slug: str | None, release_id: str) -> str:
    return f"{SITE_URL}/releases/{slug or 'release'}/{release_id}"


def question_url(slug: str | None, question_id: str) -> str:
    return f"{SITE_URL}/questions/{slug or 'question'}/{question_id}"


@dataclass(frozen=True)
class ReleasePost:
    """One community.ui.com release post, as found by a search."""

    id: str
    title: str
    version: str
    stage: str | None
    url: str
    created_at: str | None


def _post(item: dict[str, Any]) -> ReleasePost | None:
    release_id, title = _str(item.get("id")), _str(item.get("title"))
    version = normalize_version(_str(item.get("version")))
    if not release_id or not title or not version:
        return None
    return ReleasePost(
        id=release_id,
        title=title,
        version=version,
        stage=_str(item.get("stage")),
        url=release_url(_str(item.get("slug")), release_id),
        created_at=_str(item.get("createdAt")),
    )


def matching_posts(kind: str, product: str, items: list[Any]) -> list[ReleasePost]:
    """Search results that are release posts about `kind`/`product`, in the
    order the search returned them (newest first)."""
    posts = []
    for item in items:
        post = _post(_dict(item))
        if post is not None and title_matches(kind, product, post.title):
            posts.append(post)
    return posts


def pick_release(kind: str, product: str, version: str, items: list[Any]) -> ReleasePost | None:
    """The release post for exactly this version, if the search found it."""
    wanted = normalize_version(version)
    for post in matching_posts(kind, product, items):
        if post.version == wanted:
            return post
    return None


def pick_latest(kind: str, product: str, items: list[Any]) -> ReleasePost | None:
    """The newest generally-available release post for this product."""
    posts = [p for p in matching_posts(kind, product, items) if (p.stage or "GA") == "GA"]
    if not posts:
        return None
    return max(posts, key=lambda p: version_key(p.version) or ())


class CommunityClient:
    def __init__(self, http: httpx2.AsyncClient | None = None) -> None:
        self._http = http or httpx2.AsyncClient(
            timeout=TIMEOUT_SECONDS, headers={"user-agent": USER_AGENT}
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(
                GRAPHQL_URL, json={"query": query, "variables": variables}
            )
        except httpx2.HTTPError as exc:
            raise SourceError(f"community.ui.com: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise SourceError(f"community.ui.com: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SourceError("community.ui.com: response is not JSON") from exc
        body = _dict(body)
        if body.get("errors") and not body.get("data"):
            first = _dict(_list(body.get("errors"))[0] if _list(body.get("errors")) else {})
            raise SourceError(f"community.ui.com: {_str(first.get('message')) or 'error'}")
        return _dict(body.get("data"))

    async def search(self, term: str, limit: int = 25) -> list[Any]:
        data = await self._query(_RELEASES_QUERY, {"searchTerm": term, "limit": limit})
        return _list(_dict(data.get("releases")).get("items"))

    async def find_release(self, kind: str, product: str, version: str) -> ReleasePost | None:
        """The release post for this exact version — searched by the version
        number (the search matches it reliably), then by product name."""
        normalized = normalize_version(version) or version
        found = pick_release(kind, product, normalized, await self.search(normalized))
        if found is None:
            found = pick_release(
                kind,
                product,
                normalized,
                await self.search(f"{search_term(kind, product)} {normalized}"),
            )
        return found

    async def latest_release(
        self, kind: str, product: str, current_version: str | None = None
    ) -> ReleasePost | None:
        """The newest GA release post on the same release line.

        Firmware has several lines per family (AP 6.x for U6, 8.x for U7;
        switch 7.x and 8.x), so for firmware the line is the exact title of
        the post for `current_version` — without one, no guess is made.
        Applications and UniFi OS fall back to the newest post for the
        product."""
        current = (
            await self.find_release(kind, product, current_version) if current_version else None
        )
        if current is not None:
            same_line = [
                item
                for item in await self.search(current.title, 30)
                if _str(_dict(item).get("title")) == current.title
            ]
            latest = pick_latest(kind, product, same_line)
            if latest is not None:
                return latest
        if kind == ReleaseKind.FIRMWARE:
            return None
        return pick_latest(kind, product, await self.search(search_term(kind, product), 30))

    async def collect(self, evidence: Evidence, kind: str, product: str, version: str) -> None:
        """Fill `evidence` with the release post, its comments and matching
        forum questions."""
        found = await self.find_release(kind, product, version)
        search_label = version
        if found is not None:
            release_id = found.id
            data = await self._query(
                _RELEASE_QUERY, {"id": release_id, "limit": COMMENT_LIMIT, "sortBy": "SCORE"}
            )
            parse_release(evidence, _dict(data.get("release")), release_id)
            if evidence.release is not None:
                search_label = f"{evidence.release.title} {version}"
        data = await self._query(
            _QUESTIONS_QUERY, {"searchTerm": search_label, "limit": QUESTION_LIMIT}
        )
        parse_questions(evidence, data, version)


def parse_release(evidence: Evidence, release: dict[str, Any], release_id: str) -> None:
    """The release post and its comments into `evidence`."""
    title = _str(release.get("title")) or "UniFi release"
    version = _str(release.get("version")) or ""
    sections = []
    for label, key in (
        ("Release notes", "content"),
        ("Important notes", "importantNotes"),
        ("Known issues", "knownIssues"),
        ("Bugfixes", "bugfixes"),
    ):
        text = _contents_text(release.get(key))
        if text:
            sections.append(f"{label}:\n{text}")
    url = release_url(_str(release.get("slug")), release_id)
    evidence.release = CommunityRelease(
        id=release_id,
        title=title,
        version=version,
        stage=_str(release.get("stage")),
        url=url,
        created_at=_str(release.get("createdAt")),
        notes="\n\n".join(sections)[:8000],
    )
    for comment in _list(_dict(release.get("comments")).get("items")):
        comment = _dict(comment)
        text = _contents_text(comment.get("content"))
        if not text:
            continue
        evidence.add(
            EvidenceItem(
                kind="community",
                title=f"Comment on {title} {version}",
                url=url,
                text=text,
                score=_int(_dict(comment.get("stats")).get("score")),
                created_at=_str(comment.get("createdAt")),
            )
        )


def parse_questions(evidence: Evidence, data: dict[str, Any], version: str) -> None:
    for question in _list(_dict(data.get("questions")).get("items")):
        question = _dict(question)
        question_id = _str(question.get("id"))
        title = _str(question.get("title"))
        if not question_id or not title:
            continue
        stats = _dict(question.get("stats"))
        evidence.add(
            EvidenceItem(
                kind="community",
                title=title,
                url=question_url(_str(question.get("slug")), question_id),
                text=(
                    f"Forum question (search for {version}): {title}. "
                    f"{_int(stats.get('answers')) or 0} answers, "
                    f"{_int(stats.get('views')) or 0} views."
                ),
                score=_int(stats.get("score")),
                created_at=_str(question.get("createdAt")),
            )
        )
