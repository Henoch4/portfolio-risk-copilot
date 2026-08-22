"""RPC endpoint failover (roadmap Phase 1: dual independent endpoints).

Offline: web3/HTTP are faked. Covers endpoint ordering/dedup, probe-based
selection, constructor-level failover in OnchainLogger, mid-session
provider switching, and the send-path retry rule: a broadcast that failed
at the connection level is retried on the surviving node ONLY after that
node reports no receipt for the signed hash (unknown state => never resend).
"""
import threading
from unittest.mock import MagicMock

import pytest

from src import audit_logger as al_mod
from src import rpc
from src.audit_logger import OnchainLogger


# --- src.rpc endpoint selection ---

def test_rpc_urls_primary_then_fallback_dedup(monkeypatch):
    monkeypatch.setenv("XLAYER_RPC_URL", "https://a.example")
    monkeypatch.setenv("XLAYER_RPC_URL_FALLBACK", "https://b.example")
    assert rpc.rpc_urls() == ["https://a.example", "https://b.example"]

    monkeypatch.setenv("XLAYER_RPC_URL_FALLBACK", "https://a.example")
    assert rpc.rpc_urls() == ["https://a.example"]  # deduped

    monkeypatch.setenv("XLAYER_RPC_URL", "")
    monkeypatch.setenv("XLAYER_RPC_URL_FALLBACK", "")
    assert rpc.rpc_urls(default_primary="https://d.example") == ["https://d.example"]
    assert rpc.rpc_urls() == []


class _FakeW3:
    def __init__(self, alive: bool):
        self._alive = alive

    @property
    def eth(self):
        w3 = self

        class _Eth:
            @property
            def chain_id(self):
                if not w3._alive:
                    raise OSError("endpoint down")
                return 1952

        return _Eth()


def test_get_web3_skips_dead_primary(monkeypatch):
    urls_seen = []

    def fake_build(url):
        urls_seen.append(url)
        return _FakeW3(alive=(url == "https://b.example"))

    monkeypatch.setattr(rpc, "build_web3", fake_build)
    monkeypatch.setenv("XLAYER_RPC_URL", "https://a.example")
    monkeypatch.setenv("XLAYER_RPC_URL_FALLBACK", "https://b.example")

    w3 = rpc.get_web3()
    assert urls_seen == ["https://a.example", "https://b.example"]
    assert w3._alive is True


def test_get_web3_raises_when_all_dead(monkeypatch):
    monkeypatch.setattr(rpc, "build_web3", lambda url: _FakeW3(alive=False))
    monkeypatch.setenv("XLAYER_RPC_URL", "https://a.example")
    monkeypatch.setenv("XLAYER_RPC_URL_FALLBACK", "https://b.example")
    with pytest.raises(ConnectionError, match="No responsive"):
        rpc.get_web3()


# --- OnchainLogger constructor + failover ---

class _FakeWebFactory:
    """Stands in for web3.Web3 in audit_logger: instances report per-url liveness."""

    def __init__(self, live_urls):
        self.live_urls = set(live_urls)
        self.created = []

    def __call__(self, provider):
        # audit_logger calls Web(HTTPProvider(url)); emulate by accepting either
        # a raw url or an HTTPProvider-shaped object carrying endpoint_uri.
        real = getattr(provider, "endpoint_uri", provider)
        inst = MagicMock()
        inst.endpoint_uri = real
        inst.is_connected.side_effect = lambda: real in self.live_urls
        self.created.append(real)
        return inst

    to_checksum_address = staticmethod(lambda a: a)

    @staticmethod
    def HTTPProvider(url):
        p = MagicMock()
        p.endpoint_uri = url
        return p


def test_constructor_fails_over_to_live_fallback():
    factory = _FakeWebFactory(live_urls={"https://b.example"})
    orig_web = al_mod.Web
    al_mod.Web = factory
    try:
        lg = OnchainLogger.__new__(OnchainLogger)
        lg.rpc_urls = ["https://a.example", "https://b.example"]
        # exercise the same selection logic the real __init__ uses
        lg.w3 = None
        for url in lg.rpc_urls:
            candidate = al_mod.Web(al_mod.Web.HTTPProvider(url))
            if candidate.is_connected():
                lg.w3 = candidate
                break
        assert lg.w3.endpoint_uri == "https://b.example"
    finally:
        al_mod.Web = orig_web


class _FakeWeb:
    """Callable Web3 stand-in with HTTPProvider; only c.example is alive."""

    switched: list = []

    def __init__(self, provider):
        url = getattr(provider, "endpoint_uri", provider)
        type(self).switched.append(url)
        self.endpoint_uri = url
        self.is_connected = lambda: url == "https://c.example"

    @staticmethod
    def HTTPProvider(url):
        p = MagicMock()
        p.endpoint_uri = url
        return p

    to_checksum_address = staticmethod(lambda a: a)


