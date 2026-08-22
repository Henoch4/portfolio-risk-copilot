"""Integration tests for the delta-neutral funding-arb package path.

Covers: funding-rate gate -> confidence sizing -> dual-leg risk gate -> both
legs logged onchain with ONE shared package_id -> concurrent dispatch through
the shared executor (LiveFillSimulator) -> resolve/settle.

Fully offline: OkxCli is stubbed and the OnchainLogger is a recorder stub so
the package_id shared by both leg payloads can be asserted.
"""
import pytest

from src.agent import AutonomousTradingAgent
from src.audit_logger import DecisionPayload
from src.execution import RiskGate
from src.multi_leg import MultiLegExecutionManager


class _StubCli:
    """Canned market feed. Configurable funding rate flips the arb gate;
    trade orders return a filled response so the non-dry-run executor path
    completes without a real OKX session."""

    def __init__(self, funding_rate="0.005"):
        self.funding_rate = funding_rate

    async def run(self, *args, **kwargs):
        cmd = args[:1]
        if cmd == ("market",) and "instruments" in args:
            inst_id = args[args.index("--instId") + 1] if "--instId" in args else ""
            if inst_id.endswith("-SWAP"):
                # ctVal 0.001 base at px 100 => $0.10/contract.
                return {"data": [{"instId": inst_id, "instType": "SWAP",
                                  "ctVal": "0.001", "ctValCcy": "BTC",
                                  "lotSz": "0.001", "minSz": "0.001"}]}
            return {"data": [{"instId": inst_id, "instType": "SPOT",
                              "ctVal": "1", "ctValCcy": "BTC",
                              "lotSz": "0.00000001", "minSz": "0.00000001"}]}
        if cmd == ("market",) and "trades" in args:
            return {"data": [{"px": "100", "sz": "1"}]}
        if cmd == ("market",) and "funding-rate" in args:
            return {"data": [{"fundingRate": self.funding_rate}]}
        if cmd == ("account",) and "positions" in args:
            return {"data": []}
        if cmd == ("trade",) and "order" in args:
            # Executor passes --instId, --side, --sz, etc. Return a filled fill
            # whose fillPx (100) equals the market reference (100) -> 0% slippage,
            # so the package locks cleanly instead of unwinding on a fake breach.
            return {"data": [{
                "ordId": "ord-stub",
                "clOrdId": "",
                "state": "filled",
                "accFillSz": "1",
                "fillPx": "100",
                "fillSz": "1",
                "fillUsd": "100",
                "fee": "0",
                "feeCcy": "USDT",
            }]}
        return {"data": []}


class _RecordingLogger:
    """OnchainLogger substitute that captures DecisionPayloads instead of
    hitting an RPC, so the shared package_id on both legs is assertable."""

    def __init__(self):
        self.logged: list[DecisionPayload] = []
        self.agent_address = "0x" + "11" * 20

    def log_decision(self, payload: DecisionPayload) -> str:
        self.logged.append(payload)
        return f"0xtx_{len(self.logged)}"

    def record_execution(self, decision_id, fill_price, fill_size_usd,
                         fee_usd, success) -> str:
        return "0xtx_exec"


def _agent(**overrides):
    kwargs = dict(
        okx_cli=_StubCli(),
        risk_gate=RiskGate(max_position_usd=5000),
        dry_run=True,
        max_position_usd=5000,
        multi_leg_manager=MultiLegExecutionManager(),
        funding_arb_min_rate=0.001,
    )
    kwargs.update(overrides)
    return AutonomousTradingAgent(**kwargs)


@pytest.mark.asyncio
async def test_arbitrage_opportunity_gate():
    """Opportunity truth table for _funding_arb_opportunity."""
    md_pos = {"funding_rate": 0.005, "timestamp": 0.0, "position": None}
    md_neg = {"funding_rate": -0.005, "timestamp": 0.0, "position": None}
    md_low = {"funding_rate": 0.0005, "timestamp": 0.0, "position": None}

    assert _agent(multi_leg_manager=None)._funding_arb_opportunity("BTC-USDT-SWAP", md_pos) is False
    assert _agent(funding_arb_min_rate=0.01)._funding_arb_opportunity("BTC-USDT-SWAP", md_pos) is False
    assert _agent()._funding_arb_opportunity("BTC-USDT-SWAP", md_neg) is False
    assert _agent()._funding_arb_opportunity("BTC-USDT-SWAP", md_low) is False
    assert _agent()._funding_arb_opportunity("BTC-USDT-SWAP", md_pos) is True


