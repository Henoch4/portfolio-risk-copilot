"""
FastAPI router for the depositor-facing vault API.

Reads on-chain state from TradingVault and TradeAuditTrail via JSON-RPC.
All reads are server-side (no client RPC needed); transactions go directly
from the user's wallet via the ABI served at /api/v1/vault/abi.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .contracts import load_abi as _load_abi

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/vault", tags=["vault"])

# ---------------------------------------------------------------------------
# Config — addresses loaded from env; graceful absent if not deployed yet
# ---------------------------------------------------------------------------

def _get_web3():
    """Lazy-import web3 to avoid import cost at module load."""
    from web3 import Web3
    rpc_url = os.getenv("XLAYER_RPC_URL", "https://xlayertestrpc.okx.com")
    return Web3(Web3.HTTPProvider(rpc_url))


def _vault_contract(w3):
    addr = os.getenv("VAULT_CONTRACT_ADDRESS", "").strip()
    if not addr:
        return None
    abi = _load_abi("TradingVault")
    if not abi:
        return None
    return w3.eth.contract(address=addr, abi=abi)


def _audit_contract(w3):
    addr = os.getenv("AUDIT_CONTRACT_ADDRESS", "").strip()
    if not addr:
        return None
    abi = _load_abi("TradeAuditTrail")
    if not abi:
        return None
    return w3.eth.contract(address=addr, abi=abi)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class VaultStats(BaseModel):
    deployed: bool
    tvl: int | None = None
    total_supply: int | None = None
    share_price: int | None = None
    asset_decimals: int | None = None
    min_deposit: int | None = None
    max_tvl: int | None = None
    settlement_open: bool | None = None
    asset_address: str | None = None
    vault_address: str | None = None
    agent: str | None = None
    pending_reserved: int | None = None


class VaultPosition(BaseModel):
    deployed: bool
    address: str
    share_balance: int = 0
    asset_value: int = 0
    share_balance_human: str = "0"
    asset_value_human: str = "0"


class WithdrawalRequest(BaseModel):
    request_id: int
    shares: int
    usdt_out: int
    owner: str
    deadline: int
    finalized: bool


class AuditRecent(BaseModel):
    deployed: bool
    decisions: list[dict[str, Any]] = []
    count: int = 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=VaultStats)
def vault_stats():
    """Public vault stats: TVL, share price, limits, settlement status."""
    w3 = _get_web3()
    vault = _vault_contract(w3)
    if vault is None:
        return VaultStats(deployed=False)

    try:
        tvl = vault.functions.totalAssets().call()
        total_supply = vault.functions.totalSupply().call()
        share_price = vault.functions.sharePriceAsset().call()
        asset_decimals = vault.functions.assetDecimals().call()
        min_deposit = vault.functions.MIN_DEPOSIT().call()
        max_tvl = vault.functions.MAX_TVL().call()
        settlement_open = vault.functions.settlementOpen().call()
        asset_addr = vault.functions.asset().call()
        agent_addr = vault.functions.agent().call()
        pending = vault.functions.pendingReserved().call()
        vault_addr = os.getenv("VAULT_CONTRACT_ADDRESS", "").strip()

        return VaultStats(
            deployed=True,
            tvl=tvl,
            total_supply=total_supply,
            share_price=share_price,
            asset_decimals=asset_decimals,
            min_deposit=min_deposit,
            max_tvl=max_tvl,
            settlement_open=settlement_open,
            asset_address=asset_addr,
            vault_address=vault_addr,
            agent=agent_addr,
            pending_reserved=pending,
        )
    except Exception as e:
        logger.warning(f"vault_stats read failed: {e}")
        return VaultStats(deployed=False)


@router.get("/position/{address}", response_model=VaultPosition)
def vault_position(address: str):
    """Read share balance and USDT value for a specific address."""
    w3 = _get_web3()
    vault = _vault_contract(w3)
    if vault is None:
        return VaultPosition(deployed=False, address=address)

    try:
        checksum = w3.to_checksum_address(address)
        balance = vault.functions.balanceOf(checksum).call()
        value = vault.functions.convertToAssets(balance).call()
        decimals = vault.functions.assetDecimals().call()
        divisor = 10 ** decimals

        return VaultPosition(
            deployed=True,
            address=checksum,
            share_balance=balance,
            asset_value=value,
            share_balance_human=f"{balance / 1e18:.6f}",
            asset_value_human=f"{value / divisor:.6f}",
        )
    except Exception as e:
        logger.warning(f"vault_position read failed: {e}")
        return VaultPosition(deployed=False, address=address)


@router.get("/withdrawal/{request_id}", response_model=WithdrawalRequest)
def vault_withdrawal(request_id: int):
    """Read withdrawal request status by ID."""
    w3 = _get_web3()
    vault = _vault_contract(w3)
    if vault is None:
        raise HTTPException(503, "Vault not deployed")

    try:
        req = vault.functions.withdrawalRequests(request_id).call()
        return WithdrawalRequest(
            request_id=request_id,
            shares=req[0],
            usdt_out=req[1],
            owner=req[2],
            deadline=req[3],
            finalized=req[4],
        )
    except Exception as e:
        raise HTTPException(404, f"Withdrawal request {request_id} not found: {e}")


@router.get("/abi")
def vault_abi():
    """Serve the TradingVault ABI for client-side transaction construction."""
    abi = _load_abi("TradingVault")
    address = os.getenv("VAULT_CONTRACT_ADDRESS", "").strip()
    usdt_address = ""
    w3 = _get_web3()
    vault = _vault_contract(w3)
    if vault:
        try:
            usdt_address = vault.functions.asset().call()
        except Exception:
            pass
    return {
        "abi": abi,
        "address": address,
        "usdt_address": usdt_address,
        "chain_id": int(os.getenv("XLAYER_CHAIN_ID", "1952")),
    }


@router.get("/audit-recent", response_model=AuditRecent)
def audit_recent(count: int = 10):
    """Recent on-chain decisions from TradeAuditTrail for depositor verification."""
    count = min(count, 100)
    w3 = _get_web3()
    audit = _audit_contract(w3)
    if audit is None:
        return AuditRecent(deployed=False)

    try:
        total = audit.functions.getDecisionCount().call()
        start = max(0, total - count)
        decisions = []
        for i in range(start, total):
            d = audit.functions.getDecision(i).call()
            decisions.append({
                "decision_id": d[0].hex() if isinstance(d[0], bytes) else str(d[0]),
                "asset": d[3],
                "signal": d[4],
                "confidence": d[6],
                "size_usd": d[7],
                "timestamp": d[9],
                "executed": d[11],
            })
        return AuditRecent(deployed=True, decisions=decisions, count=total)
    except Exception as e:
        logger.warning(f"audit_recent read failed: {e}")
        return AuditRecent(deployed=False)
