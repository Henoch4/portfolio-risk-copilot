"""
Reconciliation service: operator-attested NAV model (DESIGN-external-vault §4.2).

Compares the vault's on-chain totalAssets against the agent's real OKX balance
and suggests an attestation value. The operator triggers attestation; this
module does NOT write to the chain — it only reads and computes.

Trust model: weakest (operator-attested), clearly labeled in the UI as
"value reported by the operator, auditable on-chain."
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from dataclasses import dataclass, asdict
from typing import Any

from .contracts import load_abi as _load_abi

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationResult:
    """Snapshot of vault vs OKX balance at a point in time."""
    timestamp: float
    vault_total_assets: int | None
    okx_balance_usdt: float | None
    okx_equity_usdt: float | None
    pending_reserved: int | None
    discrepancy_usdt: float | None
    discrepancy_pct: float | None
    suggested_attestation: int | None
    vault_deployed: bool
    okx_available: bool
    last_attestation: int | None
    attest_timelock: int | None
    read_error: str | None = None
    error: str | None = None


def _get_web3():
    from .rpc import get_web3

    return get_web3(default_primary="https://xlayertestrpc.okx.com")


def _vault_contract(w3):
    addr = os.getenv("VAULT_CONTRACT_ADDRESS", "").strip()
    if not addr:
        return None
    abi = _load_abi("TradingVault")
    if not abi:
        return None
    return w3.eth.contract(address=addr, abi=abi)


async def read_okx_balance(cli) -> dict[str, Any]:
    """Read the agent's OKX account balance via OkxCli.

    Uses `balance_all()` (the trading-account snapshot, which honors the
    account's portfolio margin and isolated positions) rather than
    `account_balance` — the latter returns the effective-balance view per
    currency, which materially under-reports equity under margin.

    Returns dict with keys: total_eq, usdt_eq, details (per-ccy balances).
    Returns empty dict on failure.
    """
    try:
        raw = await cli.balance_all()
        if not isinstance(raw, dict):
            return {}
        trading = raw.get("trading")
        if not isinstance(trading, dict):
            return {}
        details = trading.get("details") or []
        total_eq = float(trading.get("totalEq", 0) or 0)
        usdt_eq = 0.0
        if isinstance(details, list):
            for d in details:
                if d.get("ccy") == "USDT":
                    usdt_eq = float(d.get("eq", 0) or 0)
                    break
        return {"total_eq": total_eq, "usdt_eq": usdt_eq, "details": details}
    except Exception as e:
        logger.warning(f"OKX balance read failed: {e}")
        return {}


def read_vault_state() -> dict[str, Any]:
    """Read current vault on-chain state. Returns dict with vault fields."""
    w3 = _get_web3()
    vault = _vault_contract(w3)
    if vault is None:
        return {"deployed": False}

    try:
        return {
            "deployed": True,
            "total_assets": vault.functions.totalAssets().call(),
            "total_supply": vault.functions.totalSupply().call(),
            "pending_reserved": vault.functions.pendingReserved().call(),
            "last_attestation": vault.functions.lastAttestation().call(),
            "attest_timelock": vault.functions.ATTEST_TIMELOCK().call(),
            "max_attestation_delta_bps": vault.functions.MAX_ATTESTATION_DELTA_BPS().call(),
            "total_assets_priced": vault.functions.totalAssetsPriced().call(),
        }
    except Exception as e:
        logger.warning(f"Vault state read failed: {e}")
        # The address is configured, so this is a READ failure, not an absent
        # deployment. Keep deployed=True so callers distinguish "never wired
        # up" from "temporarily unreachable" instead of papering over RPC
        # outages as if the vault did not exist.
        return {"deployed": True, "error": str(e)}


def reconcile(
    vault_state: dict[str, Any],
    okx_data: dict[str, Any],
    asset_decimals: int = 6,
) -> ReconciliationResult:
    """Compute discrepancy between vault totalAssets and OKX USDT balance.

    The vault tracks value in asset minimal units (6 dp for USDT).
    OKX returns values in human-readable USDT.
    """
    now = time.time()
    vault_deployed = vault_state.get("deployed", False)
    okx_available = bool(okx_data)

    # Surface read failures instead of silently treating them as "no data":
    # a missing vault read and an unreachable OKX API are different problems
    # with different user-facing messaging ("not deployed" vs "temporarily
    # unavailable", surfaced by the frontend via read_error).
    read_error_parts = []
    if vault_state.get("error"):
        read_error_parts.append(f"vault: {vault_state['error']}")
    if not okx_data:
        read_error_parts.append("OKX balance unavailable")
    read_error = "; ".join(read_error_parts) or None

    vault_total = vault_state.get("total_assets") if vault_state.get("total_assets") is not None else vault_state.get("total_assets_priced")
    pending = vault_state.get("pending_reserved")
    last_att = vault_state.get("last_attestation")
    att_timelock = vault_state.get("attest_timelock")

    okx_usdt = okx_data.get("usdt_eq")
    okx_equity = okx_data.get("total_eq")

    discrepancy = None
    discrepancy_pct = None
    suggested = None

    if vault_total is not None and okx_usdt is not None:
        divisor = 10 ** asset_decimals
        vault_human = vault_total / divisor
        discrepancy = vault_human - okx_usdt
        if okx_usdt > 0:
            discrepancy_pct = (discrepancy / okx_usdt) * 100
        suggested = int(okx_usdt * divisor)

    return ReconciliationResult(
        timestamp=now,
        vault_total_assets=vault_total,
        okx_balance_usdt=okx_usdt,
        okx_equity_usdt=okx_equity,
        pending_reserved=pending,
        discrepancy_usdt=discrepancy,
        discrepancy_pct=discrepancy_pct,
        suggested_attestation=suggested,
        vault_deployed=vault_deployed,
        okx_available=okx_available,
        last_attestation=last_att,
        attest_timelock=att_timelock,
        read_error=read_error,
    )
