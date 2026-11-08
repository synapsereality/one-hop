import csv
import gzip
import io
import itertools
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from one_hop import Checker, read_rules
from one_hop.cli import main
from one_hop.robots import Robots
from one_hop.snapshot import USER_AGENT, Snapshot, canonical_from_link_header, parse_sitemap, write_csv


class Server:
    """A local site. routes maps a path to (status, headers, body)."""

    def __init__(self, delay: float = 0.0):
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.seen: list[tuple[str, dict[str, str], float]] = []
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with server.lock:
                    server.seen.append((self.path, dict(self.headers), time.monotonic()))
                    server.in_flight += 1
                    server.max_in_flight = max(server.max_in_flight, server.in_flight)
                try:
                    if server.delay:
                        time.sleep(server.delay)
                    status, headers, body = server.routes.get(self.path, (404, {}, b""))
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    with server.lock:
                        server.in_flight -= 1

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def html(self, path, canonical=None, status=200):
        head = f'<link rel="canonical" href="{canonical}">' if canonical else ""
        body = f"<!doctype html><html><head><title>x</title>{head}</head><body>hi</body></html>"
        self.routes[path] = (status, {"Content-Type": "text/html; charset=utf-8"}, body.encode())

    def redirect(self, path, to, status=301):
        self.routes[path] = (status, {"Location": to}, b"")

    def robots(self, text, status=200):
        self.routes["/robots.txt"] = (status, {"Content-Type": "text/plain"}, text.encode())

    def sitemap(self, path, urls, index=False, gz=False):
        tag, item = ("sitemapindex", "sitemap") if index else ("urlset", "url")
        locs = "".join(f"<{item}><loc>{u}</loc></{item}>" for u in urls)
        xml = f'<?xml version="1.0" encoding="UTF-8"?><{tag} xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</{tag}>'
        data = gzip.compress(xml.encode()) if gz else xml.encode()
        self.routes[path] = (200, {"Content-Type": "application/xml"}, data)

    def paths(self):
        return [p for p, _, _ in self.seen]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv():
    s = Server()
    yield s
    s.close()


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def fast(**kw):
    return Snapshot(rate=1000, **kw)


def run(srv, sitemaps, extra=(), **kw):
    snap = fast(**kw)
    pages = snap.fetch_all(snap.collect(sitemaps, list(extra)))
    return snap, rows(write_csv(pages))


def test_sitemap_index_gzip_and_url_list(srv):
    u = srv.url
    srv.robots("User-agent: *\nAllow: /\n")
    srv.sitemap("/sitemap.xml", [f"{u}/sitemap-pages.xml.gz", f"{u}/sitemap-posts.xml"], index=True)
    srv.sitemap("/sitemap-pages.xml.gz", [f"{u}/", f"{u}/about/"], gz=True)
    srv.sitemap("/sitemap-posts.xml", [f"{u}/blog/a/", f"{u}/about/"])
    srv.html("/", canonical=f"{u}/")
    srv.html("/about/")
    srv.html("/blog/a/", canonical="/blog/a-new/")
    srv.redirect("/old-page", "/about/")
    snap, out = run(srv, [f"{u}/sitemap.xml"], [f"{u}/old-page", f"{u}/missing"])
    assert snap.problems == []
    assert [r["old"] for r in out] == ["/", "/about/", "/blog/a/", "/old-page", "/missing"]
    by = {r["old"]: r for r in out}
    assert by["/"]["status"] == "200" and by["/"]["canonical"] == f"{u}/"
    assert by["/about/"]["canonical"] == ""
    assert by["/blog/a/"]["canonical"] == f"{u}/blog/a-new/"
    assert by["/old-page"]["status"] == "301"
    assert by["/old-page"]["final_url"] == f"{u}/about/" and by["/old-page"]["final_status"] == "200"
    assert by["/missing"]["status"] == "404" and by["/missing"]["final_status"] == "404"
    assert all(r["new"] == "" for r in out)


def test_columns():
    assert write_csv([]) == "old,new,status,final_status,final_url,canonical,note\n"


def test_canonical_from_link_header(srv):
    u = srv.url
    srv.robots("")
    srv.routes["/doc.pdf"] = (200, {"Content-Type": "application/pdf",
                                    "Link": f'<{u}/doc/>; rel="canonical"'}, b"%PDF")
    _, out = run(srv, [], [f"{u}/doc.pdf"])
    assert out[0]["canonical"] == f"{u}/doc/"
    assert canonical_from_link_header(['<https://a.example/x>; rel=preload, <https://a.example/y>; rel="canonical"']) \
        == "https://a.example/y"


