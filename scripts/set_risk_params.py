"""Set (or correct) the on-chain risk parameters for this agent.

There was previously no script in this repo that called setRiskParams, so
whatever set the live contract's maxLeverageBps=500 (instead of 50000 for
5x) left no trace of how or why. This script is the one canonical way to
do it going forward — run it any time MAX_POSITION_USD, MAX_DAILY_LOSS_USD,
or MAX_LEVERAGE change, and it's the fix for the currently-broken live
value.

IMPORTANT — before running this:
1. Make sure AGENT_WALLET_PRIVATE_KEY is a FRESH, rotated key. The key that
   was previously hardcoded in scripts/test_onchain.py is compromised (it
   was committed in plaintext to a public repo) and must not be reused,
   even on testnet.
2. Note the contract's setRiskParams has a ratchet: once params are set for
   an address, every field can only be tightened (position size and
   leverage can only decrease, never increase) — see TradeAuditTrail.sol's
   "pos too high" / "lev too high" checks. If you need to loosen a limit,
   the contract has no path for that by design; you'd need a new agent
   address.

Usage:
    export AGENT_WALLET_PRIVATE_KEY=0x...   # fresh, rotated key
    export XLAYER_RPC_URL=https://xlayertestrpc.okx.com
    export AUDIT_CONTRACT_ADDRESS=0x...
    export MAX_POSITION_USD=5000
    export MAX_DAILY_LOSS_USD=500
    export MAX_LEVERAGE=5.0          # a float multiplier, e.g. 5.0 = 5x
    export MIN_CONFIDENCE_BPS=7000
    python scripts/set_risk_params.py
"""
import os
import sys

_PRIVATE_KEY = os.environ.get("AGENT_WALLET_PRIVATE_KEY", "").strip()
_RPC_URL = os.environ.get("XLAYER_RPC_URL", "").strip()
_CONTRACT_ADDRESS = os.environ.get("AUDIT_CONTRACT_ADDRESS", "").strip()

if not all([_PRIVATE_KEY, _RPC_URL, _CONTRACT_ADDRESS]):
    print(
        "Missing required env vars. Need AGENT_WALLET_PRIVATE_KEY, "
        "XLAYER_RPC_URL, and AUDIT_CONTRACT_ADDRESS — see the docstring "
        "at the top of this file for the full list.",
        file=sys.stderr,
    )
    sys.exit(1)

sys.path.insert(0, ".")
from src.audit_logger import OnchainLogger  # noqa: E402


def main():
    max_position_usd = float(os.environ.get("MAX_POSITION_USD", "5000"))
    max_daily_loss_usd = float(os.environ.get("MAX_DAILY_LOSS_USD", "500"))
    max_leverage = float(os.environ.get("MAX_LEVERAGE", "5.0"))
    min_confidence_bps = int(os.environ.get("MIN_CONFIDENCE_BPS", "7000"))

    # bps convention: 10000 = 1x (100%). This is the exact line that was
    # wrong before (a hardcoded 500 instead of a computed value).
    max_leverage_bps = int(max_leverage * 10000)

    logger = OnchainLogger(
        rpc_url=_RPC_URL,
        contract_address=_CONTRACT_ADDRESS,
        private_key=_PRIVATE_KEY,
        chain_id=int(os.environ.get("XLAYER_CHAIN_ID", "1952")),
    )

    print(f"Agent address: {logger.agent_address}")
    print(f"Setting risk params:")
    print(f"  max_position_usd   = ${max_position_usd:,.2f}  (-> {int(max_position_usd * 1e8)} on-chain, 1e8-scaled)")
    print(f"  max_daily_loss_usd = ${max_daily_loss_usd:,.2f}  (-> {int(max_daily_loss_usd * 1e8)} on-chain, 1e8-scaled)")
    print(f"  max_leverage       = {max_leverage}x  (-> {max_leverage_bps} bps on-chain)")
    print(f"  min_confidence_bps = {min_confidence_bps}")

    current = logger.contract.functions.agentRiskParams(logger.agent_address).call()
    print(f"\nCurrent on-chain params before this call: {current}")
    if current[0] > 0 and (
        int(max_position_usd * 1e8) > current[0] or max_leverage_bps > current[1]
    ):
        print(
            "\nWARNING: the contract only allows tightening existing params "
            "(position size and leverage can't increase once set). This "
            "call will likely revert with 'pos too high' or 'lev too high'. "
            "If you need a higher limit, deploy a new agent address.",
            file=sys.stderr,
        )

    tx_hash = logger.set_risk_params(
        max_position_usd=max_position_usd,
        max_daily_loss_usd=max_daily_loss_usd,
        max_leverage_bps=max_leverage_bps,
        min_confidence_bps=min_confidence_bps,
    )
    print(f"\nDone. Tx: {tx_hash}")

    updated = logger.contract.functions.agentRiskParams(logger.agent_address).call()
    print(f"On-chain params after this call: {updated}")


if __name__ == "__main__":
    main()
