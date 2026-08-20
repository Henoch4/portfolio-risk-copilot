"""
Data models and types for the trade execution layer.

Split out of execution.py (Block G) so OrderRequest/OrderResult/OrderStatus
and the OrderSide/OrderType/TimeInForce type aliases live in a dependency-light
module with no imports from the risk layer — lets signals, multi_leg and the
audit trail reference the request shape without pulling in RiskGate/OrderExecutor.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

logger = logging.getLogger(__name__)

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
    unwind: bool = False  # True for closing/unwind legs of a multi-leg package

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
