# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `questz.breaker`: a half open trial now has a deadline. `allow` persists the trial counter
  before the protected action runs and only the process holding the trial ever cleared it, so a
  crash, a SIGKILL or a scheduler timeout during that fetch left a state file that refused every
  later invocation for ever, which is the exact case the persistence exists for. A trial older
  than the cooldown is read as abandoned and another is granted, the bound Resilience4j spends
  `maxWaitDurationInHalfOpenState` on. The snapshot gained `half_open_started_at`, read
  tolerantly so a state file written before it still loads.
- `questz.canary.run`: a browser that breaks after the navigation succeeded is `UNAVAILABLE`.
  Only `goto` was guarded, so a page that closed or navigated during the readiness wait or the
  content read escaped as a traceback and exit 1, and 1 is the code that means DRIFT.
- `questz.normalize`: a contract attribute selector with no name, `[="x"]`, raises
  `ContractError` and exits 2 instead of an `IndexError` and exit 1. The operator check was a
  substring test, and the empty string is a substring of every string.

### Changed

- `questz.driver.PlaywrightDriver` records `shots`, every `(path, mask)` pair in order, in place
  of `last_mask`. A job takes more than one screenshot and only one of them is taken with a
  password on the screen; the last mask was never that one.
- `scripts/capture_evidence.py --screenshot` regenerates `docs/evidence-login-masked.png` and
  records the boxes it painted, so the one committed artefact with no command behind it has one.
  Without the flag the script stays browserless.

## [0.1.0] - 2026-08-18

### Added

- `questz.canary`: recorded JSON contracts, a two layer check combining selector and field
  rules with a normalized structural signature, and four typed outcomes (`OK`, `DRIFT`,
  `UNAVAILABLE`, `BLOCKED`) that all fail closed.
- `questz.normalize`: an HTML tree parser, a cosmetic churn resistant serialization with a
  SHA-256 equality check and a unified diff over the same lines, and a documented CSS
  selector subset shared by the browserless and browser paths.
- `questz.breaker`: a circuit breaker with Resilience4j shaped trip conditions counted over
  a sliding window, Fowler half open semantics, state persisted to disk so `HALF_OPEN` is
  reachable across invocations, and an internal retry loop with AWS full jitter backoff.
  One logical action records exactly one breaker outcome.
- `questz.cache`: a six step atomic write and an RFC 5861 `stale-if-error` policy that
  reports the age of every stale serve and journals it at WARN.
- `questz.journal`: append only JSONL evidence with a default deny payload allowlist,
  secret key redaction, URL redaction covering credentials in the netloc as well as the
  query string, and a `report` renderer.
- `questz.driver`: a six method `PageDriver` Protocol and a `PlaywrightDriver` that masks
  the contract's secret selectors in every screenshot. No runtime import of playwright.
- `questz.testsite`: a loopback `ThreadingHTTPServer` and four page variants, `v1`, `v1c`
  (ten single axis cosmetic mutations, including the three this detector reports findings
  on), `v2` (three declared contract violations) and `v2i` (a consent interstitial).
- `questz` CLI: `canary check`, `canary record`, `report`, `serve` and `demo`, with exit
  codes 0 `OK`, 1 `DRIFT`, 2 usage, 3 `UNAVAILABLE` or `BLOCKED`. `canary check --fail-on`
  sets the severity that gates, and `canary record --decimal-separator` declares which
  character a target uses as its decimal point.
- `examples/pricewatch.py` and `examples/scenarios.py`: the demo job and the `happy`,
  `drift` and `degrade` scenarios, plus `--table` to regenerate the README detection table.
- CI on Python 3.11, 3.12, 3.13 and 3.14, with `mypy --strict` in the lint job and a
  separate browser leg.

### Notes

Nothing was published before this tag, so the fixes made between the first commits and the
release are folded in above rather than listed as a separate version. They are in the git
history under their own messages: the journal writing proxy credentials from a URL netloc,
every reader mapping an undecodable file onto the exit code that means DRIFT, the decimal
parser guessing at values that read two ways, the breaker counting failures forever, a
re-parented subtree reported as deletions, and a collapsed run discarding the nested counts
of every block but the first.

[Unreleased]: https://github.com/PNX89/QUESTZ/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/PNX89/QUESTZ/releases/tag/v0.1.0
