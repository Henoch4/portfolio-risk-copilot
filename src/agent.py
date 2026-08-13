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
from typing import Literal, Optional

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
from .audit_trail import AuditLog
from .curator import CuratorAgent, apply_env_overrides
from .data_integrity import DataIntegrityGate, IntegrityResult, MarketTick, Severity

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
    curator: dict | None = None


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
        integrity_gate: DataIntegrityGate | None = None,
        curator: CuratorAgent | None = None,
        audit_log: AuditLog | None = None,
        expected_equity: float | None = None,
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

        # Pre-signal integrity gate (runs BEFORE signal generation), curator
        # profile selector, and the local append-only audit log. All optional
        # and fail-open only in the sense that absence disables the feature —
        # when present, a HARD_BLOCK blocks the asset for the whole cycle.
        self.integrity_gate = integrity_gate
        self.curator = curator
        self.audit_log = audit_log
        self.expected_equity = expected_equity

        # Resolved curator knobs for the current cycle, forwarded to sizing /
        # signal filtering. Defaults (no curator, or knob untouched by env)
        # are neutral: multiplier 1.0, no extra confidence floor, all signals.
        self._position_size_multiplier: float = 1.0
        self._confidence_floor_bps: int | None = None
        self._enabled_signals: set[str] | None = None

        self.executor = OrderExecutor(
            cli=okx_cli,
            risk_gate=risk_gate,
            dry_run=dry_run,
        )

        # Concurrency guards for the shared, mutable pieces touched inside the
        # parallel per-asset pipeline: the risk gate's regime/volume state and
        # the append-only audit log. Network I/O (onchain writes, OKX fills)
        # runs in worker threads via asyncio.to_thread and is serialized only
        # on the nonce lock inside OnchainLogger.
        self._risk_lock = asyncio.Lock()
        self._audit_lock = asyncio.Lock()

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

    def _resolve_curator_profile(self) -> dict | None:
        """Resolve the active curator profile for this cycle.

        Default-passthrough: the profile supplies the default for every knob,
        and an operator-set env var for a knob wins only when explicitly set
        (see apply_env_overrides). Returns the resolved profile dict (or None
        when no curator is wired) and stores the cycle's sizing knobs.
        """
        if not self.curator:
            self._position_size_multiplier = 1.0
            self._confidence_floor_bps = None
            self._enabled_signals = None
            return None

        profile = self.curator.active_profile()
        resolved = apply_env_overrides(
            profile,
            {
                "position_size_multiplier": os.getenv("CURATOR_POSITION_SIZE_MULTIPLIER"),
                "confidence_floor_bps": os.getenv("CURATOR_CONFIDENCE_FLOOR_BPS"),
                "max_leverage": os.getenv("CURATOR_MAX_LEVERAGE"),
            },
            casters={
                "position_size_multiplier": float,
                "confidence_floor_bps": int,
                "max_leverage": float,
            },
        )
        self._position_size_multiplier = float(resolved.get("position_size_multiplier", 1.0))
        self._confidence_floor_bps = resolved.get("confidence_floor_bps")
        enabled = resolved.get("enabled_signals")
        self._enabled_signals = set(enabled) if enabled else None
        return resolved

    def _check_integrity(self, asset: str, market_data: dict,
                         ledger: dict | None = None) -> IntegrityResult | None:
        """Run the pre-signal integrity gate for an asset.

        Returns None when no gate is wired. A HARD_BLOCK result means the
        asset is skipped before a single signal is generated on top of
        questionable inputs. The ledger-consistency check only runs when real
        book figures are supplied (`ledger={"cash":..., "positions_value":...}`
        plus self.expected_equity) — fabricating zeros would defeat the whole
        point of the check.
        """
        if not self.integrity_gate:
            return None

        now_s = time.time()
        tick = MarketTick(
            timestamp=float(market_data.get("timestamp", now_s)),
            funding_rate=float(market_data.get("funding_rate", 0.0)),
        )
        results = [
            self.integrity_gate.check_market_data({asset: tick}, now_s=now_s),
        ]
        if ledger and self.expected_equity is not None:
            results.append(
                self.integrity_gate.check_ledger_consistency(
                    cash=float(ledger.get("cash", 0.0)),
                    positions_value=float(ledger.get("positions_value", 0.0)),
                    expected_equity=self.expected_equity,
                )
            )
        return self.integrity_gate.combine(*results)

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

        # Curator profile's enabled-signal allowlist: strategies not in the
        # active profile are dropped before they reach the ensemble.
        if self._enabled_signals is not None:
            signals = [s for s in signals if s.strategy in self._enabled_signals]

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
            base = self.max_position_usd * p
        else:
            edge = 2.0 * p - 1.0
            if edge <= 0.0:
                return 0.0
            base = self.max_position_usd * edge * self.kelly_fraction
        # Curator profile position-size multiplier (default 1.0 = unchanged).
        return base * self._position_size_multiplier

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

        side_map: dict[str, Literal["buy", "sell"]] = {"LONG": "buy", "SHORT": "sell"}

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

        # Resolve the curator profile for this cycle ONCE, before any market
        # data is fetched — its knobs (sizing multiplier, confidence floor,
        # enabled signals) are stable for the whole cycle.
        resolved_profile = self._resolve_curator_profile()
        if resolved_profile:
            result.curator = {
                "profile": self.curator.state.current_profile if self.curator else None,
                "knobs": resolved_profile,
            }

        logger.info(f"Starting trading cycle {cycle_id} for {len(assets)} assets")

        # --- Phase 1: Market Data --- (parallel across assets)
        market_data_tasks = [self._fetch_market_data(asset) for asset in assets]
        market_data_list = await asyncio.gather(*market_data_tasks, return_exceptions=True)

        # --- Phases 1.5-5: per-asset pipeline --- (parallel across assets)
        # Each asset's full path (integrity -> signal -> risk -> onchain log ->
        # execute) runs as its own coroutine. Blocking network calls (onchain
        # writes) run in worker threads via asyncio.to_thread so they overlap;
        # the shared risk gate and audit log are guarded by locks. Results are
        # collected per asset and merged back in input order so callers that
        # depend on ordering (tests, downstream consumers) see a stable shape.
        tasks = []
        errored_assets = []
        for i, md in enumerate(market_data_list):
            if isinstance(md, BaseException):
                result.errors.append(f"Market data error for {assets[i]}: {md}")
                logger.error(f"Market data error: {md}")
                errored_assets.append(assets[i])
                continue
            tasks.append(self._process_asset(md, cycle_id, result))

        per_asset = await asyncio.gather(*tasks, return_exceptions=True)
        for out in per_asset:
            if isinstance(out, BaseException):
                result.errors.append(f"Asset processing error: {out}")
                logger.error(f"Asset processing error: {out}")
                continue
            result.signals.extend(out["signals"])
            result.decisions.extend(out["decisions"])
            result.executions.extend(out["executions"])
            result.errors.extend(out["errors"])

        return result

    async def _process_asset(self, md: dict, cycle_id: str, result: "TradingCycleResult") -> dict:  # noqa: F821
        """Run the full per-asset pipeline for one market-data snapshot.

        Returns the lists this asset produced (signals/decisions/executions/
        errors) so the parent cycle can merge them in input order. Shared
        mutable state (risk gate, audit log) is guarded; blocking network I/O
        runs in worker threads.
        """
        asset = md["asset"]
        out: dict = {"signals": [], "decisions": [], "executions": [], "errors": []}

        # --- Phase 1.5: Pre-Signal Integrity Gate ---
        integrity = self._check_integrity(asset, md)
        if integrity and integrity.blocks_trading:
            msg = f"Integrity gate blocked {asset}: {integrity.reasons}"
            out["errors"].append(msg)
            logger.warning(f"Integrity gate blocked: {msg}")
            if self.audit_log:
                async with self._audit_lock:
                    self.audit_log.write("integrity_block", {
                        "cycle_id": cycle_id,
                        "asset": asset,
                        "reasons": integrity.reasons,
                    })
            return out

        # --- Phase 2: Signal Generation ---
        signals = self._generate_signals(asset, md)
        ensemble = ensemble_signal(asset, signals)

        out["signals"].append({
            "asset": asset,
            "ensemble": ensemble.to_dict(),
            "individual": [s.to_dict() for s in signals],
        })

        logger.info(
            f"Signal for {asset}: {ensemble.direction} "
            f"(conf: {ensemble.confidence_bps/100:.0f}%, "
            f"strategy: {ensemble.rationale[:80]}...)"
        )

        # Curator confidence floor
        if self._confidence_floor_bps is not None and ensemble.confidence_bps < self._confidence_floor_bps:
            msg = (f"Curator confidence floor {self._confidence_floor_bps}bps not met for "
                   f"{asset} ({ensemble.confidence_bps}bps)")
            logger.info(msg)
            if self.audit_log:
                async with self._audit_lock:
                    self.audit_log.write("curator_confidence_floor", {
                        "cycle_id": cycle_id,
                        "asset": asset,
                        "confidence_bps": ensemble.confidence_bps,
                        "floor_bps": self._confidence_floor_bps,
                    })
            return out

        # --- Phase 3: Risk Gate ---
        order = self._signal_to_order(ensemble, md)
        if order is None:
            logger.info(f"No order for {asset} (no tradeable signal or already positioned)")
            return out

        asset_prices = self._extract_prices(md)
        current_price = asset_prices[-1] if asset_prices else None

        async with self._risk_lock:
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
            out["errors"].append(f"Risk gate rejected {asset}: {risk_result.reason}")
            logger.warning(f"Risk gate rejected: {risk_result.code}: {risk_result.reason}")
            if self.audit_log:
                async with self._audit_lock:
                    self.audit_log.write("risk_rejection", {
                        "cycle_id": cycle_id,
                        "asset": asset,
                        "code": risk_result.code,
                        "reason": risk_result.reason,
                        "confidence_bps": ensemble.confidence_bps,
                    })
            return out

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
                log_tx = await asyncio.to_thread(self.onchain_logger.log_decision, decision_payload)
                out["decisions"].append({
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
                out["errors"].append(f"Onchain log failed for {asset}: {e}")
                logger.error(f"Onchain logging failed: {e}")
                # In production: do NOT execute if onchain log fails
                return out
        else:
            out["decisions"].append({
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
            fill_ok = order_result.fill_verified is not False
            if order_result.state in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and fill_ok:
                execution_status = "success"
            elif order_result.fill_verified is False:
                execution_status = "fill_verification_failed"
            else:
                execution_status = order_result.state.value
            out["executions"].append({
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

            if log_tx and self.onchain_logger:
                await asyncio.to_thread(
                    self.onchain_logger.record_execution,
                    decision_id=decision_payload.decision_id,
                    fill_price=float(order_result.fill_px or 0),
                    fill_size_usd=float(order.size),
                    fee_usd=float(order_result.fee or 0),
                    success=fill_ok,
                )

            if order_result.state in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                async with self._risk_lock:
                    self.risk_gate.report_volume(self.agent_id, float(order.size))

        except ExecutionError as e:
            out["errors"].append(f"Execution failed for {asset}: {e}")
            logger.error(f"Execution error: {e}")
            if self.onchain_logger and log_tx:
                await asyncio.to_thread(
                    self.onchain_logger.record_execution,
                    decision_id=decision_payload.decision_id,
                    fill_price=0,
                    fill_size_usd=0,
                    fee_usd=0,
                    success=False,
                )

        return out

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
