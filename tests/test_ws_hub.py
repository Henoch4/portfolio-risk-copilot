"""The live cycle hub must fan out to all clients and drop ones that error,
without raising (a stuck client must never break the HTTP /trade path)."""
import pytest

from src.main import _CycleWebSocketHub


class _FakeWS:
    def __init__(self):
        self.sent = []
        self._gone = False

    async def send_json(self, payload):
        if self._gone:
            raise RuntimeError("closed")
        self.sent.append(payload)


class _BrokenWS(_FakeWS):
    async def send_json(self, payload):
        raise RuntimeError("client gone")


@pytest.mark.asyncio
async def test_hub_fans_out_and_drops_broken():
    hub = _CycleWebSocketHub()
    good = _FakeWS()
    bad = _BrokenWS()
    hub._connections = {good, bad}

    await hub.broadcast({"cycle_id": "abc"})

    assert good.sent == [{"cycle_id": "abc"}]
    # The broken socket was removed so a future broadcast can't crash.
    assert bad not in hub._connections
    assert good in hub._connections


@pytest.mark.asyncio
async def test_hub_handles_empty():
    hub = _CycleWebSocketHub()
    await hub.broadcast({"x": 1})  # no connections => no-op, no raise
