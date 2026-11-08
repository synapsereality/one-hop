"""Command line entry point.

`one-hop CSV --base URL` checks a redirect map: exit 0 when every rule passes, 1 when
any fails, 2 on bad input. `one-hop snapshot SITEMAP` records the URL inventory before
a move: exit 0 when no sitemap failed, 1 when one did, 2 on bad input or no URLs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from . import __version__
from .core import KINDS, Checker, read_rules

if TYPE_CHECKING:
    from .snapshot import Snapshot

EPILOG = """\
examples:
  one-hop redirects.csv --base https://example.com
  one-hop redirects.csv --base https://staging.example.com --auth user:pass
  one-hop redirects.csv --base https://example.com --json report.json
  one-hop snapshot https://example.com/sitemap.xml --out snapshot.csv

The CSV has two columns, old URL then new URL. Leave the new URL empty for a
page that must answer 200 with no redirect. Docs:
https://synapsereality.io/open-source/one-hop/
"""


def _parse_header(raw: str) -> tuple[str, str]:
    name, sep, value = raw.partition(":")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(f"expected 'Name: value', got {raw!r}")
    return name.strip(), value.strip()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="one-hop",
        description="Check that every old URL redirects to its new URL in one hop, and the new URL answers 200.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("csv", nargs="+", help="redirect map(s): old,new per line; '-' reads stdin")
    ap.add_argument("--base", required=True, help="site to test, e.g. https://example.com")
    ap.add_argument("--workers", type=int, default=8, help="parallel requests (default 8)")
    ap.add_argument("--timeout", type=float, default=20.0, help="seconds per request (default 20)")
    ap.add_argument("--max-hops", type=int, default=10, help="stop following a chain after this many hops")
    ap.add_argument("--limit", type=int, default=0, help="check only the first N rules")
    ap.add_argument("--auth", help="user:password for HTTP basic auth, sent to the base host only "
                    "(or set ONE_HOP_AUTH, which keeps it out of the process list)")
    ap.add_argument("--header", action="append", type=_parse_header, default=[],
                    help="extra request header 'Name: value' (base host only; repeatable)")
    ap.add_argument("--method", choices=("HEAD", "GET"), default="HEAD",
                    help="HEAD by default; falls back to GET on a 405 or 501")
    ap.add_argument("--allow-temporary", action="store_true", help="accept 302, 303 and 307 as the one hop")
    ap.add_argument("--path-only", action="store_true",
                    help="compare only path and query of the target, not scheme and host")
    ap.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
    ap.add_argument("--json", metavar="FILE", help="write every result as JSON ('-' for stdout)")
    ap.add_argument("--show", type=int, default=50, help="print at most N failures (default 50)")
    ap.add_argument("--version", action="version", version=f"one-hop {__version__}")
    return ap


SNAPSHOT_EPILOG = """\
examples:
  one-hop snapshot https://example.com/sitemap.xml --out snapshot.csv
  one-hop snapshot https://example.com/sitemap.xml --urls extra-urls.txt --out snapshot.csv

