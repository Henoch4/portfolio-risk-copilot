"""
Order executor: shells out to the OKX CLI for place/cancel/amend after the
RiskGate has approved an order. The execution layer is dumb — it routes orders;
the risk gate decides. Split out of execution.py (Block G).
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid

from .models import (
    OrderRequest,
    OrderResult,
    OrderStatus,
    ExecutionError,
)
from ..okx_cli import OkxCli, OkxCliError

logger = logging.getLogger(__name__)


def _float_or(value: object, default: float) -> float:
    """Parse a CLI string field as float; fall back to `default` on ''/None."""
    try:
        parsed = float(value)  # type: ignore[arg-type]
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _trim_float(value: float) -> str:
    """Format a size without exponent or trailing zeros ('1.49', '0.01')."""
    return f"{value:.10f}".rstrip("0").rstrip(".")


class OrderExecutor:
    """
    Safe order executor that wraps the OKX CLI with pre-trade risk checks.
    
    Architecture:
      TradingAgent → RiskGate.check(order) → OrderExecutor.place(order)
      
    The RiskGate is non-overridable — if it rejects, the order never reaches OKX.
    """

    def __init__(self, cli: OkxCli, risk_gate, dry_run: bool = False,
                 agent_id: str = "default",
                 fill_poll_attempts: int = 3, fill_poll_delay: float = 0.5):
        self.cli = cli
        self.risk_gate = risk_gate
        self.dry_run = dry_run
        # Daily-limit bucket this executor's orders count against. The agent
        # wires its own agent_id here so the executor's pre-submission check
        # (the ONLY count_trade check in the system) lands in the same daily
        # bucket as every other check on this operator's trading — otherwise
        # every executed order also silently inflated a phantom "default"
        # bucket that nobody reports or enforces.
        self.agent_id = agent_id
        # Post-placement polling for live orders whose synchronous response
        # carries no fillPx (typical for market swaps): how many times to
        # re-query and how long to wait between attempts before giving up and
        # leaving fill_verified=None (not silently marked good).
        self.fill_poll_attempts = fill_poll_attempts
        self.fill_poll_delay = fill_poll_delay
        # Instrument metadata cache: inst_id -> {"instType", "ctVal", "ctValCcy"}
        # fetched once from `public instruments`. Needed for USD->contracts
        # conversion; failures are fatal per-order (fail closed).
        self._instruments: dict[str, dict] = {}

    @staticmethod
    def _infer_inst_type(inst_id: str) -> str:
        """Infer OKX instType from the instrument ID for the metadata lookup.

        BTC-USDT -> SPOT; BTC-USDT-SWAP -> SWAP; BTC-USDT-250926 -> FUTURES.
        """
        if inst_id.endswith("-SWAP"):
            return "SWAP"
        if len(inst_id.split("-")) >= 3 and inst_id.split("-")[-1].isdigit():
            return "FUTURES"
        return "SPOT"

    async def _get_instrument(self, inst_id: str) -> dict:
        """Fetch (and cache) instrument metadata for size-unit conversion.

        Verified live against okx CLI 1.4.2: the command is
        `market instruments --instType <t> --instId <id>` and the CLI prints a
        BARE JSON array with --json (not the raw REST {"data":[...]} shape —
        both are accepted here).

        Fail-closed: if the CLI can't return a parseable instrument record we
        refuse to size the order rather than guess units — a SWAP `--sz` in
        the wrong unit is a 100x notional error, not a cosmetic one.
        """
        if inst_id in self._instruments:
            return self._instruments[inst_id]
        try:
            result = await self.cli.run(
                "market", "instruments",
                "--instType", self._infer_inst_type(inst_id),
                "--instId", inst_id,
                use_global_flags=True,
            )
        except OkxCliError as e:
            raise ExecutionError(
                f"Cannot fetch instrument metadata for {inst_id} "
                f"(needed for size conversion): {e}"
            ) from e
        if isinstance(result, list):
            record = result[0] if result else None
        elif isinstance(result, dict):
            data = result.get("data")
            record = data[0] if isinstance(data, list) and data else None
        else:
            record = None
        if not isinstance(record, dict) or "instType" not in record:
            raise ExecutionError(
                f"No instrument metadata returned for {inst_id} — refusing to "
                f"submit with unconverted size units"
            )
        self._instruments[inst_id] = record
        return record

    def _cli_size(self, order: OrderRequest, reference_price: float | None,
                  instrument: dict) -> str:
        """Convert order.size into exchange-native CLI units.

        SWAP `sz` is in contracts (ctVal base-ccy per contract); SPOT `sz` is
        in base ccy. Both conversions need a reference price — without one we
        fail closed instead of passing USD through (the historical bug: a
        $1,000 order submitted as `--sz 1000` on BTC-USDT-SWAP meant 1000
        contracts, ~$100k+ notional).
        """
        if order.size_unit != "usd":
            return order.size

        if reference_price is None or reference_price <= 0:
            raise ExecutionError(
                f"Order for {order.inst_id} is sized in USD but no reference "
                f"price is available for unit conversion — refusing to submit"
            )
        usd = float(order.size)
        inst_type = (instrument.get("instType") or "").upper()
        try:
            ct_val = float(instrument.get("ctVal", 0) or 0)
        except (TypeError, ValueError):
            ct_val = 0.0

        if inst_type in ("SWAP", "FUTURES"):
            # Linear (USDT-margined) SWAP: ctVal is denominated in the base
            # ccy (ctValCcy = e.g. "BTC") => one contract = ctVal * price
            # quote-units. Coin-margined / inverse: ctValCcy is the settle
            # quote (USD/USDT) => one contract is ctVal quote-units outright.
            ct_val_ccy = (instrument.get("ctValCcy") or "").upper()
            contract_usd = (
                ct_val
                if ct_val_ccy in ("USD", "USDT")
                else ct_val * reference_price
            )
            if contract_usd <= 0:
                raise ExecutionError(
                    f"Unusable ctVal metadata for {order.inst_id}: "
                    f"{instrument!r}"
                )
            # Real instruments (verified live): BTC-USDT-SWAP has lotSz 0.01
            # and minSz 0.01 — sizes are FRACTIONAL contracts, so flooring to
            # whole contracts is wrong (too conservative by up to 99%).
            # Floor to the lot step instead and enforce the real minimum.
            lot_sz = _float_or(instrument.get("lotSz"), 1.0)
            min_sz = _float_or(instrument.get("minSz"), 0.0)
            raw_contracts = usd / contract_usd
            lots = math.floor((raw_contracts + 1e-12) / lot_sz)
            contracts = lots * lot_sz
            if contracts <= 0 or contracts < min_sz:
                floor_usd = max(min_sz, lot_sz) * contract_usd
                raise ExecutionError(
                    f"Order of ${usd:.2f} on {order.inst_id} is below the "
                    f"instrument minimum (~${floor_usd:.2f}: minSz {min_sz}, "
                    f"lotSz {lot_sz}, 1 contract = ${contract_usd:.2f}). "
                    f"Not submitting."
                )
            return _trim_float(contracts)

        if inst_type == "SPOT":
            lot_sz = _float_or(instrument.get("lotSz"), 1e-8)
            min_sz = _float_or(instrument.get("minSz"), 0.0)
            raw_base = usd / reference_price
            lots = math.floor((raw_base + 1e-12) / lot_sz)
            base_amount = lots * lot_sz
            if base_amount <= 0 or base_amount < min_sz:
                raise ExecutionError(
                    f"Order of ${usd:.2f} on {order.inst_id} is below the "
                    f"spot minimum (minSz {min_sz}). Not submitting."
                )
            return _trim_float(base_amount)

        raise ExecutionError(
            f"Unsupported instType {inst_type!r} for {order.inst_id} — "
            f"cannot convert USD size"
        )

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
            agent_id=self.agent_id,
            current_price=current_price,
            current_price_timestamp=current_price_timestamp,
            unwind=order.unwind,
            # In dry-run no order ever reaches the exchange, so it must not
            # burn the persisted max_daily_trades quota that real trading
            # depends on (DRY_RUN defaults to true; the old code counted every
            # dry-run placement into a phantom bucket that trailing runs would
            # then collide with). The limit check still runs — the counter
            # just is not consumed.
            count_trade=not self.dry_run,
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
        # Convert USD notional to exchange-native units (SWAP contracts /
        # SPOT base). The risk gate above checked the USD notional; the CLI
        # receives converted units. Dry-run skips conversion — the simulated
        # result reports in USD like the request.
        instrument = await self._get_instrument(order.inst_id)
        cli_size = self._cli_size(order, current_price, instrument)
        try:
            result = await self.cli.run(
                "trade", "order",
                "--instId", order.inst_id,
                "--side", order.side,
                "--ordType", "m" if order.order_type == "market" else "l",
                "--sz", cli_size,
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
            state=OrderStatus.from_exchange(order_data.get("state", "live")),
            acc_fill_sz=order_data.get("accFillSz", "0"),
            fill_px=order_data.get("fillPx"),
            fill_sz=order_data.get("fillSz"),
            fill_usd=order_data.get("fillUsd"),
            fee=order_data.get("fee", "0"),
            fee_ccy=order_data.get("feeCcy", "USDT"),
            raw=result,
        )
        # Market SWAP orders usually return no fillPx synchronously; without
        # it the post-fill slippage check below is inert. Poll a bounded
        # number of times for the fill before verifying; still no fillPx
        # leaves fill_verified=None (never silently marked good).
        order_result = await self._await_fill(order_result, order.inst_id)
        return self._verify_fill(order, order_result, reference_price=current_price)

    async def _await_fill(self, result: OrderResult, inst_id: str) -> OrderResult:
        """Poll order status until a fill price appears or attempts run out.

        Only polls when the synchronous response left the order unfilled
        (state PENDING with no fillPx). Terminal states are returned as-is.
        """
        if result.fill_px or result.state in (
            OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
        ):
            return result
        for _ in range(self.fill_poll_attempts):
            await asyncio.sleep(self.fill_poll_delay)
            polled = await self.get_order_status(result.order_id, inst_id)
            if polled.fill_px or polled.state in (
                OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
            ):
                return polled
        return result

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
            state=OrderStatus.from_exchange(data.get("state", "live")),
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
