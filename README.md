# QUESTZ

**The site changed. The job stopped. That is the feature.**

[![CI](https://github.com/PNX89/QUESTZ/actions/workflows/ci.yml/badge.svg)](https://github.com/PNX89/QUESTZ/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20to%203.14-blue)
![License](https://img.shields.io/badge/license-MIT-green)

A scraper fails loudly when a page disappears. The failure that costs money is the other one: the portal redeploys,
the price column moves one cell to the left, and the job keeps logging in, keeps finding rows, keeps writing a CSV and
keeps exiting 0. Nothing errors, so nothing pages anybody, and the numbers are wrong for as long as it takes a human
to notice by eye.

QUESTZ turns that into a loud, typed, actionable stop. Before a job acts, it checks the live page against a recorded
contract: the selectors your code depends on, the shape of the values in them, and a normalized structural signature
of the container. When the page no longer matches, the job fails closed with a report naming what moved, and nothing
is written. "Canary" here is the AWS CloudWatch Synthetics sense, a scheduled scripted check of a live surface, not
canary deployment
([reference](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Synthetics_Canaries.html)). The
target ships inside the repo because what is demonstrated is a v1 to v2 redeploy, and no third party site will change
its DOM on cue, identically, on every CI run.

> **Scope.** A pre-flight gate for one process driving one browser. It flags that a page no longer matches what your
> code assumes, and stops before the job writes. **Not in scope:** telling you the extracted data is correct, healing
> a broken locator, diffing pixels, enforcing robots.txt, shared breaker state across hosts, a second driver, async
> Playwright. **Core runtime dependencies: none.** Checker, breaker, cache, journal and CLI are stdlib only, so the
> whole unit suite runs with no browser installed.

## Quickstart

<!-- quickstart -->
```bash
git clone https://github.com/PNX89/QUESTZ && cd QUESTZ
uv sync
uv run questz canary check --contract examples/contracts/items.json --html questz/testsite/v2/items.html  # exit 1
```
<!-- /quickstart -->

Three commands, no browser download, well under a minute. The third checks the bundled "redeployed" page against the
contract recorded from the original one, and needs no browser because `--html` parses a file.

## What it prints

```console
$ uv run questz canary check --contract examples/contracts/items.json --html questz/testsite/v2/items.html; echo "exit $?"
questz canary: DRIFT
contract: items v1
target:   questz/testsite/v2/items.html

findings (4):
  CRITICAL  missing_selector   tr[data-testid="item-row"] td[data-testid="price"]
                               expected 5 to 50, found 0
  CRITICAL  missing_selector   tr[data-testid="item-row"] td[data-testid="stock"]
                               expected 5 to 50, found 0
  MAJOR     structure_removed
                               expected every baseline node still present, found 4 gone
                               th[data-testid=col-price], th[data-testid=col-stock], td[data-testid=price], td[data-testid=stock]
  WARNING   structure_added
                               expected no nodes outside the baseline, found 7 new
                               th[data-testid=col-sku], th[data-testid=col-cost], td[data-testid=sku], td[data-testid=cost], and 3 more

signature: baseline 6c9c2add1e0b observed defb7f9573c6
counts:    tr[data-testid=item-row]=12, li[data-testid=stock]=12

structure diff:
  --- baseline
  +++ observed
  @@ -7,4 +7,4 @@
   4|th|data-testid=col-name
  -4|th|data-testid=col-price
  -4|th|data-testid=col-stock
  +4|th|data-testid=col-sku
  +4|th|data-testid=col-cost
   2|tbody|
  @@ -12,3 +12,6 @@
   4|td|data-testid=name
  -4|td|data-testid=price
  -4|td|data-testid=stock
  +4|td|data-testid=sku
  +4|td|data-testid=cost
  +1|div|data-testid=stock-panel
  +2|ul|
  +3|li|data-testid=stock|x*
exit 1
```

The exit code is the interface: "the site changed" and "the site is down" need different pager behaviour.

| Code | Status | Cause |
| --- | --- | --- |
| 0 | `OK` | page loaded, container present, no findings |
| 1 | `DRIFT` | page loaded and no longer matches the contract |
| 2 | usage | bad flag, missing contract, unsupported selector |
| 3 | `UNAVAILABLE` or `BLOCKED` | navigation failed, non 2xx status, or something else was served |

The same command against all three bundled pages, run by `tests/test_readme.py`, which reads them out of this file:

```bash
uv run questz canary check --contract examples/contracts/items.json --html questz/testsite/v1/items.html   # exit 0
uv run questz canary check --contract examples/contracts/items.json --html questz/testsite/v2/items.html   # exit 1
uv run questz canary check --contract examples/contracts/items.json --html questz/testsite/v2i/items.html  # exit 3
```

## Detection results

Firing when the DOM changes is table stakes. Not firing when it changes in a way that does not matter is what decides
whether anyone leaves the check switched on.

<!-- detection-table -->
| Variant | Change | Expected | Status | Findings | Max severity |
| --- | --- | --- | --- | --- | --- |
| `v1/items.html` | the recorded page | OK | OK | 0 | none |
| `v1c/items.attr-order.html` | attribute order changed | OK | OK | 0 | none |
| `v1c/items.class-churn.html` | build hashed class names | OK | OK | 0 | none |
| `v1c/items.whitespace.html` | markup reflowed | OK | OK | 0 | none |
| `v1c/items.extra-rows.html` | 13 rows instead of 12 | OK | OK | 0 | none |
| `v1c/items.framework-ids.html` | framework ids and an inline style | OK | OK | 0 | none |
| `v1c/items.analytics-script.html` | analytics script and a comment added | OK | OK | 0 | none |
| `v2/items.html` | price renamed, stock moved out of the row, a column added | DRIFT | DRIFT | 4 | CRITICAL |

Structural variants flagged 1/1, cosmetic variants flagged 0/6, declared contract violations found in v2 3/3.
<!-- /detection-table -->

Regenerate with `uv run python examples/scenarios.py --table`; a test compares it against a fresh generation.

## How it works

```mermaid
flowchart TD
    start([job starts]) --> allow[breaker.allow]
    allow -->|OPEN| refused[CircuitOpenError, nothing requested]
    allow -->|CLOSED or HALF_OPEN| fetch[Breaker.call: goto, wait on the readiness selector<br/>retries with full jitter, inside the call]
    fetch -->|attempts exhausted| stale[cache.get_stale_if_error<br/>RFC 5861, age journaled at WARN]
    stale -->|nothing usable| refused
    stale -->|stale bytes| check
    fetch -->|html| check[canary.check_html<br/>selector rules, field shapes, structural signature]
    check -->|OK| write[extract, write the CSV] --> journal[(run.jsonl)]
    check -->|DRIFT, UNAVAILABLE or BLOCKED| stop[stop before any write] --> journal
    refused --> journal --> report[questz report]
```

Every box writes typed events into an append only JSONL journal; `questz report` renders a step list with durations
and outcomes, then this rollup:

```console
$ uv run questz report artifacts/degrade/run.jsonl | tail -5
cache: 1 stale serves, oldest 90.5s
breaker items: CLOSED to OPEN (fetch items: TransientError: navigation failed: Page.goto: net::ERR_CONNECTION_FAILED at http://127.0.0.1:8000/items.html)
breaker items: OPEN to HALF_OPEN (cooldown elapsed)
breaker items: HALF_OPEN to CLOSED (half open trial succeeded)
errors: 2
```

## Components

| Component | Module | What it owns |
| --- | --- | --- |
| canary | `questz.canary` | the contract, the two layer check, the four typed outcomes |
| breaker | `questz.breaker` | Resilience4j shaped trip conditions, persisted state, and the retry loop inside one call |
| cache | `questz.cache` | one atomic write recipe and an RFC 5861 `stale-if-error` policy that reports the age |
| journal | `questz.journal` | append only JSONL evidence with a default deny payload allowlist |
| driver | `questz.driver` | the six method page surface everything else is written against |

## Scenarios

Three browser driven runs against the bundled target. `--deterministic` installs a fake clock starting
`2026-01-01T00:00:00Z`, fixes the run id and seeds the jitter, so the journal is byte identical across runs.

### 1. Happy path

```console
$ uv run questz demo --scenario happy --deterministic
scenario: happy
variant:  v1, served from 127.0.0.1 by the bundled test site
canary:   OK, no findings
csv:      artifacts/happy/items.csv, 12 rows
breaker:  CLOSED, 1 recorded call
journal:  artifacts/happy/run.jsonl, 14 entries
exit:     0
```

### 2. Drift, the same job against the redeployed page

The variant is the only thing that changed, and the canary runs before extract, so the CSV path is never created.

```console
$ uv run questz demo --scenario drift --deterministic | tail -6

canary:   DRIFT, 4 findings, max severity CRITICAL
csv:      not written, artifacts/drift/items.csv does not exist
breaker:  CLOSED, 1 recorded call
journal:  artifacts/drift/run.jsonl, 11 entries
exit:     1
```

### 3. Degradation and recovery

Playwright `page.route()` aborts the first three requests for `items.html` with `connectionfailed`: deterministic
injection at the network layer, not a sleep and not a random number. The clock is injected and advanced, because
crossing a 60 second cooldown by waiting 60 seconds is no demonstration.

```console
$ uv run questz demo --scenario degrade --deterministic
scenario: degrade
variant:  v1, served from 127.0.0.1 by the bundled test site
clock:    injected, advanced rather than waited on
priming:  1 fetch, 12 rows cached

invocation 1, the first 3 requests for items.html are refused
  retries:  3 attempts, then one recorded failure
  breaker:  CLOSED to OPEN
  cache:    stale-if-error served bytes 90.5s old
  canary:   OK, no findings
  csv:      artifacts/degrade/items.csv, written from the cached bytes
  exit:     0

invocation 2, inside the cooldown
  breaker:  OPEN, refused before the request was made
  detail:   breaker 'items' is OPEN, 60.0s of cooldown left
  exit:     3

invocation 3, the clock is 61s past the cooldown
  breaker:  OPEN to HALF_OPEN to CLOSED
  canary:   OK, no findings
  csv:      artifacts/degrade/items.csv, 12 rows
  exit:     0

journal:  artifacts/degrade/run.jsonl, 41 entries
exit:     0
```

Three attempts inside one logical fetch produce one recorded breaker failure, not three, and the three invocations
share state through a JSON file, which is what makes HALF_OPEN reachable by a process that exits after two minutes.

## Design decisions

**Two layers, because each misses what the other catches.** A selector contract passes when the class you depend on
survives but an interstitial appeared over it; a signature passes when the skeleton is identical but your anchor got
renamed. So selector and field rules are primary and carry severity; the signature is the diffable secondary. Diffable
matters: a SHA-256 of a tree carries one bit, so `questz.normalize` emits a line per node (`depth|tag|attr=val`),
hashes it as a fast equality check, and diffs those lines to name the node that moved.

**Normalization is a list of false positive traps, closed one at a time.** Text nodes go. `class`, `style` and `id` go
entirely, because Tailwind JIT, CSS modules, styled-components, React `:r7:` ids and Angular `_ngcontent` hashes churn
on every build. Only `role`, `type`, `name`, `data-testid` and `aria-label` survive, and a value survives only if it
matches `^[A-Za-z][A-Za-z0-9_-]{0,31}$`, so a build hashed value becomes `*` and keeps the anchor without the churn.
Comments, doctype and the subtrees of `script`, `style`, `svg` and `noscript` are discarded, which makes an injected
analytics tag a non event. Runs of identical adjacent siblings collapse to one line marked `x*`, with the number
recorded separately in `counts`, because 12 rows and 13 rows are different trees and without this the false positive
rate on any list page approaches 100 percent. The check is scoped to a container selector, so ads, cookie banners,
personalization and A/B variants outside it are invisible to it.

**A number that reads two ways is refused, not guessed.** `parse_decimal` is the single parser the contract's field
check and the job's extraction both call, so a value the canary passed is a value the job can read. It strips currency
decoration, accepts `.` `,` and thin spaces as grouping, and returns nothing for anything with two readings: `1.234` is
a thousand euros in Frankfurt and one euro twenty-three in Dublin, and a parser that picks one writes a clean CSV that
is wrong by 1000x. A contract whose target ships values like that declares `decimal_separator` (`questz canary record
--decimal-separator .`) and they parse; without it they surface as a `field_shape` finding.

**Contracts are JSON, not YAML, and the baseline is recorded rather than hand written.** The core has no third party
dependencies, stdlib `tomllib` is read only so `record` could not write TOML, and PyYAML would be a dependency for one
file; `json.dumps(..., indent=2, sort_keys=True)` keeps a contract diff line oriented. `questz canary record` derives
signature, baseline and counts from a page you have decided is good, pins every selector to the count observed there,
and tells you to widen the ranges by hand. That is where the fingerprint comes from.

**Four typed outcomes, all closed.** Reporting every failure as a DOM change would be a false alarm on the very axis
this repo claims competence in. So `UNAVAILABLE` (navigation raised, or a status outside 200 to 299), `BLOCKED` (2xx,
but the readiness selector never appeared and the container is absent, with the `data-testid` values actually served
named in the reason), `DRIFT` and `OK` are separate statuses with separate exit codes, and the breaker counts
`UNAVAILABLE` against the target, not the contract. Readiness is an explicit selector, never `networkidle`, which
Playwright marks DISCOURAGED: "Don't use this method for testing, rely on web assertions to assess readiness instead."

**The breaker vocabulary is Resilience4j's**, deliberately: `failureRateThreshold`, `minimumNumberOfCalls`,
`permittedNumberOfCallsInHalfOpenState` ([docs](https://resilience4j.readme.io/docs/circuitbreaker)). The denominator
is calls recorded so far, not a planned total, and the rate must be strictly exceeded once the minimum is reached: 2
failures in 4 calls do not trip a 0.5 threshold, 3 in 4 do, and both sides are tested. Half open follows Fowler
([reference](https://martinfowler.com/bliki/CircuitBreaker.html)): a permitted trial call, a success that zeroes the
counters and closes, a failure that reopens and restarts the cooldown.

**One logical action is exactly one breaker outcome.** Retries live inside `Breaker.call` and the outcome is recorded
once they are exhausted. Without that, `RetryPolicy(max_attempts=3)` against `consecutive_failure_threshold=3` opens
the breaker on the first failing action, which is the regression this arrangement exists to catch, and there is a test
named after it. Backoff is full jitter from the AWS Architecture Blog
([reference](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)),
`random_between(0, min(cap, base * 2 ** attempt))`, seeded per policy rather than by a global `random.seed()`, which
leaks state across tests and parallel workers.

**Stale serves are loud.** The cache policy has a standard name, RFC 5861 `stale-if-error`
([RFC](https://www.rfc-editor.org/rfc/rfc5861)), so that is what it is called here. Every stale serve returns its age,
refuses past `max_stale_seconds`, and writes a WARN journal line carrying that age, because the classic incident is
not "served stale", it is "served stale silently, forever, and nobody noticed". Cache entries and breaker state share
one atomic write: a temporary file in the target's own directory (`os.replace` across filesystems raises `EXDEV`),
`fsync`, an optional `F_FULLFSYNC` on macOS, the previous file's mode restored because `mkstemp` creates 0600,
`os.replace`, an `fsync` of the parent directory, and an unlink of the temporary file in a `finally`.

**Redaction is default deny.** Only payload keys listed in `EVENT_FIELDS[event]` are written at all, which is a
different property from masking a value after the fact; on top of that, any key matching
`pass|secret|token|auth|cookie|session|api[_-]?key` becomes `<redacted>`, and every URL found in a value, including one
sitting inside a longer error message, has its userinfo blanked, its query replaced and its fragment dropped. The
userinfo is the part that actually leaks: `http://user:hunter2@proxy.example.com:8080/items.html` is how a scraping
proxy is ordinarily configured, and it carries no query string for a query-only redactor to find. The real leak vector is
not the JSON though, it is the screenshots, so the driver hands `contract.secret_selectors` to Playwright's
`page.screenshot(mask=[...], mask_color="#000000")` ([docs](https://playwright.dev/python/docs/api/class-page)).
`docs/evidence-login-masked.png` is the committed screenshot of the login page with both credential fields filled,
which makes that checkable in one image.

**The target ships with the repo.** Drift detection cannot be regression tested against a third party site: you cannot
ask one to redeploy on cue, and scraping one from a public repo's CI is a terms of service and flakiness problem on
every run. Precedent: Zyte runs [toscrape.com](https://toscrape.com/) as an official scraping sandbox, and
the-internet.herokuapp.com plays that role for Selenium. The server is `http.server.ThreadingHTTPServer`, not bare
`HTTPServer`, because the stdlib docs say the threading version exists since "web browsers pre-open sockets, on which
HTTPServer would wait indefinitely" ([docs](https://docs.python.org/3/library/http.server.html)); single threaded plus
Chromium is an intermittent hang presenting as a Playwright timeout. It binds `127.0.0.1` on port 0 and reads the port
back so CI legs cannot collide, and navigates to that literal address rather than `localhost`, which can resolve to
`::1` first and stall. The login page is a client side stub: `app.js` compares against hardcoded demo credentials and
sets a `sessionStorage` flag. It is not authentication and nothing here claims it is.

**The `PageDriver` Protocol has one implementer and earns its place today.** The unit suite drives `canary.run`, the
breaker, the journal and the whole demo job through a `FakeDriver` in `tests/conftest.py`, which is the entire reason
199 of the tests need no browser. Driver agnosticism across real drivers is a non-goal, not a promise. It is sync:
pytest-playwright ships sync fixtures only and async needs the separate `pytest-playwright-asyncio` package
([test runners](https://playwright.dev/python/docs/test-runners)), the workflow is sequential so async buys no wall
clock time, and async would push `async def` through every public signature. The cost is in
[Limitations](#limitations).

**The breaker, journal and backoff shape recur across the Q_Z toolset** as a deliberate personal standard, tuned per
domain rather than copied: what changes is the taxonomy of what is retryable, and what one logical action is.

## Prior art

| Tool | What it does | How QUESTZ differs |
| --- | --- | --- |
| [Playwright ARIA snapshots](https://playwright.dev/python/docs/aria-snapshots) | `page.aria_snapshot()` and `expect(...).to_match_aria_snapshot()` assert an accessibility tree | an assertion inside a test run, not a gate in front of a data job |
| [Spidermon](https://github.com/scrapinghub/spidermon) | Scrapy side monitoring and item validation | validates items after extraction; QUESTZ refuses to extract |
| [Healenium](https://healenium.io/) | self healing locators with a similarity confidence score | QUESTZ stops rather than guessing a replacement anchor |
| Percy, BackstopJS, Applitools | visual regression over rendered pixels | structural and value contracts, no image dependency |
| [CloudWatch Synthetics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Synthetics_Canaries.html) | scheduled scripted browser canaries | the same scheduling shape, with the contract and the fail closed gate as the product |

QUESTZ differs by being a pre-flight gate for a data job, failing closed before anything is written.

**Considered and rejected: building the signature on `page.aria_snapshot()`.** It would have been less code, but it
makes the core depend on Playwright and therefore on a browser, killing the `--html` path and the no browser suite;
and scraping targets are frequently presentational `div` soup with no accessible role, so the accessibility tree is
too coarse for a data contract. The honest counter argument: ARIA snapshots are maintained by the Playwright team and
handle shadow DOM properly.

## Ethics and legal

The only thing this repo scrapes is the target bundled inside it, on loopback: nothing contacts a host outside the
machine. The Robots Exclusion Protocol was standardised as [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309) in
September 2022 and `Crawl-delay` is not part of it, while Python's stdlib `urllib.robotparser` implements the older de
facto standard plus the `crawl-delay`, `request-rate` and `sitemap` extensions. Respect whichever the site publishes,
rate limit regardless, and do not circumvent CAPTCHAs. On exposure, in the order it bites after hiQ: contract and
terms of service first, personal data under GDPR second, copyright third, the CFAA last. robots.txt carries no
independent legal force, but ignoring it is used as evidence of bad faith.

## Limitations

- Single process, single host. The breaker store is a JSON file; there is no distributed queue and no shared breaker
  state. One driver implementation, Playwright, today.
- The normalizer uses stdlib `html.parser`, not a full HTML5 tree builder. The browser path feeds it Chromium's own
  serialized DOM, so parsing quirks arise only on the `--html` path.
- Crash atomicity is argued from POSIX `rename(2)` semantics, not proven by the suite: a pytest process cannot kill
  itself mid syscall. The suite tests the weaker claim, that a failed write leaves the previous bytes byte identical
  with no `.tmp` behind.
- `os.replace` is atomic on POSIX by the `rename(2)` requirement, but on Windows it goes through `MoveFileEx`, which
  carries no such requirement ([background](https://lwn.net/Articles/682988/)). macOS `fsync` does not flush the
  drive's write buffer, hence `full_fsync`.
- The sync Playwright API is greenlet based over a hidden asyncio loop, so it raises "It looks like you are using
  Playwright Sync API inside the asyncio loop" when a loop is running, and it is not thread safe, so parallelism means
  processes ([library docs](https://playwright.dev/python/docs/library)).
- A contract is only as good as the ranges a human widened. `record` pins them to what one page had, and leaving them
  pinned produces noise on the first legitimate change.
- QUESTZ cannot tell you the data is correct. It tells you the page still matches what your code assumes about it.

## Why I built this

I maintained a set of jobs pulling prices and stock levels off supplier portals for a client. One portal redeployed,
moved a column, and kept every class name and every id it had before. The job did not error, because there was nothing
to error on: it found rows, read cells, and wrote a file that looked exactly like yesterday's. Those numbers were
downstream for over a week, and the cost was not the fix, it was re-deriving which days of data had been poisoned.
Everything here comes from wanting that job to have stopped on day one, naming the column.

## Development

```bash
uv sync                                   # dev tools only; the package itself has no dependencies
uv run pytest -q                          # e2e is skipped with a reason when no browser is present
uv run ruff check . && uv run ruff format --check .
```

The browser leg is a separate, larger step, roughly a 30 second download with `--only-shell`:

```bash
uv sync --extra playwright --group e2e
uv run playwright install --only-shell chromium
uv run pytest -m e2e
uv run questz demo --scenario degrade --deterministic
```

CI is three jobs in one workflow: `lint`; `unit` on Python 3.11, 3.12, 3.13 and 3.14 with no browser and so no C
extensions in the matrix; and `e2e` on 3.13 with chromium, traces retained on failure and a 10 minute timeout. There
is no browser binary cache step, because Playwright's own CI docs say restoring one takes about as long as downloading
the binaries ([CI docs](https://playwright.dev/python/docs/ci)). See also `questz serve --help` and
`questz canary record --help`.

## License

MIT. Copyright (c) 2026 Quelin Zammit. Part of the Q...Z toolset: QUACKZ, QUOTEZ, QUELLZ, QUIDZ, QUESTZ.
