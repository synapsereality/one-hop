"""Record a site's URL inventory before a move: each URL, its status, where it ends up
and its canonical.

The URLs come from the sitemap (indexes and .gz files are followed) and from an
optional list file. Every request is paced per host, robots.txt is read first and
obeyed for the "one-hop" token, and the user agent names one-hop and its repository.
The output is a CSV whose `old` and `new` columns are the redirect map that
`one-hop CSV --base URL` reads. Fill in `new` for each URL that moves.

It records the inventory, not the site: no page bodies are kept.
"""

from __future__ import annotations

import csv
import http.client
import io
import ssl
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import __version__
from .core import normalise
from .robots import Robots

__all__ = ["COLUMNS", "Page", "Snapshot", "read_url_list", "write_csv"]

TOKEN = "one-hop"
REPO = "https://github.com/synapsereality/one-hop"
USER_AGENT = f"one-hop/{__version__} (+{REPO})"
COLUMNS = ("old", "new", "status", "final_status", "final_url", "canonical", "note")

# Limits on what is read, so a hostile or broken site can't exhaust memory.
MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # the sitemap protocol's own limit, uncompressed
MAX_PAGE_BYTES = 1024 * 1024  # enough to find <link rel="canonical"> in the head
MAX_ROBOTS_REDIRECTS = 5  # RFC 9309 2.3.1.2
MAX_SITEMAP_REDIRECTS = 5
MAX_SITEMAPS = 1000  # sitemap files read in one run, indexes included
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


@dataclass
class Page:
    url: str
    status: int | None = None
    final_status: int | None = None
    final_url: str = ""
    canonical: str = ""
    note: str = ""


@dataclass
class _Response:
    url: str
    status: int
    headers: http.client.HTTPMessage
    body: bytes


class _CanonicalParser(HTMLParser):
    """Finds the first <link rel="canonical" href> before the body starts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href: str | None = None
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        if tag == "body":
            self.done = True
        elif tag == "link":
            a = {k.lower(): (v or "") for k, v in attrs}
            if "canonical" in a.get("rel", "").lower().split() and a.get("href"):
                self.href = a["href"].strip()
                self.done = True


def canonical_from_html(body: bytes, charset: str | None) -> str | None:
    parser = _CanonicalParser()
    try:
        parser.feed(body.decode(charset or "utf-8", errors="replace"))
    except (LookupError, AssertionError):
        parser.feed(body.decode("utf-8", errors="replace"))
    return parser.href


def canonical_from_link_header(values: list[str]) -> str | None:
    """`Link: <https://example.com/a>; rel="canonical"` (RFC 8288)."""
    for value in values:
        for part in value.split(","):
            if "<" not in part or ">" not in part:
                continue
            target = part[part.index("<") + 1 : part.index(">")]
            params = part[part.index(">") + 1 :].lower().replace(" ", "")
            if 'rel="canonical"' in params or "rel=canonical" in params:
                return target.strip()
    return None


def read_url_list(text: str) -> list[str]:
    """One URL per line. Blank lines and lines starting with # are skipped."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _decompress(data: bytes, url: str) -> bytes:
    if data[:2] != b"\x1f\x8b":
        return data
    out = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, MAX_SITEMAP_BYTES + 1)
    if len(out) > MAX_SITEMAP_BYTES:
        raise ValueError(f"{url}: more than {MAX_SITEMAP_BYTES} bytes once uncompressed")
    return out


def parse_sitemap(data: bytes, url: str) -> tuple[list[str], list[str]]:
    """Return (page URLs, child sitemap URLs) from a urlset or a sitemapindex."""
    # The sitemap protocol requires UTF-8. Checking that first also means the DOCTYPE
    # test below can't be dodged with another encoding, such as UTF-16.
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{url}: not UTF-8, which the sitemap protocol requires") from None
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        # No sitemap needs a DTD, and entities are how XML bombs work.
        raise ValueError(f"{url}: sitemaps with a DOCTYPE or entities are not read")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"{url}: not a valid sitemap ({e})") from None
    tag = root.tag.removeprefix(SITEMAP_NS)
    locs = [(el.text or "").strip() for el in root.iter() if el.tag.removeprefix(SITEMAP_NS) == "loc"]
    locs = [urljoin(url, loc) for loc in locs if loc]
    if tag == "sitemapindex":
        return [], locs
    if tag == "urlset":
        return locs, []
    raise ValueError(f"{url}: expected <urlset> or <sitemapindex>, got <{tag}>")


