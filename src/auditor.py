"""
Risk-audit logic for the AuditTrail Trader ASP.

Two entry points:
  1. run_audit()        -- CLI-based (local testing, needs OKX credentials)
  2. run_audit_from_data() -- data-forwarding (production, no credentials needed)

Three checks, all against the connected OKX account:
  1. Concentration risk  -- one currency dominating trading equity
  2. Leverage risk       -- open positions above a leverage threshold
  3. Smart-money divergence -- account positioned opposite the
     smart-money pool's consensus direction on the same asset

All thresholds are conservative starting points, not tuned values.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .okx_cli import OkxCli, OkxCliConfig, OkxCliError

# -- Tunable thresholds --
CONCENTRATION_THRESHOLD_PCT = 0.60
LEVERAGE_WARN = 5
LEVERAGE_HIGH = 10
DIVERGENCE_MIN_NOTIONAL_USDT = 100_000
MAX_SMARTMONEY_CALLS_PER_AUDIT = 5


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


# -- Shared helpers (used by both CLI and data-forwarding paths) --

def _pct(part: float, whole: float) -> float:
    return (part / whole) if whole else 0.0


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _check_concentration(trading_data: dict) -> list[RiskFlag]:
    """Check if any single currency dominates trading equity."""
    flags = []
    total_eq = _safe_float(trading_data.get("totalEq"))
    for row in trading_data.get("details", []) or []:
        eq = _safe_float(row.get("eq", row.get("equity")))
        share = _pct(eq, total_eq)
        if share >= CONCENTRATION_THRESHOLD_PCT:
            flags.append(RiskFlag(
                code="CONCENTRATION",
                severity="warning",
                detail=f"{row.get('ccy', '?')} is {share:.0%} of trading equity",
            ))
    return flags


def _check_leverage(position_list: list) -> tuple[list[RiskFlag], set[str]]:
    """Check leverage thresholds. Returns (flags, base_currencies_seen)."""
    flags = []
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

    return flags, base_currencies_seen


def _check_smart_money_divergence(
    position_list: list,
    base_currencies: set[str],
    smartmoney_fn,
) -> list[RiskFlag]:
    """Check if account position diverges from smart-money consensus.
    smartmoney_fn: callable(ccy) -> dict (sync, returns smart money signal data).
    """
    flags = []
    for ccy in list(base_currencies)[:MAX_SMARTMONEY_CALLS_PER_AUDIT]:
        try:
            signal = smartmoney_fn(ccy)
        except Exception:
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

        notional = row.get("notional") or {}
        long_short = row.get("longShortRatio") or {}
        net_notional = _safe_float(notional.get("netNotionalUsdt"))
        long_ratio = _safe_float(long_short.get("weightedLongRatio"))
        short_ratio = _safe_float(long_short.get("weightedShortRatio"))

        if abs(net_notional) < DIVERGENCE_MIN_NOTIONAL_USDT:
            continue

        pool_side = "long" if long_ratio > short_ratio else "short"
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
    return flags


def _calculate_risk_score(flags: list) -> float:
    return min(
        1.0,
        0.15 * sum(1 for f in flags if f.severity == "warning")
        + 0.30 * sum(1 for f in flags if f.severity == "high")
        + 0.05 * sum(1 for f in flags if f.severity == "info"),
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


# -- Input validation (whitelist-based) --

def _validate_balance_data(data: dict) -> dict:
    """Validate balance_data. Raises ValueError on invalid input."""
    if not isinstance(data, dict):
        raise ValueError("balance_data must be a JSON object")

    trading = data.get("trading")
    if not isinstance(trading, dict):
        raise ValueError("balance_data.trading must be a JSON object")

    total_eq = trading.get("totalEq")
    if total_eq is None:
        raise ValueError("balance_data.trading.totalEq is required")
    try:
        float(total_eq)
    except (TypeError, ValueError):
        raise ValueError("balance_data.trading.totalEq must be a numeric string")

    details = trading.get("details")
    if details is not None and not isinstance(details, list):
        raise ValueError("balance_data.trading.details must be an array")

    return data


def _validate_positions_data(data: list | dict | None) -> list:
    """Validate positions_data. Raises ValueError on invalid input."""
    if data is None:
        return []

    if isinstance(data, dict):
        positions = data.get("data") or data.get("positions") or []
        if not isinstance(positions, list):
            raise ValueError("positions_data.data must be an array")
        return positions

    if isinstance(data, list):
        for i, pos in enumerate(data):
            if not isinstance(pos, dict):
                raise ValueError(f"positions_data[{i}] must be a JSON object")
            if "instId" not in pos:
                raise ValueError(f"positions_data[{i}].instId is required")
        return data

    raise ValueError("positions_data must be a JSON array or object")


# -- Entry point 1: CLI-based (local testing) --

async def run_audit(
    demo: bool = True,
    profile: str | None = None,
    inst_type: str | None = None,
) -> AuditReport:
    audit_id = f"audit_{uuid.uuid4().hex[:10]}"
    mode_label = "demo" if demo else "live"
    cli = OkxCli(OkxCliConfig(demo=demo, profile=profile))

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

    try:
        balances = await cli.balance_all()
    except OkxCliError as e:
        return AuditReport(
            audit_id=audit_id, mode=mode_label, authenticated=True,
            risk_score=0.0, needs_human_review=True,
            error=f"account balance-all failed: {e}",
        )

    trading = (balances or {}).get("trading", {}) if isinstance(balances, dict) else {}
    flags.extend(_check_concentration(trading))

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

    lev_flags, base_currencies_seen = _check_leverage(position_list)
    flags.extend(lev_flags)

    async def _smartmoney_fn(ccy):
        return await cli.smartmoney_signal(ccy)

    sm_flags = await _check_smart_money_divergence_async(
        position_list, base_currencies_seen, _smartmoney_fn,
    )
    flags.extend(sm_flags)

    total_eq = _safe_float(trading.get("totalEq"))
    risk_score = _calculate_risk_score(flags)
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


async def _check_smart_money_divergence_async(
    position_list: list,
    base_currencies: set[str],
    smartmoney_fn,
) -> list[RiskFlag]:
    """Async wrapper for _check_smart_money_divergence."""
    flags = []
    for ccy in list(base_currencies)[:MAX_SMARTMONEY_CALLS_PER_AUDIT]:
        try:
            signal = await smartmoney_fn(ccy)
        except Exception:
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

        notional = row.get("notional") or {}
        long_short = row.get("longShortRatio") or {}
        net_notional = _safe_float(notional.get("netNotionalUsdt"))
        long_ratio = _safe_float(long_short.get("weightedLongRatio"))
        short_ratio = _safe_float(long_short.get("weightedShortRatio"))

        if abs(net_notional) < DIVERGENCE_MIN_NOTIONAL_USDT:
            continue

        pool_side = "long" if long_ratio > short_ratio else "short"
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
    return flags


# -- Entry point 2: Data-forwarding (production) --

def run_audit_from_data(
    balance_data: dict,
    positions_data: list | dict | None = None,
    inst_type: str | None = None,
    smartmoney_fn=None,
) -> AuditReport:
    """Analyze pre-gathered account data. No CLI calls, no credentials needed.

    smartmoney_fn: optional async callable(ccy) -> dict for smart money signals.
    If not provided, smart money checks are skipped (data must be gathered separately).
    """
    audit_id = f"audit_{uuid.uuid4().hex[:10]}"

    # Validate inputs (whitelist-based)
    _validate_balance_data(balance_data)
    position_list = _validate_positions_data(positions_data)

    # 1. Concentration risk
    trading = balance_data.get("trading", {})
    flags = _check_concentration(trading)

    # 2. Leverage risk
    lev_flags, base_currencies_seen = _check_leverage(position_list)
    flags.extend(lev_flags)

    # 3. Smart-money divergence (if smartmoney_fn provided)
    if smartmoney_fn and base_currencies_seen:
        sm_flags = _check_smart_money_divergence(
            position_list, base_currencies_seen, smartmoney_fn,
        )
        flags.extend(sm_flags)

    total_eq = _safe_float(trading.get("totalEq"))
    risk_score = _calculate_risk_score(flags)
    report_md = _render_report(audit_id, "data", total_eq, flags, risk_score)

    return AuditReport(
        audit_id=audit_id,
        mode="data",
        authenticated=True,
        risk_score=risk_score,
        flags=flags,
        report_md=report_md,
        needs_human_review=risk_score >= 0.7,
    )
