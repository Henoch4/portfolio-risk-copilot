# AuditTrail Trader

**Autonomous AI Trading Agent with Onchain Audit Trail**
*Built for OKX Build X AI Season Hackathon 2026 (Aug 7–21)*

A multi-agent AI trading system that combines Wall Street risk management principles with onchain transparency — every trading decision is logged to X Layer **before** execution, creating an immutable, verifiable audit trail.

## Architecture

```
Market Data → Signal Engine → Risk Gate → Onchain Logger → Execution

  OKX CLI       Mean Reversion
                     Momentum          (non-overridable)
                     Funding Rate         ↓
                                          ↓  if rejected → BLOCKED
                                          ↓  if approved → logDecision() on X Layer
                                          ↓  then executeOrder() via OKX CLI
```

### Key Design Decisions

1. **Non-overridable risk gate** (Wall Street principle): The RiskGate sits between the AI signal generator and the execution layer. The AI cannot bypass position limits, daily loss limits, or confidence thresholds.

2. **Onchain audit trail**: Every decision is signed with EIP-191 and submitted to `TradeAuditTrail.sol` on X Layer **before** the order hits OKX. If logging fails, trading is blocked.

3. **Multi-agent pipeline**: Inspired by TradingAgents research paper — separate specialized agents for market data, signal generation, risk evaluation, and execution.

## Ported Governance Layers

Five modules ported from a sibling MVP so the live signal set gets the same
pre-trade governance a funding-arbitrage desk would demand (`config/profiles.yaml`
drives the curator):

- **Pre-signal data integrity gate** (`src/data_integrity.py`) — runs BEFORE
  signal generation (Phase 1.5), so a stale/NaN feed or an unreconciled ledger
  blocks the asset before any trade is ever *considered*. Hard blocks are
  audited. Toggle staleness via `DATA_STALENESS_SECONDS`.
- **Curator profile selector** (`src/curator.py`) — selects only from a fixed
  profile allowlist (never writes raw risk params), enforces a switch cooldown,
  auto-reverts on underperformance, and forces `defensive` on drawdown breach.
  Integration is **default-passthrough**: the profile is the default per knob;
  `CURATOR_*` env vars override only the knob they name.
- **Atomic multi-leg execution** (`src/multi_leg.py`) — a two+ leg package
  dispatched concurrently through an explicit state machine
  (PENDING_FILL → LOCKED → SETTLED, or ABORTED). Partial fills unwind the
  filled leg immediately; unlike the source MVP, per-leg `max_slippage_pct` is
  actually enforced — a breached fill triggers the unwind path, never LOCKED.
- **Strategy validation** (`src/validation.py`) — walk-forward windows, PBO,
  Sharpe/CAGR/max-drawdown/Calmar, and a `cleared_for_paper_trading` gate
  (Calmar ≥ 1.0 AND PBO ≤ 0.5). Surface: `GET /api/v1/validation`.
- **Local append-only audit log** (`src/audit_trail.py`) — JSONL log (default
  `audit_log.jsonl`, override `AUDIT_LOG_PATH`) recording every curator switch,
  integrity block, confidence-floor skip, and risk-gate rejection, complementing
  the on-chain decision log. Surface: `GET /api/v1/curator-profile`.

Tests: `python -m pytest tests/ -q` — 280 tests, fully offline.

> **External-user strategy:** see `docs/DESIGN-external-vault.md` — pooled
> `TradingVault.sol` (ERC-4626 style) so external deposits grow the same pot
> the agent already trades, plus a separate depositor-facing surface that
> answers "is my money safe / is it working / can I get it out" and links
> directly to the on-chain audit trail. Vault contract, offline EVM tests
> (`tests/test_trading_vault.py`, 22 cases) and artifacts in
> `contracts/artifacts/`; the depositor-facing UI surface is still design-only.

## Files

```
Portfolio-risk-copilot/
├── contracts/
│   ├── contracts/TradeAuditTrail.sol    # Audit trail smart contract
│   ├── artifacts/TradeAuditTrail_abi.json  # Compiled ABI
│   ├── artifacts/TradeAuditTrail_bytecode.txt
│   ├── scripts/deploy.py                 # Python deploy script
│   └── scripts/deploy.js                 # Hardhat deploy script
├── src/
│   ├── main.py          # FastAPI: /hire, /trade, /audit-stats, /risk-stats,
│   │                    #   /kill-switch, /api/v1/{validation,curator-profile}
│   ├── agent.py         # Multi-agent orchestrator
│   ├── signals.py       # Signal: mean rev + momentum + funding
│   ├── execution.py     # OrderExecutor + RiskGate (non-overridable)
│   ├── audit_logger.py  # OnchainLogger (X Layer)
│   ├── auditor.py       # Existing risk audit (extended)
│   ├── okx_cli.py       # OKX CLI wrapper
│   ├── validation.py    # Walk-forward + PBO + Calmar strategy validation gate
│   ├── data_integrity.py# Pre-signal integrity gate (staleness/NaN/ledger/orphan)
│   ├── audit_trail.py   # Local append-only JSONL audit log
│   ├── multi_leg.py     # Atomic multi-leg execution (state machine, simulated fills)
│   └── curator.py       # Profile selector (allowlist, cooldown, auto-revert)
├── config/
│   └── profiles.yaml    # Fixed profile allowlist for the curator
├── tests/
│ ├── test_signals.py       # 15 signal tests
│ ├── test_execution.py     # 20 risk gate tests (incl. kill switch)
│ ├── test_auditor.py       # 24 audit tests
│ ├── test_validation.py    # validation pipeline
│ ├── test_data_integrity.py # integrity gate
│ ├── test_audit_trail.py   # local audit log
│ ├── test_multi_leg.py     # multi-leg state machine (incl. slippage unwind)
│ ├── test_curator.py       # curator + default-passthrough env knobs
│ ├── test_agent_wiring.py  # integrity + curator wired into the trading loop
│ └── test_agent_sizing.py  # fractional-Kelly sizing
├── scripts/
│   ├── smoke_test.py            # Legacy audit smoke test
│   └── smoke_test_trading.py    # Trading pipeline smoke test
├── manifest.json          # ASP manifest for okx.ai
├── requirements.txt
└── HACKATHON_SUBMISSION.md
```

