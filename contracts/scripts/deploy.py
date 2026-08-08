"""
Deploy TradeAuditTrail.sol to X Layer Testnet using web3.py.

Prerequisites:
  pip install web3 eth-account eth-abi
  Set environment:
    XLAYER_RPC_URL=https://testnet-rpc.xlayer.tech
    DEPLOYER_PRIVATE_KEY=0x...
    AUDIT_CONTRACT_ADDRESS=<after first deploy>

Usage:
  python scripts/deploy_contract.py
"""
import json
import os
import sys
from pathlib import Path

from web3 import Web
from eth_account import Account

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "contracts"
ABI_PATH = CONTRACT_DIR / "artifacts" / "TradeAuditTrail_abi.json"
BYTECODE_PATH = CONTRACT_DIR / "artifacts" / "TradeAuditTrail_bytecode.txt"
RPC_URL = os.getenv("XLAYER_RPC_URL", "https://testnet-rpc.xlayer.tech")
PRIVATE_KEY = os.getenv("DEPLOYER_PRIVATE_KEY")
CHAIN_ID = 195  # X Layer Testnet


def deploy_contract():
    if not PRIVATE_KEY:
        print("ERROR: Set DEPLOYER_PRIVATE_KEY environment variable")
        print("Example: set DEPLOYER_PRIVATE_KEY=0x...")
        sys.exit(1)

    if not ABI_PATH.exists() or not BYTECODE_PATH.exists():
        print("ERROR: Compiled artifacts not found.")
        print("Run the Solidity compiler first:")
        print('  python compile_contract.py')
        sys.exit(1)

    abi = json.loads(ABI_PATH.read_text())
    bytecode = BYTECODE_PATH.read_text().strip()

    print(f"Connecting to X Layer Testnet: {RPC_URL}")
    w3 = Web(Web.HTTPProvider(RPC_URL))

    if not w3.is_connected():
        print("ERROR: Cannot connect to X Layer RPC")
        sys.exit(1)

    print(f"Connected. Chain ID: {w3.eth.chain_id}")
    print(f"Latest block: {w3.eth.block_number}")

    account = Account.from_key(PRIVATE_KEY)
    print(f"Deploying from: {account.address}")
    balance = w3.eth.get_balance(account.address)
    balance_native = w3.from_wei(balance, "ether")
    print(f"Balance: {balance_native} OKB (native gas token on X Layer)")

    if balance < w3.to_wei("0.01", "ether"):
        print("WARNING: Low balance. You need testnet OKB from the faucet.")
        print("Get OKB (gas token for X Layer) at: https://faucet.xlayer.tech")
        print("Continuing anyway...")

    print("\nDeploying TradeAuditTrail contract...")
    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    nonce = w3.eth.get_transaction_count(account.address)
    tx = Contract.constructor().build_transaction({
        "chainId": CHAIN_ID,
        "gas": 3000000,
        "gasPrice": w3.to_wei("1", "gwei"),
        "nonce": nonce,
    })

    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    print(f"Signed transaction. Sending...")

    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Transaction hash: {tx_hash.hex()}")
    print(f"Waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    print(f"\nContract deployed!")
    print(f"Address: {receipt.contract_address}")
    print(f"Block: {receipt.blockNumber}")
    print(f"Gas used: {receipt.gasUsed}")
    print(f"Status: {'SUCCESS' if receipt.status == 1 else 'FAILED'}")

    # Verify contract is callable
    deployed = w3.eth.contract(address=receipt.contract_address, abi=abi)
    count = deployed.functions.getDecisionCount().call()
    print(f"\nVerification: getDecisionCount() = {count}")

    # Save deployment info
    deploy_info = {
        "address": receipt.contract_address,
        "chain_id": CHAIN_ID,
        "tx_hash": tx_hash.hex(),
        "gas_used": receipt.gasUsed,
        "block_number": receipt.blockNumber,
        "deployer": account.address,
    }
    info_path = CONTRACT_DIR / "deployment.json"
    with open(info_path, "w") as f:
        json.dump(deploy_info, f, indent=2)
    print(f"\nDeployment info saved to {info_path}")

    print(f"\nNext steps:")
    print(f"  1. Set AUDIT_CONTRACT_ADDRESS={receipt.contract_address}")
    print(f"  2. Run setRiskParams() to configure the agent")
    print(f"  3. Start the trading agent: uvicorn src.main:app --port 8000")

    return receipt.contract_address


if __name__ == "__main__":
    deploy_contract()
