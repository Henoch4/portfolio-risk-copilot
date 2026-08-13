# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and this repo uses Conventional
Commits.

## [Unreleased]

### Added
- Parallel per-asset trading pipeline: `run_trading_cycle` now runs each asset's
  full integrity → signal → risk → onchain-log → execute path as its own
  coroutine via `asyncio.gather`, with blocking network I/O (onchain writes)
  offloaded to worker threads via `asyncio.to_thread`. Results are merged back in
  input order. (`src/agent.py`)
- Thread-safe onchain nonce counter in `OnchainLogger` (`src/audit_logger.py`):
  a local monotonic counter guarded by a lock, so concurrent sends (kill switch
  + logDecision) can no longer collide on `get_transaction_count`.
- Dynamic gas handling: `OnchainLogger` now estimates gas (capped at 1.5M) with a
  safe fallback and uses a 20%-buffered node gas price with a 1 gwei floor,
  instead of a hardcoded 1 gwei / 300k. A reverted receipt now raises instead of
  being treated as success.
- Validation gate wired end-to-end: `/api/v1/validation` runs the real
  `validation_report()` when `VALIDATION_RETURNS_PATH` is configured, and
  `scripts/check_validation_gate.py` blocks deploy/CI when
  `cleared_for_paper_trading == False` (honest null when no data is configured).
- `mypy` config (`mypy.ini`) and a `type-check` CI job.
- Regression tests: chain-ID consistency, gas/nonce send path, validation gate,
  parallel cycle.

### Fixed
- **Chain-ID consistency (P0):** `OnchainLogger` defaulted to chain `195` while
  the deployed contract, `main.py`, and `scripts/` use X Layer Testnet `1952`.
  The default is now `1952` and README reports the correct chain. A logger built
  without `XLAYER_CHAIN_ID` no longer signs for the wrong chain.
- `validation_report` `calmar_ratio` returned `inf` on a zero-drawdown series,
  which is not JSON-serializable and broke the `/api/v1/validation` response. It
  now returns a finite cap (`1e9`), still clearing any Calmar bar.

### Docs
- `docs/adr/0001-fractional-kelly.md`, `docs/adr/0002-x-layer-testnet.md`.
