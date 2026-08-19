import json, os, pathlib, sys
from web3 import Web3
from eth_account import Account

KEY = os.environ.get("DEPLOYER_PRIVATE_KEY", "").strip()
if not KEY:
    print("ERROR: Set DEPLOYER_PRIVATE_KEY environment variable"); sys.exit(1)
CHAIN = 1952
ARTIFACTS = pathlib.Path(__file__).resolve().parent.parent / "artifacts"

RPC = os.environ.get("XLAYER_RPC_URL", "https://xlayertestrpc.okx.com")
w3 = Web3(Web3.HTTPProvider(RPC))
acct = Account.from_key(KEY)
print(f"Deployer: {acct.address}")
print(f"Balance: {w3.from_wei(w3.eth.get_balance(acct.address), 'ether')} OKB")

abi = json.loads((ARTIFACTS / "MockUSDT_abi.json").read_text())
bytecode = (ARTIFACTS / "MockUSDT_bytecode.txt").read_text().strip()

initial_supply = 10**15
contract = w3.eth.contract(abi=abi, bytecode=bytecode)
nonce = w3.eth.get_transaction_count(acct.address)
tx = contract.constructor(initial_supply).build_transaction({
    "chainId": CHAIN, "gas": 2000000, "gasPrice": w3.to_wei("1", "gwei"), "nonce": nonce
})
signed = w3.eth.account.sign_transaction(tx, KEY)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
print(f"Tx: {tx_hash.hex()}")
sys.stdout.flush()

receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
addr = receipt.get("contractAddress") or receipt.get("contract_address")
status = "OK" if receipt.get("status") == 1 else "FAILED"
print(f"Status: {status}")
print(f"MockUSDT deployed at: {addr}")
print(f"Gas used: {receipt.get('gasUsed') or receipt.get('gas_used')}")
