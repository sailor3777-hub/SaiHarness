"""HTML → readable text (standard library). Drops scripts, navigation and layout, keeps the structure."""
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag

HARD_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "select"}  # neither text nor links
SOFT_SKIP = {"form", "nav", "footer", "aside"}  # text is dropped, links are collected (the crawler finds the site structure through them)
SOFT_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search"}
SOFT_NAMES = {"sidebar", "sphinxsidebar", "related", "navigation", "navbar", "nav", "breadcrumb", "breadcrumbs", "menu",
              "footer", "toc", "mw-editsection", "mw-jump-link", "vector-toc", "vector-header-container", "vector-page-toolbar",
              "vector-sticky-header", "navbox", "catlinks", "printfooter", "mw-portlet", "vector-dropdown", "mw-panel"}
VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
BLOCK = {"p", "div", "section", "article", "main", "header", "ul", "ol", "table", "tr", "blockquote", "pre",
         "figure", "figcaption", "dl", "dt", "dd", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "li", "details", "summary"}
MAIN_TAGS = {"main", "article"}
MIN_MAIN_CHARS = 200


@dataclass
class Page:
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)


def _is_soft(tag: str, attrs: dict) -> bool:
    """A navigation or decoration block by role, class or id (many sites, Sphinx for example, do not use <nav>)."""
    if tag in VOID:
        return False
    if (attrs.get("role") or "").lower() in SOFT_ROLES:
        return True
    names = (attrs.get("class") or "").lower().split() + [(attrs.get("id") or "").lower()]
    return any(n in SOFT_NAMES or "cookie" in n or "sidebar" in n for n in names if n)


class _Parser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.skip = 0        # inside HARD_SKIP
        self.soft = 0        # inside SOFT_SKIP tags
        self.soft_tag = None  # root of a block chosen by class/role, and the depth of nested tags with the same name
        self.soft_nest = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.all: list[str] = []
        self.main: list[str] = []
        self.main_depth = 0
        self.in_pre = 0
        self.links: list[str] = []
        self.nav_links: list[str] = []

    def _in_soft(self) -> bool:
        return self.soft > 0 or self.soft_tag is not None

    def _add_link(self, attrs):
        href = dict(attrs).get("href")
        if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
            (self.nav_links if self._in_soft() else self.links).append(urldefrag(urljoin(self.base, href))[0])

    def _emit(self, s: str):
        self.all.append(s)
        if self.main_depth:
            self.main.append(s)

    def handle_starttag(self, tag, attrs):
        if tag in HARD_SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if self.soft_tag is not None:
            if tag == self.soft_tag:
                self.soft_nest += 1
        elif tag not in SOFT_SKIP and _is_soft(tag, dict(attrs)):
            self.soft_tag, self.soft_nest = tag, 1
        if tag in SOFT_SKIP:
            self.soft += 1
        if tag == "a":
            self._add_link(attrs)
        if self._in_soft():
            return
        if tag == "title":
            self.in_title = True
        if tag in MAIN_TAGS:
            self.main_depth += 1
        if tag == "pre":
            self.in_pre += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
                self.links.append(urldefrag(urljoin(self.base, href))[0])
        if tag in BLOCK:
            self._emit("\n")
        if tag[0] == "h" and len(tag) == 2 and tag[1].isdigit():
            self._emit("#" * int(tag[1]) + " ")
        elif tag == "li":
            self._emit("- ")

    def handle_endtag(self, tag):
        if tag in HARD_SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in SOFT_SKIP:
            self.soft = max(0, self.soft - 1)
            return
        if self.soft_tag is not None:
            if tag == self.soft_tag:
                self.soft_nest -= 1
                if self.soft_nest == 0:
                    self.soft_tag = None
            return
        if self.soft:
            return
        if tag == "title":
            self.in_title = False
        if tag in MAIN_TAGS:
            self.main_depth = max(0, self.main_depth - 1)
        if tag == "pre":
            self.in_pre = max(0, self.in_pre - 1)
        if tag in BLOCK or tag in ("td", "th"):
            self._emit("\n" if tag not in ("td", "th") else " ")

    def handle_data(self, data):
        if self.skip or self._in_soft():
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        self._emit(data.replace(" ", " ") if self.in_pre else re.sub(r"\s+", " ", data))


def _clean(parts: list[str]) -> str:
    text = "".join(parts)
    lines = [re.sub(r"[ \t]+", " ", ln).strip(" \t") for ln in text.split("\n")]
    out, blank = [], 0
    for ln in lines:
        if ln == "-":  # an empty list item (an icon or a link without text)
            continue
        if ln:
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1 and out:
                out.append("")
    return "\n".join(out).strip().replace(" ", " ")  # non-breaking spaces are indentation from <pre>


def extract(html: str, base_url: str = "") -> Page:
    p = _Parser(base_url)
    p.feed(html)
    p.close()
    main_text = _clean(p.main)
    text = main_text if len(main_text) >= MIN_MAIN_CHARS else _clean(p.all)
    title = re.sub(r"\s+", " ", "".join(p.title_parts)).strip()
    seen, links = set(), []
    for link in p.links + p.nav_links:
        if link not in seen:
            seen.add(link)
            links.append(link)
    return Page(title=title, text=text, links=links)
