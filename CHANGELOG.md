# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-08-18

### Added

- `questz.canary`: recorded JSON contracts, a two layer check combining selector and field
  rules with a normalized structural signature, and four typed outcomes (`OK`, `DRIFT`,
  `UNAVAILABLE`, `BLOCKED`) that all fail closed.
- `questz.normalize`: an HTML tree parser, a cosmetic churn resistant serialization with a
  SHA-256 equality check and a unified diff over the same lines, and a documented CSS
  selector subset shared by the browserless and browser paths.
- `questz.breaker`: a circuit breaker with Resilience4j shaped trip conditions, Fowler half
  open semantics, state persisted to disk so `HALF_OPEN` is reachable across invocations,
  and an internal retry loop with AWS full jitter backoff. One logical action records
  exactly one breaker outcome.
- `questz.cache`: a six step atomic write and an RFC 5861 `stale-if-error` policy that
  reports the age of every stale serve and journals it at WARN.
- `questz.journal`: append only JSONL evidence with a default deny payload allowlist, URL
  query redaction, secret key redaction, and a `report` renderer.
- `questz.driver`: a six method `PageDriver` Protocol and a `PlaywrightDriver` that masks
  the contract's secret selectors in every screenshot. No runtime import of playwright.
- `questz.testsite`: a loopback `ThreadingHTTPServer` and four page variants, `v1`, `v1c`
  (six single axis cosmetic mutations), `v2` (three declared contract violations) and `v2i`
  (a consent interstitial).
- `questz` CLI: `canary check`, `canary record`, `report`, `serve` and `demo`, with exit
  codes 0 `OK`, 1 `DRIFT`, 2 usage, 3 `UNAVAILABLE` or `BLOCKED`.
- `examples/pricewatch.py` and `examples/scenarios.py`: the demo job and the `happy`,
  `drift` and `degrade` scenarios, plus `--table` to regenerate the README detection table.
- CI on Python 3.11, 3.12, 3.13 and 3.14, with a separate browser leg.

[0.1.0]: https://github.com/PNX89/QUESTZ/releases/tag/v0.1.0
