# AGENTS.md

## Read these first

- `README.md` — architecture, setup, env vars, deployment
- `HACKATHON_SUBMISSION.md` — the product framing for the OKX Build X AI hackathon
- `.env.example` — all env vars the repo reads (documented here, not in code)
- `vercel.json` — serverless config; `src` not `source`, DRY_RUN lives in env secrets

## Skills

Repo-scoped skills live in `.agents/skills/` and are the authority for this repo's
known failure modes. The `okx` skill is an index: it maps the global OKX skills
(installed in `~/.agents/skills/`) to this repo's packages and tells you which to
load for which surface. The rest are runbooks.

| Skill | Load when |
|---|---|
| `okx` | Any OKX/X Layer/onchain surface, or to find which skill owns a question |
| `onchain-audit-trail` | Anything touching `src/audit_logger.py`, `contracts/`, signature scheme, chain ID, gas, or `scripts/set_risk_params.py` |
| `risk-gate` | Anything touching `src/execution.py` RiskGate/OrderExecutor, kill switch, reduce-only, slippage collar |
| `serverless-deploy` | Vercel deploy failures, dependency crashes, x402 guard, DRY_RUN/auth in prod |

Global skills that apply: `software-god-agent` (quality floor), `gstack` (QA/review/
ship), `caveman` (terse output), `internet-research` (fetching docs), and the OKX
platform skills referenced from the `okx` index skill.

## Environment setup traps

- `okx` CLI must be installed globally: `npm i -g @okx_ai/okx-trade-cli`. `OkxCli`
  in `src/okx_cli.py` shells out to the `okx` binary; it never takes API credentials.
  Authenticate once via `okx auth` (demo profile is fine for tests).
- `requirements.txt` deliberately has NO `ta-lib` — it is an unused native dep that
  crashes Vercel builds. Do not re-add it.
- Tests run fully offline: `OkxCli` is monkeypatched, and
  `tests/test_signature_roundtrip.py` proves the sign/verify path without an RPC.
  Run them with `python -m pytest tests/ -q` from the repo root.
- Never put real wallet/API secrets in code, `vercel.json`, or this file. The repo's
  original `AGENT_WALLET_PRIVATE_KEY` is compromised (commit `829576c` scrubbed it);
  any working key must come from env/secrets and be treated as rotated.

## Ported governance modules (2026-08)

`src/data_integrity.py` (pre-signal gate), `src/curator.py` (profile selector),
`src/multi_leg.py` (atomic multi-leg), `src/validation.py` (walk-forward/PBO/
Calmar gate), and `src/audit_trail.py` (local JSONL log) were ported from a
sibling MVP and wired into `run_trading_cycle` (Phase 1.5 integrity gate,
curator knobs, audit events) and `src/main.py` (`/api/v1/validation`,
`/api/v1/curator-profile`). Policy lives in `config/profiles.yaml`. Regression
tests: `tests/test_data_integrity.py`, `test_curator.py`, `test_multi_leg.py`,
`test_validation.py`, `test_audit_trail.py`, `test_agent_wiring.py`.
Two source bugs were fixed while porting — do not reintroduce them:
`max_slippage_pct` must be enforced in the multi-leg dispatch path (regression
`test_multi_leg.py::test_dispatch_unwinds_on_slippage_breach`), and
`PaperFillSimulator` must never clamp slippage (regression
`test_multi_leg.py::test_paper_fill_simulator_can_actually_breach_slippage`).

## Checks before you finish

1. `python -m pytest tests/ -q` — all tests green.
2. `git diff` — no `.env`, no secrets, no `AGENT_WALLET_PRIVATE_KEY`/`AGENT_API_TOKEN`.
3. If you touched Solidity or `scripts/` (deploy, set_risk_params): recompile with
   the solc script and re-verify the onchain path per `onchain-audit-trail`.
4. If you touched `vercel.json` or `src/main.py`: confirm `src` (not `source`) is
   used and that x402 imports are guarded per `serverless-deploy`.
5. Gitleaks runs in CI (`.github/workflows/secret-scan.yml`) and as a pre-commit
   hook (`.pre-commit-config.yaml`); keep it passing.

## Commits and PRs

- Conventional prefixes: `fix:`, `feat:`, `chore:`, `docs:`, `test:` (matches git log).
- Commit messages explain the *mechanism* of the fix (symptom → cause), not just the
  change — see `git log` for the house style.
- PRs must reference the regression test that guards the fix (see the runbooks for
  which test owns which bug).
