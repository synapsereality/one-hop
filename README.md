# one-hop

Checks a redirect map. Every old URL has to answer with one permanent redirect
(301 or 308) straight to its new URL. The new URL has to answer 200. Anything else
fails: a chain, a loop, a 404, a redirect to the wrong page. The exit code is then
1, so CI can stop the release.

Docs: https://synapsereality.io/open-source/one-hop/

```bash
pip install one-hop
one-hop redirects.csv --base https://example.com
```

## Why one hop

A chain such as `/about-us` to `/about` to `/about/` still loads in a browser, so
nobody spots it. Crawlers do. Google follows up to 10 hops and then gives up, and
every extra hop is one more request per URL. After a migration the map is often
right on paper and wrong on the server. One trailing-slash rule or a leftover
redirect plugin adds a second hop to hundreds of rules at once.

We wrote the first version for our own site move in September 2026, 2,030 rules
from a WordPress site to a static build. This is that script, made general.

## The CSV

Two columns, old URL then new URL. A header row is optional. With one, the columns
can be named `old`/`new`, `source`/`destination` or `from`/`to`, and extra columns
are ignored.

```csv
old,new
/blog/old-post,/blog/new-post/
/about-us,/about/
/services,https://example.com/services/seo/
/pricing,
```

Leave the new URL empty (as for `/pricing` above) when the page should answer 200
with no redirect at all. Relative URLs are resolved against `--base`. Lines that
start with `#` are skipped.

## Output

Against a local test server with five rules:

```text
$ one-hop redirects.csv --base https://example.com
checking 5 rules against https://example.com
1/5 OK
  loop: 1
  not_found: 2
  chain: 1
FAIL [loop] line 5: /team  redirect loop: 301 https://example.com/team -> 301 https://example.com/people/ -> /team
FAIL [not_found] line 4: /services  new URL returned 404
FAIL [not_found] line 6: /pricing  old URL returned 404, no redirect
FAIL [chain] line 3: /about-us  reaches the new URL after 2 redirects: 301 https://example.com/about-us -> 301 https://example.com/about -> 200 https://example.com/about/
```

| kind | what happened |
|---|---|
| `chain` | more than one redirect before a final answer |
| `loop` | a redirect came back to a URL already visited |
| `not_found` | the old or the new URL answered 404 or 410 |
| `wrong_target` | the redirect points somewhere other than the new URL in the map |
| `no_redirect` | the old URL answered 200 when a redirect was expected |
| `temporary` | a 302, 303 or 307 where a permanent redirect was expected |
| `redirected` | a row with no new URL redirected instead of answering 200 |
| `bad_status` | anything else: a 5xx, a redirect with no `Location` header |
| `error` | the request failed (DNS, TLS, timeout, refused) |

`--json report.json` writes every result with each hop's URL, status and
`Location`. `--json -` prints it to stdout instead.

## Options

| flag | default | |
|---|---|---|
| `--base URL` | required | the site to test |
| `--workers N` | 8 | parallel requests |
| `--timeout S` | 20 | seconds per request |
| `--max-hops N` | 10 | stop following a chain after N hops |
| `--limit N` | all | check only the first N rules |
| `--auth user:pass` | none | basic auth for a staging host. `ONE_HOP_AUTH` works too |
| `--header 'Name: value'` | none | extra header, repeatable |
| `--method GET` | HEAD | HEAD falls back to GET on a 405 or 501 anyway |
| `--allow-temporary` | off | accept 302, 303 and 307 as the one hop |
| `--path-only` | off | compare path and query only, ignore scheme and host |
| `--insecure` | off | skip TLS certificate checks (self-signed staging) |

Credentials from `--auth` and `--header` go to the `--base` host only. If a
redirect points at another host, that request is sent without them.

Trailing slashes count. `/about` and `/about/` are two URLs, and a map that says
`/about/` when the server redirects to `/about` fails as `wrong_target`.

## GitHub Actions

```yaml
- uses: actions/checkout@v4
- uses: synapsereality/one-hop@v0.1.0
  with:
    csv: redirects.csv
    base: https://staging.example.com
    auth: ${{ secrets.STAGING_AUTH }}
    args: --workers 4
```

The action needs `python3` on the runner, which GitHub's hosted runners have. It
installs nothing.

## Install from source

```bash
git clone https://github.com/synapsereality/one-hop
cd one-hop
pip install .
```

Python 3.11 or later, no dependencies. `python -m one_hop` works without
installing if `src` is on `PYTHONPATH`.

## Tests

```bash
pip install -e ".[test]"
pytest
```

The tests start a local HTTP server and cover every result kind, the HEAD to GET
fallback, and credentials staying on the base host.

## Licence

MIT. Made by [Synapse](https://synapsereality.io/open-source/one-hop/).
