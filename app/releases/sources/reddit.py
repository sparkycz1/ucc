"""Reddit — posts in r/Ubiquiti and r/UNIFI that mention the version, with
their top comments.

Reddit refuses anonymous API calls (HTTP 403), so this needs an app's
OAuth credentials (reddit.com/prefs/apps → "script" app): an
application-only token from the `client_credentials` grant, read-only, no
Reddit account involved. Without credentials the source is skipped.
"""

from __future__ import annotations

from typing import Any

import httpx2

from app.releases.evidence import Evidence, EvidenceItem, SourceError
from app.releases.text import html_to_text

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 - a URL, not a secret
API_URL = "https://oauth.reddit.com"
SITE_URL = "https://www.reddit.com"
USER_AGENT = "linux:ucc-release-assessment:1.0 (UniFi Control Center)"
SUBREDDITS = "Ubiquiti+UNIFI"
TIMEOUT_SECONDS = 20.0
POST_LIMIT = 12
POSTS_WITH_COMMENTS = 4
COMMENTS_PER_POST = 8


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_posts(payload: object) -> list[dict[str, Any]]:
    """`[{id, title, text, url, score, comments, created}]` from a search
    listing."""
    posts = []
    for child in _list(_dict(_dict(payload).get("data")).get("children")):
        data = _dict(_dict(child).get("data"))
        post_id, title, permalink = (
            _str(data.get("id")),
            _str(data.get("title")),
            _str(data.get("permalink")),
        )
        if not post_id or not title or not permalink or not permalink.startswith("/r/"):
            continue
        created = data.get("created_utc")
        posts.append(
            {
                "id": post_id,
                "title": title,
                "text": html_to_text(_str(data.get("selftext"))),
                "url": SITE_URL + permalink,
                "score": _int(data.get("score")),
                "comments": _int(data.get("num_comments")) or 0,
                "created": str(int(created)) if isinstance(created, (int, float)) else None,
            }
        )
    return posts


def parse_comments(payload: object) -> list[tuple[str, int | None]]:
    """Top-level comment bodies with their score from a comments listing
    (`[post listing, comment listing]`)."""
    listings = _list(payload)
    if len(listings) < 2:
        return []
    out = []
    for child in _list(_dict(_dict(listings[1]).get("data")).get("children")):
        if _dict(child).get("kind") != "t1":
            continue
        data = _dict(_dict(child).get("data"))
        body = _str(data.get("body"))
        if body and body not in ("[deleted]", "[removed]"):
            out.append((body, _int(data.get("score"))))
    return out


class RedditClient:
    def __init__(
        self, client_id: str, client_secret: str, http: httpx2.AsyncClient | None = None
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx2.AsyncClient(
            timeout=TIMEOUT_SECONDS, headers={"user-agent": USER_AGENT}
        )
        self._token: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _authenticate(self) -> str:
        if self._token is not None:
            return self._token
        try:
            response = await self._http.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
            )
        except httpx2.HTTPError as exc:
            raise SourceError(f"Reddit: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise SourceError(f"Reddit rejected the app credentials (HTTP {response.status_code})")
        try:
            token = _str(_dict(response.json()).get("access_token"))
        except ValueError as exc:
            raise SourceError("Reddit: token response is not JSON") from exc
        if not token:
            raise SourceError("Reddit: no access token in the response")
        self._token = token
        return token

    async def _get(self, path: str, params: dict[str, str]) -> object:
        token = await self._authenticate()
        try:
            response = await self._http.get(
                API_URL + path, params=params, headers={"authorization": f"Bearer {token}"}
            )
        except httpx2.HTTPError as exc:
            raise SourceError(f"Reddit: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise SourceError(f"Reddit: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError("Reddit: response is not JSON") from exc

    async def check(self) -> None:
        """Raises `SourceError` unless the credentials work (Settings' test)."""
        await self._authenticate()

    async def collect(self, evidence: Evidence, product_label: str, version: str) -> None:
        listing = await self._get(
            f"/r/{SUBREDDITS}/search",
            {
                "q": f'"{version}" {product_label}',
                "restrict_sr": "1",
                "sort": "new",
                "t": "year",
                "limit": str(POST_LIMIT),
                "raw_json": "1",
            },
        )
        posts = [p for p in parse_posts(listing) if version in p["title"] + " " + p["text"]]
        for post in posts:
            evidence.add(
                EvidenceItem(
                    kind="reddit",
                    title=post["title"],
                    url=post["url"],
                    text=post["text"] or post["title"],
                    score=post["score"],
                    created_at=post["created"],
                )
            )
        for post in sorted(posts, key=lambda p: p["comments"], reverse=True)[:POSTS_WITH_COMMENTS]:
            if not post["comments"]:
                continue
            thread = await self._get(
                f"/comments/{post['id']}",
                {"sort": "top", "limit": str(COMMENTS_PER_POST), "depth": "1", "raw_json": "1"},
            )
            for body, score in parse_comments(thread)[:COMMENTS_PER_POST]:
                evidence.add(
                    EvidenceItem(
                        kind="reddit",
                        title=f"Comment on: {post['title']}",
                        url=post["url"],
                        text=body,
                        score=score,
                    )
                )
