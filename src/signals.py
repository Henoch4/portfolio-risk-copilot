"""
Trading signal engine for the autonomous trading agent.

Implements multiple signal strategies with confidence scoring:
  - Mean reversion (Z-score based on rolling window)
  - Momentum (price trend + volume confirmation)
  - Smart-money flow divergence
  - Funding-rate arbitrage signal

Each signal returns a Signal object with direction, confidence, and rationale.
The risk engine then gates these before any execution.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


SignalDirection = Literal["LONG", "SHORT", "NEUTRAL"]


class SignalStrength(Enum):
    WEAK = 0.3
    MODERATE = 0.5
    STRONG = 0.7
    VERY_STRONG = 0.9


@dataclass
class Signal:
    """A single trading signal from one strategy."""
    strategy: str
    asset: str          # e.g. "BTC-USDT-SWAP"
    direction: SignalDirection
    confidence_bps: int   # 0–10000 (basis points, 7000 = 70%)
    entry_price: float | None = None
    rationale: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def is_tradeable(self) -> bool:
        """A signal is tradeable if direction != NEUTRAL and confidence >= 60%."""
        return self.direction != "NEUTRAL" and self.confidence_bps >= 6000

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "asset": self.asset,
            "direction": self.direction,
            "confidence_bps": self.confidence_bps,
            "entry_price": self.entry_price,
            "rationale": self.rationale,
            "metadata": self.metadata,
            "is_tradeable": self.is_tradeable,
        }


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def mean_reversion_signal(
    asset: str,
    prices: list[float],
    window: int = 20,
    z_threshold: float = 2.0,
) -> Signal:
    """
    Mean-reversion signal based on Z-score of recent prices.
    Buy when price is significantly below the rolling mean (oversold).
    Sell when significantly above (overbought).
    """
    if len(prices) < window + 2:
        return Signal(
            strategy="mean_reversion",
            asset=asset,
            direction="NEUTRAL",
            confidence_bps=0,
            entry_price=prices[-1] if prices else None,
            rationale=f"Insufficient data: {len(prices)} < {window + 2} required",
        )

    recent = prices[-window:]
    mean_price = statistics.mean(recent)
    std_price = statistics.pstdev(recent) if len(recent) > 1 else 0.0

    if std_price == 0 or math.isnan(std_price):
        return Signal(
            strategy="mean_reversion",
            asset=asset,
            direction="NEUTRAL",
            confidence_bps=0,
            entry_price=prices[-1],
            rationale="Zero or NaN standard deviation — no signal",
        )

    z_score = (prices[-1] - mean_price) / std_price
    current_price = prices[-1]

    # Higher |z| → higher confidence
    confidence_factor = min(abs(z_score) / z_threshold, 1.0)
    # Base confidence: 60% minimum if we have a signal
    base_confidence = 0.60
    # Scale from 60% to 95% based on how extreme the z-score is
    confidence = base_confidence + (0.35 * confidence_factor)
    confidence_bps = int(confidence * 10000)

    if z_score < -z_threshold:
        # Price is below mean → oversold → buy
        return Signal(
            strategy="mean_reversion",
            asset=asset,
            direction="LONG",
            confidence_bps=min(confidence_bps, 9500),
            entry_price=current_price,
            rationale=(
                f"Z-score {z_score:.2f} below -{z_threshold} threshold. "
                f"Price {current_price:.2f} vs mean {mean_price:.2f} "
                f"(std {std_price:.2f}). Oversold — mean reversion expected."
            ),
            metadata={
                "z_score": z_score,
                "mean": mean_price,
                "std": std_price,
                "current_price": current_price,
            },
        )
    elif z_score > z_threshold:
        # Price is above mean → overbought → sell/short
        return Signal(
            strategy="mean_reversion",
            asset=asset,
            direction="SHORT",
            confidence_bps=min(confidence_bps, 9500),
            entry_price=current_price,
            rationale=(
                f"Z-score {z_score:.2f} above +{z_threshold} threshold. "
                f"Price {current_price:.2f} vs mean {mean_price:.2f} "
                f"(std {std_price:.2f}). Overbought — mean reversion expected."
            ),
            metadata={
                "z_score": z_score,
                "mean": mean_price,
                "std": std_price,
                "current_price": current_price,
            },
        )
    else:
        # Within mean — no signal, but note convergence
        strength = SignalStrength.WEAK.value
        return Signal(
            strategy="mean_reversion",
            asset=asset,
            direction="NEUTRAL",
            confidence_bps=int(strength * 10000),
            entry_price=current_price,
            rationale=(
                f"Z-score {z_score:.2f} within ±{z_threshold} band. "
                f"No mean-reversion opportunity."
            ),
            metadata={
                "z_score": z_score,
                "mean": mean_price,
                "std": std_price,
                "current_price": current_price,
            },
        )


def momentum_signal(
    asset: str,
    price_data: list[dict],
    short_window: int = 5,
    long_window: int = 20,
) -> Signal:
    """
    Momentum signal based on moving average crossover + volume confirmation.
    
    price_data items: {"close": float, "volume": float}
    """
    if len(price_data) < long_window:
        return Signal(
            strategy="momentum",
            asset=asset,
            direction="NEUTRAL",
            confidence_bps=0,
            entry_price=price_data[-1]["close"] if price_data else None,
            rationale=f"Insufficient data: {len(price_data)} < {long_window}",
        )

    closes = [d["close"] for d in price_data]
    short_ma = statistics.mean(closes[-short_window:])
    long_ma = statistics.mean(closes[-long_window:])
    current_price = closes[-1]

    # Volume confirmation
    recent_volumes = [d["volume"] for d in price_data[-short_window:]]
    avg_volume = statistics.mean(recent_volumes) if recent_volumes else 0
    vol_ratio = recent_volumes[-1] / avg_volume if avg_volume > 0 else 1.0
    vol_confirmed = vol_ratio > 0.8  # at least 80% of recent avg volume

    # Price momentum
    price_change = (current_price - long_ma) / long_ma if long_ma > 0 else 0

    # Confidence based on MA spread + volume confirmation
    spread_ratio = abs(short_ma - long_ma) / long_ma if long_ma > 0 else 0
    confidence = min(spread_ratio * 50 + (0.6 if vol_confirmed else 0.4), 0.95)
    confidence_bps = int(confidence * 10000)

    if short_ma > long_ma * 1.001 and price_change > 0:
        return Signal(
            strategy="momentum",
            asset=asset,
            direction="LONG",
            confidence_bps=confidence_bps,
            entry_price=current_price,
            rationale=(
                f"MA crossover: short MA {short_ma:.2f} > long MA {long_ma:.2f}. "
                f"Volume {'confirmed' if vol_confirmed else 'weak'} "
                f"(ratio {vol_ratio:.2f}). Price up {price_change:.2%}."
            ),
            metadata={
                "short_ma": short_ma,
                "long_ma": long_ma,
                "vol_ratio": vol_ratio,
                "price_change": price_change,
            },
        )
    elif short_ma < long_ma * 0.999 and price_change < 0:
        return Signal(
            strategy="momentum",
            asset=asset,
            direction="SHORT",
            confidence_bps=confidence_bps,
            entry_price=current_price,
            rationale=(
                f"MA crossover: short MA {short_ma:.2f} < long MA {long_ma:.2f}. "
                f"Volume {'confirmed' if vol_confirmed else 'weak'} "
                f"(ratio {vol_ratio:.2f}). Price down {price_change:.2%}."
            ),
            metadata={
                "short_ma": short_ma,
                "long_ma": long_ma,
                "vol_ratio": vol_ratio,
                "price_change": price_change,
            },
        )
    else:
        strength = SignalStrength.WEAK.value
        return Signal(
            strategy="momentum",
            asset=asset,
            direction="NEUTRAL",
            confidence_bps=int(strength * 10000),
            entry_price=current_price,
            rationale=(
                f"MA crossover: short {short_ma:.2f} vs long {long_ma:.2f}. "
                f"No clear momentum signal."
            ),
            metadata={
                "short_ma": short_ma,
                "long_ma": long_ma,
                "vol_ratio": vol_ratio,
                "price_change": price_change,
            },
        )


def funding_rate_signal(
    asset: str,
    funding_rate: float,
    threshold: float = 0.001,
) -> Signal:
    """
    Funding rate arbitrage signal.
    When funding rate is very positive (longs pay shorts), it's a contrarian signal.
    When very negative (shorts pay longs), it's a contrarian buy signal.
    """
    if abs(funding_rate) < threshold:
        return Signal(
            strategy="funding_rate",
            asset=asset,
            direction="NEUTRAL",
            confidence_bps=int(0.3 * 10000),  # 30% confidence for weak signal
            entry_price=None,
            rationale=(
                f"Funding rate {funding_rate:.6f} within ±{threshold} band. "
                f"No significant signal."
            ),
            metadata={"funding_rate": funding_rate, "threshold": threshold},
        )

    # Confidence based on funding rate extremity
    # 0.1% funding = moderate, 0.5%+ = strong
    confidence = min(0.60 + abs(funding_rate) * 100, 0.90)
    confidence_bps = int(confidence * 10000)

    if funding_rate > threshold:
        # Longs pay shorts → contrarian SHORT signal
        return Signal(
            strategy="funding_rate",
            asset=asset,
            direction="SHORT",
            confidence_bps=confidence_bps,
            entry_price=None,
            rationale=(
                f"Funding rate {funding_rate:.6f} strongly positive "
                f"(longs pay shorts). Contrarian short signal."
            ),
            metadata={"funding_rate": funding_rate, "threshold": threshold},
        )
    else:
        # Shorts pay longs → contrarian LONG signal
        return Signal(
            strategy="funding_rate",
            asset=asset,
            direction="LONG",
            confidence_bps=confidence_bps,
            entry_price=None,
            rationale=(
                f"Funding rate {funding_rate:.6f} strongly negative "
                f"(shorts pay longs). Contrarian long signal."
            ),
            metadata={"funding_rate": funding_rate, "threshold": threshold},
        )


def ensemble_signal(asset: str, signals: list[Signal]) -> Signal:
    """
    Combine multiple signals into an ensemble decision.
    Uses weighted confidence voting.
    """
    if not signals:
        return Signal(
            strategy="ensemble",
            asset=asset,
            direction="NEUTRAL",
            confidence_bps=0,
            rationale="No signals provided",
        )

    # Weight by confidence
    long_score = 0.0
    short_score = 0.0
    long_conf_sum = 0.0
    short_conf_sum = 0.0

    for sig in signals:
        conf = sig.confidence_bps / 10000.0
        if sig.direction == "LONG":
            long_score += conf * (1 if sig.is_tradeable else 0.5)
            long_conf_sum += conf
        elif sig.direction == "SHORT":
            short_score += conf * (1 if sig.is_tradeable else 0.5)
            short_conf_sum += conf

    total_conf = long_conf_sum + short_conf_sum
    if total_conf == 0:
        return Signal(
            strategy="ensemble",
            asset=asset,
            direction="NEUTRAL",
            confidence_bps=0,
            rationale=(
                f"All {len(signals)} signals neutral. No clear direction."
            ),
            metadata={"signals": [s.to_dict() for s in signals]},
        )

    if long_score > short_score and long_score > 0:
        confidence = min(long_score / max(len(signals), 1), 0.95)
        return Signal(
            strategy="ensemble",
            asset=asset,
            direction="LONG",
            confidence_bps=int(confidence * 10000),
            entry_price=next(
                (s.entry_price for s in signals if s.entry_price), None
            ),
            rationale=(
                f"Ensemble: {sum(1 for s in signals if s.direction == 'LONG')}/"
                f"{len(signals)} signals bullish. "
                f"Weighted score {long_score:.2f} vs {short_score:.2f}."
            ),
            metadata={"signals": [s.to_dict() for s in signals]},
        )
    elif short_score > 0:
        confidence = min(short_score / max(len(signals), 1), 0.95)
        return Signal(
            strategy="ensemble",
            asset=asset,
            direction="SHORT",
            confidence_bps=int(confidence * 10000),
            entry_price=next(
                (s.entry_price for s in signals if s.entry_price), None
            ),
            rationale=(
                f"Ensemble: {sum(1 for s in signals if s.direction == 'SHORT')}/"
                f"{len(signals)} signals bearish. "
                f"Weighted score {long_score:.2f} vs {short_score:.2f}."
            ),
            metadata={"signals": [s.to_dict() for s in signals]},
        )

    return Signal(
        strategy="ensemble",
        asset=asset,
        direction="NEUTRAL",
        confidence_bps=0,
        rationale=f"No clear majority from {len(signals)} signals",
        metadata={"signals": [s.to_dict() for s in signals]},
    )


# --- Simple backtest helper ---

@dataclass
class BacktestResult:
    total_return_bps: int
    sharpe_ratio: float
    max_drawdown_bps: int
    num_trades: int
    win_rate: float


def backtest_simple(
    prices: list[float],
    signals: list[Signal],
    initial_capital: float = 10000,
) -> BacktestResult:
    """Basic backtest: execute tradeable signals, track PnL."""
    capital = initial_capital
    peak_capital = capital
    returns = []
    wins = 0
    losses = 0
    num_trades = 0

    for i, sig in enumerate(signals):
        if not sig.is_tradeable or i + 1 >= len(prices):
            continue

        num_trades += 1
        entry = prices[i]
        exit_price = prices[i + 1]

        if sig.direction == "LONG":
            pnl = (exit_price - entry) / entry * capital
        elif sig.direction == "SHORT":
            pnl = (entry - exit_price) / entry * capital
        else:
            continue

        capital += pnl
        peak_capital = max(peak_capital, capital)

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

        returns.append(pnl / initial_capital)

    total_return = (capital - initial_capital) / initial_capital
    total_return_bps = int(total_return * 10000)

    if returns:
        avg_return = statistics.mean(returns)
        std_return = statistics.pstdev(returns) if len(returns) > 1 else 0
        sharpe = avg_return / std_return if std_return > 0 else 0
    else:
        sharpe = 0

    max_dd = max((peak_capital - capital) / peak_capital if peak_capital > 0 else 0, 0)
    max_dd_bps = int(max_dd * 10000)

    win_rate = wins / num_trades if num_trades > 0 else 0

    return BacktestResult(
        total_return_bps=total_return_bps,
        sharpe_ratio=sharpe,
        max_drawdown_bps=max_dd_bps,
        num_trades=num_trades,
        win_rate=win_rate,
    )
