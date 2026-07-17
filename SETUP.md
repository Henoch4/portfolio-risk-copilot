# Portfolio Risk Copilot — Handoff & Setup

## What this is (plain English)

An ASP ("Agent Service Provider") for the OKX.AI Genesis Hackathon. Other
agents (or a person) can "hire" it via one API call, and it audits the
**connected OKX trading account** for three things:

1. **Concentration risk** — is too much of the portfolio sitting in one asset?
2. **Leverage risk** — are any open positions dangerously leveraged?
3. **Smart-money divergence** — is the account positioned opposite what
   OKX's top traders are currently doing on the same asset?

It returns a risk score (0.0–1.0) and a list of specific flags. **It is
strictly read-only** — it cannot place, cancel, or transfer anything, and
it never touches an arbitrary third-party wallet address, only the
account it's authenticated as.

Everything in this repo was built and verified against the actual OKX
Agent Trade Kit source (the GitHub repo you uploaded) — every CLI command
referenced in the code is real and copy-pasteable from
`docs/cli-reference.md` and the `skills/` folder in that repo. Nothing
was invented.

## What's already done

- [x] Code written and verified against the real Trade Kit docs
- [x] Offline logic test suite — runs with **zero pip installs**, already
      passing (see "Verify without installing anything" below)
- [x] Read-only by design; live-account access requires an explicit
      opt-in environment variable (`ALLOW_LIVE=true`), off by default
- [x] A full critique pass — re-checked every field name used in the code
      against the actual documented response schemas, not just re-run
      against its own tests. Found and fixed 4 real bugs (see changelog
      below), including one that was silently invisible to the original
      test suite because the test fixtures encoded the same wrong
      assumption as the code.

## Bug-fix changelog (from the critique pass)

1. **Smart-money divergence check was dead code.** `netNotionalUsdt`,
   `weightedLongRatio`, `weightedShortRatio` were read as top-level
   fields; the real API nests them under `notional` and `longShortRatio`
   sub-objects (confirmed in
   `skills/okx-cex-smartmoney/references/signal-commands.md`). This meant
   `net_notional` always evaluated to `0`, always fell below the noise
   threshold, and the flag could never fire — against the real CLI,
   silently, while the old test "passed" because its fixture had the same
   flat shape. **Verified the fix mattered**, not just theoretically:
   reverting it and re-running the smoke test reproduces the failure.
2. **False-positive risk in ticker matching.** `instId.startswith(ccy)`
   would match a lookalike ticker (e.g. `"BTCUP-USDT-SWAP"` matching
   `ccy="BTC"`). Worse, in a mixed position list this could cause a
   **false negative** — the wrong position's side gets used for the
   divergence check, masking a real one. Fixed to an exact
   `instId.split("-")[0] == ccy` comparison. Added a regression test with
   the lookalike position listed first specifically to catch this.
3. **Unhandled exceptions could leak a raw traceback.** Malformed CLI
   output (`json.JSONDecodeError`) or a subprocess launch failure
   (`OSError`) weren't caught anywhere — they'd propagate straight past
   the `except OkxCliError` handling and crash `/hire` with a raw 500.
   Both are now wrapped into `OkxCliError`, and `/hire` has a catch-all
   that turns any surviving one into a clean `502` instead.
4. **`manifest.json` (the file) and the `/manifest` route had already
   drifted** into two different, hand-maintained copies (the route was
   missing `input_schema`, `output_schema`, `sla_ms`, and used a
   different `hire_endpoint` format). The route now loads the file
   instead of duplicating it.

## What's NOT done yet (needs your technical team + internet access)

- [ ] Install Node.js + the real `okx` CLI and confirm it against a live/demo OKX account
- [ ] `pip install -r requirements.txt` and run the FastAPI server
- [ ] Deploy somewhere with a public URL (Render, Fly.io, Railway, etc. —
      **not Vercel**: it can't bundle the Node.js CLI alongside the Python
      backend and has no persistent filesystem for OKX credentials; see
      chat history for the full reasoning if this needs to be re-litigated)
