"""Test the onchain logger against the deployed contract.

SECURITY NOTE: this script previously had a live private key hardcoded in
plaintext, committed to this public repo. That key (deriving to
0x4E80761B7c711a659b9De2d6398d1C45f19289f0) must be treated as permanently
compromised — rotate it (deploy a new agent wallet, update
AGENT_WALLET_PRIVATE_KEY everywhere it's configured, and re-run
setRiskParams from the new address) even though this is testnet. This
script now reads the key from the environment only and refuses to run
without it, so a hardcoded key can't be reintroduced here by accident.
"""
import asyncio
import os
import sys

os.environ.setdefault('XLAYER_RPC_URL', 'https://xlayertestrpc.okx.com')
os.environ.setdefault('XLAYER_CHAIN_ID', '1952')
os.environ.setdefault('AUDIT_CONTRACT_ADDRESS', '0x6019b96e9d0Ba17588eb22579d9c2dEf0473d07c')
os.environ.setdefault('PYTHONPATH', '.')

_PRIVATE_KEY = os.environ.get('AGENT_WALLET_PRIVATE_KEY', '').strip()
if not _PRIVATE_KEY:
    print(
        "AGENT_WALLET_PRIVATE_KEY is not set. Export it before running this "
        "script — never hardcode a key in this file again:\n"
        "  export AGENT_WALLET_PRIVATE_KEY=0x...\n"
        "  python scripts/test_onchain.py",
        file=sys.stderr,
    )
    sys.exit(1)

from src.audit_logger import OnchainLogger, DecisionPayload

async def main():
    logger = OnchainLogger(
        rpc_url=os.environ['XLAYER_RPC_URL'],
        contract_address=os.environ['AUDIT_CONTRACT_ADDRESS'],
        private_key=_PRIVATE_KEY,
        chain_id=int(os.environ['XLAYER_CHAIN_ID']),
    )

    print(f"Agent address: {logger.agent_address}")
    print(f"Connected: {logger.is_connected()}")

    # Check kill switch
    ks = logger.is_kill_switch_active()
    print(f"Kill switch active: {ks}")

    # Check contract stats
    stats = logger.get_contract_stats(days=7)
    print(f"Contract stats: {stats}")

    # Query risk params directly
    risk_params = logger.contract.functions.agentRiskParams(logger.agent_address).call()
    print(f"Risk params: {risk_params}")

    print("\nAll onchain checks passed!")

if __name__ == '__main__':
    asyncio.run(main())
