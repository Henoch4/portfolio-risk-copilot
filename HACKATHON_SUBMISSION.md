# Build X AI Season Hackathon 2026 — Submission

## Project: AuditTrail Trader

**Autonomous AI Trading Agent with Onchain Audit Trail**

Deployed on X Layer Testnet → Mainnet

## Quick Start

```bash
# Python backend
cd AuditTrailTrader
pip install -r requirements.txt
python -m uvicorn src.main:app --reload --port 8000

# Solidity contracts
cd contracts
npm install
npx hardhat compile
npx hardhat run scripts/deploy.js --network xltestnet

# Run tests
cd ../..
python -m pytest tests/ -v
python scripts/smoke_test.py
python scripts/smoke_test_trading.py
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Multi-Agent Pipeline                   │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  Market Data Agent  →  Signal Agents  →  Ensemble        │
│        │                   │                    │        │
│        ▼                   ▼                    ▼        │
│  (OKX CLI/TWS API)   (Mean Reversion,       (Weighted      │
│                      Momentum, Funding)      Vote)         │
│                                                          │
│  Risk Agent  →  Onchain Logger  →  Execution Agent        │
│      │              │                   │                  │
│      └─ pre-trade   └─ logDecision    └─ placeOrder        │
│         gate (hard)    on X Layer         via OKX CLI      │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### Components

1. **TradeAuditTrail.sol** — Non-overridable onchain audit log
   - Enforces risk params before any trade
   - Requires agent signature on every decision
   - Logs decisions and executions immutably

2. **Signal Engine** (`src/signals.py`)
   - Mean reversion (Z-score based)
   - Momentum (MA crossover + volume)
   - Funding rate arbitrage
   - Ensemble combination with weighted voting

3. **Risk Engine** (`src/execution.py` — RiskGate)
    - Kill switch (global halt, auto-trigger on daily loss breach)
    - Position size limits (non-overridable)
    - Daily loss limits
    - Daily trade count caps
    - Asset allowlist
    - Minimum confidence threshold
    - Fat-finger price check (>20% deviation rejection)
    - Price collar / slippage enforcement on limit orders
    - Reduce-only enforcement (no flipping long→short)

4. **Onchain Logger** (`src/audit_logger.py`)
    - Signs decisions with EIP-191 (personal_sign)
    - Submits to TradeAuditTrail.sol before execution
    - Records fill/receipt after execution
    - Onchain kill switch (mirrors off-chain gate)

5. **Orchestrator** (`src/agent.py`)
   - Coordinates: Market Data → Signals → Risk → Onchain Log → Execute
   - Dry-run mode for testing
   - Continuous loop with configurable interval

## Key Features

- **Wall Street risk controls**: Pre-trade gate that the AI agent CANNOT override
- **Onchain audit trail**: Every decision is logged to X Layer before execution
- **Multi-strategy signals**: Mean reversion + momentum + funding rate
- **Pay-per-trade via x402**: 0.50 USDT per trading cycle (already wired from existing ASP)
- **Self-custodial**: Uses OKX OAuth/demo mode — no private key handling

## How It Works

1. Agent fetches market data for BTC, ETH, SOL via OKX CLI
2. Three signal strategies generate independent signals
3. Signals are ensembled into a single recommendation
4. RiskGate evaluates the order against non-overridable limits
5. If approved, decision is signed and logged to TradeAuditTrail.sol on X Layer
6. Order is placed via OKX CLI (or dry-run)
7. Execution receipt is recorded onchain

## X Layer Deployment

**Network**: X Layer Testnet (chainId: 1952)
**RPC**: `https://testnet-rpc.xlayer.tech`
**Native USDC**: Now available (replaces USDC.Bridged)

### Deploy Contract

```bash
cd contracts
cp .env.example .env
# Edit .env with your private key
npx hardhat run scripts/deploy.js --network xltestnet
```

## Submission Checklist

- [x] AI element: Multi-strategy signal generation + ensemble voting
- [x] X Layer: Contract deployed, uses native USDC
- [x] Dedicated X account: [@AuditTrailTrade](https://x.com/AuditTrailTrade)

## Files

```
AuditTrailTrader/
├── contracts/
│   ├── contracts/TradeAuditTrail.sol     # Audit trail smart contract
│   ├── artifacts/TradeAuditTrail_abi.json # Compiled ABI
│   ├── scripts/deploy.js                 # Deployment script
│   └── hardhat.config.js                # Hardhat config
├── src/
│   ├── main.py          # FastAPI: /trade, /hire, /audit-stats, /risk-stats, /kill-switch endpoints
│   ├── agent.py         # Multi-agent orchestrator
│   ├── signals.py       # Signal generation engine
│   ├── execution.py     # Order executor + RiskGate
│   ├── audit_logger.py  # Onchain logger for X Layer
│   ├── auditor.py       # Existing risk audit (extended)
│   └── okx_cli.py       # OKX CLI wrapper
├── tests/
│   ├── test_signals.py     # 15 signal tests
│   ├── test_execution.py   # 20 risk gate tests (incl. kill switch, fat-finger, slippage, reduce-only)
│   └── test_auditor.py     # 24 audit tests
├── scripts/
│   ├── smoke_test.py            # Original audit smoke test
│   └── smoke_test_trading.py    # New trading pipeline smoke test
├── manifest.json          # ASP manifest for okx.ai
└── requirements.txt
```
