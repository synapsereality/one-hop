"""robots.txt rules as RFC 9309 defines them, for the snapshot command.

Python's urllib.robotparser applies the first matching rule in file order. RFC 9309
says the longest matching rule wins and Allow wins a tie, so a file such as
"Disallow: /" followed by "Allow: /blog/" reads differently. This module follows
the RFC, which is also what Google documents.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

# RFC 9309 2.5: crawlers should parse at least 500 KiB.
MAX_BYTES = 500 * 1024
# A Crawl-delay above this is read as this, so one line can't stall a run for days.
MAX_CRAWL_DELAY = 60.0
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: float | None = None


def _normalise_path(path: str) -> str:
    """Spell a path the same way on both sides (RFC 9309 2.2.2).

    An escape of an unreserved character is decoded, so /a%7Eb matches /a~b. Other
    escapes stay escaped, in upper case, so %2A is never a wildcard and %2F is not a
    path separator. Characters outside ASCII are UTF-8 percent-encoded.
    """

    def unescape(m: re.Match[str]) -> str:
        char = chr(int(m.group(1), 16))
        return char if char in _UNRESERVED else "%" + m.group(1).upper()

    return quote(_ESCAPE.sub(unescape, path), safe="/?=&*$:@!,;+'()[]%-._~")


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in _normalise_path(body).split("*"))
    return re.compile(regex + ("$" if anchored else ""))


class Robots:
    """One site's robots.txt. `allows(url)` answers for the given product token."""

    def __init__(self, groups: list[Group], *, allow_all: bool = False, disallow_all: bool = False,
                 reason: str = ""):
        self.groups = groups
        self.allow_all = allow_all
        self.disallow_all = disallow_all
        self.reason = reason  # why everything is disallowed, when it is

    @classmethod
    def unavailable(cls) -> Robots:
        """A 4xx robots.txt: no rules, everything allowed (RFC 9309 2.3.1.3)."""
        return cls([], allow_all=True)

    @classmethod
    def unreachable(cls, reason: str = "") -> Robots:
        """A 5xx, a 429 or no answer: assume everything is disallowed (RFC 9309 2.3.1.4)."""
        return cls([], disallow_all=True, reason=reason)

    @classmethod
    def parse(cls, text: str) -> Robots:
        groups: list[Group] = []
        current: Group | None = None
        last_was_agent = False
        for raw in text[:MAX_BYTES].splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = Group()
                    groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if current is None:
                continue
            if key in ("allow", "disallow"):
                # An empty Disallow allows everything, so it adds no rule.
                if value:
                    current.rules.append((key == "allow", value))
            elif key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if math.isfinite(delay):
                    current.crawl_delay = min(max(0.0, delay), MAX_CRAWL_DELAY)
        return cls(groups)

    def _group_for(self, token: str) -> Group | None:
        token = token.lower()
        named = [g for g in self.groups if token in g.agents]
        chosen = named or [g for g in self.groups if "*" in g.agents]
        if not chosen:
            return None
        # Groups that name the same crawler are merged (RFC 9309 2.2.1).
        merged = Group(agents=[token])
        for g in chosen:
            merged.rules += g.rules
            if g.crawl_delay is not None:
                merged.crawl_delay = max(merged.crawl_delay or 0.0, g.crawl_delay)
        return merged

    def allows(self, url: str, token: str) -> bool:
        if self.allow_all:
            return True
        if self.disallow_all:
            return False
        p = urlsplit(url)
        path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        if path == "/robots.txt":
            return True
        group = self._group_for(token)
        if group is None:
            return True
        path = _normalise_path(path)
        best_len, best_allow = -1, True
        for allow, pattern in group.rules:
            if _pattern_regex(pattern).match(path):
                length = len(pattern)
                if length > best_len or (length == best_len and allow):
                    best_len, best_allow = length, allow
        return best_allow

    def crawl_delay(self, token: str) -> float | None:
        if self.allow_all or self.disallow_all:
            return None
        group = self._group_for(token)
        return group.crawl_delay if group else None
