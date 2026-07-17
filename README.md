# Portfolio Risk Copilot

An **Agent Service Provider (ASP)** built for the [OKX.AI Genesis Hackathon](https://web3.okx.com/build/hackathon). Other agents — or a human — "hire" it with one API call, and it audits the **connected OKX trading account** for risk, returning a score and specific flags instead of raw numbers.

🔴 **Registered on OKX.AI** — Agent ID `#6274`
🟢 **Live:** [`portfolio-risk-copilot.onrender.com`](https://portfolio-risk-copilot.onrender.com) · [API docs](https://portfolio-risk-copilot.onrender.com/docs)

## What it checks

| Check | What it flags |
|---|---|
| **Concentration risk** | Too much of the portfolio sitting in one asset |
| **Leverage risk** | Open positions at dangerous leverage |
| **Smart-money divergence** | Account positioned opposite what OKX's top traders are currently doing on the same asset |

Returns a `risk_score` (0.0–1.0), a list of specific `flags`, and a markdown report.

## Read-only, by design

This service **cannot place, cancel, or transfer anything**, and it never audits an arbitrary third-party wallet — only the account it's authenticated as. Live-account access is an explicit opt-in at the process level (`ALLOW_LIVE=true`), off by default. Without it, `/hire` runs in `demo` mode and honestly reports `authenticated: false` and `needs_human_review: true` rather than fabricating a score.

## API

| Method | Route | What it does |
|---|---|---|
| `POST` | `/hire` | Run an audit — see [`manifest.json`](./manifest.json) for the full input/output schema |
| `GET` | `/manifest` | ASP manifest (capabilities, schema, endpoint) |
| `GET` | `/health` | Liveness check |

Example:

```bash
curl -X POST 'https://portfolio-risk-copilot.onrender.com/hire' \
  -H 'Content-Type: application/json' \
  -d '{"mode": "own_account", "profile_mode": "demo", "inst_type": "SWAP"}'
```

## Stack

- **API:** FastAPI + Uvicorn (Python)
- **OKX access:** [`@okx_ai/okx-trade-cli`](https://www.npmjs.com/package/@okx_ai/okx-trade-cli) (Node.js), shelled out to from Python
- **Registration:** [Onchain OS](https://github.com/okx/onchainos-skills) — Agentic Wallet + on-chain Agent identity on XLayer

## Running it locally

Full setup — including why the OKX CLI isn't in `requirements.txt`, how credentials are separated from this repo, and known open questions — is in [`SETUP.md`](./SETUP.md).

Quick version:

```bash
pip install -r requirements.txt
npm install @okx_ai/okx-trade-cli
uvicorn src.main:app --reload --port 8000
```

## Verifying the logic without installing anything

The core risk-scoring logic has an offline test suite that runs with zero dependencies:

```bash
python3 scripts/smoke_test.py
```

This exercises concentration risk, leverage risk, smart-money divergence (including a regression test for a lookalike-ticker false-negative bug), and the unauthenticated fail-safe path — all without touching a real OKX account.

## Project structure

```
src/
  main.py       # FastAPI routes: /hire, /manifest, /health
  auditor.py    # Risk-scoring logic
  okx_cli.py    # Thin wrapper around the okx CLI
tests/
  test_auditor.py
scripts/
  smoke_test.py # Zero-dependency logic verification
manifest.json   # ASP manifest (source of truth for /manifest)
SETUP.md        # Full setup notes + bug-fix changelog
```
