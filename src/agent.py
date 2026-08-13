"""
Multi-agent orchestrator for the autonomous trading agent.

Architecture (inspired by TradingAgents + Jim Simons' research factory):
  MarketDataAgent → SignalAgent → RiskAgent → ExecutionAgent → OnchainLogger

Each agent has a clear, specialized role. The orchestrator sequences them
and handles retries, timeouts, and error recovery.

This is the main entry point — the /hire endpoint calls run_trading_cycle().
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .signals import (
    Signal,
    mean_reversion_signal,
    momentum_signal,
    funding_rate_signal,
    ensemble_signal,
    backtest_simple,
)
from .execution import (
    OrderExecutor,
    OrderRequest,
    OrderResult,
    OrderStatus,
    RiskGate,
    RiskCheckResult,
    ExecutionError,
)
from .audit_logger import OnchainLogger, DecisionPayload
from .okx_cli import OkxCli, OkxCliConfig, OkxCliError

logger = logging.getLogger(__name__)


@dataclass
class TradingCycleResult:
    """Result of a complete trading cycle."""
    cycle_id: str
    timestamp: float
    signals: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    executions: list[dict] = field(default_factory=list)
    total_pnl_usd: float = 0.0
    total_fees_usd: float = 0.0
    status: str = "completed"
    errors: list[str] = field(default_factory=list)


class AutonomousTradingAgent:
    """
    Main orchestrator that coordinates the multi-agent trading pipeline.
    
    Usage:
        agent = AutonomousTradingAgent(
            okx_cli=OkxCli(OkxCliConfig(demo=True)),
            risk_gate=RiskGate(...),
            dry_run=True,
        )
        result = await agent.run_trading_cycle(assets=["BTC-USDT-SWAP"])
    """

    def __init__(
        self,
        okx_cli: OkxCli,
        risk_gate: RiskGate,
        onchain_logger: OnchainLogger | None = None,
        dry_run: bool = True,
        max_position_usd: float = 5000,
        agent_id: str = "autonomous-trader-001",
        enable_momentum: bool = False,
        sizing_mode: str = "kelly",
        kelly_fraction: float = 0.5,
    ):
        self.cli = okx_cli
        self.risk_gate = risk_gate
        self.onchain_logger = onchain_logger
        self.dry_run = dry_run
        self.max_position_usd = max_position_usd
        self.agent_id = agent_id
        self.sizing_mode = sizing_mode
        self.kelly_fraction = kelly_fraction
        # Off by default — see _generate_signals docstring. Momentum is a
        # phase-2+ strategy per the design doc's Section 0 MVP scope.
        self.enable_momentum = enable_momentum

        self.executor = OrderExecutor(
            cli=okx_cli,
            risk_gate=risk_gate,
            dry_run=dry_run,
        )

        # Track open positions for this agent
        self._open_positions: dict[str, dict] = {}
        self._daily_pnl: float = 0.0

    async def _fetch_market_data(self, asset: str, lookback: int = 50) -> dict:
        """Fetch market data: price history, funding rate, current position."""
        # Fetch recent trade data
        try:
            trades = await self.cli.run(
                "market", "trades",
                "--instId", asset,
                "--limit", str(lookback),
                use_global_flags=False,
            )
        except OkxCliError as e:
            logger.warning(f"Failed to fetch trades for {asset}: {e}")
            trades = {"data": []}

        # Fetch funding rate
        try:
            funding = await self.cli.run(
                "market", "funding-rate",
                "--instId", asset,
                use_global_flags=False,
            )
        except OkxCliError:
            funding = {"data": [{"fundingRate": "0"}]}

        # Fetch current position
        position = await self.executor.get_position(asset)

        return {
            "asset": asset,
            "trades": trades.get("data", []),
            "funding_rate": float(funding.get("data", [{}])[0].get("fundingRate", "0")),
            "position": position,
            "timestamp": time.time(),
        }

    def _extract_prices(self, market_data: dict) -> list[float]:
        """Extract close prices from trade data.

        Returns [] when there is no real price data — never a fabricated
        price. The risk gate's freshness check treats an absent price as
        NO_PRICE_REFERENCE and refuses to trade; a fake 1.0 fallback here
        would quietly defeat that gate.
        """
        prices = []
        for trade in market_data.get("trades", []):
            try:
                px = float(trade.get("px", 0))
                if px > 0:
                    prices.append(px)
            except (ValueError, TypeError):
                continue
        return prices

    def _extract_price_data(self, market_data: dict) -> list[dict]:
        """Extract structured price data (close + volume) for momentum signal."""
        price_data = []
        for trade in market_data.get("trades", []):
            try:
                price_data.append({
                    "close": float(trade.get("px", 0)),
                    "volume": float(trade.get("sz", 0)),
                })
            except (ValueError, TypeError):
                continue
        if not price_data:
            price_data = [{"close": 1.0, "volume": 0.0}]
        return price_data

    def _generate_signals(self, asset: str, market_data: dict) -> list[Signal]:
        """Generate signals from all enabled strategies.

        Design-doc note (Section 0): momentum is a directional strategy the
        design doc explicitly excluded from MVP scope — it's harder to defend
        against a bad directional call than the market-neutral funding-arb
        thesis. It's kept available here for phase 2 but disabled by default;
        enable via self.enable_momentum only once that phase's graduation
        criteria (Section 7) are actually met.

        Also note: funding_rate_signal below is a directional contrarian bet
        on funding-rate extremes reverting — it is NOT the delta-neutral
        long-spot/short-perp funding arbitrage described in the design doc's
        Section 0 thesis. That strategy requires a two-leg hedged position
        this executor doesn't place. Until that's built, this signal carries
        real directional risk and should be sized and reasoned about
        accordingly, not treated as market-neutral.
        """
        prices = self._extract_prices(market_data)
        price_data = self._extract_price_data(market_data)
        funding_rate = market_data.get("funding_rate", 0.0)

        signals = [
            mean_reversion_signal(asset, prices, window=20, z_threshold=2.0),
            funding_rate_signal(asset, funding_rate, threshold=0.001),
        ]

        if self.enable_momentum:
            signals.append(
                momentum_signal(asset, price_data, short_window=5, long_window=20)
            )

        return signals

    def _compute_order_size(self, signal: Signal) -> float:
        """Compute order size in USD from confidence using fractional Kelly.

        Kelly criterion for even-money (b=1) bets: f* = 2p - 1, the share of
        the bankroll that maximizes long-run geometric growth. We scale the
        full-Kelly stake by `kelly_fraction` (default 0.5 = half-Kelly), the
        standard conservative choice: half-Kelly gives ~75% of full-Kelly's
        growth at ~25% of its drawdown variance, per the original 1956 Kelly
        sizing literature.

            f = (2p - 1) * kelly_fraction      where p = signal.probability
            size_usd = max_position_usd * f

        This replaces the older linear sizing (size = max * p), which
        overbet weak signals: a 55% signal spent 55% of max. Under half-Kelly
        the same signal spends (2*0.55 - 1)*0.5 = 5% of max. Signals below
        50% confidence produce a negative edge and size 0 (no trade), which
        is exactly the desired filter.
        """
        p = signal.confidence_bps / 10000.0
        if not (0.0 <= p <= 1.0):
            p = 0.0
        if self.sizing_mode == "linear":
            return self.max_position_usd * p
        edge = 2.0 * p - 1.0
        if edge <= 0.0:
            return 0.0
        return self.max_position_usd * edge * self.kelly_fraction

    def _signal_to_order(
        self, signal: Signal, market_data: dict
    ) -> OrderRequest | None:
        """Convert a signal into an order request, or None if not tradeable."""
        if not signal.is_tradeable:
            return None

        position = market_data.get("position")
        current_size = float(position.get("pos", 0)) if position else 0
        current_side = position.get("side") if position else None

        # Determine order size from confidence via fractional Kelly sizing
        order_size = self._compute_order_size(signal)
        if order_size <= 0.0:
            logger.info(
                f"No order for {signal.asset}: {signal.direction} "
                f"(conf {signal.confidence_bps}/10000 has no positive Kelly edge)"
            )
            return None

        # If we have a position in the same direction, consider it a hold
        if current_side and abs(current_size) > 0:
            direction_map = {"long": "LONG", "short": "SHORT"}
            if direction_map.get(current_side.lower()) == signal.direction:
                logger.info(
                    f"Already {current_side} {current_size} {signal.asset}. "
                    f"Skipping entry, considering add/reduce."
                )
                # For now, skip — don't double down
                return None

        side_map = {"LONG": "buy", "SHORT": "sell"}

        return OrderRequest(
            inst_id=signal.asset,
            side=side_map[signal.direction],
            order_type="market",
            size=f"{order_size:.2f}",
            client_oid=f"signal_{signal.strategy}_{uuid.uuid4().hex[:8]}",
            reduce_only=(signal.direction == "SHORT"),  # shorts reduce long positions
            confidence_bps=signal.confidence_bps,
        )

    async def run_trading_cycle(self, assets: list[str]) -> TradingCycleResult:
        """
        Run a complete trading cycle:
        1. Fetch market data for all assets
        2. Generate signals
        3. Pass signals through risk engine
        4. Log decisions onchain (if configured)
        5. Execute approved orders
        6. Return results
        """
        cycle_id = f"cycle_{uuid.uuid4().hex[:10]}"
        result = TradingCycleResult(
            cycle_id=cycle_id,
            timestamp=time.time(),
        )

        logger.info(f"Starting trading cycle {cycle_id} for {len(assets)} assets")

        # --- Phase 1: Market Data ---
        market_data_tasks = [self._fetch_market_data(asset) for asset in assets]
        market_data_list = await asyncio.gather(*market_data_tasks, return_exceptions=True)

        for i, md in enumerate(market_data_list):
            if isinstance(md, Exception):
                result.errors.append(f"Market data error for {assets[i]}: {md}")
                logger.error(f"Market data error: {md}")
                continue

            asset = md["asset"]

            # --- Phase 2: Signal Generation ---
            signals = self._generate_signals(asset, md)
            ensemble = ensemble_signal(asset, signals)

            result.signals.append({
                "asset": asset,
                "ensemble": ensemble.to_dict(),
                "individual": [s.to_dict() for s in signals],
            })

            logger.info(
                f"Signal for {asset}: {ensemble.direction} "
                f"(conf: {ensemble.confidence_bps/100:.0f}%, "
                f"strategy: {ensemble.rationale[:80]}...)"
            )

            # --- Phase 3: Risk Gate ---
            order = self._signal_to_order(ensemble, md)
            if order is None:
                logger.info(f"No order for {asset} (no tradeable signal or already positioned)")
                continue

            asset_prices = self._extract_prices(md)
            current_price = asset_prices[-1] if asset_prices else None

            # Feed the regime-throttle gate: observed prices let the gate's
            # volatility governor decide whether to scale down the position cap.
            if current_price is not None:
                self.risk_gate.observe_price(asset, current_price)

            current_position_side = (md.get("position") or {}).get("side")

            risk_result = self.risk_gate.check_order(
                order,
                self.agent_id,
                current_price=current_price,
                current_price_timestamp=md.get("timestamp"),
                current_position_side=current_position_side,
            )
            if not risk_result.approved:
                result.errors.append(
                    f"Risk gate rejected {asset}: {risk_result.reason}"
                )
                logger.warning(f"Risk gate rejected: {risk_result.code}: {risk_result.reason}")
                continue

            # --- Phase 4: Onchain Decision Log ---
            decision_payload = DecisionPayload(
                decision_id=f"dec_{uuid.uuid4().hex[:12]}",
                agent_address=self.onchain_logger.agent_address if self.onchain_logger else "0x0000000000000000000000000000000000000000",
                asset=asset,
                signal=ensemble.direction,
                strategy=f"{ensemble.strategy}+{ensemble.metadata.get('signals', [{}])[0].get('strategy', 'unknown') if ensemble.metadata.get('signals') else 'unknown'}",
                confidence_bps=ensemble.confidence_bps,
                entry_price=ensemble.entry_price or 0,
                size_usd=float(order.size),
                risk_params_hash=self.risk_gate.compute_risk_hash() if hasattr(self.risk_gate, 'compute_risk_hash') else "0x" + "00" * 32,
                timestamp=int(time.time()),
            )

            log_tx = None
            if self.onchain_logger and not self.dry_run:
                try:
                    log_tx = self.onchain_logger.log_decision(decision_payload)
                    result.decisions.append({
                        "decision_id": decision_payload.decision_id,
                        "asset": asset,
                        "tx_hash": log_tx,
                        "signal": ensemble.direction,
                        "confidence_bps": ensemble.confidence_bps,
                        "confidence": ensemble.confidence_bps / 10000.0,
                        "side": order.side,
                        "size_usd": float(order.size),
                        "risk_hash": decision_payload.risk_params_hash,
                        "status": "approved",
                    })
                    logger.info(f"Decision logged onchain: {log_tx}")
                except Exception as e:
                    result.errors.append(f"Onchain log failed for {asset}: {e}")
                    logger.error(f"Onchain logging failed: {e}")
                    # In production: do NOT execute if onchain log fails
                    continue
            else:
                result.decisions.append({
                    "decision_id": decision_payload.decision_id,
                    "asset": asset,
                    "tx_hash": None,
                    "signal": ensemble.direction,
                    "confidence_bps": ensemble.confidence_bps,
                    "confidence": ensemble.confidence_bps / 10000.0,
                    "side": order.side,
                    "size_usd": float(order.size),
                    "risk_hash": decision_payload.risk_params_hash,
                    "status": "approved",
                    "dry_run": True,
                })

            # --- Phase 5: Execution ---
            try:
                order_result = await self.executor.place_order(
                    order,
                    current_price=current_price,
                    current_price_timestamp=md.get("timestamp"),
                )
                # A fill that fails post-fill verification (bad slippage) is NOT
                # a success, even if the exchange reports it filled — see
                # OrderExecutor._verify_fill.
                fill_ok = order_result.fill_verified is not False
                if order_result.state in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and fill_ok:
                    execution_status = "success"
                elif order_result.fill_verified is False:
                    execution_status = "fill_verification_failed"
                else:
                    execution_status = order_result.state.value
                result.executions.append({
                    "decision_id": decision_payload.decision_id,
                    "asset": asset,
                    "order_id": order_result.order_id,
                    "client_oid": order_result.client_oid,
                    "state": order_result.state.value,
                    "fill_px": order_result.fill_px,
                    "fill_price": order_result.fill_px,
                    "slippage_pct": order_result.slippage_pct,
                    "size_usd": float(order.size),
                    "tx_hash": None,
                    "status": execution_status,
                    "fee": order_result.fee,
                    "fee_ccy": order_result.fee_ccy,
                })
                logger.info(f"Order placed: {order_result.order_id}, state={order_result.state}")

                # Record execution onchain
                if log_tx and self.onchain_logger:
                    fill_price = float(order_result.fill_px or 0)
                    fee = float(order_result.fee or 0)
                    self.onchain_logger.record_execution(
                        decision_id=decision_payload.decision_id,
                        fill_price=fill_price,
                        fill_size_usd=float(order.size),
                        fee_usd=fee,
                        success=fill_ok,
                    )

                # Update daily stats (bad fills still count as trades)
                if order_result.state in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                    self.risk_gate.report_volume(self.agent_id, float(order.size))

            except ExecutionError as e:
                result.errors.append(f"Execution failed for {asset}: {e}")
                logger.error(f"Execution error: {e}")
                if self.onchain_logger and log_tx:
                    self.onchain_logger.record_execution(
                        decision_id=decision_payload.decision_id,
                        fill_price=0,
                        fill_size_usd=0,
                        fee_usd=0,
                        success=False,
                    )

        return result

    async def run(self, assets: list[str], interval_seconds: int = 300) -> None:
        """Run continuous trading loop."""
        logger.info(f"Starting autonomous agent {self.agent_id}")
        if self.onchain_logger:
            logger.info(f"Connected to chain: {self.onchain_logger.is_connected()}")
            logger.info(f"Agent address: {self.onchain_logger.agent_address}")

        while True:
            try:
                result = await self.run_trading_cycle(assets)
                logger.info(
                    f"Cycle complete: {len(result.executions)} executed, "
                    f"PnL={result.total_pnl_usd:.2f}"
                )
                if result.errors:
                    logger.warning(f"Errors: {result.errors}")
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)

            await asyncio.sleep(interval_seconds)