## Quick Start

### Run Tests (no network needed)

```bash
pip install -r requirements.txt
python scripts/smoke_test_trading.py
python -m pytest tests/ -v
```

### Deploy Contract

```bash
# Install py-solc-x
pip install py-solc-x

# Compile
python scripts/compile_contract.py

# Deploy to X Layer Testnet
set XLAYER_RPC_URL=https://testnet-rpc.xlayer.tech
set DEPLOYER_PRIVATE_KEY=0xYOUR_PRIVATE_KEY
python scripts/deploy_contract.py
```

### Run Trading Agent

```bash
# Configure environment
set XLAYER_RPC_URL=https://testnet-rpc.xlayer.tech
set AUDIT_CONTRACT_ADDRESS=<deployed_contract_address>
set AGENT_WALLET_PRIVATE_KEY=<agent_signing_key>
set OKX_API_KEY=<okx_api_key>
set OKX_SECRET_KEY=<okx_secret>
set OKX_PASSPHRASE=<okx_passphrase>
set DRY_RUN=true  # Set false for live trading

# Start server
python -m uvicorn src.main:app --reload --port 8000

# Run a trading cycle
curl -X POST http://localhost:8000/trade \
  -H "Content-Type: application/json" \
  -d '{"assets": ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]}'
```

### OKX AI Platform (okx.ai) Endpoint

```bash
curl -X POST http://localhost:8000/hire \
  -H "Content-Type: application/json" \
  -d '{"mode": "own_account", "profile_mode": "demo"}'
```

## Strategy Overview

### Signal Engine (`src/signals.py`)

| Strategy | Description | Tradeable Threshold |
|---|---|---|
| Mean Reversion | Z-score of rolling window; LONG when oversold, SHORT when overbought | Z > 2.0 |
| Momentum | MA crossover (5 vs 20) + volume confirmation | MA spread > 1% |
| Funding Rate | Contrarian signal based on funding rate extremes | ±0.1% |
| Ensemble | Weighted vote of all strategies | Confidence ≥ 70% |

### Risk Engine (`src/execution.py::RiskGate`)

| Parameter | Default | Description |
|---|---|---|
| kill_switch | Inactive | Global halt (auto-triggers on daily loss breach) |
| max_position_usd | $5,000 | Max per-trade position |
| max_daily_loss_usd | $500 | Daily loss limit |
| max_daily_trades | 10 | Daily trade count limit |
| max_leverage | 5.0x | Max leverage allowed |
| min_confidence_bps | 7000 (70%) | Min signal confidence |
| allowed_assets | BTC, ETH, SOL, BNB | Asset allowlist |

> **Note on daily counters**: `RiskGate`'s daily-loss and daily-trade counters are
> in-memory (`src/execution.py`, `_daily_loss` / `_daily_trade_count`), so a
> process restart resets today's accumulated loss/trade counts. Fine for dry-run
> and demo use; for real capital, persist them (Redis/DB) so limits survive
> restarts. The contract-level limits on `TradeAuditTrail.sol` are onchain and do
> survive restarts, but the off-chain counters in the Python gate are not.

## Smart Contract: TradeAuditTrail.sol

**Deployed on**: X Layer Testnet (chainId: 1952)
**Native USDC**: Supported (CCTP-ready, MiCA-compliant)

### Contract Functions

| Function | Visibility | Description |
|---|---|---|
| `setRiskParams()` | external | Set non-overridable risk params (can only tighten) |
| `activateKillSwitch()` | external | Halt all trading from this agent |
| `deactivateKillSwitch()` | external | Resume trading after kill switch |
| `logDecision()` | external | Log a trade decision (requires signature + risk check) |
| `recordExecution()` | external | Record post-trade execution receipt |
| `getAgentDailyStats()` | view | Query daily stats for an agent |
| `getRecentDecisions()` | view | Query recent decisions |

### Security Features
- **Signature verification**: EIP-191 personal_sign on every decision
- **Risk param enforcement**: Contract-level position/loss limits
- **Kill switch**: Onchain + off-chain halt, auto-trigger on loss breach
- **Tightening only**: Risk params can only become stricter
- **No relayer bypass**: `onlyAgent` modifier prevents third-party calls