- [ ] Register on the OKX.AI marketplace (mandatory per hackathon rules —
      **we have not ingested OKX.AI's actual listing/Installation Guide**,
      only the hackathon rules page, so this step needs to be checked
      fresh against OKX.AI's own docs)
- [ ] Two open questions that documentation alone can't resolve — need a
      live `okx` binary to confirm:
      - Whether `--demo`/`--profile` flags are accepted by the
        `smartmoney` module (`okx_cli.py`, `smartmoney_signal`)
      - The exact JSON shape of `okx config show --json` (no literal
        example exists anywhere in the docs — only "any profile with a
        non-empty `api_key` field"). `check_auth()` defensively handles
        both a dict-of-profiles and a list-of-profiles shape, but this
        is an inference, not a confirmed contract. **If auth detection
        misbehaves in testing, this is the first place to look.**

---

## Verify without installing anything

Anyone — technical or not — can confirm the core logic actually works,
right now, with just Python already on the machine (no `pip install`,
no OKX account, no internet):

```bash
python3 scripts/smoke_test.py
```

Expected output ends with `ALL CHECKS PASSED`. This proves the
risk-scoring logic is correct against realistic fixture data. It does
**not** prove the real `okx` CLI behaves identically — that's the next
step, below.

---

## Full setup (technical — needs Node.js + internet)

### 1. Install the OKX CLI

```bash
npm install -g @okx_ai/okx-trade-cli
```

Requires Node.js >= 18. Verify:

```bash
okx --help
```

### 2. Authenticate (demo mode recommended for the hackathon)

Two options — pick one:

```bash
# Option A: API key (generate one in OKX's UI, demo/simulated trading)
okx config init

# Option B: OAuth (browser login)
okx auth login --manual
```

Confirm it worked:

```bash
okx --demo account balance --json
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the real test suite (optional, mirrors smoke_test.py but via pytest)

```bash
pytest tests/ -v
```

### 5. Run the server

```bash
cp .env.example .env      # leave ALLOW_LIVE=false for the demo
uvicorn src.main:app --reload --port 8000
```

Test it:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/manifest
curl -X POST http://localhost:8000/hire \
  -H "Content-Type: application/json" \
  -d '{"mode": "own_account", "profile_mode": "demo"}'
```

### 6. Deploy

Any host that runs a Node.js binary alongside a Python process works
(the FastAPI app shells out to `okx`, so both runtimes need to be on the
same machine/container). Render, Fly.io, and Railway all support
multi-runtime Docker images if you need one; a simple Dockerfile
installing both Node 18+ and Python 3.11+, then `npm install -g
@okx_ai/okx-trade-cli` and `pip install -r requirements.txt`, is enough.

### 7. Update `manifest.json`

Replace `hire_endpoint` with your real deployed URL before submitting.

### 8. Submit to OKX.AI

Follow OKX.AI's own Installation/Listing Guide (not yet ingested into
this project — check the live docs before this step) to register the
ASP, then complete the hackathon's remaining steps: X post with
`#OKXAI` (demo can be embedded in the post, no separate video needed),
and the Google form before **July 17, 23:59 UTC**.

---

## Project structure

```
portfolio-risk-copilot/
├── requirements.txt
├── manifest.json
├── .env.example
├── SETUP.md                    # this file
├── scripts/
│   └── smoke_test.py           # zero-dependency logic check
├── src/
│   ├── __init__.py
│   ├── main.py                 # FastAPI: /hire, /manifest, /health
│   ├── auditor.py              # risk-scoring logic
│   └── okx_cli.py               # the ONLY file that calls the real `okx` binary
└── tests/
    └── test_auditor.py         # same checks as smoke_test.py, via pytest
```

## Design decisions worth knowing about

- **`ALLOW_LIVE` is a process-level flag, not a per-request one.** A
  malformed or malicious request body can never silently flip a
  demo-only deployment into reading a live account — the operator has to
  explicitly set the environment variable.
- **Auth detection follows OKX's own documented decision table**
  (`config show` first, `auth status` as fallback) rather than a
  simplified guess — this was a real footgun called out in their own
  skill docs (the `auth status` `apiKey` field is always `false` and is
  not a substitute for checking the TOML config).
- **The smart-money divergence check ignores thin pools** (below
  $100k net notional) to avoid flagging noise on illiquid assets — see
  `DIVERGENCE_MIN_NOTIONAL_USDT` in `auditor.py` if you want to tune
  that.
- **Thresholds are starting points, not tuned values** — `auditor.py`
  has them all at the top as named constants for your team to adjust
  before the demo.
