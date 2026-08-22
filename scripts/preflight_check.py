"""Phase-1 env pre-flight check (roadmap: "one misconfigured env var is the
cheapest catastrophic bug available").

Run before any deploy or restart that will touch real money:

    python scripts/preflight_check.py

Exit code 0 = no FAIL lines, 1 = at least one FAIL. Secrets are never
printed — only presence/absence and derived-safe values (e.g. the agent
ADDRESS derived from the key, never the key itself).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass
class CheckResult:
    status: str  # PASS | WARN | FAIL
    name: str
    detail: str


def _flag(env: dict, name: str) -> str:
    return (env.get(name) or "").strip()


def run_checks(env: dict) -> list[CheckResult]:
    results: list[CheckResult] = []

    def add(status: str, name: str, detail: str) -> None:
        results.append(CheckResult(status, name, detail))

    # --- Trading mode ---
    dry_run = _flag(env, "DRY_RUN").lower()
    allow_live = _flag(env, "ALLOW_LIVE").lower()
    if dry_run not in ("true", "false"):
        add("FAIL", "DRY_RUN", f"unset/unrecognized ({dry_run!r}) — must be explicitly 'true' or 'false'")
        dry_run_val = None
    else:
        dry_run_val = dry_run == "true"
        if dry_run_val:
            add("PASS", "DRY_RUN", "true — orders are simulated")
        else:
            add("WARN", "DRY_RUN", "FALSE — live order placement enabled. Confirm this is intended.")

    if dry_run_val is False and allow_live != "true":
        add("FAIL", "ALLOW_LIVE", "DRY_RUN=false requires ALLOW_LIVE=true as a second explicit switch")
    elif allow_live == "true" and dry_run_val is False:
        add("WARN", "ALLOW_LIVE", "true with DRY_RUN=false — live trading ARMED")
    elif allow_live == "true":
        add("WARN", "ALLOW_LIVE", "set true but DRY_RUN=true — live trading still off")
    else:
        add("PASS", "ALLOW_LIVE", "false/unset — live trading off")

    # --- API auth ---
    token_set = bool(_flag(env, "AGENT_API_TOKEN"))
    if dry_run_val is False and not token_set:
        add("FAIL", "AGENT_API_TOKEN", "required once DRY_RUN=false (/trade and /kill-switch/* refuse without it)")
    elif token_set:
        add("PASS", "AGENT_API_TOKEN", "set (value hidden)")
    else:
        add("WARN", "AGENT_API_TOKEN", "unset — fine for local dry-run only")

    # --- Risk state durability ---
    risk_state = _flag(env, "RISK_STATE_PATH")
    if risk_state:
        parent = os.path.dirname(risk_state) or "."
        if os.path.isdir(parent):
            add("PASS", "RISK_STATE_PATH", f"{risk_state} (parent exists)")
        else:
            add("FAIL", "RISK_STATE_PATH", f"parent directory does not exist: {parent}")
    else:
        add(
            "WARN",
            "RISK_STATE_PATH",
            "unset — durable daily counters fall back to a TEMP dir and are lost on reboot/redeploy. "
            "Set this to persistent storage before real money.",
        )

    # --- Chain endpoints ---
    rpc_primary = _flag(env, "XLAYER_RPC_URL")
    rpc_fallback = _flag(env, "XLAYER_RPC_URL_FALLBACK")
    if not rpc_primary:
        add("FAIL", "XLAYER_RPC_URL", "unset — onchain surface cannot initialize")
    elif rpc_fallback:
        add("PASS", "XLAYER_RPC_URL", "primary + fallback configured (values hidden)")
    else:
        add("WARN", "XLAYER_RPC_URL", "no XLAYER_RPC_URL_FALLBACK — single-endpoint dependency")

    chain_id = _flag(env, "XLAYER_CHAIN_ID") or "1952"
    if chain_id not in ("195", "1952"):
        add("FAIL", "XLAYER_CHAIN_ID", f"unexpected value {chain_id!r} (195=testnet-era alias? 1952=X Layer)")
    else:
        label = "X Layer testnet" if chain_id == "1952" else chain_id
        add("PASS", "XLAYER_CHAIN_ID", f"{chain_id} ({label})")

    # --- Contracts ---
    for var in ("AUDIT_CONTRACT_ADDRESS", "VAULT_CONTRACT_ADDRESS"):
        addr = _flag(env, var)
        if not addr:
            add("WARN", var, "unset — related endpoints disabled until set")
        elif not (addr.startswith("0x") and len(addr) == 42):
            add("FAIL", var, "malformed address (expected 0x + 40 hex chars)")
        else:
            add("PASS", var, "well-formed (value hidden)")

    # --- Agent signing key ---
    pk = _flag(env, "AGENT_WALLET_PRIVATE_KEY")
    if not pk:
        add("WARN", "AGENT_WALLET_PRIVATE_KEY", "unset — onchain logging disabled (dry-run OK)")
    elif not (pk.startswith("0x") and len(pk) == 66):
        add("FAIL", "AGENT_WALLET_PRIVATE_KEY", "malformed (expected 0x + 64 hex chars) — value NOT printed")
    else:
        try:
            from eth_account import Account

            address = Account.from_key(pk).address
            add("PASS", "AGENT_WALLET_PRIVATE_KEY", f"valid; agent address {address} (key itself hidden)")
        except Exception:
            add("FAIL", "AGENT_WALLET_PRIVATE_KEY", "not derivable to an address — corrupt key? value NOT printed")

    # --- x402 paywall must stay inert in Phase 1 ---
    pay_to = _flag(env, "PAY_TO_ADDRESS")
    if pay_to:
        add(
            "FAIL",
            "PAY_TO_ADDRESS",
            "SET — the x402 paywall goes live. Phase 1 must charge no fees "
            "(compliance safety); unset this unless you decided fees deliberately.",
        )
    else:
        add("PASS", "PAY_TO_ADDRESS", "unset — paywall inert, no fees charged")

    # --- OKX credentials: all-or-nothing ---
    okx = [_flag(env, k) for k in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")]
    if all(okx):
        add("PASS", "OKX credentials", "all three set (values hidden)")
    elif any(okx):
        add("FAIL", "OKX credentials", "partially configured — set all of OKX_API_KEY/SECRET_KEY/PASSPHRASE or none")
    else:
        add("WARN", "OKX credentials", "none set — demo/dry-run only")

    # --- Integrity gate sanity ---
    staleness_raw = _flag(env, "DATA_STALENESS_SECONDS") or "30"
    try:
        staleness = float(staleness_raw)
        if staleness <= 0 or staleness > 300:
            add("WARN", "DATA_STALENESS_SECONDS", f"{staleness:g}s outside sane band (1–300s)")
        else:
            add("PASS", "DATA_STALENESS_SECONDS", f"{staleness:g}s")
    except ValueError:
        add("FAIL", "DATA_STALENESS_SECONDS", f"not a number: {staleness_raw!r}")

    # --- Alerting ---
    if _flag(env, "ALERT_WEBHOOK_URL"):
        add("PASS", "ALERT_WEBHOOK_URL", "set — kill-switch/state-write alerts fire (value hidden)")
    else:
        add("WARN", "ALERT_WEBHOOK_URL", "unset — kill switch exists only in logs. Configure before real money.")

    return results


def main() -> int:
    env = dict(os.environ)
    # Convenience: also read .env from repo root if present (no override of real env).
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env.setdefault(key.strip(), value.strip())

    results = run_checks(env)
    width = max(len(r.name) for r in results)
    print("\nAuditTrail Trader — env pre-flight\n" + "=" * 60)
    for r in results:
        marker = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[r.status]
        print(f"{marker} {r.name:<{width}}  {r.detail}")
    fails = sum(1 for r in results if r.status == "FAIL")
    warns = sum(1 for r in results if r.status == "WARN")
    print("=" * 60)
    print(f"{len(results)} checks: {fails} fail, {warns} warn")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
