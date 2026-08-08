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
│   ├── main.py          # FastAPI: /hire, /trade, /audit-stats, /risk-stats, /kill-switch
│   ├── agent.py         # Multi-agent orchestrator
│   ├── signals.py       # Signal: mean rev + momentum + funding
│   ├── execution.py     # OrderExecutor + RiskGate (non-overridable)
│   ├── audit_logger.py  # OnchainLogger (X Layer)
│   ├── auditor.py       # Existing risk audit (extended)
│   └── okx_cli.py       # OKX CLI wrapper
├── tests/
│   ├── test_signals.py     # 15 signal tests
│   ├── test_execution.py   # 20 risk gate tests (incl. kill switch)
│   └── test_auditor.py     # 24 audit tests
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

## Smart Contract: TradeAuditTrail.sol

**Deployed on**: X Layer Testnet (chainId: 195)
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

## Hackathon Alignment

| Requirement | Status |
|---|---|
| Incorporate AI elements | Multi-strategy signal engine with ensemble voting |
| Deploy on X Layer | Solidity contract + Python backend on X Layer |
| Active X account | [@AuditTrailTrader](https://twitter.com/AuditTrailTrader) |
| Tag @XLayerOfficial | On submission post |
| Google Form by Aug 21 | [Link](https://docs.google.com/forms/d/e/1FAIpQLSfgU_3zcXdxK0GJQxj33QeUWdEcAaYnieVe9p5cFDb2JFQa4Q/viewform) |

## Prize Track Opportunities

1. **AI-RWA track** (50K Liquidity Grant) — Can tokenize risk-adjusted yield strategies
2. **Launch Grant** (up to 200K) — Trading volume via OKX DEX interface
3. **Hackathon Grant** (30K 1st place) — Product completeness + innovation
