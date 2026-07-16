"""
Risk-audit logic for the Portfolio Risk Copilot ASP.

This module talks only to okx_cli.OkxCli -- it never shells out directly.
Three checks, all against the connected OKX account (not a third-party
wallet -- see README.md for why that scope was chosen):

  1. Concentration risk  -- one currency dominating trading equity
  2. Leverage risk       -- open positions above a leverage threshold
  3. Smart-money divergence -- account positioned opposite the
     smart-money pool's consensus direction on the same asset

All thresholds are conservative starting points, not tuned values --
flag for adjustment once real account data is available.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .okx_cli import OkxCli, OkxCliConfig, OkxCliError

# -- Tunable thresholds --
CONCENTRATION_THRESHOLD_PCT = 0.60       # single currency > 60% of trading equity
LEVERAGE_WARN = 5
LEVERAGE_HIGH = 10
DIVERGENCE_MIN_NOTIONAL_USDT = 100_000   # ignore thin/illiquid smart-money pools
MAX_SMARTMONEY_CALLS_PER_AUDIT = 5       # cap external calls per audit


@dataclass
class RiskFlag:
    code: str
    severity: str  # "info" | "warning" | "high"
    detail: str


@dataclass
class AuditReport:
    audit_id: str
    mode: str  # "demo" | "live"
    authenticated: bool
    risk_score: float
    flags: list = field(default_factory=list)
    report_md: str = ""
    needs_human_review: bool = False
    error: str | None = None


def _pct(part: float, whole: float) -> float:
    return (part / whole) if whole else 0.0


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def run_audit(
    demo: bool = True,
    profile: str | None = None,
    inst_type: str | None = None,
) -> AuditReport:
    audit_id = f"audit_{uuid.uuid4().hex[:10]}"
    mode_label = "demo" if demo else "live"
    cli = OkxCli(OkxCliConfig(demo=demo, profile=profile))

    # -- Auth check first. An unauthenticated account is an expected
    # outcome, not an exception -- return a report that says so. --
    auth = await cli.check_auth()
    if not auth["authenticated"]:
        return AuditReport(
            audit_id=audit_id,
            mode=mode_label,
            authenticated=False,
            risk_score=0.0,
            needs_human_review=True,
            error=(
                "Not authenticated. Run `okx config init` (API-key mode) or "
                "`okx auth login` (OAuth mode) before calling /hire. "
                f"detail={auth.get('detail')}"
            ),
        )

    flags: list = []

    # -- 1. Concentration risk --
    try:
        balances = await cli.balance_all()
    except OkxCliError as e:
        return AuditReport(
            audit_id=audit_id, mode=mode_label, authenticated=True,
            risk_score=0.0, needs_human_review=True,
            error=f"account balance-all failed: {e}",
        )

    trading = (balances or {}).get("trading", {}) if isinstance(balances, dict) else {}
    total_eq = _safe_float(trading.get("totalEq"))
    for row in trading.get("details", []) or []:
        eq = _safe_float(row.get("eq", row.get("equity")))
        share = _pct(eq, total_eq)
        if share >= CONCENTRATION_THRESHOLD_PCT:
            flags.append(RiskFlag(
                code="CONCENTRATION",
                severity="warning",
                detail=f"{row.get('ccy', '?')} is {share:.0%} of trading equity",
            ))

    # -- 2. Leverage risk --
    try:
        positions = await cli.positions(inst_type=inst_type)
    except OkxCliError as e:
        positions = []
        flags.append(RiskFlag(
            code="POSITIONS_UNAVAILABLE", severity="info", detail=str(e),
        ))

    if isinstance(positions, dict):
        position_list = positions.get("data", []) or []
    elif isinstance(positions, list):
        position_list = positions
    else:
        position_list = []

    base_currencies_seen = set()

    for pos in position_list:
        lever = _safe_float(pos.get("lever"))
        inst_id = pos.get("instId", "?")
        if lever >= LEVERAGE_HIGH:
            flags.append(RiskFlag(
                code="HIGH_LEVERAGE", severity="high",
                detail=f"{inst_id} at {lever:.0f}x leverage",
            ))
        elif lever >= LEVERAGE_WARN:
            flags.append(RiskFlag(
                code="ELEVATED_LEVERAGE", severity="warning",
                detail=f"{inst_id} at {lever:.0f}x leverage",
            ))
        if isinstance(inst_id, str) and "-" in inst_id:
            base_currencies_seen.add(inst_id.split("-")[0])

    # -- 3. Smart-money divergence (best-effort; never fails the whole audit) --
    for ccy in list(base_currencies_seen)[:MAX_SMARTMONEY_CALLS_PER_AUDIT]:
        try:
            signal = await cli.smartmoney_signal(ccy)
        except OkxCliError:
            continue

        rows = signal.get("data", signal) if isinstance(signal, dict) else signal
        if isinstance(rows, list) and rows:
            row = rows[0]
        elif isinstance(rows, dict):
            row = rows
        else:
            row = None
        if not row:
            continue

        # BUGFIX: these are nested, not top-level, per the documented response
        # shape in skills/okx-cex-smartmoney/references/signal-commands.md
        # ("Response Fields (per instrument, array data[])" -> notional group,
        # longShortRatio group). Reading them as row.get("netNotionalUsdt")
        # etc. always returned None -> 0.0 -> divergence check silently never
        # fired. Caught during a critique pass against the real doc, not by
        # the original tests, which had fixtures shaped the same wrong way.
        notional = row.get("notional") or {}
        long_short = row.get("longShortRatio") or {}
        net_notional = _safe_float(notional.get("netNotionalUsdt"))
        long_ratio = _safe_float(long_short.get("weightedLongRatio"))
        short_ratio = _safe_float(long_short.get("weightedShortRatio"))

        if abs(net_notional) < DIVERGENCE_MIN_NOTIONAL_USDT:
            continue

        pool_side = "long" if long_ratio > short_ratio else "short"
        # BUGFIX: startswith() false-matches lookalike tickers (e.g. ccy="BTC"
        # would also match "BTCUP-USDT-SWAP"). Compare the exact base-currency
        # segment instead.
        account_side = next(
            (p.get("side") for p in position_list
             if isinstance(p.get("instId"), str) and p["instId"].split("-")[0] == ccy),
            None,
        )
        if account_side and account_side.lower() != pool_side:
            flags.append(RiskFlag(
                code="SMART_MONEY_DIVERGENCE", severity="info",
                detail=(
                    f"{ccy}: account is {account_side}, smart-money pool leans "
                    f"{pool_side} (long {long_ratio:.0%} / short {short_ratio:.0%})"
                ),
            ))

    risk_score = min(
        1.0,
        0.15 * sum(1 for f in flags if f.severity == "warning")
        + 0.30 * sum(1 for f in flags if f.severity == "high")
        + 0.05 * sum(1 for f in flags if f.severity == "info"),
    )

    report_md = _render_report(audit_id, mode_label, total_eq, flags, risk_score)

    return AuditReport(
        audit_id=audit_id,
        mode=mode_label,
        authenticated=True,
        risk_score=risk_score,
        flags=flags,
        report_md=report_md,
        needs_human_review=risk_score >= 0.7,
    )


def _render_report(audit_id: str, mode_label: str, total_eq: float,
                    flags: list, risk_score: float) -> str:
    lines = [
        f"# Portfolio Risk Audit — {audit_id}",
        f"- Mode: **{mode_label}**",
        f"- Trading equity: **{total_eq:,.2f} USDT**",
        f"- Risk score: **{risk_score:.2f}**",
        "",
        "## Flags" if flags else "## No flags raised",
    ]
    for f in flags:
        lines.append(f"- `{f.severity.upper()}` **{f.code}** — {f.detail}")
    return "\n".join(lines)
