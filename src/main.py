"""
FastAPI ASP surface for the Portfolio Risk Copilot.
Exposes /hire (run an audit), /manifest, /health.

Run locally (after completing README.md setup):
    uvicorn src.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time
from collections import defaultdict
from dataclasses import asdict
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# x402 payment middleware — only available when OKX credentials are configured.
# Guard against import failures so the app boots in dry-run mode without x402 deps.
# NOTE: x402 >= 2.19 renamed the facilitator API — the OKXAuthConfig /
# OKXFacilitatorClient / OKXFacilitatorConfig classes were removed and replaced
# by FacilitatorConfig (url/auth_provider) + HTTPFacilitatorClient. The imports
# below target that current API; the `# type: ignore[assignment]` lines only
# cover the None fallback when the SDK is absent.
try:
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    from x402.mechanisms.evm.exact.server import ExactEvmScheme
    from x402.server import x402ResourceServer
    _x402_available = True
except ImportError:
    _x402_available = False
    FacilitatorConfig = None  # type: ignore[assignment,misc]
    HTTPFacilitatorClient = None  # type: ignore[assignment,misc]
    PaymentMiddlewareASGI = None  # type: ignore[assignment,misc]
    ExactEvmScheme = None  # type: ignore[assignment,misc]
    x402ResourceServer = None  # type: ignore[assignment,misc]

from .auditor import run_audit, run_audit_from_data, AuditReport
from .okx_cli import OkxCli, OkxCliConfig, OkxCliError
from .execution import RiskGate
from .agent import AutonomousTradingAgent
from .audit_logger import OnchainLogger
from .validation import validation_report
from .multi_leg import MultiLegExecutionManager
from .audit_trail import AuditLog
from .curator import CuratorAgent
from .data_integrity import DataIntegrityGate
from .vault_api import router as vault_router
from .reconciliation import read_vault_state, read_okx_balance, reconcile

logger = logging.getLogger(__name__)


# Safety guard: live-account audits are opt-in at the PROCESS level.
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

_MANIFEST_PATH = pathlib.Path(__file__).resolve().parent.parent / "manifest.json"

app = FastAPI(title="Portfolio Risk Copilot", version="0.1.0")
app.include_router(vault_router)


class _CycleWebSocketHub:
    """Fan-out hub for live cycle results. Best-effort: a slow/disconnected
    client never blocks the HTTP /trade path that broadcasts to it."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        # Snapshot so a disconnect mid-iteration can't mutate our loop.
        stale: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


_ws_hub = _CycleWebSocketHub()

# --- Funding-rate cache (dashboard polls; funding moves slowly) ---
_FUNDING_CACHE: dict = {"ts": 0.0, "data": None}
_FUNDING_TTL = 60.0

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


# --- Auth for mutating, money-path endpoints ---
# /trade and /kill-switch/* were previously unauthenticated: anyone who
# found the URL could trigger live trades or toggle the kill switch. This
# is a shared-secret header check, not full auth, but it closes the
# "anyone on the internet can call these" gap.
_AGENT_API_TOKEN = os.getenv("AGENT_API_TOKEN", "").strip()


def _require_agent_token(x_agent_token: str | None = Header(default=None)) -> None:
    if not _AGENT_API_TOKEN:
        if not _dry_run:
            # Fail closed: refuse to run unauthenticated mutating endpoints
            # in live mode rather than silently allowing open access to a
            # real-money trading path.
            raise HTTPException(
                500,
                "AGENT_API_TOKEN is not configured. Live mode requires it "
                "before /trade or /kill-switch/* will accept requests.",
            )
        # Dry-run with no token configured: allow, for local/demo use, but
        # this branch never applies once DRY_RUN=false.
        return
    if x_agent_token != _AGENT_API_TOKEN:
        raise HTTPException(401, "Missing or invalid X-Agent-Token header.")