class _Pacer:
    """At most one request per host every `interval` seconds, across all threads."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.extra: dict[str, float] = {}
        self.next: dict[str, float] = {}
        self.lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next.get(host, now))
            self.next[host] = at + max(self.interval, self.extra.get(host, 0.0))
        if at > now:
            time.sleep(at - now)


class Snapshot:
    """Reads sitemaps and fetches each URL, slowly and within robots.txt."""

    def __init__(
        self,
        *,
        concurrency: int = 2,
        rate: float = 2.0,
        timeout: float = 20.0,
        max_hops: int = 10,
        max_urls: int = 50000,
        insecure: bool = False,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if not rate > 0:
            raise ValueError("rate must be more than 0 requests a second")
        if not timeout > 0:
            raise ValueError("timeout must be more than 0 seconds")
        if max_hops < 0:
            raise ValueError("max-hops can't be negative")
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_hops = max_hops
        self.max_urls = max_urls
        self.pacer = _Pacer(1.0 / rate)
        self._ssl = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        self._robots: dict[str, Robots] = {}
        self._robots_locks: dict[str, threading.Lock] = {}
        self._robots_guard = threading.Lock()
        self.problems: list[str] = []

    # ----------------------------------------------------------------- http ---
    def _get(self, url: str, limit: int) -> _Response:
        p = urlsplit(url)
        if p.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                p.hostname, p.port, timeout=self.timeout, context=self._ssl)
        elif p.scheme == "http":
            conn = http.client.HTTPConnection(p.hostname, p.port, timeout=self.timeout)
        else:
            raise ValueError(f"unsupported URL scheme in {url!r}")
        path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        self.pacer.wait(p.netloc.lower())
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "*/*",
                                               "Accept-Encoding": "identity"})
            r = conn.getresponse()
            body = r.read(limit) if 200 <= r.status < 300 else b""
            return _Response(url, r.status, r.headers, body)
        finally:
            conn.close()

    def robots_for(self, url: str) -> Robots:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc.lower()}"
        with self._robots_guard:
            if origin in self._robots:
                return self._robots[origin]
            lock = self._robots_locks.setdefault(origin, threading.Lock())
        with lock:
            if origin not in self._robots:
                self._robots[origin] = self._fetch_robots(origin)
                delay = self._robots[origin].crawl_delay(TOKEN)
                if delay:
                    host = p.netloc.lower()
                    with self.pacer.lock:
                        self.pacer.extra[host] = max(delay, self.pacer.extra.get(host, 0.0))
                        # The next request waits the delay, not just the --rate interval.
                        now = time.monotonic()
                        self.pacer.next[host] = max(self.pacer.next.get(host, now), now + delay)
            return self._robots[origin]

    def _fetch_robots(self, origin: str) -> Robots:
        url = origin + "/robots.txt"
        try:
            for _ in range(MAX_ROBOTS_REDIRECTS + 1):
                r = self._get(url, 512 * 1024)
                if 300 <= r.status < 400 and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                break
            else:
                return Robots.unreachable("robots.txt redirected too many times, so this host was skipped")
        except Exception as e:  # noqa: BLE001 - no answer means "disallow everything"
            return Robots.unreachable(f"robots.txt could not be fetched ({type(e).__name__}), so this host was skipped")
        if 200 <= r.status < 300:
            return Robots.parse(r.body.decode("utf-8-sig", errors="replace"))
        if 400 <= r.status < 500 and r.status != 429:
            return Robots.unavailable()
        return Robots.unreachable(f"robots.txt answered {r.status}, so this host was skipped")

    def allowed(self, url: str) -> bool:
        return self.robots_for(url).allows(url, TOKEN)

    def refusal(self, url: str) -> str:
        """Why `url` isn't fetched, for the note column."""
        return self.robots_for(url).reason or "disallowed by robots.txt"

    # ------------------------------------------------------------- sitemaps ---
    def _read_source(self, src: str) -> bytes:
        if urlsplit(src).scheme in ("http", "https"):
            url = src
            for _ in range(MAX_SITEMAP_REDIRECTS + 1):
                if urlsplit(url).scheme not in ("http", "https"):
                    raise ValueError(f"{src}: redirects to {url}, which isn't http or https")
                if not self.allowed(url):
                    raise ValueError(f"{src}: {self.refusal(url)}" + ("" if url == src else f" ({url})"))
                r = self._get(url, MAX_SITEMAP_BYTES + 1)
                if 300 <= r.status < 400 and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                break
            else:
                raise ValueError(f"{src}: more than {MAX_SITEMAP_REDIRECTS} redirects")
            if r.status != 200:
                raise ValueError(f"{src}: HTTP {r.status}" + ("" if url == src else f" at {url}"))
            data = r.body
        else:
            with open(src, "rb") as f:
                data = f.read(MAX_SITEMAP_BYTES + 1)
        if len(data) > MAX_SITEMAP_BYTES:
            raise ValueError(f"{src}: more than {MAX_SITEMAP_BYTES} bytes")
        return _decompress(data, src)

    def collect(self, sitemaps: list[str], extra_urls: list[str] = ()) -> list[str]:
        """Every page URL from the sitemaps (following indexes), then the extra URLs, in
        order and without duplicates, up to max_urls. Unreadable sitemaps go to
        self.problems."""
        urls: dict[str, None] = {}
        queue = list(sitemaps)
        seen: set[str] = set()
        while queue and len(urls) < self.max_urls:
            src = queue.pop(0)
            if src in seen:
                continue
            if len(seen) >= MAX_SITEMAPS:
                self.problems.append(f"sitemap limit: read {MAX_SITEMAPS} sitemaps, {len(set(queue) - seen) + 1} more not read")
                queue.clear()
                break
            seen.add(src)
            try:
                pages, children = parse_sitemap(self._read_source(src), src)
            except (OSError, ValueError, http.client.HTTPException) as e:
                self.problems.append(f"sitemap {e}" if str(e).startswith(src) else f"sitemap {src}: {e}")
                continue
            for page in pages:
                urls.setdefault(page, None)
            queue.extend(children)
        for url in extra_urls:
            urls.setdefault(url, None)
        result = list(urls)
        unread = len(set(queue) - seen)
        if len(result) > self.max_urls or unread:
            more = [f"{len(result) - self.max_urls} more URLs"] if len(result) > self.max_urls else []
            more += [f"{unread} sitemap(s)"] if unread else []
            self.problems.append(f"stopped at --max-urls {self.max_urls}; {' and '.join(more)} not read")
            result = result[: self.max_urls]
        return result

    # ---------------------------------------------------------------- pages ---
    def fetch(self, url: str) -> Page:
        page = Page(url)
        if urlsplit(url).scheme not in ("http", "https"):
            page.note = "not an http or https URL"
            return page
        current = url
        visited: set[str] = set()
        try:
            for hop in range(self.max_hops + 1):
                if urlsplit(current).scheme not in ("http", "https"):
                    page.final_url = current
                    page.note = f"redirects to a URL that isn't http or https: {current}"
                    return page
                if not self.allowed(current):
                    reason = self.refusal(current)
                    page.note = reason if hop == 0 else f"redirect target not fetched: {reason}"
                    page.final_url = current if hop else ""
                    return page
                visited.add(normalise(current))
                r = self._get(current, MAX_PAGE_BYTES)
                if page.status is None:
                    page.status = r.status
                location = r.headers.get("Location")
                if 300 <= r.status < 400 and location:
                    nxt = urljoin(current, location)
                    if normalise(nxt) in visited:
                        page.final_url, page.final_status = nxt, None
                        page.note = "redirect loop"
                        return page
                    current = nxt
                    continue
                page.final_url, page.final_status = current, r.status
                if 200 <= r.status < 300:
                    found = canonical_from_link_header(r.headers.get_all("Link") or [])
                    ctype = r.headers.get_content_type()
                    if found is None and ctype in ("text/html", "application/xhtml+xml"):
                        found = canonical_from_html(r.body, r.headers.get_content_charset())
                    if found:
                        page.canonical = urljoin(current, found)
                return page
            page.final_url = current
            page.note = f"more than {self.max_hops} redirects"
            return page
        except Exception as e:  # noqa: BLE001 - one bad URL is a row, not a crash
            page.note = f"request failed: {type(e).__name__}: {e}"
            return page

    def fetch_all(self, urls: list[str], progress=None) -> list[Page]:
        done = 0
        lock = threading.Lock()

        def one(url: str) -> Page:
            nonlocal done
            page = self.fetch(url)
            if progress:
                with lock:
                    done += 1
                    progress(done, len(urls))
            return page

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            return list(pool.map(one, urls))


