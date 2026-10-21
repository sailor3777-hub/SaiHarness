"""Client for the user's own SearXNG (internet metasearch). The user sets the address, usually a machine on the local network,
so the private-address protection (core/web/safety.py) does not apply here: it is for pages the model reads."""
from urllib.parse import urlsplit

import httpx

TIMEOUT = 15.0
CATEGORIES = ("general", "it", "science", "news")
SNIPPET_MAX = 300


class SearxError(Exception):
    """The search failed; the error text is shown to the user and the model."""


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Invalid SearXNG address: {url}")
    return url


def search(base_url: str, query: str, limit: int = 5, language: str = "", category: str = "general",
           transport=None, timeout: float = TIMEOUT) -> list[dict]:
    """A list of {title, url, snippet}. Raises SearxError with a clear reason."""
    base_url = normalize_url(base_url)
    if not base_url:
        raise SearxError("The SearXNG address is not set: enter it on the Web tab.")
    if category not in CATEGORIES:
        raise SearxError(f"Unknown category \"{category}\". Available: {', '.join(CATEGORIES)}.")
    params = {"q": query, "format": "json", "safesearch": "0"}
    if category != "general":
        params["categories"] = category
    if language:
        params["language"] = language
    try:
        with httpx.Client(transport=transport, timeout=timeout, trust_env=False) as client:
            r = client.get(base_url + "/search", params=params, headers={"User-Agent": "SaiHarness/1.0", "Accept": "application/json"})
    except httpx.TimeoutException:
        raise SearxError(f"SearXNG did not answer within {timeout:g} s ({base_url})") from None
    except httpx.HTTPError as e:
        raise SearxError(f"SearXNG is unavailable ({base_url}): {e.__class__.__name__}") from None
    if r.status_code == 403:
        raise SearxError("SearXNG returned 403: enable json in search.formats in its settings.yml "
                         "(formats: [html, json]) and restart the service.")
    if r.status_code >= 400:
        raise SearxError(f"SearXNG answered {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise SearxError("SearXNG did not return JSON: check the address and that json is enabled in search.formats.") from None
    out = []
    for item in data.get("results") or []:
        url = item.get("url")
        if not url:
            continue
        out.append({"title": (item.get("title") or "").strip(), "url": url,
                    "snippet": " ".join((item.get("content") or "").split())[:SNIPPET_MAX]})
        if len(out) >= limit:
            break
    return out