@pytest.mark.asyncio
async def test_arb_package_logs_both_legs_with_shared_package_id():
    """Both legs are onchained with the SAME package_id — the linkage that
    ties the atomic package together on-chain."""
    logger = _RecordingLogger()
    agent = _agent(onchain_logger=logger, dry_run=False)
    await agent.run_trading_cycle(["BTC-USDT-SWAP"])

    assert len(logger.logged) == 2, f"expected 2 legs logged, got {len(logger.logged)}"
    pkgs = [p.package_id for p in logger.logged]
    assert pkgs[0] is not None
    assert pkgs[0] == pkgs[1], "both legs must share one package_id"
    assets = {p.asset for p in logger.logged}
    assert assets == {"BTC-USDT-SWAP", "BTC-USDT"}
    short = next(p for p in logger.logged if p.is_short)
    long = next(p for p in logger.logged if not p.is_short)
    assert short.asset == "BTC-USDT-SWAP"
    assert long.asset == "BTC-USDT"


@pytest.mark.asyncio
async def test_arb_package_dispatches_and_locks():
    """End-to-end: dispatch through the shared executor, both legs fill at the
    reference price, the package resolves LOCKED and settles."""
    logger = _RecordingLogger()
    agent = _agent(onchain_logger=logger, dry_run=False)
    result = await agent.run_trading_cycle(["BTC-USDT-SWAP"])

    assert not any("risk gate rejected" in e or "package" in e.lower()
                   and "ended" in e for e in result.errors), (
        f"unexpected package failure: {result.errors}"
    )
    # two legs executed for the same package_id
    execs = [e for e in result.executions]
    assert len(execs) == 2
    assert {e["asset"] for e in execs} == {"BTC-USDT-SWAP", "BTC-USDT"}
    assert {e["package_id"] for e in execs} == {execs[0]["package_id"]}
    assert all(e["status"] == "success" for e in execs)


@pytest.mark.asyncio
async def test_arb_falls_through_to_directional_on_negative_funding():
    """Negative funding -> no arb package -> the directional signal path is
    reached (signals generated) and no funding_arbitrage decision is made.
    """
    agent = _agent()
    agent.cli = _StubCli(funding_rate="-0.005")
    result = await agent.run_trading_cycle(["BTC-USDT-SWAP"])
    # signals were generated (directional path reached, not short-circuited)
    assert result.signals, "negative funding should reach the directional signal path"
    # the arbitrage package did NOT fire
    arb_decisions = [d for d in result.decisions if d.get("strategy") == "funding_arbitrage"]
    assert arb_decisions == [], "no funding_arbitrage decision when funding is negative"


@pytest.mark.asyncio
async def test_arb_aborted_when_leg_rejected_by_risk_gate():
    """A leg rejected by the risk gate (here the confidence floor) must fail
    the whole package closed: no onchain log, no execution."""
    logger = _RecordingLogger()
    agent = _agent(
        onchain_logger=logger,
        dry_run=False,
        risk_gate=RiskGate(max_position_usd=5000, min_confidence_bps=9500),
        # funding 0.005 -> conf 9000 bps < 9500 floor -> gate rejects both legs
    )
    result = await agent.run_trading_cycle(["BTC-USDT-SWAP"])
    assert logger.logged == [], "no decision should be onchained when the gate rejects"
    assert any("risk gate rejected" in e for e in result.errors)


@pytest.mark.asyncio
async def test_arb_aborted_when_not_a_perp():
    """Non-SWAP instruments have no perp to hedge against and don't run the
    package; they fall through to the directional path."""
    agent = _agent()
    agent.cli = _StubCli(funding_rate="0.05")
    result = await agent.run_trading_cycle(["BTC-USDT"])  # spot, not a perp
    assert any("no spot pair" in e for e in result.errors)
