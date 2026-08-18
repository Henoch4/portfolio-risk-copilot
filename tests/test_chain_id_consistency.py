"""Guards the chain-ID consistency the review flagged.

The live contract is deployed on X Layer Testnet chainId 1952 (per RPC in
contracts/scripts/deploy.py). Every source of a chain id in the repo must
agree, otherwise a logger built without the XLAYER_CHAIN_ID env var would
sign for the wrong chain and every tx would revert.
"""
import inspect
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_onchain_logger_default_chain_id_is_1952():
    from src.audit_logger import OnchainLogger
    default = inspect.signature(OnchainLogger.__init__).parameters["chain_id"].default
    assert default == 1952, f"OnchainLogger default chain_id is {default}, expected 1952"


def test_deploy_script_chain_id_is_1952():
    deploy = (REPO / "contracts" / "scripts" / "deploy.py").read_text(encoding="utf-8")
    assert "CHAIN_ID = 1952" in deploy, "contracts/scripts/deploy.py must deploy to chain 1952"


def test_main_defaults_to_1952():
    main = (REPO / "src" / "main.py").read_text(encoding="utf-8")
    assert 'os.getenv("XLAYER_CHAIN_ID", "1952")' in main, (
        "src/main.py must default XLAYER_CHAIN_ID to 1952 to match the deployment"
    )


def test_readme_reports_1952():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "chainId: 1952" in readme, "README must report the deployed chain id (1952)"


def test_hardhat_config_deploys_to_1952():
    hardhat = (REPO / "contracts" / "hardhat.config.js").read_text(encoding="utf-8")
    assert "chainId: 1952," in hardhat, (
        "hardhat.config.js xltestnet network must use chainId 1952 to match the RPC"
    )


def test_submission_doc_reports_1952():
    sub = (REPO / "HACKATHON_SUBMISSION.md").read_text(encoding="utf-8")
    assert "chainId: 1952" in sub, (
        "HACKATHON_SUBMISSION.md must report the deployed chain id (1952)"
    )


def test_no_stale_195_chain_id_literal():
    # 195 (without the trailing 2) must not appear as a chain id anywhere.
    for path in [
        REPO / "src" / "audit_logger.py",
        REPO / "src" / "main.py",
        REPO / "contracts" / "scripts" / "deploy.py",
        REPO / "contracts" / "hardhat.config.js",
        REPO / "README.md",
        REPO / "HACKATHON_SUBMISSION.md",
    ]:
        text = path.read_text(encoding="utf-8")
        assert "chain_id: int = 195," not in text
        assert "chainId: 195)" not in text
        assert "chainId: 195," not in text
