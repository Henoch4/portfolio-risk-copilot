"""Test the onchain logger against the deployed contract."""
import asyncio
import os
import sys

os.environ['XLAYER_RPC_URL'] = 'https://xlayertestrpc.okx.com'
os.environ['XLAYER_CHAIN_ID'] = '1952'
os.environ['AUDIT_CONTRACT_ADDRESS'] = '0x6019b96e9d0Ba17588eb22579d9c2dEf0473d07c'
os.environ['AGENT_WALLET_PRIVATE_KEY'] = '0x<REDACTED_PRIVATE_KEY>'
os.environ['PYTHONPATH'] = '.'

from src.audit_logger import OnchainLogger, DecisionPayload

async def main():
    logger = OnchainLogger(
        rpc_url='https://xlayertestrpc.okx.com',
        contract_address='0x6019b96e9d0Ba17588eb22579d9c2dEf0473d07c',
        private_key='0x<REDACTED_PRIVATE_KEY>',
        chain_id=1952,
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
