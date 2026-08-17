---
name: serverless-deploy
description: |
  Runbook for deploying this repo to Vercel (serverless). Load when a build
  or deploy fails, when adding a Python dependency, when touching
  vercel.json, x402/payment imports, DRY_RUN or API-token auth in prod,
  or when the app boots locally but 500s on Vercel. Encodes each known
  failure pattern as symptom → mechanism → fix → regression test.
version: 0.1.0
---

# Serverless deploy runbook

This is a FastAPI app deployed on Vercel's Python runtime. Vercel is pickier
than local: no native deps, no long-running loops, no secrets in config files.
Every deploy bug in this repo's history was one of a small set of causes —
this runbook is that set.

## When to reach for this

- Vercel build fails (dependency install, missing `src`, import errors).
- App works locally (`uvicorn`) but 500s or times out on Vercel.
- Adding a dependency to `requirements.txt`.
- Editing `vercel.json`, `src/main.py` imports, or auth/DRY_RUN handling.
- Anything involving the x402 payment middleware or paid `/hire` route.

## What you need

- `vercel.json` — build config. Remember: it uses `src` (not `source`).
- `.env.example` — the 19 env vars; secrets go in Vercel project env, never in
  `vercel.json` or the repo.
- `src/main.py` — where the x402 guard and auth live.
- Vercel project dashboard for env/secrets and logs.

## Known failure patterns

Each pattern: symptom → mechanism → fix → test-for-it.

### 1. Native dependencies crash the build (ta-lib)

**Symptom:** Vercel build/install fails on a compiled dep (e.g. `ta-lib`).
**Mechanism:** Vercel Python builds can't compile native C libraries reliably.
`ta-lib` was added, then removed as unused (`72378e5`). It is not in
`requirements.txt` today — keep it that way.
**Fix:** before adding ANY dependency, check it has wheels for the Vercel
Python runtime. If it needs `gcc` or native libs at build time, don't add it.
**Test-for-it:** deploy preview; local-only deps that pass `pytest` locally
but have no wheel will still break Vercel.

### 2. x402 / payments imports crash the dry-run boot

**Symptom:** app boots locally but 500s on import on Vercel; or the dry-run
deployment fails because payment-protocol deps aren't installed there.
**Mechanism:** the x402 payment middleware (`PaymentMiddlewareASGI`, `_PAID_ROUTES`)
is guarded behind a try/except so the app can boot WITHOUT payment deps
(`4b89d00`). If the guard is removed, Vercel's slimmer install breaks the
whole app.
**Fix:** keep x402 imports guarded (`src/main.py`). If the app must serve paid
routes, the guard must degrade to "route not available", not crash the app.
**Test-for-it:** import `src.main` in a clean env without the payment deps —
it must not raise. A tiny test asserting the module imports is enough.

### 3. vercel.json uses `source` instead of `src`

**Symptom:** build uses the wrong directory; static files/routes 404; build
picks up wrong files.
**Mechanism:** the Vercel config key is `src`, not `source` (`c683a4b` fixed a
typo). `source` is a routes rewrite key; using it as a build path silently
misconfigures the build.
**Fix:** build/static config uses `src`. If you copy config from another
project, re-check every key against Vercel's schema.
**Test-for-it:** `vercel build` locally (or a preview deploy) after any
`vercel.json` edit; confirm routes for `/health` and static assets resolve.

### 4. Secrets and DRY_RUN in the wrong place

**Symptom:** DRY_RUN or a token appears in `vercel.json`, `.env.example`, or a
committed file; prod behaves like demo mode or leaks a key.
**Mechanism:** DRY_RUN was once in `vercel.json`, making live trading
impossible or demo-mode-by-default depending on the value, and bled config into
the repo. `81ce680` moved DRY_RUN to project env secrets. `829576c` scrubbed a
leaked `AGENT_WALLET_PRIVATE_KEY` and added gitleaks guards.
**Fix:** ALL secrets (`AGENT_WALLET_PRIVATE_KEY`, `AGENT_API_TOKEN`) and runtime
switches like `DRY_RUN`/`ALLOW_LIVE` go in Vercel project env. Nothing in
`vercel.json`, `.env`, or source. Gitleaks runs in CI and pre-commit.
**Test-for-it:** `gitleaks detect` + a repo grep for the old key; after deploy,
read the env via a `/health`-style debug endpoint and confirm the expected mode.

### 5. Continuous cycles don't fit serverless

**Symptom:** the app tries to run an infinite trading loop on Vercel and times
out / is charged for hours.
**Mechanism:** serverless functions have hard time/memory limits — no
background loops. The UI's continuous-cycle mode was removed (`9c492e4`); a
"cycle" run returns an honest 501 with a note to use an external scheduler
instead.
**Fix:** `/hire` runs a single cycle (`mode="single"`); scheduling repeats is
the caller's job (cron/scheduler hitting the endpoint). Don't re-add
`mode="cycle"` on serverless.
**Test-for-it:** call `/hire` with `mode=cycle` and assert it 501s rather than
hanging; a unit/integration test on the route is fine.

## Recipe library

- **Local sanity check:** `uvicorn src.main:app --reload` with a `.env` —
  app must boot. Then `python -m pytest tests/ -q`.
- **Deploy dry-run:** `vercel build` locally catches most config typos.
- **Verify auth fail-closed:** with `DRY_RUN=false`, `/trade` and `/kill-switch/*`
  must reject missing/wrong `AGENT_API_TOKEN` (`829576c` added endpoint auth).
  The 401 path is the safety property, not the happy path.

## What this skill is NOT

- Not a Vercel platform manual. Only the failure modes this repo has actually
  hit. Platform behavior changes → check Vercel docs (via `internet-research`
  if you need to fetch them).
- Not the on-chain deploy path. Contract deployment is `scripts/` + X Layer —
  that's `onchain-audit-trail`'s domain.
- Not general FastAPI. `src/main.py` specifics live here only where Vercel
  makes them matter.

## When to escalate

- Adding a NEW heavy dependency or any package with native components — verify
  Vercel support before merging.
- Any change to x402 payment middleware or paid-route auth — touches money;
  load `okx-agent-payments-protocol` and test the 401/paid paths explicitly.
- A leaked key appears anywhere — stop, rotate the key, scrub history, add a
  gitleaks rule, then proceed.