def test_canonical_after_body_is_ignored(srv):
    srv.robots("")
    body = b'<html><head></head><body><link rel="canonical" href="/late/"></body></html>'
    srv.routes["/p"] = (200, {"Content-Type": "text/html"}, body)
    _, out = run(srv, [], [f"{srv.url}/p"])
    assert out[0]["canonical"] == ""


def test_robots_disallowed_url_is_listed_but_not_fetched(srv):
    u = srv.url
    srv.robots("User-agent: *\nDisallow: /private/\n")
    srv.sitemap("/sitemap.xml", [f"{u}/", f"{u}/private/x"])
    srv.html("/")
    srv.html("/private/x")
    _, out = run(srv, [f"{u}/sitemap.xml"])
    assert out[1]["old"] == "/private/x" and out[1]["status"] == ""
    assert out[1]["note"] == "disallowed by robots.txt"
    assert "/private/x" not in srv.paths()


def test_robots_group_for_one_hop_wins_over_star(srv):
    u = srv.url
    srv.robots("User-agent: *\nDisallow: /\n\nUser-agent: one-hop\nAllow: /\n")
    srv.html("/a")
    _, out = run(srv, [], [f"{u}/a"])
    assert out[0]["status"] == "200"


def test_redirect_into_a_disallowed_path_stops_there(srv):
    u = srv.url
    srv.robots("User-agent: *\nDisallow: /private/\n")
    srv.redirect("/a", "/private/b")
    srv.html("/private/b")
    _, out = run(srv, [], [f"{u}/a"])
    assert out[0]["status"] == "301" and out[0]["final_url"] == f"{u}/private/b"
    assert out[0]["note"] == "redirect target not fetched: disallowed by robots.txt"
    assert "/private/b" not in srv.paths()


@pytest.mark.parametrize("status", [500, 503, 429])
def test_unreachable_robots_means_nothing_is_fetched(srv, status):
    srv.robots("", status=status)
    srv.html("/a")
    _, out = run(srv, [], [f"{srv.url}/a"])
    assert out[0]["note"] == f"robots.txt answered {status}, so this host was skipped"
    assert srv.paths() == ["/robots.txt"]


def test_missing_robots_allows_everything(srv):
    srv.html("/a")
    _, out = run(srv, [], [f"{srv.url}/a"])
    assert out[0]["status"] == "200"
    assert srv.paths() == ["/robots.txt", "/a"]


def test_robots_is_read_once_per_host(srv):
    srv.robots("")
    for p in ("/a", "/b", "/c"):
        srv.html(p)
    run(srv, [], [f"{srv.url}{p}" for p in ("/a", "/b", "/c")], concurrency=3)
    assert srv.paths().count("/robots.txt") == 1


def test_sitemap_disallowed_by_robots_is_not_read(srv):
    srv.robots("User-agent: *\nDisallow: /sitemap.xml\n")
    srv.sitemap("/sitemap.xml", [f"{srv.url}/"])
    snap = fast()
    assert snap.collect([f"{srv.url}/sitemap.xml"]) == []
    assert "disallowed by robots.txt" in snap.problems[0]
    assert "/sitemap.xml" not in srv.paths()


def test_user_agent_names_one_hop_and_the_repo(srv):
    srv.robots("")
    srv.html("/a")
    run(srv, [], [f"{srv.url}/a"])
    agents = {h.get("User-Agent") for _, h, _ in srv.seen}
    assert agents == {USER_AGENT}
    assert USER_AGENT.startswith("one-hop/") and "https://github.com/synapsereality/one-hop" in USER_AGENT


def test_rate_limit_spaces_requests_to_a_host(srv):
    srv.robots("")
    for i in range(5):
        srv.html(f"/{i}")
    snap = Snapshot(rate=10, concurrency=4)
    snap.fetch_all([f"{srv.url}/{i}" for i in range(5)])
    times = sorted(t for _, _, t in srv.seen)
    gaps = [b - a for a, b in itertools.pairwise(times)]
    # robots.txt plus 5 pages at 10 a second: at least 0.1 s between any two.
    assert len(times) == 6 and min(gaps) >= 0.08


def test_crawl_delay_slows_the_rate(srv):
    srv.robots("User-agent: *\nCrawl-delay: 0.3\n")
    srv.html("/a")
    srv.html("/b")
    Snapshot(rate=100, concurrency=2).fetch_all([f"{srv.url}/a", f"{srv.url}/b"])
    page_times = sorted(t for p, _, t in srv.seen if p != "/robots.txt")
    assert page_times[1] - page_times[0] >= 0.25


