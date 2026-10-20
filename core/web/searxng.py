"""Клиент собственного SearXNG (метапоиск по интернету). Адрес задаёт пользователь, обычно это машина в локальной сети,
поэтому защита от частных адресов (core/web/safety.py) здесь не применяется: она нужна для страниц, которые читает модель."""
from urllib.parse import urlsplit

import httpx

TIMEOUT = 15.0
CATEGORIES = ("general", "it", "science", "news")
SNIPPET_MAX = 300


class SearxError(Exception):
    """Поиск не удался; текст ошибки показывается пользователю и модели."""


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Некорректный адрес SearXNG: {url}")
    return url


def search(base_url: str, query: str, limit: int = 5, language: str = "", category: str = "general",
           transport=None, timeout: float = TIMEOUT) -> list[dict]:
    """Список {title, url, snippet}. Бросает SearxError с понятной причиной."""
    base_url = normalize_url(base_url)
    if not base_url:
        raise SearxError("Адрес SearXNG не задан: укажи его во вкладке «Веб».")
    if category not in CATEGORIES:
        raise SearxError(f"Неизвестная категория «{category}». Доступны: {', '.join(CATEGORIES)}.")
    params = {"q": query, "format": "json", "safesearch": "0"}
    if category != "general":
        params["categories"] = category
    if language:
        params["language"] = language
    try:
        with httpx.Client(transport=transport, timeout=timeout, trust_env=False) as client:
            r = client.get(base_url + "/search", params=params, headers={"User-Agent": "SaiHarness/1.0", "Accept": "application/json"})
    except httpx.TimeoutException:
        raise SearxError(f"SearXNG не ответил за {timeout:g} с ({base_url})") from None
    except httpx.HTTPError as e:
        raise SearxError(f"SearXNG недоступен ({base_url}): {e.__class__.__name__}") from None
    if r.status_code == 403:
        raise SearxError("SearXNG вернул 403: в его settings.yml нужно включить json в search.formats "
                         "(formats: [html, json]) и перезапустить сервис.")
    if r.status_code >= 400:
        raise SearxError(f"SearXNG ответил {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise SearxError("SearXNG вернул не JSON: проверь адрес и что в search.formats включён json.") from None
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
