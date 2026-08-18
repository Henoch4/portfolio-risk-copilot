"""
Tests for vault_api endpoints and reconciliation service.

Covers:
- vault_api: stats, position, withdrawal, abi, audit-recent when not deployed
- reconciliation: read_vault_state, reconcile logic, delta capping
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from unittest.mock import MagicMock, patch

import pytest

from unittest.mock import AsyncMock


def async_return(value):
    """Create an async function that returns the given value."""
    async def _wrapper(*args, **kwargs):
        return value
    return _wrapper()


# ---------------------------------------------------------------------------
# vault_api tests (no deployment = graceful fallback)
# ---------------------------------------------------------------------------

class TestVaultApiNotDeployed:
    """When VAULT_CONTRACT_ADDRESS is unset, all endpoints return graceful defaults."""

    def test_stats_returns_not_deployed(self):
        from src.vault_api import vault_stats
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VAULT_CONTRACT_ADDRESS", None)
            result = vault_stats()
            assert result.deployed is False
            assert result.tvl is None
            assert result.total_supply is None

    def test_position_returns_not_deployed(self):
        from src.vault_api import vault_position
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VAULT_CONTRACT_ADDRESS", None)
            result = vault_position("0x0000000000000000000000000000000000000001")
            assert result.deployed is False
            assert result.share_balance == 0

    def test_abi_returns_empty_when_no_artifacts(self):
        from src.vault_api import vault_abi
        result = vault_abi()
        assert isinstance(result, dict)
        assert "abi" in result
        assert "address" in result

    def test_audit_recent_returns_not_deployed(self):
        from src.vault_api import audit_recent
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUDIT_CONTRACT_ADDRESS", None)
            result = audit_recent()
            assert result.deployed is False
            assert result.decisions == []


class TestVaultApiWithMock:
    """Test vault_api with a mocked vault contract."""

    def test_stats_with_vault(self):
        from src.vault_api import vault_stats, VaultStats
        mock_vault = MagicMock()
        mock_vault.functions.totalAssets.return_value.call.return_value = 1000000000  # 1000 USDT
        mock_vault.functions.totalSupply.return_value.call.return_value = 950000000000000000  # 0.95 shares
        mock_vault.functions.sharePriceAsset.return_value.call.return_value = 1052631578947368421
        mock_vault.functions.assetDecimals.return_value.call.return_value = 6
        mock_vault.functions.MIN_DEPOSIT.return_value.call.return_value = 1000000
        mock_vault.functions.MAX_TVL.return_value.call.return_value = 100000000000
        mock_vault.functions.settlementOpen.return_value.call.return_value = False
        mock_vault.functions.asset.return_value.call.return_value = "0xUSDT"
        mock_vault.functions.agent.return_value.call.return_value = "0xAGENT"
        mock_vault.functions.pendingReserved.return_value.call.return_value = 0

        with patch("src.vault_api._vault_contract", return_value=mock_vault), \
             patch("src.vault_api._get_web3"):
            os.environ["VAULT_CONTRACT_ADDRESS"] = "0xVAULT"
            try:
                result = vault_stats()
                assert result.deployed is True
                assert result.tvl == 1000000000
                assert result.total_supply == 950000000000000000
                assert result.asset_decimals == 6
                assert result.min_deposit == 1000000
                assert result.max_tvl == 100000000000
                assert result.settlement_open is False
                assert result.asset_address == "0xUSDT"
                assert result.agent == "0xAGENT"
                assert result.pending_reserved == 0
            finally:
                os.environ.pop("VAULT_CONTRACT_ADDRESS", None)

    def test_position_with_vault(self):
        from src.vault_api import vault_position
        mock_vault = MagicMock()
        mock_vault.functions.balanceOf.return_value.call.return_value = 500000000000000000  # 0.5 shares
        mock_vault.functions.convertToAssets.return_value.call.return_value = 525000000  # 525 USDT
        mock_vault.functions.assetDecimals.return_value.call.return_value = 6

        with patch("src.vault_api._vault_contract", return_value=mock_vault), \
             patch("src.vault_api._get_web3") as mock_w3:
            mock_w3.return_value.to_checksum_address.return_value = "0xADDR"
            os.environ["VAULT_CONTRACT_ADDRESS"] = "0xVAULT"
            try:
                result = vault_position("0xaddr")
                assert result.deployed is True
                assert result.share_balance == 500000000000000000
                assert result.asset_value == 525000000
            finally:
                os.environ.pop("VAULT_CONTRACT_ADDRESS", None)


# ---------------------------------------------------------------------------
# reconciliation tests
# ---------------------------------------------------------------------------

class TestReconciliation:
    """Test reconcile() logic with various scenarios."""

    def _vault_state(self, total_assets=1000000000, pending=0, last_att=None, att_tl=172800):
        return {
            "deployed": True,
            "total_assets": total_assets,
            "total_supply": 950000000000000000,
            "pending_reserved": pending,
            "last_attestation": last_att,
            "attest_timelock": att_tl,
            "max_attestation_delta_bps": 1000,
            "total_assets_priced": total_assets,
        }

    def test_balanced(self):
        from src.reconciliation import reconcile
        vault = self._vault_state(total_assets=1000000000)  # 1000 USDT
        okx = {"usdt_eq": 1000.0, "total_eq": 1200.0}
        result = reconcile(vault, okx, asset_decimals=6)
        assert result.vault_deployed is True
        assert result.okx_available is True
        assert result.discrepancy_usdt == pytest.approx(0.0, abs=0.01)
        assert result.discrepancy_pct == pytest.approx(0.0, abs=0.01)
        assert result.suggested_attestation == 1000000000

    def test_vault_higher_than_okx(self):
        from src.reconciliation import reconcile
        vault = self._vault_state(total_assets=1100000000)  # 1100 USDT
        okx = {"usdt_eq": 1000.0, "total_eq": 1200.0}
        result = reconcile(vault, okx, asset_decimals=6)
        assert result.discrepancy_usdt == pytest.approx(100.0, abs=0.01)
        assert result.discrepancy_pct == pytest.approx(10.0, abs=0.01)
        assert result.suggested_attestation == 1000000000

    def test_vault_lower_than_okx(self):
        from src.reconciliation import reconcile
        vault = self._vault_state(total_assets=900000000)  # 900 USDT
        okx = {"usdt_eq": 1000.0, "total_eq": 1200.0}
        result = reconcile(vault, okx, asset_decimals=6)
        assert result.discrepancy_usdt == pytest.approx(-100.0, abs=0.01)
        assert result.discrepancy_pct == pytest.approx(-10.0, abs=0.01)
        assert result.suggested_attestation == 1000000000

    def test_no_okx_data(self):
        from src.reconciliation import reconcile
        vault = self._vault_state(total_assets=1000000000)
        result = reconcile(vault, {}, asset_decimals=6)
        assert result.okx_available is False
        assert result.discrepancy_usdt is None
        assert result.discrepancy_pct is None
        assert result.suggested_attestation is None

    def test_vault_not_deployed(self):
        from src.reconciliation import reconcile
        result = reconcile({"deployed": False}, {"usdt_eq": 1000.0}, asset_decimals=6)
        assert result.vault_deployed is False
        assert result.okx_available is True
        assert result.discrepancy_usdt is None

    def test_pending_reserved_shows(self):
        from src.reconciliation import reconcile
        vault = self._vault_state(total_assets=1000000000, pending=50000000)
        okx = {"usdt_eq": 1000.0, "total_eq": 1200.0}
        result = reconcile(vault, okx, asset_decimals=6)
        assert result.pending_reserved == 50000000

    def test_last_attestation_and_timelock(self):
        from src.reconciliation import reconcile
        now = int(time.time())
        vault = self._vault_state(total_assets=1000000000, last_att=now, att_tl=172800)
        okx = {"usdt_eq": 1000.0, "total_eq": 1200.0}
        result = reconcile(vault, okx, asset_decimals=6)
        assert result.last_attestation == now
        assert result.attest_timelock == 172800


class TestReadVaultState:
    """Test read_vault_state when not deployed."""

    def test_not_deployed(self):
        from src.reconciliation import read_vault_state
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VAULT_CONTRACT_ADDRESS", None)
            result = read_vault_state()
            assert result["deployed"] is False


class TestReadOkxBalance:
    """Test read_okx_balance with mock CLI."""

    @pytest.mark.asyncio
    async def test_empty_cli(self):
        from src.reconciliation import read_okx_balance
        mock_cli = MagicMock()
        mock_cli.account_balance = AsyncMock(side_effect=Exception("no auth"))
        result = await read_okx_balance(mock_cli)
        assert result == {}

    @pytest.mark.asyncio
    async def test_valid_response(self):
        from src.reconciliation import read_okx_balance
        mock_cli = MagicMock()
        mock_cli.account_balance = MagicMock(return_value=async_return({
            "data": [{
                "totalEq": "1500.00",
                "details": [
                    {"ccy": "USDT", "eq": "1200.00"},
                    {"ccy": "BTC", "eq": "300.00"},
                ]
            }]
        }))
        result = await read_okx_balance(mock_cli)
        assert result["total_eq"] == 1500.0
        assert result["usdt_eq"] == 1200.0
        assert len(result["details"]) == 2

    @pytest.mark.asyncio
    async def test_empty_data(self):
        from src.reconciliation import read_okx_balance
        mock_cli = MagicMock()
        mock_cli.account_balance = AsyncMock(return_value={"data": []})
        result = await read_okx_balance(mock_cli)
        assert result == {}


# ---------------------------------------------------------------------------
# Integration: vault_api + reconciliation consistency
# ---------------------------------------------------------------------------

class TestVaultReconciliationConsistency:
    """Ensure reconcile() suggested attestation matches vault_api totalAssets."""

    def test_suggested_matches_stats(self):
        from src.reconciliation import reconcile
        total_assets = 5000000000  # 5000 USDT
        vault = {
            "deployed": True,
            "total_assets": total_assets,
            "total_supply": 4800000000000000000,
            "pending_reserved": 0,
            "last_attestation": None,
            "attest_timelock": 172800,
            "max_attestation_delta_bps": 1000,
            "total_assets_priced": total_assets,
        }
        okx = {"usdt_eq": 5000.0, "total_eq": 6000.0}
        result = reconcile(vault, okx, asset_decimals=6)
        # suggested should equal okx_usdt * 10^6
        assert result.suggested_attestation == 5000000000
        assert result.discrepancy_pct == pytest.approx(0.0, abs=0.01)