def test_concurrency_limit():
    slow = Server(delay=0.15)
    try:
        slow.robots("")
        for i in range(6):
            slow.html(f"/{i}")
        Snapshot(rate=1000, concurrency=2).fetch_all([f"{slow.url}/{i}" for i in range(6)])
        assert slow.max_in_flight == 2
    finally:
        slow.close()


def test_redirect_loop_and_too_many_hops(srv):
    srv.robots("")
    srv.redirect("/a", "/b")
    srv.redirect("/b", "/a")
    srv.redirect("/c", "/d")
    srv.redirect("/d", "/e")
    srv.redirect("/e", "/f")
    _, out = run(srv, [], [f"{srv.url}/a", f"{srv.url}/c"], max_hops=1)
    assert out[0]["note"] == "redirect loop"
    assert out[1]["note"] == "more than 1 redirects"


def test_connection_refused_is_a_row():
    snap = Snapshot(rate=1000, timeout=2)
    page = snap.fetch("http://127.0.0.1:1/a")
    assert page.status is None
    assert page.note == "robots.txt could not be fetched (ConnectionRefusedError), so this host was skipped"


def test_other_hosts_keep_full_urls(srv):
    srv.robots("")
    srv.html("/a")
    srv.html("/b")
    other = f"http://localhost:{srv.httpd.server_port}/c"
    srv.html("/c")
    _, out = run(srv, [], [f"{srv.url}/a", f"{srv.url}/b?x=1", other])
    assert [r["old"] for r in out] == ["/a", "/b?x=1", other]


def test_snapshot_is_a_redirect_map_one_hop_can_check(srv):
    """The CSV, with new left empty, says every URL must still answer 200. After the
    move, a URL nobody redirected fails."""
    srv.robots("")
    srv.html("/a")
    srv.html("/b")
    snap = fast()
    text = write_csv(snap.fetch_all([f"{srv.url}/a", f"{srv.url}/b"]))
    rules = read_rules(text)
    assert [(r.old, r.new) for r in rules] == [("/a", ""), ("/b", "")]
    checker = Checker(srv.url)
    assert all(checker.check(r).ok for r in rules)
    del srv.routes["/b"]
    assert [checker.check(r).kind for r in rules] == ["ok", "not_found"]


def test_max_urls(srv):
    srv.robots("")
    srv.sitemap("/s.xml", [f"{srv.url}/{i}" for i in range(5)])
    snap = fast(max_urls=3)
    assert len(snap.collect([f"{srv.url}/s.xml"])) == 3
    assert "max-urls" in snap.problems[0]


@pytest.mark.parametrize("data", [
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>',
    b"<html><body>not a sitemap</body></html>",
    b"<urlset><url><loc>",
])
def test_bad_sitemaps_are_refused(data):
    with pytest.raises(ValueError):
        parse_sitemap(data, "s.xml")


def test_cli_writes_csv_and_exit_codes(srv, tmp_path, capsys):
    u = srv.url
    srv.robots("")
    srv.sitemap("/sitemap.xml", [f"{u}/a"])
    srv.html("/a")
    urls = tmp_path / "urls.txt"
    urls.write_text(f"# extra\n{u}/a\n\n{u}/gone\n")
    out = tmp_path / "snapshot.csv"
    assert main(["snapshot", f"{u}/sitemap.xml", "--urls", str(urls), "--out", str(out), "--rate", "1000"]) == 0
    got = rows(out.read_text())
    assert [(r["old"], r["status"]) for r in got] == [("/a", "200"), ("/gone", "404")]
    assert "wrote 2 rows" in capsys.readouterr().err

    # One sitemap unreadable: the CSV still has the rest, and the exit code says so.
    assert main(["snapshot", f"{u}/sitemap.xml", f"{u}/nope.xml", "--out", str(out), "--rate", "1000"]) == 1
    assert len(rows(out.read_text())) == 1
    assert "nope.xml: HTTP 404" in capsys.readouterr().err

    # A local sitemap file, written to stdout.
    local = tmp_path / "sitemap.xml"
    local.write_bytes(srv.routes["/sitemap.xml"][2])
    assert main(["snapshot", str(local), "--rate", "1000"]) == 0
    assert capsys.readouterr().out.startswith("old,new,status")


