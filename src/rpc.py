"""X Layer RPC endpoints with one-level failover (roadmap Phase 1).

The system previously depended on a single XLAYER_RPC_URL for every chain
read and write, so one endpoint's outage was indistinguishable from "the
chain is down". This module centralizes endpoint selection:

- ``rpc_urls()``: primary (XLAYER_RPC_URL) then independent fallback
  (XLAYER_RPC_URL_FALLBACK), deduplicated.
- ``get_web3()``: first endpoint that actually answers ``eth_chainId``.
  Raises ConnectionError when nothing responds — callers already treat
  that as "onchain surface unavailable" and fail closed.

Transaction-level failover lives in OnchainLogger._send_transaction: a tx
that was never broadcast is retried on the surviving endpoint; a tx whose
state is unknown is verified by hash before any resend decision.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_TESTNET = "https://xlayertestrpc.okx.com"
_PROBE_TIMEOUT_SECONDS = 2


def rpc_urls(default_primary: str | None = None) -> list[str]:
    """Configured endpoints, primary first, deduplicated, non-empty."""
    primary = os.getenv("XLAYER_RPC_URL", "").strip() or (default_primary or "")
    fallback = os.getenv("XLAYER_RPC_URL_FALLBACK", "").strip()
    urls: list[str] = []
    for url in (primary, fallback):
        if url and url not in urls:
            urls.append(url)
    return urls


def build_web3(url: str):
    from web3 import Web3

    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": _PROBE_TIMEOUT_SECONDS}))


def _alive(w3) -> bool:
    try:
        return bool(w3.eth.chain_id)
    except Exception:  # noqa: BLE001 — any probe failure means "not alive"
        return False


def get_web3(default_primary: str | None = None):
    """Web3 bound to the first configured endpoint that answers eth_chainId."""
    urls = rpc_urls(default_primary=default_primary)
    tried: list[str] = []
    for url in urls:
        w3 = build_web3(url)
        if _alive(w3):
            return w3
        tried.append(url)
    raise ConnectionError(
        "No responsive X Layer RPC endpoint. Tried: " + (", ".join(tried) or "<none configured>")
    )
