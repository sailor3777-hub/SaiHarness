"""Page loading: address checks on every hop, a size limit, robots.txt, a pause between requests, a disk cache."""
import hashlib
import json
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from core.web.extract import extract
from core.web.safety import UnsafeUrl, check_url

USER_AGENT = "SaiHarness/1.0 (+local agent harness)"
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
MIN_STATIC_CHARS = 80      # an almost empty page with scripts is drawn by JavaScript (SPAs give 0; example.com gives 156, not an SPA)
TEXT_TYPES = ("text/html", "application/xhtml", "text/plain", "application/xml", "text/xml")


class FetchError(Exception):
    """The page could not be fetched; the error text is shown to the model."""


@dataclass
class Fetched:
    url: str
    final_url: str
    title: str
    text: str
    links: list[str] = field(default_factory=list)
    from_cache: bool = False
    rendered: bool = False
    render_error: str = ""


class WebFetcher:
    def __init__(self, cache_dir: Path | None = None, ttl_minutes: float = 60, allow_private: bool = False,
                 blocklist=(), transport=None, resolve=socket.getaddrinfo, delay: float = 1.0,
                 renderer=None, clock=time.time, sleep=time.sleep, timeout: float = 20.0):
        self.cache_dir = cache_dir
        self.ttl = ttl_minutes * 60
        self.allow_private = allow_private
        self.blocklist = tuple(blocklist)
        self.transport = transport
        self.resolve = resolve
        self.delay = delay
        self.renderer = renderer  # callable(url) -> html; None means JS pages are not rendered
        self.clock, self.sleep = clock, sleep
        self.timeout = timeout
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    # --- network ---

    def _check(self, url: str) -> str:
        try:
            return check_url(url, self.allow_private, self.blocklist, self.resolve)
        except UnsafeUrl as e:
            raise FetchError(str(e)) from None

    def _throttle(self, host: str):
        wait = self._last_hit.get(host, 0) + self.delay - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last_hit[host] = self.clock()

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=self.timeout, follow_redirects=False, trust_env=False)

    def _get(self, client: httpx.Client, url: str) -> tuple[httpx.Response, bytes, str]:
        """GET with manual redirects: every hop is checked again. Returns (response, body, final URL)."""
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                   "Accept-Language": "ru,en;q=0.8"}
        for _ in range(MAX_REDIRECTS + 1):
            url = self._check(url)
            self._throttle(urlsplit(url).netloc)
            try:
                with client.stream("GET", url, headers=headers) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = str(httpx.URL(url).join(r.headers["location"]))
                        continue
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    return r, bytes(body[:MAX_BYTES]), url
            except httpx.TimeoutException:
                raise FetchError(f"Timeout while loading {url}") from None
            except httpx.HTTPError as e:
                raise FetchError(f"Network error while loading {url}: {e.__class__.__name__}") from None
        raise FetchError(f"Too many redirects (more than {MAX_REDIRECTS})")

    # --- robots.txt ---

    def robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = None
            try:
                with self._client() as client:
                    r, body, _ = self._get(client, origin + "/robots.txt")
                if r.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(body.decode("utf-8", "replace").splitlines())
            except FetchError:
                parser = None
            self._robots[origin] = parser
        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    # --- cache ---

    def _cache_file(self, url: str) -> Path | None:
        return self.cache_dir / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json") if self.cache_dir else None

    def _cache_get(self, url: str) -> Fetched | None:
        f = self._cache_file(url)
        if not f or not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if self.clock() - data["at"] > self.ttl:
                return None
            return Fetched(url, data["final_url"], data["title"], data["text"], data["links"], True, data["rendered"])
        except (OSError, ValueError, KeyError):
            return None

    def _cache_put(self, url: str, page: Fetched):
        f = self._cache_file(url)
        if not f or self.allow_private:  # local addresses are not cached
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"at": self.clock(), "final_url": page.final_url, "title": page.title, "text": page.text,
                                     "links": page.links, "rendered": page.rendered}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # --- public method ---

    def fetch(self, url: str, render: bool = False, use_cache: bool = True, robots: bool = False) -> Fetched:
        url = self._check(url)
        if use_cache and not render:
            hit = self._cache_get(url)
            if hit:
                return hit
        if robots and not self.robots_allowed(url):
            raise FetchError(f"The site's robots.txt forbids loading {url}")
        html, final_url, rendered, render_error = "", url, False, ""
        if not render:
            with self._client() as client:
                r, body, final_url = self._get(client, url)
            if r.status_code >= 400:
                raise FetchError(f"The site answered {r.status_code} for {url}")
            ctype = r.headers.get("content-type", "text/html").split(";")[0].strip().lower()
            if not ctype.startswith(TEXT_TYPES):
                raise FetchError(f"Content type {ctype} is not supported (text or HTML is required)")
            html = body.decode(r.charset_encoding or "utf-8", "replace")
            if ctype in ("text/plain", "application/xml", "text/xml"):
                page = Fetched(url, final_url, "", html.strip())
                self._cache_put(url, page)
                return page
        if render or self._looks_js_only(html):
            if self.renderer is None:
                if render:
                    raise FetchError("JavaScript rendering is unavailable (Qt WebEngine is not available)")
            else:
                try:
                    html, rendered = self.renderer(final_url), True
                except Exception as e:  # the renderer is a separate process; its failure must not break reading
                    if render:
                        raise FetchError(f"Could not render the page: {e}") from None
                    render_error = str(e)
        parsed = extract(html, final_url)
        page = Fetched(url, final_url, parsed.title, parsed.text, parsed.links, False, rendered, render_error)
        if not render_error:  # a failed render is not cached: next time it is worth trying again
            self._cache_put(url, page)
        return page

    @staticmethod
    def _looks_js_only(html: str) -> bool:
        return len(extract(html).text) < MIN_STATIC_CHARS and "<script" in html.lower()