Run it before the move. It reads robots.txt first and skips what it disallows for
"one-hop", fetches each URL slowly, and keeps no page content. The CSV's old and new
columns are a redirect map: fill in new, then run one-hop on it with --base.
Docs: https://synapsereality.io/open-source/one-hop/
"""


def build_snapshot_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="one-hop snapshot",
        description="Record every URL in a sitemap before a site move: its status, final URL and canonical.",
        epilog=SNAPSHOT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("sitemap", nargs="*", help="sitemap or sitemap index, as a URL or a file; .gz works")
    ap.add_argument("--urls", metavar="FILE", action="append", default=[],
                    help="also fetch the URLs in FILE, one per line ('-' reads stdin; repeatable)")
    ap.add_argument("--out", metavar="FILE", default="-", help="where to write the CSV (default: stdout)")
    ap.add_argument("--concurrency", type=int, default=2, help="requests in flight at once (default 2)")
    ap.add_argument("--rate", type=float, default=2.0,
                    help="requests a second to any one host (default 2). A slower Crawl-delay wins")
    ap.add_argument("--timeout", type=float, default=20.0, help="seconds per request (default 20)")
    ap.add_argument("--max-hops", type=int, default=10, help="stop following a redirect chain after N hops")
    ap.add_argument("--max-urls", type=int, default=50000, help="stop after N URLs (default 50000)")
    ap.add_argument("--absolute", action="store_true",
                    help="write full URLs in the old column instead of paths")
    ap.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
    ap.add_argument("--version", action="version", version=f"one-hop {__version__}")
    return ap


def snapshot_main(argv: list[str]) -> int:
    from .snapshot import Snapshot, read_url_list

    args = build_snapshot_parser().parse_args(argv)
    extra: list[str] = []
    try:
        for src in args.urls:
            text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8-sig")
            extra += read_url_list(text)
        snap = Snapshot(concurrency=args.concurrency, rate=args.rate, timeout=args.timeout,
                        max_hops=args.max_hops, max_urls=args.max_urls, insecure=args.insecure)
    except (OSError, ValueError) as e:
        print(f"one-hop snapshot: {e}", file=sys.stderr)
        return 2
    if not args.sitemap and not extra:
        print("one-hop snapshot: give at least one sitemap or --urls FILE", file=sys.stderr)
        return 2
    if args.max_urls < 1:
        print("one-hop snapshot: --max-urls must be at least 1", file=sys.stderr)
        return 2
    # Open the output first, so a bad path fails now and not after every fetch. Write to
    # a temporary file beside it, so a failed run never truncates an earlier snapshot.
    tmp = None if args.out == "-" else Path(args.out).with_name(f".{Path(args.out).name}.{os.getpid()}.tmp")
    try:
        out = sys.stdout if tmp is None else open(tmp, "w", encoding="utf-8", newline="")  # noqa: SIM115 - closed below
    except OSError as e:
        print(f"one-hop snapshot: can't write {args.out}: {e}", file=sys.stderr)
        return 2
    written = False
    try:
        code = _snapshot(args, snap, extra, out)
        written = code != 2
        return code
    finally:
        if tmp is not None:
            out.close()
            if written:
                os.replace(tmp, args.out)
            else:
                tmp.unlink(missing_ok=True)


def _snapshot(args: argparse.Namespace, snap: Snapshot, extra: list[str], out: TextIO) -> int:
    from .snapshot import write_csv

    urls = snap.collect(args.sitemap, extra)
    for problem in snap.problems:
        print(f"one-hop snapshot: {problem}", file=sys.stderr)
    if not urls:
        print("one-hop snapshot: no URLs to fetch", file=sys.stderr)
        return 2
    print(f"fetching {len(urls)} URLs, {args.rate:g} a second per host, {args.concurrency} at a time",
          file=sys.stderr)

    def progress(done: int, total: int) -> None:
        if done % 100 == 0 or done == total:
            print(f"  {done}/{total}", file=sys.stderr)

    pages = snap.fetch_all(urls, progress)
    out.write(write_csv(pages, absolute=args.absolute))
    statuses = Counter(str(p.status) if p.status else (p.note.split(":")[0] or "no answer") for p in pages)
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items()))
    where = "" if args.out == "-" else f" to {args.out}"
    print(f"wrote {len(pages)} rows{where} ({summary})", file=sys.stderr)
    return 1 if any(p.startswith("sitemap ") for p in snap.problems) else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # "snapshot" is the subcommand, unless --base says it's a map file of that name.
    if argv[:1] == ["snapshot"] and not any(a == "--base" or a.startswith("--base=") for a in argv):
        return snapshot_main(argv[1:])
    args = build_parser().parse_args(argv)
    rules = []
    try:
        for src in args.csv:
            text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8-sig")
            rules += read_rules(text)
    except (OSError, ValueError) as e:
        print(f"one-hop: {e}", file=sys.stderr)
        return 2
    if args.limit:
        rules = rules[: args.limit]
    if not rules:
        print("one-hop: no rules found in the input", file=sys.stderr)
        return 2

    headers = dict(args.header)
    auth = args.auth or os.environ.get("ONE_HOP_AUTH")
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    try:
        checker = Checker(args.base, headers=headers, timeout=args.timeout, max_hops=args.max_hops,
                          allow_temporary=args.allow_temporary, path_only=args.path_only,
                          method=args.method, insecure=args.insecure)
    except ValueError as e:
        print(f"one-hop: {e}", file=sys.stderr)
        return 2

    quiet = args.json == "-"
    log = sys.stderr if quiet else sys.stdout
    print(f"checking {len(rules)} rules against {args.base}", file=log)
    results = checker.check_all(rules, workers=args.workers)
    bad = [r for r in results if not r.ok]
    bad.sort(key=lambda r: (KINDS.index(r.kind), r.rule.line))

    counts = Counter(r.kind for r in results)
    print(f"{len(results) - len(bad)}/{len(results)} OK", file=log)
    for kind in KINDS:
        if kind != "ok" and counts[kind]:
            print(f"  {kind}: {counts[kind]}", file=log)
    for r in bad[: args.show]:
        where = f"line {r.rule.line}: " if r.rule.line else ""
        print(f"FAIL [{r.kind}] {where}{r.rule.old}  {r.detail}", file=log)
    if len(bad) > args.show:
        print(f"... and {len(bad) - args.show} more (use --json for all)", file=log)

    if args.json:
        payload = json.dumps({"base": args.base, "total": len(results), "failed": len(bad),
                              "results": [r.as_dict() for r in results]}, indent=2)
        if quiet:
            print(payload)
        else:
            Path(args.json).write_text(payload + "\n", encoding="utf-8")
    return 1 if bad else 0
