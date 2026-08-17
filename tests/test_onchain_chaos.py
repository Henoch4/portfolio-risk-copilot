"""Chaos test: if the onchain decision log fails, the order must NOT execute.

The hard invariant is "log onchain BEFORE execution, and never execute if the
log failed." We make the logger raise on log_decision and assert the cycle still
completes, records the error, and appends no decision and no execution for the
asset (it returns before reaching the executor). Exercised through the real
run_trading_cycle / _process_asset path.
"""
import pytest

from src.agent import AutonomousTradingAgent
from src.execution import OrderRequest, RiskCheckResult, RiskGate


class _StubCli:
    async def run(self, *args, **kwargs):
        cmd = args[:1]
        if cmd == ("market",) and "trades" in args:
            return {"data": [{"px": "100", "sz": "1"}]}
        if cmd == ("market",) and "funding-rate" in args:
            return {"data": [{"fundingRate": "0.005"}]}
        if cmd == ("account",) and "positions" in args:
            return {"data": []}
        return {"data": []}


class _FailingLogger:
    agent_address = "0x" + "0" * 40

    def log_decision(self, payload):
        raise RuntimeError("onchain RPC unavailable")

    def record_execution(self, **kwargs):
        raise RuntimeError("should not be called when log failed")


def _agent(**overrides):
    kwargs = dict(okx_cli=_StubCli(), risk_gate=RiskGate(), dry_run=False, max_position_usd=5000)
    kwargs.update(overrides)
    return AutonomousTradingAgent(**kwargs)


def _force_tradeable(agent):
    # The stub feed yields a non-tradeable ensemble (1/2 signals bearish), so
    # _signal_to_order returns None and we never reach the log step. Force a
    # real order so the onchain-failure path is exercised.
    agent.risk_gate.check_order = lambda *a, **k: RiskCheckResult(
        approved=True, code="APPROVED", reason="stubbed"
    )
    agent._signal_to_order = lambda ensemble, md: OrderRequest(
        inst_id="BTC-USDT-SWAP", side="buy", order_type="market", size="100",
        confidence_bps=9000,
    )


@pytest.mark.asyncio
async def test_onchain_log_failure_blocks_execution():
    agent = _agent(onchain_logger=_FailingLogger())
    _force_tradeable(agent)

    result = await agent.run_trading_cycle(["BTC-USDT-SWAP"])

    # Cycle completed; the failure was caught, not propagated.
    assert any("Onchain log failed" in e for e in result.errors)
    # No decision was logged, so nothing was appended and nothing executed.
    assert result.decisions == []
    assert result.executions == []


@pytest.mark.asyncio
async def test_onchain_log_failure_does_not_crash_cycle():
    agent = _agent(onchain_logger=_FailingLogger())
    _force_tradeable(agent)
    # Multiple assets: one failing logger must not abort the whole gather.
    result = await agent.run_trading_cycle(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    assert len(result.errors) >= 1
    assert result.executions == []