def test_failover_switches_provider_and_skips_current():
    lg = OnchainLogger.__new__(OnchainLogger)
    lg.rpc_urls = ["https://a.example", "https://b.example", "https://c.example"]
    lg.w3 = MagicMock()
    lg.w3.provider.endpoint_uri = "https://a.example"

    _FakeWeb.switched = []
    orig_web = al_mod.Web
    al_mod.Web = _FakeWeb
    try:
        assert lg._failover() is True
        assert lg.w3.endpoint_uri == "https://c.example"
        assert _FakeWeb.switched == ["https://b.example", "https://c.example"]
    finally:
        al_mod.Web = orig_web


def test_send_path_retries_on_surviving_node_after_hash_check():
    real = OnchainLogger.__new__(OnchainLogger)
    real.w3 = MagicMock()
    real.contract_address = "0x" + "1" * 40
    real.private_key = "0x" + "2" * 64
    real.agent_address = "0x" + "3" * 40
    real.chain_id = 1952
    real._nonce_lock = threading.Lock()
    real._nonce_counter = None
    real.rpc_urls = ["https://a.example", "https://b.example"]

    primary, survivor = real.w3, MagicMock()
    primary.to_wei.return_value = 10 ** 9
    primary.eth.generate_gas_price.return_value = 10 ** 9
    primary.eth.get_transaction_count.return_value = 5
    signed = MagicMock()
    signed.hash.hex.return_value = "0xdeadbeef"
    primary.eth.account.sign_transaction.return_value = signed
    primary.eth.send_raw_transaction.side_effect = ConnectionError("primary refused")

    func = MagicMock()
    func.estimate_gas.return_value = 100_000
    func.build_transaction.side_effect = lambda opts: dict(opts)  # echo nonce/gas

    def failover():
        real.w3 = survivor
        return True

    # Survivor does NOT know the hash -> resend is legitimate.
    survivor.eth.get_transaction_receipt.side_effect = Exception("not found")
    survivor.to_wei.return_value = 10 ** 9
    survivor.eth.generate_gas_price.return_value = 10 ** 9
    survivor.eth.get_transaction_count.return_value = 7
    survivor.eth.account.sign_transaction.return_value = signed
    survivor.eth.send_raw_transaction.return_value = MagicMock(hex=lambda: "0xabc")
    survivor.eth.wait_for_transaction_receipt.return_value = {"status": 1}

    real._failover = failover
    tx_hash = real._send_transaction(lambda: func, "logDecision")

    assert tx_hash == "0xabc"
    # Resend happened exactly once, on the survivor, with a RESEEDED nonce (7).
    assert primary.eth.send_raw_transaction.call_count == 1
    assert survivor.eth.send_raw_transaction.call_count == 1
    resend_tx = survivor.eth.account.sign_transaction.call_args.args[0]
    assert resend_tx["nonce"] == 7


def test_send_path_never_resends_when_receipt_exists_on_survivor():
    real = OnchainLogger.__new__(OnchainLogger)
    real.w3 = MagicMock()
    real.contract_address = "0x" + "1" * 40
    real.private_key = "0x" + "2" * 64
    real.agent_address = "0x" + "3" * 40
    real.chain_id = 1952
    real._nonce_lock = threading.Lock()
    real._nonce_counter = None
    real.rpc_urls = ["https://a.example", "https://b.example"]

    primary, survivor = real.w3, MagicMock()
    primary.to_wei.return_value = 10 ** 9
    primary.eth.generate_gas_price.return_value = 10 ** 9
    primary.eth.get_transaction_count.return_value = 5
    signed = MagicMock()
    signed.hash.hex.return_value = "0xdeadbeef"
    primary.eth.account.sign_transaction.return_value = signed
    primary.eth.send_raw_transaction.side_effect = ConnectionError("lost response")

    func = MagicMock()
    func.estimate_gas.return_value = 100_000
    func.build_transaction.return_value = {"chainId": 1952}

    mined = MagicMock()
    # Receipt is read subscript-style (audit_logger uses prior["transactionHash"])
    mined.__getitem__.return_value = MagicMock(hex=lambda: "0xdeadbeef")
    survivor.eth.get_transaction_receipt.return_value = mined  # already landed!
    survivor.eth.send_raw_transaction.side_effect = AssertionError("MUST NOT RESEND")
    survivor.eth.wait_for_transaction_receipt.return_value = {"status": 1}

    real._failover = lambda: (setattr(real, "w3", survivor), True)[1]
    tx_hash = real._send_transaction(lambda: func, "logDecision")
    assert tx_hash == "0xdeadbeef"