# --- x402 payment SDK wiring ---
_pay_to = os.getenv("PAY_TO_ADDRESS", "")
if _pay_to and _x402_available:
    _facilitator = HTTPFacilitatorClient(
        FacilitatorConfig(url=os.getenv("OKX_BASE_URL", ""))
    )
    _x402_server = x402ResourceServer(_facilitator)
    _x402_server.register("eip155:196", ExactEvmScheme())
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

    @app.get("/depositor", include_in_schema=False)
    def depositor_page():
        return FileResponse(str(_STATIC_DIR / "depositor.html"))


# --- Trading Agent Setup ---
_ALLOWED_ASSETS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP"]

def _make_risk_gate() -> RiskGate:
    return RiskGate(
        max_position_usd=float(os.getenv("MAX_POSITION_USD", "5000")),
        max_daily_loss_usd=float(os.getenv("MAX_DAILY_LOSS_USD", "500")),
        max_daily_trades=int(os.getenv("MAX_DAILY_TRADES", "10")),
        max_leverage=float(os.getenv("MAX_LEVERAGE", "5.0")),
        min_confidence_bps=int(os.getenv("MIN_CONFIDENCE_BPS", "7000")),
        max_price_age_seconds=float(os.getenv("MAX_PRICE_AGE_SECONDS", "60")),
        allowed_assets=_ALLOWED_ASSETS,
        regime_throttle=os.getenv("REGIME_THROTTLE", "false").lower() in ("1", "true", "yes"),
        regime_band_pct=float(os.getenv("REGIME_BAND_PCT", "5.0")),
        regime_size_scale=float(os.getenv("REGIME_SIZE_SCALE", "0.8")),
    )

def _make_onchain_logger() -> OnchainLogger | None:
    """Create onchain logger if configured. Returns None if not configured."""
    rpc_url = os.getenv("XLAYER_RPC_URL", "").strip()
    contract_addr = os.getenv("AUDIT_CONTRACT_ADDRESS", "").strip()
    private_key = os.getenv("AGENT_WALLET_PRIVATE_KEY", "").strip()
    if not all([rpc_url, contract_addr, private_key]):
        return None
    return OnchainLogger(
        rpc_url=rpc_url,
        contract_address=contract_addr,
        private_key=private_key,
        chain_id=int(os.getenv("XLAYER_CHAIN_ID", "1952")),
    )

def _make_curator() -> CuratorAgent | None:
    """Create the curator from config/profiles.yaml. None if the file is absent
    (the trading agent falls back to its neutral defaults)."""
    profiles_path = pathlib.Path(__file__).resolve().parent.parent / "config" / "profiles.yaml"
    if not profiles_path.exists():
        return None
    return CuratorAgent(profiles_path, audit_log=_audit_log)

_dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
_risk_gate = _make_risk_gate()
_onchain_logger = _make_onchain_logger()
_cli = OkxCli(OkxCliConfig(demo=not _dry_run))
_audit_log = AuditLog(path=os.getenv("AUDIT_LOG_PATH", "audit_log.jsonl"))
_curator = _make_curator()
_integrity_gate = DataIntegrityGate(
    staleness_threshold_s=float(os.getenv("DATA_STALENESS_SECONDS", "30")),
)
_multi_leg_manager = MultiLegExecutionManager(
    max_concurrent_packages=int(os.getenv("MAX_CONCURRENT_PACKAGES", "3")),
)
_trading_agent = AutonomousTradingAgent(
    okx_cli=_cli,
    risk_gate=_risk_gate,
    onchain_logger=_onchain_logger,
    dry_run=_dry_run,
    max_position_usd=float(os.getenv("MAX_POSITION_USD", "5000")),
    agent_id=os.getenv("AGENT_ID", "autonomous-trader-001"),
    sizing_mode=os.getenv("SIZING_MODE", "kelly"),
    kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.5")),
    integrity_gate=_integrity_gate,
    curator=_curator,
    audit_log=_audit_log,
    multi_leg_manager=_multi_leg_manager,
    funding_arb_min_rate=float(os.getenv("FUNDING_ARB_MIN_RATE", "0.001")),
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
async def trade(req: TradeRequest, _auth: None = Depends(_require_agent_token)):
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

    if req.mode == "cycle":
        # This was previously accepted and silently ignored — a single
        # cycle ran regardless of what the caller asked for. Continuous
        # mode isn't wired to anything, and given this deploys to Vercel
        # (serverless, no long-running background loop), the honest fix is
        # to run repeated cycles externally — e.g. a Vercel Cron Job or any
        # scheduler calling POST /trade with mode="single" on an interval —
        # rather than pretend a serverless function can run a loop.
        raise HTTPException(
            501,
            "mode='cycle' is not implemented. This deploys to a serverless "
            "environment that can't run a persistent background loop — call "
            "POST /trade with mode='single' on a schedule (e.g. a Vercel "
            "Cron Job) instead.",
        )

    try:
        result = await _trading_agent.run_trading_cycle(req.assets)
        response = {
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
        # Push the completed cycle to any live dashboard clients. Best-effort:
        # a broadcast failure must not fail the HTTP response.
        try:
            await _ws_hub.broadcast(response)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"WebSocket broadcast failed: {e}")
        return response
    except Exception as e:
        raise HTTPException(500, f"Trading cycle failed: {e}")


@app.get("/audit-stats")
async def audit_stats(days: int = 7):
    """Query onchain audit trail for recent decisions and executions."""
    if not _onchain_logger:
        return {"error": "Onchain logger not configured. Set XLAYER_RPC_URL, AUDIT_CONTRACT_ADDRESS, and AGENT_WALLET_PRIVATE_KEY."}
    
    try:
        stats = _onchain_logger.get_contract_stats(days=days)
        return stats
    except Exception as e:
        raise HTTPException(500, f"Audit query failed: {e}")


@app.get("/risk-stats")
async def risk_stats():
    """Get current daily risk statistics for the agent."""
    stats = _risk_gate.get_daily_stats(_trading_agent.agent_id)
    # Include per-asset regime status if throttle is enabled
    regime_data = {}
    if _risk_gate.regime_throttle:
        for asset in _ALLOWED_ASSETS:
            regime_data[asset] = _risk_gate.regime_status(asset)
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
        "dry_run": _dry_run,
        "regime": regime_data,
    }


@app.get("/positions")
async def positions():
    """Fetch live positions from OKX. Returns per-asset position data
    including side, size, entry price, unrealized P&L, and leverage."""
    try:
        raw = await _cli.positions(inst_type="SWAP")
        if isinstance(raw, dict) and "data" in raw:
            raw = raw["data"]
        if not isinstance(raw, list):
            raw = []
        result = []
        for p in raw:
            inst_id = p.get("instId", "")
            pos_amt = float(p.get("pos", 0) or 0)
            if pos_amt == 0:
                continue
            avg_px = float(p.get("avgPx", 0) or 0)
            upl = float(p.get("upl", 0) or 0)
            lever = float(p.get("lever", 1) or 1)
            side = "long" if pos_amt > 0 else "short"
            notional = abs(pos_amt) * avg_px if avg_px else 0
            result.append({
                "inst_id": inst_id,
                "asset": inst_id.replace("-SWAP", ""),
                "side": side,
                "size": abs(pos_amt),
                "avg_price": avg_px,
                "unrealized_pnl": upl,
                "leverage": lever,
                "notional_usd": notional,
            })
        return {"positions": result}
    except Exception as e:
        return {"positions": [], "error": str(e)}


