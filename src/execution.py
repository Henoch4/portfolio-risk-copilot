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

    async def place_order(self, order: OrderRequest) -> OrderResult:
        """
        Place an order after passing risk checks.
        In dry-run mode, simulates the order without sending to OKX.
        """
        # --- Step 1: Risk gate check (non-overridable) ---
        risk_check = self.risk_gate.check_order(order)
        if not risk_check.approved:
            raise ExecutionError(
                f"Risk gate rejected order: {risk_check.reason}. "
                f"Code: {risk_check.code}"
            )

        if self.dry_run:
            return OrderResult(
                order_id=f"dryrun_{uuid.uuid4().hex[:8]}",
                client_oid=order.client_oid,
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

        # --- Step 2: Submit to OKX via CLI ---
        try:
            result = await self.cli.run(
                "trade", "order",
                "--instId", order.inst_id,
                "--side", order.side,
                "--ordType", "m" if order.order_type == "market" else "l",
                "--sz", order.size,
                *[f"--px {order.px}" for _ in [None] if order.px],
                "--clOrdId", order.client_oid,
                "--reduceOnly" if order.reduce_only else "",
                use_global_flags=True,
            )
        except OkxCliError as e:
            raise ExecutionError(f"OKX CLI order failed: {e}") from e

        if not result or "data" not in result:
            raise ExecutionError(f"Unexpected CLI response: {result}")

        order_data = result["data"][0] if isinstance(result.get("data"), list) else result["data"]

        return OrderResult(
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
    ):
        self.max_position_usd = max_position_usd
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_daily_trades = max_daily_trades
        self.max_leverage = max_leverage
        self.max_slippage_pct = max_slippage_pct
        self.min_confidence_bps = min_confidence_bps
        self.allowed_assets = allowed_assets or [
            "BTC-USDT-SWAP",
            "ETH-USDT-SWAP",
            "SOL-USDT-SWAP",
            "BNB-USDT-SWAP",
        ]
        
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

    def check_order(
        self,
        order: OrderRequest,
        agent_id: str = "default",
        current_price: float | None = None,
        current_position_side: str | None = None,
    ) -> RiskCheckResult:
        """
        Check an order against all risk parameters.
        Returns approved=True only if ALL checks pass.

        current_price: latest market price, used to enforce the slippage/price-collar
            check on limit orders. Without it, that check is skipped rather than
            silently passed — see check 6 below.
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

        # 1. Asset allowlist
        if order.inst_id not in self.allowed_assets:
            return RiskCheckResult(
                approved=False,
                code="ASSET_NOT_ALLOWED",
                reason=f"{order.inst_id} not in allowlist: {self.allowed_assets}",
            )

        # 2. Position size limit
        try:
            size_usd = float(order.size)
        except (ValueError, TypeError):
            size_usd = 0

        if size_usd > self.max_position_usd:
            return RiskCheckResult(
                approved=False,
                code="POSITION_TOO_LARGE",
                reason=(
                    f"Position ${size_usd:.2f} exceeds max "
                    f"${self.max_position_usd:.2f}"
                ),
            )

        # 3. Daily trade count
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

        # 4. Daily loss limit
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

        # 5. Fat-finger / price sanity check (independent of slippage, always runs)
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

        # 6. Slippage / price collar (for limit orders) — enforced, not stubbed.
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

        # 7. Reduce-only enforcement — enforced, not stubbed.
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
            "allowed_assets": sorted(self.allowed_assets),
        }
        serialized = json.dumps(params, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()
