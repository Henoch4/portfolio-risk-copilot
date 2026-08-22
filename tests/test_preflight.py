"""Offline tests for scripts/preflight_check.py — env matrix, no secrets printed."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from preflight_check import run_checks  # noqa: E402


def _base_env():
    return {
        "DRY_RUN": "true",
        "ALLOW_LIVE": "false",
        "XLAYER_RPC_URL": "https://xlayertestrpc.okx.com",
        "XLAYER_RPC_URL_FALLBACK": "https://backup.example",
        "XLAYER_CHAIN_ID": "1952",
        "PAY_TO_ADDRESS": "",
    }


def _by_name(results):
    return {r.name: r for r in results}


def test_all_clear_dry_run():
    results = _by_name(run_checks(_base_env()))
    assert all(r.status != "FAIL" for r in results.values())
    assert results["PAY_TO_ADDRESS"].status == "PASS"


def test_live_mode_requires_token_and_allow_live():
    env = _base_env() | {"DRY_RUN": "false"}
    results = _by_name(run_checks(env))
    assert results["ALLOW_LIVE"].status == "FAIL"
    assert results["AGENT_API_TOKEN"].status == "FAIL"

    env["ALLOW_LIVE"] = "true"
    env["AGENT_API_TOKEN"] = "tok"
    results = _by_name(run_checks(env))
    assert results["ALLOW_LIVE"].status != "FAIL"
    assert results["AGENT_API_TOKEN"].status == "PASS"
    # live mode still warns that it is live
    assert results["DRY_RUN"].status == "WARN"


def test_pay_to_address_set_is_fail_in_phase1():
    env = _base_env() | {"PAY_TO_ADDRESS": "0x" + "a" * 40}
    results = _by_name(run_checks(env))
    assert results["PAY_TO_ADDRESS"].status == "FAIL"
    assert "no fees" in results["PAY_TO_ADDRESS"].detail


def test_partial_okx_credentials_fail():
    env = _base_env() | {"OKX_API_KEY": "k"}
    results = _by_name(run_checks(env))
    assert results["OKX credentials"].status == "FAIL"

    env |= {"OKX_SECRET_KEY": "s", "OKX_PASSPHRASE": "p"}
    results = _by_name(run_checks(env))
    assert results["OKX credentials"].status == "PASS"


def test_malformed_private_key_fails_without_printing():
    env = _base_env() | {"AGENT_WALLET_PRIVATE_KEY": "not-a-key-SUPER-SECRET-VALUE"}
    results = _by_name(run_checks(env))
    assert results["AGENT_WALLET_PRIVATE_KEY"].status == "FAIL"
    joined = " ".join(r.detail for r in results.values())
    assert "SUPER-SECRET-VALUE" not in joined


def test_missing_risk_state_path_warns_not_fails():
    env = _base_env()
    results = _by_name(run_checks(env))
    assert results["RISK_STATE_PATH"].status == "WARN"


def test_no_rpc_configured_fails():
    env = _base_env() | {"XLAYER_RPC_URL": ""}
    results = _by_name(run_checks(env))
    assert results["XLAYER_RPC_URL"].status == "FAIL"
