"""
Public API for the execution layer. Everything that lived in execution.py is
re-exported here so existing importers (``from src.execution import RiskGate``)
keep working unchanged after the Block G modular split.
"""
from __future__ import annotations

from .models import (
    OrderSide,
    OrderType,
    TimeInForce,
    OrderStatus,
    OrderRequest,
    OrderResult,
    ExecutionError,
)
from .risk_gate import (
    DurableDailyCounters,
    RiskCheckResult,
    RiskGate,
)
from .executor import OrderExecutor

__all__ = [
    "OrderSide",
    "OrderType",
    "TimeInForce",
    "OrderStatus",
    "OrderRequest",
    "OrderResult",
    "ExecutionError",
    "DurableDailyCounters",
    "RiskCheckResult",
    "RiskGate",
    "OrderExecutor",
]
