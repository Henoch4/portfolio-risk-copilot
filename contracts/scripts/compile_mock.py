import json, pathlib, sys
from solcx import compile_files, set_solc_version

set_solc_version('0.8.29')
contracts_dir = pathlib.Path('contracts')
artifacts = pathlib.Path('artifacts')
artifacts.mkdir(exist_ok=True)

files = [str(contracts_dir / 'MockUSDT.sol'), str(contracts_dir / 'TradingVault.sol')]
print(f"Compiling: {files}")

try:
    out = compile_files(
        files,
        output_values=['abi', 'bin'],
        solc_kwargs={'allow_paths': '.', 'optimize': {'enabled': True, 'runs': 200}}
    )
    for name, artifact in out.items():
        short = name.split(':')[-1]
        (artifacts / f'{short}_abi.json').write_text(json.dumps(artifact['abi'], indent=2))
        (artifacts / f'{short}_bytecode.txt').write_text(artifact['bin'])
        print(f'OK: {short} — abi={len(artifact["abi"])} entries, bytecode={len(artifact["bin"])} chars')
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
