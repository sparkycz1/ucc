"""General web search — forum threads, blog posts and bug trackers outside
community.ui.com and Reddit.

Two backends, both optional (Settings → Releases):

- **SearXNG** — a self-hosted metasearch instance (its JSON output must be
  enabled: `search.formats: [html, json]` in its settings.yml). No key.
- **Brave Search API** — a subscription token.

Only titles, URLs and the search engine's snippet are used; pages are not
fetched.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from app.db.models.app_settings import WebSearchKind
from app.releases.evidence import Evidence, EvidenceItem, SourceError
from app.releases.text import html_to_text

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
USER_AGENT = "ucc (UniFi Control Center) release assessment"
TIMEOUT_SECONDS = 20.0
RESULT_LIMIT = 10
# Already covered by their own sources.
_SKIP_HOSTS = ("community.ui.com", "reddit.com")


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _keep(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return not any(host == skip or host.endswith("." + skip) for skip in _SKIP_HOSTS)


def parse_results(kind: str, payload: object) -> list[tuple[str, str, str]]:
    """`[(title, url, snippet)]` from a SearXNG or Brave response."""
    body = _dict(payload)
    if kind == WebSearchKind.BRAVE:
        raw = _list(_dict(body.get("web")).get("results"))
        snippet_key = "description"
    else:
        raw = _list(body.get("results"))
        snippet_key = "content"
    out = []
    for item in raw:
        item = _dict(item)
        title, url = _str(item.get("title")), _str(item.get("url"))
        if title and url and _keep(url):
            out.append((html_to_text(title), url, html_to_text(_str(item.get(snippet_key)))))
    return out[:RESULT_LIMIT]


def validate_searxng_url(value: str) -> str | None:
    """An error message, or `None` if `value` is a usable SearXNG base URL."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "The SearXNG URL must start with http:// or https://."
    if parts.username or parts.password:
        return "The SearXNG URL must not contain credentials."
    return None


class WebSearchClient:
    def __init__(
        self,
        kind: str,
        *,
        url: str | None = None,
        api_key: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._kind = kind
        self._url = (url or "").rstrip("/")
        self._api_key = api_key
        self._http = http or httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, headers={"user-agent": USER_AGENT}
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search(self, query: str) -> list[tuple[str, str, str]]:
        try:
            if self._kind == WebSearchKind.BRAVE:
                if not self._api_key:
                    raise SourceError("Web search: no Brave API key configured")
                response = await self._http.get(
                    BRAVE_URL,
                    params={"q": query, "count": str(RESULT_LIMIT)},
                    headers={"x-subscription-token": self._api_key, "accept": "application/json"},
                )
            elif self._kind == WebSearchKind.SEARXNG:
                if not self._url:
                    raise SourceError("Web search: no SearXNG URL configured")
                response = await self._http.get(
                    f"{self._url}/search", params={"q": query, "format": "json"}
                )
            else:
                return []
        except httpx.HTTPError as exc:
            raise SourceError(f"Web search: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise SourceError(f"Web search: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("Web search: response is not JSON") from exc
        return parse_results(self._kind, payload)

    async def collect(self, evidence: Evidence, product_label: str, version: str) -> None:
        for title, url, snippet in await self.search(
            f'"{product_label}" "{version}" problem OR issue OR bug'
        ):
            evidence.add(EvidenceItem(kind="web", title=title, url=url, text=snippet or title))