def _relative(url: str) -> str | None:
    """The path and query of `url`, or None when that would read back as another URL.

    A path that starts with // reads as a host ("//cdn.example/x"), so such a URL keeps
    its scheme and host.
    """
    p = urlsplit(url)
    path = p.path or "/"
    if path.startswith("//"):
        return None
    return urlunsplit(("", "", path, p.query, ""))


def main_host(urls: list[str]) -> str | None:
    """The scheme and host most URLs share. Ties go to the one seen first."""
    counts = Counter(urlsplit(u)[:2] for u in urls if urlsplit(u).netloc)
    if not counts:
        return None
    best = max(counts.values())
    for u in urls:
        key = urlsplit(u)[:2]
        if counts.get(key) == best:
            return "{}://{}".format(*key)
    return None


def write_csv(pages: list[Page], out: io.TextIOBase | None = None, *, absolute: bool = False) -> str:
    """Write the snapshot. `old` is a path for URLs on the main host, so the file works
    with `--base` pointing at staging or the new site. Other hosts keep the full URL."""
    home = None if absolute else main_host([p.url for p in pages])
    buf = out or io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for p in pages:
        on_home = home is not None and "{}://{}".format(*urlsplit(p.url)[:2]) == home
        old = (_relative(p.url) if on_home else None) or p.url
        w.writerow([old, "", p.status or "", p.final_status or "", p.final_url, p.canonical, p.note])
    return buf.getvalue() if out is None else ""


def write_file(pages: list[Page], path: str, *, absolute: bool = False) -> None:
    Path(path).write_text(write_csv(pages, absolute=absolute), encoding="utf-8")
