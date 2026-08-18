"""
Compile repo Solidity contracts using py-solc-x.
Outputs ABI and bytecode to contracts/artifacts/.
"""
import json
import sys
from pathlib import Path

from solcx import compile_source, install_solc

CONTRACT_DIR = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = CONTRACT_DIR / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

SOLC_VERSION = "0.8.30"

CONTRACTS = {
    "TradeAuditTrail": {
        "path": CONTRACT_DIR / "contracts" / "TradeAuditTrail.sol",
        "via_ir": True,
    },
    "TradingVault": {
        "path": CONTRACT_DIR / "contracts" / "TradingVault.sol",
        "via_ir": True,
    },
}


def compile_contract(name, path, via_ir=True):
    # Ensure solc is installed
    try:
        install_solc(SOLC_VERSION)
    except Exception as e:
        print(f"solc install note: {e}")

    print(f"Compiling {path}...")

    source = path.read_text()

    compiled = compile_source(
        source,
        solc_version=SOLC_VERSION,
        output_values=["abi", "bin"],
        via_ir=via_ir,
    )

    contract_id = f"<stdin>:{name}"
    if contract_id not in compiled:
        contract_id = list(compiled.keys())[0]

    abi = compiled[contract_id]["abi"]
    bytecode = compiled[contract_id]["bin"]

    abi_path = ARTIFACTS_DIR / f"{name}_abi.json"
    bin_path = ARTIFACTS_DIR / f"{name}_bytecode.txt"

    abi_path.write_text(json.dumps(abi, indent=2))
    bin_path.write_text(bytecode)

    print(f"ABI saved to {abi_path}")
    print(f"Bytecode saved to {bin_path}")
    print(f"Bytecode length: {len(bytecode)} chars")
    print(f"ABI entries: {len(abi)}")

    return abi, bytecode


def main():
    names = sys.argv[1:] or list(CONTRACTS.keys())
    for name in names:
        if name not in CONTRACTS:
            print(f"Unknown contract: {name}")
            sys.exit(1)
        compile_contract(name, **CONTRACTS[name])


if __name__ == "__main__":
    main()