def test_cli_bad_input(tmp_path, capsys):
    assert main(["snapshot"]) == 2
    assert main(["snapshot", "--urls", str(tmp_path / "missing.txt")]) == 2
    assert main(["snapshot", str(tmp_path / "missing.xml")]) == 2
    assert main(["snapshot", "s.xml", "--rate", "0"]) == 2
    assert main(["snapshot", "s.xml", "--concurrency", "0"]) == 2
    with pytest.raises(SystemExit) as e:
        main(["snapshot", "--bogus"])
    assert e.value.code == 2


def test_check_still_works_without_the_subcommand(srv, tmp_path):
    srv.routes["/a"] = (200, {}, b"")
    m = tmp_path / "map.csv"
    m.write_text("old,new\n/a,\n")
    assert main([str(m), "--base", srv.url]) == 0


# robots.txt rules (RFC 9309)


def allows(text, path, token="one-hop"):
    return Robots.parse(text).allows("https://example.com" + path, token)


def test_longest_match_wins():
    text = "User-agent: *\nDisallow: /\nAllow: /blog/\n"
    assert allows(text, "/blog/post") and not allows(text, "/about")
    # Order in the file doesn't matter, only length.
    text = "User-agent: *\nAllow: /blog/\nDisallow: /\nDisallow: /blog/drafts/\nAllow: /a\n"
    assert allows(text, "/blog/post") and not allows(text, "/blog/drafts/x") and not allows(text, "/b")


def test_allow_wins_a_tie():
    assert allows("User-agent: *\nDisallow: /a\nAllow: /a\n", "/a")


def test_wildcards_and_end_anchor():
    text = "User-agent: *\nDisallow: /*.pdf$\nDisallow: /tmp*/x\n"
    assert not allows(text, "/files/a.pdf")
    assert allows(text, "/files/a.pdf?download=1")
    assert not allows(text, "/tmp123/x")


def test_named_group_does_not_inherit_star():
    text = "User-agent: *\nDisallow: /a\n\nUser-agent: ONE-HOP\nDisallow: /b\n"
    assert allows(text, "/a") and not allows(text, "/b")


def test_groups_for_the_same_agent_are_merged():
    text = "User-agent: one-hop\nDisallow: /a\n\nUser-agent: other\nDisallow: /\n\nUser-agent: one-hop\nDisallow: /b\n"
    assert not allows(text, "/a") and not allows(text, "/b") and allows(text, "/c")


def test_several_agents_share_a_group():
    text = "User-agent: x\nUser-agent: one-hop\nDisallow: /\n"
    assert not allows(text, "/a")


def test_empty_disallow_and_comments():
    assert allows("User-agent: * # all\nDisallow:\n", "/anything")


def test_percent_encoding_matches_both_ways():
    assert not allows("User-agent: *\nDisallow: /caf%C3%A9\n", "/café")
    assert not allows("User-agent: *\nDisallow: /a~b\n", "/a%7Eb")


def test_robots_txt_itself_is_always_allowed():
    assert allows("User-agent: *\nDisallow: /\n", "/robots.txt")


def test_crawl_delay():
    assert Robots.parse("User-agent: *\nCrawl-delay: 5\n").crawl_delay("one-hop") == 5.0
    assert Robots.parse("User-agent: *\nCrawl-delay: soon\n").crawl_delay("one-hop") is None


# Regressions found in review


def test_sitemap_redirect_is_followed(srv):
    srv.robots("User-agent: *\nDisallow: /blocked/\n")
    srv.redirect("/sitemap.xml", "/sitemap_index.xml")
    srv.sitemap("/sitemap_index.xml", [f"{srv.url}/a"])
    srv.redirect("/s2.xml", "/blocked/s.xml")
    srv.sitemap("/blocked/s.xml", [f"{srv.url}/b"])
    snap = fast()
    assert snap.collect([f"{srv.url}/sitemap.xml", f"{srv.url}/s2.xml"]) == [f"{srv.url}/a"]
    assert "disallowed by robots.txt" in snap.problems[0] and "/blocked/s.xml" not in srv.paths()


def test_double_slash_path_keeps_its_host(srv):
    srv.robots("")
    srv.html("/a")
    srv.routes["//cdn.example/x"] = (200, {}, b"")
    _, out = run(srv, [], [f"{srv.url}/a", f"{srv.url}//cdn.example/x"])
    assert [r["old"] for r in out] == ["/a", f"{srv.url}//cdn.example/x"]