@app.get("/funding-arb-status")
async def funding_arb_status():
    """Return current funding rates and arb package status for all assets.
    Funding rates are fetched live from the market (cached for 60s) and
    package state comes from the multi-leg manager's open-package tracking."""
    packages = []
    if _multi_leg_manager:
        for pkg in _multi_leg_manager._open_packages.values():
            packages.append({
                "id": pkg.id,
                "asset": pkg.steps[0].asset if pkg.steps else None,
                "state": pkg.state.value,
                "notional": pkg.notional,
                "slippage_breached": pkg.slippage_breached,
                "leg_count": len(pkg.steps),
            })

    now = time.time()
    if _FUNDING_CACHE["data"] is None or (now - _FUNDING_CACHE["ts"]) > _FUNDING_TTL:
        async def _funding_rate(inst_id: str) -> tuple[str, float | None]:
            try:
                funding = await _cli.run("market", "funding-rate", "--instId", inst_id, use_global_flags=False)
                raw = funding.get("data", [{}])[0].get("fundingRate", None)
                return inst_id, (float(raw) if raw is not None else None)
            except Exception:
                return inst_id, None

        fetched = await asyncio.gather(*(_funding_rate(a) for a in _ALLOWED_ASSETS))
        _FUNDING_CACHE["data"] = {asset.replace("-SWAP", ""): rate for asset, rate in fetched}
        _FUNDING_CACHE["ts"] = now

    return {
        "funding_rates": _FUNDING_CACHE["data"],
        "min_rate": _trading_agent.funding_arb_min_rate,
        "open_packages": packages,
        "max_concurrent": _multi_leg_manager.max_concurrent_packages if _multi_leg_manager else 0,
    }


@app.get("/api/v1/validation")
async def validation_status():
    """Read-only: the strategy-validation pipeline gate (walk-forward + PBO +
    Calmar). When VALIDATION_RETURNS_PATH points at a CSV of historical returns
    (columns: in_sample, out_of_sample), the real validation_report() is run and
    its cleared_for_paper_trading boolean is returned. Without data, the gate
    reports an honest null — an unvalidated strategy is not the same as a failed
    one, so it does not block the demo. CI blocks the deploy only when the gate
    is configured AND clears=False (see scripts/check_validation_gate.py)."""
    report = None
    gate_configured = False
    path = os.getenv("VALIDATION_RETURNS_PATH", "").strip()
    if path and os.path.exists(path):
        try:
            import numpy as np
            data = np.loadtxt(path, delimiter=",", skiprows=1)
            in_sample = data[:, 0]
            oos = data[:, 1] if data.shape[1] > 1 else data[:, 0]
            report = validation_report(in_sample, oos)
            gate_configured = True
        except Exception as e:
            logger.warning(f"Validation gate data load failed: {e}")
    return {
        "pipeline": "validation_report (walk-forward + PBO + Calmar bar)",
        "wired": True,
        "gate_configured": gate_configured,
        "calmar_bar": 1.0,
        "pbo_max": 0.5,
        "last_report": report,
        "cleared_for_paper_trading": report["cleared_for_paper_trading"] if report else None,
        "dry_run": _dry_run,
    }


