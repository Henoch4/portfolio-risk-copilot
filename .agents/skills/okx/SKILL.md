---
name: okx
description: |
  Index of OKX-platform skills relevant to this repo. Load when working on
  anything that touches OKX: the OKX CLI wrapper, X Layer (chain 1952),
  the onchain audit trail, x402 payment middleware, ASP manifest/hire
  endpoint, or the OKX Build X AI hackathon. Maps the globally-installed
  OKX skills (~/.agents/skills/) to this repo's packages so you know which
  skill owns which surface. For repo-specific failure modes (signature
  scheme, risk gate, Vercel), load the matching runbook instead.
version: 0.1.0
---

# OKX platform skills for this repo

This repo is an autonomous trading agent built for the OKX Build X AI hackathon.
It is the ASP (Autonomous Service Provider) that an agent on okx.ai hires via
`/hire` to run a risk-gated trading cycle. Everything OKX-shaped has a matching
skill; this index is the router.

## When to reach for this

- The task touches OKX CLI, X Layer, the onchain audit trail, x402, manifest.json,
  `/hire`, or the hackathon itself.
- You're not sure which skill owns an OKX question.
- You see `OkxCli`, `OnchainLogger`, `PaymentMiddlewareASGI`, or `XLAYER_CHAIN_ID`
  and need the context around them.

## The global OKX skills, mapped to this repo

All live in `~/.agents/skills/`. Load them directly when the task is on their
surface. The repo only *uses* the surfaces below; the skills are the platform
documentation.

| Skill | Owns | Where it bites in this repo |
|---|---|---|
| `okx-ai` | ASP identity & task marketplace (ERC-8004) | `manifest.json`, `src/main.py` `/manifest` + `/hire`, agent registration on okx.ai |
| `okx-agentic-wallet` | Wallet & on-chain execution | `src/audit_logger.py` signing, X Layer transactions, gas (OKB, not ETH) |
| `okx-agent-payments-protocol` | x402 / MPP / A2A payments | `PaymentMiddlewareASGI` in `src/main.py`; `_PAID_ROUTES` for `/hire` |
| `okx-defi` | DeFi products & positions | Less central; relevant if strategy surfaces yield/liquidity data |
| `okx-dapp-discovery` | DApp plugin routing | Not used in this repo's core path |
| `okx-dex-market` | DEX market data | Not used; this repo trades CEX swaps via the OKX CLI |
| `okx-growth-competition` | Trading competitions | The Build X AI hackathon this repo targets; bounties/leaderboards |
| `okx-guide` | Onboarding & support | First-time setup questions about OKX accounts/roles |

## The repo's own packages

- `src/okx_cli.py` — subprocess wrapper around the `okx` CLI binary (`OkxCli`,
  `OkxCliConfig`, `OkxCliError`). Uses demo profile unless told otherwise.
- `src/audit_logger.py` — `OnchainLogger`, logs every decision to
  `TradeAuditTrail.sol` on X Layer before execution. Non-overridable gate.
- `src/execution.py` — `RiskGate` (non-overridable checks) + `OrderExecutor`.
- `src/agent.py` — `AutonomousTradingAgent`, the multi-agent orchestrator
  (MarketData → Signal → Risk → Execution → OnchainLogger).
- `src/main.py` — FastAPI app; ASP endpoints (`/hire`, `/manifest`, `/health`,
  `/audit-decisions`, `/audit-stats`, `/kill-switch/*`) + x402 middleware.
- `contracts/contracts/TradeAuditTrail.sol` — the onchain risk/audit contract.
- `scripts/` — deploy + param-setting scripts, smoke tests.

## Repo-scoped runbooks

For anything that can *go wrong* in this repo, the runbooks are the source of
truth — they encode the actual bugs that were fixed and their regression tests:

- `onchain-audit-trail` — signature scheme, chain ID, gas token, leverage units, scaling
- `risk-gate` — kill switch, reduce-only, slippage collar, fail-closed checks
- `serverless-deploy` — Vercel crashes (ta-lib), x402 import guard, DRY_RUN, auth

## What this skill is NOT

- Not a substitute for the global skills. It routes; it does not document OKX.
- Not a runbook. If you're debugging a repo failure, load the matching runbook.
- Not the contract spec. Read `contracts/contracts/TradeAuditTrail.sol` directly.

## When to escalate

- Changes to the onchain contract or signature scheme → load `onchain-audit-trail`.
- Changes to `RiskGate` or kill-switch semantics → load `risk-gate`.
- Any deploy/build issue → load `serverless-deploy`.
- New payments/x402 flow → load `okx-agent-payments-protocol` and `okx-ai`.
