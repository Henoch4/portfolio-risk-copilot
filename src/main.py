"""
FastAPI ASP surface for the Portfolio Risk Copilot.
Exposes /hire (run an audit), /manifest, /health.

Run locally (after completing README.md setup):
    uvicorn src.main:app --reload --port 8000
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from collections import defaultdict
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from x402.http import OKXAuthConfig, OKXFacilitatorClient, OKXFacilitatorConfig
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.mechanisms.evm.exact.server import ExactEvmScheme
from x402.mechanisms.evm.deferred.server import AggrDeferredEvmScheme
from x402.server import x402ResourceServer

from .auditor import run_audit, run_audit_from_data, AuditReport
from .okx_cli import OkxCli, OkxCliConfig, OkxCliError
from .execution import RiskGate
from .agent import AutonomousTradingAgent
from .audit_logger import OnchainLogger


# Safety guard: live-account audits are opt-in at the PROCESS level.
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

_MANIFEST_PATH = pathlib.Path(__file__).resolve().parent.parent / "manifest.json"

app = FastAPI(title="Portfolio Risk Copilot", version="0.1.0")

# --- Rate limiting ---
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 30     # requests per window


def _check_rate_limit(client_ip: str = "global") -> bool:
    """Return True if request is allowed, False if rate limited."""
    now = time.time()
    _rate_limit_store[client_ip] = [
        t for t in _rate_limit_store[client_ip] if now - t < RATE_LIMIT_WINDOW
    ]
    if len(_rate_limit_store[client_ip]) >= RATE_LIMIT_MAX:
        return False
    _rate_limit_store[client_ip].append(now)
    return True


# --- x402 payment SDK wiring ---
_pay_to = os.getenv("PAY_TO_ADDRESS", "")
if _pay_to:
    _facilitator = OKXFacilitatorClient(
        OKXFacilitatorConfig(
            auth=OKXAuthConfig(
                api_key=os.getenv("OKX_API_KEY", ""),
                secret_key=os.getenv("OKX_SECRET_KEY", ""),
                passphrase=os.getenv("OKX_PASSPHRASE", ""),
            ),
            base_url=os.getenv("OKX_BASE_URL", ""),
        )
    )
    _x402_server = x402ResourceServer(_facilitator)
    _x402_server.register("eip155:196", ExactEvmScheme())
    _x402_server.register("eip155:196", AggrDeferredEvmScheme())
    _PAID_ROUTES: dict = {}
    app.add_middleware(PaymentMiddlewareASGI, routes=_PAID_ROUTES, server=_x402_server)


class HireRequest(BaseModel):
    mode: Literal["own_account"] = Field(
        "own_account", description="Only 'own_account' is supported."
    )
    profile_mode: Literal["demo", "live"] = Field(
        "demo", description="'demo' or 'live'."
    )
    inst_type: Literal["SWAP", "FUTURES", "OPTION"] | None = Field(
        None, description="Optional filter for the leverage/positions check."
    )
    # Data-forwarding mode (production): User Agent sends pre-gathered data
    balance_data: dict | None = Field(
        None,
        description="Output of `okx account balance-all --json`. Required for data-forwarding mode.",
    )
    positions_data: list | dict | None = Field(
        None,
        description="Output of `okx account positions --json`. Array of position objects.",
    )


@app.get("/manifest")
def manifest():
    if not _MANIFEST_PATH.exists():
        raise HTTPException(500, "manifest.json missing from deployment")
    data = json.loads(_MANIFEST_PATH.read_text())
    data["live_mode_enabled"] = ALLOW_LIVE
    return data


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/hire")
async def hire(req: HireRequest):
    if req.profile_mode == "live" and not ALLOW_LIVE:
        raise HTTPException(
            403,
            "Live-account audits are disabled on this deployment. "
            "Set ALLOW_LIVE=true in the environment to enable.",
        )

    # Rate limiting
    if not _check_rate_limit():
        raise HTTPException(429, "Rate limit exceeded. Try again later.")

    # Mode detection: data-forwarding vs CLI
    if req.balance_data is not None:
        # Data-forwarding mode (production) — no credentials needed
        try:
            report = run_audit_from_data(
                balance_data=req.balance_data,
                positions_data=req.positions_data,
                inst_type=req.inst_type,
            )
        except ValueError as e:
            raise HTTPException(400, f"Invalid input data: {e}")
    else:
        # CLI mode (local testing) — needs OKX credentials on server
        try:
            report = await run_audit(
                demo=(req.profile_mode == "demo"),
                profile=None,
                inst_type=req.inst_type,
            )
        except OkxCliError as e:
            raise HTTPException(502, f"OKX CLI call failed: {e}")

    return asdict(report)


# --- Dashboard ---
_STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(str(_STATIC_DIR / "index.html"))


# --- Trading Agent Setup ---
_ALLOWED_ASSETS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP"]

def _make_risk_gate() -> RiskGate:
    return RiskGate(
        max_position_usd=float(os.getenv("MAX_POSITION_USD", "5000")),
        max_daily_loss_usd=float(os.getenv("MAX_DAILY_LOSS_USD", "500")),
        max_daily_trades=int(os.getenv("MAX_DAILY_TRADES", "10")),
        max_leverage=float(os.getenv("MAX_LEVERAGE", "5.0")),
        min_confidence_bps=int(os.getenv("MIN_CONFIDENCE_BPS", "7000")),
        allowed_assets=_ALLOWED_ASSETS,
    )

def _make_onchain_logger() -> OnchainLogger | None:
    """Create onchain logger if configured. Returns None if not configured."""
    rpc_url = os.getenv("XLAYER_RPC_URL")
    contract_addr = os.getenv("AUDIT_CONTRACT_ADDRESS")
    private_key = os.getenv("AGENT_WALLET_PRIVATE_KEY")
    if not all([rpc_url, contract_addr, private_key]):
        return None
    return OnchainLogger(
        rpc_url=rpc_url,
        contract_address=contract_addr,
        private_key=private_key,
        chain_id=int(os.getenv("XLAYER_CHAIN_ID", "195")),
    )

_dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
_risk_gate = _make_risk_gate()
_onchain_logger = _make_onchain_logger()
_cli = OkxCli(OkxCliConfig(demo=not _dry_run))
_trading_agent = AutonomousTradingAgent(
    okx_cli=_cli,
    risk_gate=_risk_gate,
    onchain_logger=_onchain_logger,
    dry_run=_dry_run,
    max_position_usd=float(os.getenv("MAX_POSITION_USD", "5000")),
    agent_id=os.getenv("AGENT_ID", "autonomous-trader-001"),
)


class TradeRequest(BaseModel):
    assets: list[str] = Field(default=_ALLOWED_ASSETS, description="Assets to trade")
    mode: Literal["single", "cycle"] = Field("single", description="Single cycle or continuous")
    interval_seconds: int = Field(300, description="Interval for continuous mode")


class KillSwitchRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="Why the kill switch is being activated")


class AuditStatsRequest(BaseModel):
    days: int = Field(7, ge=1, le=30, description="Number of days to query")


@app.post("/trade")
async def trade(req: TradeRequest):
    """
    Run a trading cycle: generate signals → pass risk checks → log onchain → execute.
    
    In dry-run mode (default), no real trades are placed and no onchain
    transactions are sent. Set DRY_RUN=false and provide wallet credentials
    to enable live trading.
    """
    # Short-circuit the whole cycle if the kill switch is active — every
    # individual order would be rejected by risk_gate.check_order anyway,
    # but there's no reason to spend a cycle generating signals and calling
    # out to market data just to reject everything at the last step.
    kill_status = _risk_gate.kill_switch_status()
    if kill_status["active"]:
        raise HTTPException(
            423,
            f"Kill switch is active: {kill_status['reason']}. "
            f"Call POST /kill-switch/deactivate to resume.",
        )

    try:
        result = await _trading_agent.run_trading_cycle(req.assets)
        return {
            "cycle_id": result.cycle_id,
            "timestamp": result.timestamp,
            "signals": [
                {
                    "asset": sig["asset"],
                    "action": sig["ensemble"]["direction"],
                    "confidence": sig["ensemble"]["confidence_bps"] / 10000.0,
                    "rationale": sig["ensemble"]["rationale"],
                }
                for sig in result.signals
            ],
            "decisions": result.decisions,
            "executions": result.executions,
            "total_pnl_usd": result.total_pnl_usd,
            "total_fees_usd": result.total_fees_usd,
            "status": result.status,
            "errors": result.errors,
            "dry_run": _dry_run,
        }
    except Exception as e:
        raise HTTPException(500, f"Trading cycle failed: {e}")


@app.get("/audit-stats")
async def audit_stats(req: AuditStatsRequest):
    """Query onchain audit trail for recent decisions and executions."""
    if not _onchain_logger:
        return {"error": "Onchain logger not configured. Set XLAYER_RPC_URL, AUDIT_CONTRACT_ADDRESS, and AGENT_WALLET_PRIVATE_KEY."}
    
    try:
        stats = _onchain_logger.get_contract_stats(days=req.days)
        return stats
    except Exception as e:
        raise HTTPException(500, f"Audit query failed: {e}")


@app.get("/risk-stats")
async def risk_stats():
    """Get current daily risk statistics for the agent."""
    stats = _risk_gate.get_daily_stats(_trading_agent.agent_id)
    return {
        "daily_loss_usd": stats["loss"],
        "daily_pnl_usd": -stats["loss"],
        "daily_trade_count": stats["trade_count"],
        "position_size_usd": stats["volume"],
        "max_position_usd": _risk_gate.max_position_usd,
        "max_daily_loss_usd": _risk_gate.max_daily_loss_usd,
        "max_daily_trades": _risk_gate.max_daily_trades,
        "max_leverage": _risk_gate.max_leverage,
        "min_confidence_bps": _risk_gate.min_confidence_bps,
        "kill_switch": _risk_gate.kill_switch_status(),
    }


@app.get("/kill-switch")
async def kill_switch_status():
    """Current kill switch status. Checks the off-chain gate; also checks the
    onchain contract if configured, since either layer can independently halt
    trading (see TradeAuditTrail.sol activateKillSwitch)."""
    status = _risk_gate.kill_switch_status()
    if _onchain_logger:
        try:
            status["onchain_active"] = _onchain_logger.is_kill_switch_active()
        except Exception as e:
            status["onchain_check_error"] = str(e)
    return status


@app.post("/kill-switch/activate")
async def activate_kill_switch(req: KillSwitchRequest):
    """Halt all trading immediately. Always activates the off-chain gate;
    also activates onchain if the audit logger is configured, so the halt
    holds even if a caller only checks one layer."""
    _risk_gate.activate_kill_switch(req.reason)
    result = {"status": "activated", "reason": req.reason, "onchain": None}
    if _onchain_logger:
        try:
            tx_hash = _onchain_logger.activate_kill_switch(req.reason)
            result["onchain"] = {"tx_hash": tx_hash}
        except Exception as e:
            result["onchain"] = {"error": str(e)}
    return result


@app.post("/kill-switch/deactivate")
async def deactivate_kill_switch():
    """Resume trading. A deliberate, separate call — this is never triggered
    automatically, only the activation is (see RiskGate.report_loss)."""
    _risk_gate.deactivate_kill_switch()
    result = {"status": "deactivated", "onchain": None}
    if _onchain_logger:
        try:
            tx_hash = _onchain_logger.deactivate_kill_switch()
            result["onchain"] = {"tx_hash": tx_hash}
        except Exception as e:
            result["onchain"] = {"error": str(e)}
    return result
