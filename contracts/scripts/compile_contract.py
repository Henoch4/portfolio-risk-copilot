"""
Compile TradeAuditTrail.sol using py-solc-x.
Outputs ABI and bytecode to contracts/artifacts/.
"""
import json
import os
import sys
from pathlib import Path

from solcx import compile_source, install_solc

CONTRACT_DIR = Path(__file__).resolve().parent.parent
CONTRACT_PATH = CONTRACT_DIR / "contracts" / "TradeAuditTrail.sol"
ARTIFACTS_DIR = CONTRACT_DIR / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

SOLC_VERSION = "0.8.30"

def compile_contract():
    # Ensure solc is installed
    try:
        install_solc(SOLC_VERSION)
    except Exception as e:
        print(f"solc install note: {e}")

    print(f"Compiling {CONTRACT_PATH}...")

    with open(CONTRACT_PATH, "r") as f:
        source = f.read()

    compiled = compile_source(
        source,
        solc_version=SOLC_VERSION,
        output_values=["abi", "bin"],
        via_ir=True,
    )

    contract_id = f"<stdin>:TradeAuditTrail"
    if contract_id not in compiled:
        contract_id = list(compiled.keys())[0]

    abi = compiled[contract_id]["abi"]
    bytecode = compiled[contract_id]["bin"]

    abi_path = ARTIFACTS_DIR / "TradeAuditTrail_abi.json"
    bin_path = ARTIFACTS_DIR / "TradeAuditTrail_bytecode.txt"

    abi_path.write_text(json.dumps(abi, indent=2))
    bin_path.write_text(bytecode)

    print(f"ABI saved to {abi_path}")
    print(f"Bytecode saved to {bin_path}")
    print(f"Bytecode length: {len(bytecode)} chars")
    print(f"ABI entries: {len(abi)}")

    # Check for the new function in ABI
    for item in abi:
        if item.get("name") == "decisionIndex":
            print("decisionIndex mapping is in ABI (viewable via .decisionIndex())")

    return abi, bytecode

if __name__ == "__main__":
    compile_contract()