def test_endless_sitemap_indexes_stop(srv, monkeypatch, tmp_path, capsys):
    from one_hop import snapshot

    monkeypatch.setattr(snapshot, "MAX_SITEMAPS", 5)
    srv.robots("")
    srv.html("/a")
    srv.sitemap("/s0.xml", [f"{srv.url}/a"])
    for i in range(1, 20):
        srv.sitemap(f"/i{i}.xml", [f"{srv.url}/i{i + 1}.xml"], index=True)
    out = tmp_path / "s.csv"
    code = main(["snapshot", f"{srv.url}/s0.xml", f"{srv.url}/i1.xml", "--out", str(out), "--rate", "1000"])
    assert code == 1 and "sitemap limit: read 5 sitemaps" in capsys.readouterr().err
    assert len(rows(out.read_text())) == 1


def test_max_urls_is_not_an_error(srv, tmp_path, capsys):
    srv.robots("")
    srv.sitemap("/s.xml", [f"{srv.url}/a", f"{srv.url}/b"])
    out = tmp_path / "s.csv"
    assert main(["snapshot", f"{srv.url}/s.xml", "--max-urls", "1", "--out", str(out), "--rate", "1000"]) == 0
    assert "stopped at --max-urls 1; 1 more URLs not read" in capsys.readouterr().err


def test_unwritable_out_fails_before_fetching(srv, tmp_path):
    srv.robots("")
    srv.html("/a")
    urls = tmp_path / "u.txt"
    urls.write_text(f"{srv.url}/a\n")
    assert main(["snapshot", "--urls", str(urls), "--out", str(tmp_path / "no-dir" / "s.csv")]) == 2
    assert srv.paths() == []


def test_failed_run_keeps_the_earlier_snapshot(tmp_path):
    out = tmp_path / "s.csv"
    out.write_text("earlier\n")
    assert main(["snapshot", str(tmp_path / "missing.xml"), "--out", str(out)]) == 2
    assert out.read_text() == "earlier\n"
    assert [p.name for p in tmp_path.iterdir()] == ["s.csv"]


def test_utf16_sitemap_is_refused():
    xml = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>'
    with pytest.raises(ValueError, match="not UTF-8"):
        parse_sitemap(xml.encode("utf-16"), "s.xml")


def test_doctype_late_in_the_file_is_refused():
    xml = "<?xml version='1.0'?>" + " " * 5000 + "<!DOCTYPE urlset><urlset></urlset>"
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_sitemap(xml.encode(), "s.xml")


@pytest.mark.parametrize("value,expected", [("inf", None), ("nan", None), ("1e9", 60.0), ("-3", 0.0)])
def test_crawl_delay_is_bounded(value, expected):
    assert Robots.parse(f"User-agent: *\nCrawl-delay: {value}\n").crawl_delay("one-hop") == expected


def test_first_page_waits_the_crawl_delay(srv):
    srv.robots("User-agent: *\nCrawl-delay: 0.3\n")
    srv.html("/a")
    Snapshot(rate=100).fetch_all([f"{srv.url}/a"])
    (robots_at, page_at) = [t for _, _, t in srv.seen]
    assert page_at - robots_at >= 0.25


def test_robots_bom_is_ignored(srv):
    srv.routes["/robots.txt"] = (200, {"Content-Type": "text/plain"}, "﻿User-agent: *\nDisallow: /\n".encode())
    srv.html("/a")
    _, out = run(srv, [], [f"{srv.url}/a"])
    assert out[0]["note"] == "disallowed by robots.txt"


def test_escaped_reserved_characters_stay_escaped():
    assert allows("User-agent: *\nDisallow: /a%2Ab\n", "/aXYZb")
    assert not allows("User-agent: *\nDisallow: /a%2Ab\n", "/a%2ab")
    assert allows("User-agent: *\nDisallow: /a%2Fb\n", "/a/b")


def test_map_file_named_snapshot_is_still_checked(srv, tmp_path, monkeypatch):
    srv.routes["/a"] = (200, {}, b"")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "snapshot").write_text("old,new\n/a,\n")
    assert main(["snapshot", "--base", srv.url]) == 0


def test_redirect_to_another_scheme(srv):
    srv.robots("")
    srv.redirect("/a", "ftp://example.com/a")
    _, out = run(srv, [], [f"{srv.url}/a"])
    assert out[0]["status"] == "301"
    assert out[0]["note"] == "redirects to a URL that isn't http or https: ftp://example.com/a"


@pytest.mark.parametrize("flags", [["--timeout", "0"], ["--max-hops", "-1"]])
def test_bad_timeout_and_hops(flags):
    assert main(["snapshot", "s.xml", *flags]) == 2
