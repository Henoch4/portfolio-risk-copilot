"""Equity-delta loss feed (roadmap Phase 1: the halt must be fed by reality).

Before this feed, nothing in production ever called RiskGate.report_loss, so
max_daily_loss_usd and its auto-kill-switch were inert. These tests pin the
contract: live cycles compare OKX total equity against the previous cycle,
negative deltas are reported as losses pinned to the cycle's UTC day, gains
are not reported, dry-run is disabled, and a failed balance read never kills
the cycle.
"""
import pytest

from src.agent import AutonomousTradingAgent
from src.execution import RiskGate


class _BalanceStubCli:
    """Canned market data + a mutable OKX account equity."""

    def __init__(self, equity: float = 1000.0):
        self.equity = equity

    async def run(self, *args, **kwargs):
        cmd = args[:1]
        if cmd == ("market",) and "trades" in args:
            return {"data": [{"px": "100", "sz": "1"}]}
        if cmd == ("market",) and "funding-rate" in args:
            return {"data": [{"fundingRate": "0.005"}]}
        if cmd == ("account",) and "positions" in args:
            return {"data": []}
        return {"data": []}

    async def balance_all(self):
        return {
            "trading": {
                "totalEq": str(self.equity),
                "details": [],
            }
        }


def _agent(cli, dry_run: bool, max_daily_loss: float = 500.0):
    return AutonomousTradingAgent(
        okx_cli=cli,
        risk_gate=RiskGate(max_daily_loss_usd=max_daily_loss),
        dry_run=dry_run,
        max_position_usd=5000,
    )


@pytest.mark.asyncio
async def test_dry_run_feed_is_disabled():
    cli = _BalanceStubCli(equity=1000)
    agent = _agent(cli, dry_run=True)
    await agent._report_cycle_loss(agent.risk_gate.current_day_key(agent.agent_id))
    assert agent._last_cycle_equity is None  # never even baselines in dry-run


@pytest.mark.asyncio
async def test_first_live_cycle_sets_baseline_only():
    cli = _BalanceStubCli(equity=1000)
    agent = _agent(cli, dry_run=False)
    await agent._report_cycle_loss(agent.risk_gate.current_day_key(agent.agent_id))
    assert agent._last_cycle_equity == 1000.0
    assert agent.risk_gate.get_daily_stats(agent.agent_id)["loss"] == 0


@pytest.mark.asyncio
async def test_equity_drop_reports_loss_and_trips_kill_switch():
    cli = _BalanceStubCli(equity=1000)
    agent = _agent(cli, dry_run=False, max_daily_loss=500)
    day_key = agent.risk_gate.current_day_key(agent.agent_id)

    await agent._report_cycle_loss(day_key)
    cli.equity = 400  # -$600, past the $500 limit
    await agent._report_cycle_loss(day_key)

    stats = agent.risk_gate.get_daily_stats(agent.agent_id)
    assert stats["loss"] == pytest.approx(600.0)
    status = agent.risk_gate.kill_switch_status()
    assert status["active"] is True
    assert "Auto-triggered" in status["reason"]


@pytest.mark.asyncio
async def test_equity_gain_reports_nothing():
    cli = _BalanceStubCli(equity=1000)
    agent = _agent(cli, dry_run=False)
    day_key = agent.risk_gate.current_day_key(agent.agent_id)
    await agent._report_cycle_loss(day_key)
    cli.equity = 1100
    await agent._report_cycle_loss(day_key)
    assert agent.risk_gate.get_daily_stats(agent.agent_id)["loss"] == 0
    assert agent.risk_gate.kill_switch_status()["active"] is False


@pytest.mark.asyncio
async def test_failed_balance_read_never_kills_cycle():
    cli = _BalanceStubCli(equity=1000)
    agent = _agent(cli, dry_run=False)
    day_key = agent.risk_gate.current_day_key(agent.agent_id)
    await agent._report_cycle_loss(day_key)

    async def boom():
        raise RuntimeError("rpc down")

    cli.balance_all = boom  # type: ignore[method-assign]
    await agent._report_cycle_loss(day_key)  # must not raise
    # Baseline preserved from the last good reading.
    assert agent._last_cycle_equity == 1000.0
