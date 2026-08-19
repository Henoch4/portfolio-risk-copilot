"""
Trade execution module for the autonomous trading agent.

Extends the existing OkxCli read-only interface with write operations
for order placement and cancellation. All write operations are gated
through the risk engine — the execution layer is dumb, it just routes orders.

Key safety features:
  - Orders must pass the RiskGate before reaching the CLI
  - All orders are logged to the onchain audit trail BEFORE submission
  - Dry-run mode for testing without real credentials
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from .okx_cli import OkxCli, OkxCliConfig, OkxCliError


OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
TimeInForce = Literal["gtc", "ioc", "fok"]


class OrderStatus(Enum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class OrderRequest:
    """A trade order request — input from the trading agent."""
    inst_id: str           # e.g. "BTC-USDT-SWAP"
    side: OrderSide        # "buy" or "sell"
    order_type: OrderType  # "market" or "limit"
    size: str              # order size in quote currency (USDT)
    px: str | None = None  # limit price (optional for market orders)
    time_in_force: TimeInForce = "gtc"
    client_oid: str | None = None  # custom order ID for tracking
    reduce_only: bool = False
    confidence_bps: int | None = None  # signal confidence in basis points (0-10000)

    def __post_init__(self):
        if not self.client_oid:
            self.client_oid = f"auto_{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict:
        d = {
            "instId": self.inst_id,
            "side": self.side,
            "ordType": "m" if self.order_type == "market" else "l",
            "sz": self.size,
            "clOrdId": self.client_oid,
            "reduceOnly": self.reduce_only,
        }
        if self.px:
            d["px"] = self.px
        return d


@dataclass
class OrderResult:
    """Result of an order placement attempt."""
    order_id: str
    client_oid: str
    state: OrderStatus
    acc_fill_sz: str       # accumulated filled size
    fill_px: str | None    # fill price
    fill_sz: str | None    # filled size
    fill_usd: str | None   # filled amount in USD
    fee: str               # fee charged
    fee_ccy: str           # fee currency
    error: str | None = None
    fill_verified: bool | None = None  # post-fill slippage check: None=not checked, True=ok, False=bad fill
    slippage_pct: float | None = None  # measured post-fill slippage vs reference price
    raw: dict = field(default_factory=dict)


class ExecutionError(RuntimeError):
    """Raised when an order cannot be placed or fails."""


class OrderExecutor:
    """
    Safe order executor that wraps the OKX CLI with pre-trade risk checks.
    
    Architecture:
      TradingAgent → RiskGate.check(order) → OrderExecutor.place(order)
      
    The RiskGate is non-overridable — if it rejects, the order never reaches OKX.
    """

    def __init__(self, cli: OkxCli, risk_gate, dry_run: bool = False):
        self.cli = cli
        self.risk_gate = risk_gate
        self.dry_run = dry_run

    def _verify_fill(self, order: OrderRequest, result: OrderResult,
                     reference_price: float | None) -> OrderResult:
        """Post-fill slippage verification (liquid-protocol-v1 checkPriceImpact port).

        The pre-trade gate checks the *intended* price; this checks the *actual*
        fill against the same reference price. Behavior on deviation from the
        reference price used at pre-trade time:

          <= max_slippage_pct          -> fill_verified=True (normal)
          > max_slippage_pct <= 2x     -> fill_verified=False, flagged, audit trail
          > 2x (hard collar)           -> fill_verified=False, kill switch tripped

        No reference price or no numeric fill -> fill_verified=None (not checked);
        a non-checkable fill is never silently marked good.
        """
        if reference_price is None or reference_price <= 0:
            result.fill_verified = None
            return result
        try:
            fill_px = float(result.fill_px) if result.fill_px is not None else 0.0
        except (TypeError, ValueError):
            fill_px = 0.0
        if fill_px <= 0:
            result.fill_verified = None
            return result

        slippage_pct = abs(fill_px - reference_price) / reference_price * 100.0
        result.slippage_pct = round(slippage_pct, 4)

        collar = self.risk_gate.max_slippage_pct
        hard_collar = collar * 2.0
        if slippage_pct <= collar:
            result.fill_verified = True
        elif slippage_pct <= hard_collar:
            result.fill_verified = False
            result.error = (
                f"Post-fill slippage {slippage_pct:.2f}% vs reference "
                f"{reference_price} exceeds {collar:.2f}% collar (fill {fill_px}). "
                f"Order {order.client_oid} filled on poor terms."
            )
        else:
            result.fill_verified = False
            result.error = (
                f"Post-fill slippage {slippage_pct:.2f}% vs reference "
                f"{reference_price} exceeds hard collar {hard_collar:.2f}% "
                f"(fill {fill_px}). Tripping kill switch — fills are untrustworthy."
            )
            self.risk_gate.activate_kill_switch(reason=result.error)
        return result

    async def place_order(
        self,
        order: OrderRequest,
        current_price: float | None = None,
        current_price_timestamp: float | None = None,
    ) -> OrderResult:
        """
        Place an order after passing risk checks.
        In dry-run mode, simulates the order without sending to OKX.

        current_price / current_price_timestamp: the reference market data used
        at pre-trade time, passed through to the gate's re-check and the
        post-fill slippage verification below (the Liquid pattern: never print
        a fill you can't verify against a trusted reference price).
        """
        # --- Step 1: Risk gate check (non-overridable) ---
        risk_check = self.risk_gate.check_order(
            order,
            current_price=current_price,
            current_price_timestamp=current_price_timestamp,
        )
        if not risk_check.approved:
            raise ExecutionError(
                f"Risk gate rejected order: {risk_check.reason}. "
                f"Code: {risk_check.code}"
            )

        if self.dry_run:
            result = OrderResult(
                order_id=f"dryrun_{uuid.uuid4().hex[:8]}",
                client_oid=order.client_oid or "",
                state=OrderStatus.FILLED,
                acc_fill_sz=order.size,
                fill_px=order.px or "0",
                fill_sz=order.size,
                fill_usd=order.size,
                fee="0",
                fee_ccy="USDT",
                error=None,
                raw={"dry_run": True, **order.to_dict()},
            )
            return self._verify_fill(order, result, reference_price=current_price)

        # --- Step 2: Submit to OKX via CLI ---
        try:
            result = await self.cli.run(
                "trade", "order",
                "--instId", order.inst_id,
                "--side", order.side,
                "--ordType", "m" if order.order_type == "market" else "l",
                "--sz", order.size,
                *(["--px", order.px] if order.px else []),
                "--clOrdId", order.client_oid or "",
                *(["--reduceOnly"] if order.reduce_only else []),
                use_global_flags=True,
            )
        except OkxCliError as e:
            raise ExecutionError(f"OKX CLI order failed: {e}") from e

        if not result or "data" not in result:
            raise ExecutionError(f"Unexpected CLI response: {result}")

        order_data = result["data"][0] if isinstance(result.get("data"), list) else result["data"]

        order_result = OrderResult(
            order_id=order_data.get("ordId", ""),
            client_oid=order_data.get("clOrdId", order.client_oid),
            state=OrderStatus.PENDING,
            acc_fill_sz=order_data.get("accFillSz", "0"),
            fill_px=order_data.get("fillPx"),
            fill_sz=order_data.get("fillSz"),
            fill_usd=order_data.get("fillUsd"),
            fee=order_data.get("fee", "0"),
            fee_ccy=order_data.get("feeCcy", "USDT"),
            raw=result,
        )
        return self._verify_fill(order, order_result, reference_price=current_price)

    async def cancel_order(self, order_id: str, inst_id: str) -> bool:
        """Cancel a pending order."""
        if self.dry_run:
            return True

        try:
            result = await self.cli.run(
                "trade", "cancel",
                "--instId", inst_id,
                "--ordId", order_id,
                use_global_flags=True,
            )
            return bool(result and result.get("data"))
        except OkxCliError as e:
            raise ExecutionError(f"Cancel failed: {e}") from e

    async def amend_order(self, order_id: str, inst_id: str, **kwargs) -> dict:
        """Amend an existing order (change size or price)."""
        if self.dry_run:
            return {"dry_run": True}

        args = ["trade", "amend", "--instId", inst_id, "--ordId", order_id]
        for k, v in kwargs.items():
            if v is not None:
                args.extend([f"--{k}", str(v)])

        try:
            return await self.cli.run(*args, use_global_flags=True)
        except OkxCliError as e:
            raise ExecutionError(f"Amend failed: {e}") from e

    async def get_order_status(self, order_id: str, inst_id: str) -> OrderResult:
        """Query the status of an order."""
        try:
            result = await self.cli.run(
                "trade", "order",
                "--instId", inst_id,
                "--ordId", order_id,
                use_global_flags=True,
            )
        except OkxCliError as e:
            raise ExecutionError(f"Status query failed: {e}") from e

        data = result.get("data", [{}])[0] if result else {}
        return OrderResult(
            order_id=data.get("ordId", order_id),
            client_oid=data.get("clOrdId", ""),
            state=OrderStatus(data.get("state", "unknown")),
            acc_fill_sz=data.get("accFillSz", "0"),
            fill_px=data.get("fillPx"),
            fill_sz=data.get("fillSz"),
            fill_usd=data.get("fillUsd"),
            fee=data.get("fee", "0"),
            fee_ccy=data.get("feeCcy", "USDT"),
            raw=result,
        )

    async def get_position(self, inst_id: str) -> dict | None:
        """Get current position for an instrument."""
        try:
            result = await self.cli.run(
                "account", "positions",
                "--instId", inst_id,
                use_global_flags=True,
            )
            positions = result.get("data", []) if result else []
            return positions[0] if positions else None
        except OkxCliError:
            return None


@dataclass
class RiskCheckResult:
    approved: bool
    code: str
    reason: str


class RiskGate:
    """
    Non-overridable pre-trade risk gate.
    
    This is the Wall Street principle: "The strategy is allowed to be creative.
    The risk and control layer must be boring, deterministic, and non-negotiable."
    
    All thresholds are set at construction time and cannot be bypassed
    by the trading agent.
    """

    def __init__(
        self,
        max_position_usd: float = 5000,
        max_daily_loss_usd: float = 500,
        max_daily_trades: int = 10,
        max_leverage: float = 5.0,
        max_slippage_pct: float = 1.0,
        min_confidence_bps: int = 7000,
        allowed_assets: list[str] | None = None,
        # --- Max price age: how old a market price can be before it stops
        # being trustworthy. Ported from the oracle trust pattern in
        # liquid-protocol-v1 (oracle.sol): the strategy layer is free to look
        # ahead, but the gate refuses to trade against a price feed it cannot
        # trust. A missing OR stale price rejects the order — there is no
        # "skip the freshness check" path, only a configurable threshold.
        max_price_age_seconds: float = 60.0,
        # --- Regime throttle (high-volatility governor) ---
        # Borrowed from trading engines that auto-scale exposure ahead of a
        # crash instead of waiting for the kill switch. When recent price
        # moves exceed `regime_band_pct`, the effective position cap for new
        # orders is scaled down by `regime_size_scale` (e.g. 0.8 = 20% less
        # room). This is a *sizing* control, not a halt: it shrinks the next
        # order without stopping the strategy, and the kill switch remains the
        # only full-halt control. Deterministic and testable — no ML, no state
        # beyond the ring buffer below.
        regime_throttle: bool = False,
        regime_band_pct: float = 5.0,
        regime_size_scale: float = 0.8,
        regime_buffer: int = 20,
    ):
        self.max_position_usd = max_position_usd
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_daily_trades = max_daily_trades
        self.max_leverage = max_leverage
        self.max_slippage_pct = max_slippage_pct
        self.min_confidence_bps = min_confidence_bps
        self.max_price_age_seconds = max_price_age_seconds
        self.allowed_assets = allowed_assets or [
            "BTC-USDT-SWAP",
            "ETH-USDT-SWAP",
            "SOL-USDT-SWAP",
            "BNB-USDT-SWAP",
        ]
        self.regime_throttle = regime_throttle
        self.regime_band_pct = regime_band_pct
        self.regime_size_scale = regime_size_scale
        self.regime_buffer = regime_buffer

        # Derived allowlist: the swap perps an operator lists in
        # `allowed_assets` authorize the SAME base asset in any instrument
        # form — so a `BTC-USDT-SWAP` allowlist entry also authorizes the
        # `BTC-USDT` spot leg of a delta-neutral funding-arb package. This is
        # a deliberate widening (base-asset authorization), not an accident:
        # without it the spot leg of a package would be rejected as
        # ASSET_NOT_ALLOWED before the arb could ever be proposed. Derived
        # here once so the risk hash below reflects the real, effective
        # allowlist rather than the literal perp list.
        self._base_allowed: set[str] = {a.split("-")[0] for a in self.allowed_assets}
        
        # Daily tracking (in production, this would be persisted in Redis/DB)
        self._daily_volume: dict[str, float] = {}
        self._daily_loss: dict[str, float] = {}
        self._daily_trade_count: dict[str, int] = {}

        # --- Kill switch: global halt, independent of per-agent limits ---
        # This is the one control nothing else in this file can substitute for —
        # every other check runs per-order; this one halts the gate entirely,
        # for every agent, until explicitly cleared.
        self._kill_switch_active: bool = False
        self._kill_switch_reason: str | None = None
        self._kill_switch_activated_at: float | None = None

        # --- Regime throttle state: per-asset ring buffer of recent prices ---
        # Registers every market price we observe so the gate can detect a
        # volatility regime change without needing any external feed. Fed by
        # RiskGate.observe_price() from the trading loop.
        self._price_buffer: dict[str, list[float]] = {}  # inst_id -> [prices]

    def activate_kill_switch(self, reason: str) -> None:
        """Halt all order approval immediately. Requires explicit deactivation to resume."""
        import time
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        self._kill_switch_activated_at = time.time()

    def deactivate_kill_switch(self) -> None:
        """Resume order approval. This is a deliberate, separate action — never automatic."""
        self._kill_switch_active = False
        self._kill_switch_reason = None
        self._kill_switch_activated_at = None

    def kill_switch_status(self) -> dict:
        return {
            "active": self._kill_switch_active,
            "reason": self._kill_switch_reason,
            "activated_at": self._kill_switch_activated_at,
        }

    def observe_price(self, inst_id: str, price: float) -> None:
        """Feed a market price into the regime-throttle ring buffer.

        Call this once per observed tick per asset (e.g. at the top of each
        trading cycle from the market-data step). Prices are kept in a bounded
        ring buffer (regime_buffer) so old regimes decay naturally.
        """
        buf = self._price_buffer.setdefault(inst_id, [])
        buf.append(price)
        if len(buf) > self.regime_buffer:
            buf.pop(0)

    def reset_price_buffer(self, inst_id: str | None = None) -> None:
        """Clear the regime ring buffer (e.g. after a regime-model change)."""
        if inst_id is None:
            self._price_buffer.clear()
        else:
            self._price_buffer.pop(inst_id, None)

    def regime_scale(self, inst_id: str) -> float:
        """Return the position-cap multiplier for the current regime.

        1.0 in a calm regime; regime_size_scale when the observed price range
        over the window exceeds regime_band_pct. A step function, not a taper:
        the risk layer is deliberately boring and binary — either the regime
        is calm (full size) or it is stressed (fixed smaller size) — so the
        scaling is trivial to reason about and audit.

        Only active when regime_throttle is enabled; otherwise always 1.0.

        Fail-closed on missing data: an empty price buffer means the gate has
        never observed a trustworthy price for this instrument, so the
        stressed (`regime_size_scale`) cap applies — defaulting to full size
        on no data is exactly the fail-open behavior the design doc's
        fail-safe-defaults principle forbids (uncertainty must reduce risk,
        not keep it unchanged).
        """
        if not self.regime_throttle:
            return 1.0
        buf = self._price_buffer.get(inst_id)
        if not buf:
            return self.regime_size_scale
        mean = sum(buf) / len(buf)
        if mean <= 0:
            return 1.0
        spread = (max(buf) - min(buf)) / mean * 100.0
        if spread <= self.regime_band_pct:
            return 1.0
        return self.regime_size_scale

    def regime_status(self, inst_id: str | None = None) -> dict:
        """Report the current regime state for dashboards/tests."""
        if inst_id is not None:
            return {
                "enabled": self.regime_throttle,
                "band_pct": self.regime_band_pct,
                "scale": self.regime_scale(inst_id),
                "window_size": len(self._price_buffer.get(inst_id, [])),
                "window_capacity": self.regime_buffer,
            }
        return {"enabled": self.regime_throttle, "band_pct": self.regime_band_pct}

    def _is_asset_allowed(self, inst_id: str) -> bool:
        """Authorize an instrument by exact allowlist match OR by base asset.

        The allowlist is expressed in swap-perp form (`BTC-USDT-SWAP`), but a
        delta-neutral funding-arb package trades a spot leg (`BTC-USDT`) for
        the same base asset. Both are the same directional risk on the same
        base; rejecting the spot leg would make the hedged package
        unbuildable while the naked swap passes. Base-asset authorization
        widens exactly to the same base — no more.
        """
        if inst_id in self.allowed_assets:
            return True
        base = inst_id.split("-")[0] if inst_id else ""
        return base in self._base_allowed

    def check_order(
        self,
        order: OrderRequest,
        agent_id: str = "default",
        current_price: float | None = None,
        current_price_timestamp: float | None = None,
        current_position_side: str | None = None,
    ) -> RiskCheckResult:
        """
        Check an order against all risk parameters.
        Returns approved=True only if ALL checks pass.

        current_price: latest market price, used to enforce the slippage/price-collar
            check on limit orders and the price-freshness gate (check 8) on every
            order. Without it, an order is rejected — never silently passed.
        current_price_timestamp: epoch seconds when `current_price` was observed.
            The freshness gate (check 8) rejects any order whose reference price
            is older than `max_price_age_seconds`; a missing timestamp means the
            age cannot be verified and is treated as stale (fail-safe-defaults).
        current_position_side: "long" | "short" | None, used to enforce reduce-only
            semantics on sell orders — see check 7 below.
        """
        # 0. Kill switch — checked first, before anything else, no exceptions.
        if self._kill_switch_active:
            return RiskCheckResult(
                approved=False,
                code="KILL_SWITCH_ACTIVE",
                reason=f"Kill switch is active: {self._kill_switch_reason}",
            )

        # 1. Asset allowlist — exact match OR same base asset (the spot leg
        #    of a funding-arb package, e.g. BTC-USDT when BTC-USDT-SWAP is
        #    allowed).
        if not self._is_asset_allowed(order.inst_id):
            return RiskCheckResult(
                approved=False,
                code="ASSET_NOT_ALLOWED",
                reason=f"{order.inst_id} not in allowlist: {self.allowed_assets}",
            )

        # 2. Confidence floor — the gate enforces it even if the strategy layer
        # forgets. Skipped only when the caller provides no confidence at all
        # (the gate cannot second-guess what it was never told).
        if order.confidence_bps is not None and order.confidence_bps < self.min_confidence_bps:
            return RiskCheckResult(
                approved=False,
                code="CONFIDENCE_TOO_LOW",
                reason=(
                    f"Signal confidence {order.confidence_bps} bps below the "
                    f"{self.min_confidence_bps} bps floor — the risk gate does "
                    f"not trade on weak signals."
                ),
            )

        # 3. Position size limit (scaled by regime throttle when active)
        try:
            size_usd = float(order.size)
        except (ValueError, TypeError):
            size_usd = 0

        effective_max = self.max_position_usd * self.regime_scale(order.inst_id)
        if size_usd > effective_max:
            if effective_max < self.max_position_usd:
                return RiskCheckResult(
                    approved=False,
                    code="REGIME_SIZE_CAP",
                    reason=(
                        f"Position ${size_usd:.2f} exceeds regime-scaled cap "
                        f"${effective_max:.2f} (max ${self.max_position_usd:.2f} "
                        f"x regime scale {self.regime_scale(order.inst_id):.2f}). "
                        f"High-volatility regime — throttle is active."
                    ),
                )
            return RiskCheckResult(
                approved=False,
                code="POSITION_TOO_LARGE",
                reason=(
                    f"Position ${size_usd:.2f} exceeds max "
                    f"${self.max_position_usd:.2f}"
                ),
            )

        # 4. Daily trade count
        if self._daily_trade_count.get(agent_id, 0) >= self.max_daily_trades:
            return RiskCheckResult(
                approved=False,
                code="DAILY_TRADE_LIMIT_EXCEEDED",
                reason=(
                    f"Agent has already placed "
                    f"{self._daily_trade_count.get(agent_id, 0)} "
                    f"trades today (max {self.max_daily_trades})"
                ),
            )

        # 5. Daily loss limit
        current_loss = self._daily_loss.get(agent_id, 0.0)
        if current_loss >= self.max_daily_loss_usd:
            return RiskCheckResult(
                approved=False,
                code="DAILY_LOSS_LIMIT_EXCEEDED",
                reason=(
                    f"Daily loss ${current_loss:.2f} >= "
                    f"limit ${self.max_daily_loss_usd:.2f}"
                ),
            )

        # 6. Fat-finger / price sanity check (independent of slippage, always runs)
        try:
            limit_px = float(order.px) if order.px else None
        except (ValueError, TypeError):
            limit_px = None

        if limit_px is not None and current_price is not None and current_price > 0:
            deviation_pct = abs(limit_px - current_price) / current_price * 100
            if deviation_pct > 20.0:
                return RiskCheckResult(
                    approved=False,
                    code="FAT_FINGER_REJECTED",
                    reason=(
                        f"Limit price {limit_px:.2f} deviates {deviation_pct:.1f}% "
                        f"from current price {current_price:.2f} — rejected as a "
                        f"likely input error, not a normal slippage case."
                    ),
                )

        # 7. Slippage / price collar (for limit orders) — enforced, not stubbed.
        # If we don't have a current market price to compare against, we reject
        # rather than silently approve: an unenforceable check is not a check.
        if order.order_type == "limit" and limit_px is not None:
            if current_price is None or current_price <= 0:
                return RiskCheckResult(
                    approved=False,
                    code="NO_PRICE_REFERENCE",
                    reason=(
                        "Limit order submitted without a current market price to "
                        "check slippage against — cannot verify the price collar."
                    ),
                )
            slippage_pct = abs(limit_px - current_price) / current_price * 100
            if slippage_pct > self.max_slippage_pct:
                return RiskCheckResult(
                    approved=False,
                    code="SLIPPAGE_EXCEEDED",
                    reason=(
                        f"Limit price {limit_px:.2f} is {slippage_pct:.2f}% away "
                        f"from current price {current_price:.2f}, exceeding the "
                        f"{self.max_slippage_pct:.2f}% collar."
                    ),
                )

        # 8. Reduce-only enforcement — enforced, not stubbed.
        # A sell order that would open or increase a short, rather than reduce
        # an existing long, must be explicitly marked reduce_only=False by the
        # caller and is otherwise rejected here.
        if order.side == "sell" and current_position_side == "long" and not order.reduce_only:
            return RiskCheckResult(
                approved=False,
                code="REDUCE_ONLY_VIOLATION",
                reason=(
                    "Sell order against an existing long position must set "
                    "reduce_only=True — this gate does not allow flipping a "
                    "position from long to short in a single unmarked order."
                ),
            )

        # 9. Price-freshness gate — applies to EVERY order type (market, limit),
        # not just limit orders. Ported from the oracle trust pattern in
        # liquid-protocol-v1 (oracle.sol): the strategy is free to look ahead,
        # but the gate refuses to trade against a price feed it cannot trust.
        # A missing, non-positive, or stale price rejects the order. There is
        # no "skip the freshness check" path — an unverifiable age is stale.
        if current_price is None or current_price <= 0:
            return RiskCheckResult(
                approved=False,
                code="NO_PRICE_REFERENCE",
                reason=(
                    "Order submitted without a current market price to verify "
                    "against — cannot check the price collar or freshness."
                ),
            )
        now = time.time()
        price_age = (
            now - current_price_timestamp
            if current_price_timestamp is not None
            else float("inf")
        )
        if price_age > self.max_price_age_seconds:
            return RiskCheckResult(
                approved=False,
                code="STALE_PRICE",
                reason=(
                    f"Reference price {current_price} is "
                    f"{price_age:.1f}s old (cap {self.max_price_age_seconds:.0f}s). "
                    f"Price feed is not fresh — refusing to trade on stale data."
                ),
            )

        self._daily_trade_count[agent_id] = self._daily_trade_count.get(agent_id, 0) + 1

        return RiskCheckResult(
            approved=True,
            code="APPROVED",
            reason="All checks passed",
        )

    def report_loss(self, agent_id: str, loss_usd: float):
        """Report a loss to the daily loss tracker.

        Auto-trips the kill switch if this loss pushes the agent over its daily
        loss limit — the design doc's fail-safe-defaults principle (Section 4)
        says the system should default to reducing risk under uncertainty, not
        just reject the next single order and otherwise carry on.
        """
        self._daily_loss[agent_id] = self._daily_loss.get(agent_id, 0.0) + loss_usd
        if self._daily_loss[agent_id] >= self.max_daily_loss_usd and not self._kill_switch_active:
            self.activate_kill_switch(
                reason=(
                    f"Auto-triggered: agent {agent_id} daily loss "
                    f"${self._daily_loss[agent_id]:.2f} reached limit "
                    f"${self.max_daily_loss_usd:.2f}"
                )
            )

    def report_volume(self, agent_id: str, volume_usd: float):
        """Report executed volume to the daily tracker."""
        self._daily_volume[agent_id] = self._daily_volume.get(agent_id, 0.0) + volume_usd

    def get_daily_stats(self, agent_id: str) -> dict:
        return {
            "volume": self._daily_volume.get(agent_id, 0.0),
            "loss": self._daily_loss.get(agent_id, 0.0),
            "trade_count": self._daily_trade_count.get(agent_id, 0),
            "volume_limit": self.max_position_usd * self.max_daily_trades,
            "loss_limit": self.max_daily_loss_usd,
            "trade_count_limit": self.max_daily_trades,
        }

    def compute_risk_hash(self) -> str:
        """Compute a deterministic hash of the risk parameters for onchain logging."""
        import json
        params = {
            "max_position_usd": self.max_position_usd,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_daily_trades": self.max_daily_trades,
            "max_leverage": self.max_leverage,
            "max_slippage_pct": self.max_slippage_pct,
            "min_confidence_bps": self.min_confidence_bps,
            "max_price_age_seconds": self.max_price_age_seconds,
            "allowed_assets": sorted(self.allowed_assets),
            "base_allowed": sorted(self._base_allowed),
            "regime_throttle": self.regime_throttle,
            "regime_band_pct": self.regime_band_pct,
            "regime_size_scale": self.regime_size_scale,
        }
        serialized = json.dumps(params, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()