@app.get("/api/v1/curator-profile")
async def curator_profile():
    """Read-only: active curator profile with the resolve knobs for the next
    cycle (profile defaults, overridden per-knob by CURATOR_* env vars)."""
    curator = _trading_agent.curator
    if not curator:
        return {"enabled": False, "defaults": "neutral (no profiles.yaml)"}
    resolved = _trading_agent._resolve_curator_profile()
    return {
        "enabled": True,
        "current_profile": curator.state.current_profile,
        "default_profile": curator.default_profile,
        "allowlist": sorted(curator.profiles.keys()),
        "knobs": resolved if resolved is not None else None,
        "cooldown_cycles": curator.cooldown_cycles,
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


@app.websocket("/ws/cycles")
async def ws_cycles(websocket: WebSocket):
    """Live cycle stream. The dashboard connects here to receive each completed
    cycle's result as soon as POST /trade returns. Connections that error out
    are dropped silently; the HTTP API remains the source of truth."""
    await _ws_hub.connect(websocket)
    try:
        # Keep the socket open; ignore anything the client sends.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _ws_hub.disconnect(websocket)
    except Exception:
        _ws_hub.disconnect(websocket)


@app.post("/kill-switch/activate")
async def activate_kill_switch(req: KillSwitchRequest, _auth: None = Depends(_require_agent_token)):
    """Halt all trading immediately. Always activates the off-chain gate;
    also activates onchain if the audit logger is configured, so the halt
    holds even if a caller only checks one layer."""
    _risk_gate.activate_kill_switch(req.reason)
    result: dict = {"status": "activated", "reason": req.reason, "onchain": None}
    if _onchain_logger:
        try:
            tx_hash = _onchain_logger.activate_kill_switch(req.reason)
            result["onchain"] = {"tx_hash": tx_hash}
        except Exception as e:
            result["onchain"] = {"error": str(e)}
    return result


@app.post("/kill-switch/deactivate")
async def deactivate_kill_switch(_auth: None = Depends(_require_agent_token)):
    """Resume trading. A deliberate, separate call — this is never triggered
    automatically, only the activation is (see RiskGate.report_loss)."""
    _risk_gate.deactivate_kill_switch()
    result: dict = {"status": "deactivated", "onchain": None}
    if _onchain_logger:
        try:
            tx_hash = _onchain_logger.deactivate_kill_switch()
            result["onchain"] = {"tx_hash": tx_hash}
        except Exception as e:
            result["onchain"] = {"error": str(e)}
    return result


# --- Reconciliation (Phase 3: operator-attested NAV) ---

@app.get("/api/v1/vault/reconciliation")
async def vault_reconciliation(_auth: None = Depends(_require_agent_token)):
    """Operator-only reconciliation snapshot: vault totalAssets vs OKX balance.
    Requires agent token — leaks OKX balance and suggested attestation."""
    vault_state = read_vault_state()
    okx_data = await read_okx_balance(_cli)
    result = reconcile(vault_state, okx_data)
    return asdict(result)


@app.post("/api/v1/vault/attest")
async def vault_attest(_auth: None = Depends(_require_agent_token)):
    """Operator triggers attestTotalAssets on the vault contract.
    Reads the current OKX balance, computes the suggested attestation value,
    and sends the transaction. Requires AGENT_API_TOKEN + vault configured."""
    vault_state = read_vault_state()
    if not vault_state.get("deployed"):
        raise HTTPException(503, "Vault not deployed or not configured")

    okx_data = await read_okx_balance(_cli)
    if not okx_data:
        raise HTTPException(502, "Could not read OKX balance")

    result = reconcile(vault_state, okx_data)
    if result.suggested_attestation is None:
        raise HTTPException(500, "Could not compute attestation value")

    # Send the attestation transaction
    rpc_url = os.getenv("XLAYER_RPC_URL", "").strip()
    private_key = os.getenv("AGENT_WALLET_PRIVATE_KEY", "").strip()
    vault_addr = os.getenv("VAULT_CONTRACT_ADDRESS", "").strip()
    if not all([rpc_url, private_key, vault_addr]):
        raise HTTPException(500, "Missing XLAYER_RPC_URL, AGENT_WALLET_PRIVATE_KEY, or VAULT_CONTRACT_ADDRESS")

    try:
        from web3 import Web3
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not w3.is_connected():
            raise HTTPException(502, "Cannot connect to X Layer RPC")

        abi_path = pathlib.Path(__file__).resolve().parent.parent / "contracts" / "artifacts" / "TradingVault_abi.json"
        abi = json.loads(abi_path.read_text()) if abi_path.exists() else []
        vault = w3.eth.contract(address=vault_addr, abi=abi)

        account = Account.from_key(private_key)
        nonce = w3.eth.get_transaction_count(account.address)
        tx = vault.functions.attestTotalAssets(result.suggested_attestation).build_transaction({
            "chainId": int(os.getenv("XLAYER_CHAIN_ID", "1952")),
            "gas": 300000,
            "gasPrice": w3.to_wei("1", "gwei"),
            "nonce": nonce,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        return {
            "status": "attested",
            "suggested": result.suggested_attestation,
            "tx_hash": tx_hash.hex(),
            "gas_used": receipt.get("gasUsed", receipt.get("gas_used", 0)),
            "discrepancy_usdt": result.discrepancy_usdt,
            "okx_balance_usdt": result.okx_balance_usdt,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"vault_attest failed: {e}")
        raise HTTPException(500, "Attestation transaction failed")
