"""
Order executor: shells out to the OKX CLI for place/cancel/amend after the
RiskGate has approved an order. The execution layer is dumb — it routes orders;
the risk gate decides. Split out of execution.py (Block G).
"""
from __future__ import annotations

import logging
import uuid

from .models import (
    OrderRequest,
    OrderResult,
    OrderStatus,
    ExecutionError,
)
from ..okx_cli import OkxCli, OkxCliError

logger = logging.getLogger(__name__)

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
            unwind=order.unwind,
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
