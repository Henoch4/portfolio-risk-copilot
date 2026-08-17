"""Unit tests for src/curator.py — profile selection, allowlist, cooldown,
auto-revert, drawdown override, and default-passthrough env knobs.

No deps beyond PyYAML. Run: pytest tests/test_curator.py -v
"""
from pathlib import Path

from src.curator import CuratorAgent, apply_env_overrides

PROFILES_PATH = Path(__file__).resolve().parent.parent / "config" / "profiles.yaml"


def test_rejects_off_allowlist_profile():
    curator = CuratorAgent(PROFILES_PATH)
    log = curator.request_switch("aggressive_yolo", "bad idea", cycle=100, drawdown_breach=False)
    assert log["outcome"] == "rejected_not_on_allowlist"
    assert curator.state.current_profile == curator.default_profile


def test_applies_valid_switch_after_cooldown():
    curator = CuratorAgent(PROFILES_PATH)
    log = curator.request_switch("conservative", "high vol regime",
                                 cycle=curator.cooldown_cycles + 1, drawdown_breach=False)
    assert log["outcome"] == "applied"
    assert curator.state.current_profile == "conservative"


def test_cooldown_blocks_rapid_switching():
    curator = CuratorAgent(PROFILES_PATH)
    curator.request_switch("conservative", "r1", cycle=100, drawdown_breach=False)
    log2 = curator.request_switch("standard", "r2", cycle=101, drawdown_breach=False)
    assert log2["outcome"] == "rejected_cooldown_active"


def test_drawdown_breach_forces_defensive_overriding_curator_choice():
    curator = CuratorAgent(PROFILES_PATH)
    log = curator.request_switch("standard", "curator wants standard", cycle=100, drawdown_breach=True)
    assert log["outcome"] == "forced_defensive_by_drawdown_breaker"
    assert curator.state.current_profile == "defensive"


def test_auto_revert_on_underperformance():
    curator = CuratorAgent(PROFILES_PATH)
    curator.request_switch("conservative", "switch", cycle=curator.cooldown_cycles + 1,
                           drawdown_breach=False)
    assert curator.state.current_profile == "conservative"

    revert_log = None
    for i in range(curator.auto_revert_lookback_trades):
        revert_log = curator.record_trade_pnl(pnl=-0.01, cycle=curator.cooldown_cycles + 2 + i)

    assert revert_log is not None
    assert revert_log["event"] == "auto_revert"
    assert curator.state.current_profile == curator.default_profile


def test_active_profile_is_populated_and_bps_typed():
    curator = CuratorAgent(PROFILES_PATH)
    profile = curator.active_profile()
    assert profile["confidence_floor_bps"] in (7500, 6000, 9000)  # bps vocabulary, not 0-1
    assert 0 < profile["max_leverage"] <= 2.0
    assert isinstance(profile["enabled_signals"], list)


def test_passthrough_defaults_without_env():
    # No env override -> resolved knobs are exactly the profile's.
    curator = CuratorAgent(PROFILES_PATH)
    overrides = {k: None for k in ("KNOB_A", "KNOB_B")}
    resolved = apply_env_overrides(curator.active_profile(), overrides)
    assert resolved == curator.active_profile()


def test_passthrough_env_wins_per_knob_only():
    curator = CuratorAgent(PROFILES_PATH)
    profile = curator.active_profile()
    resolved = apply_env_overrides(
        profile,
        {"confidence_floor_bps": "8000", "max_leverage": None},  # leverage unset -> stays profile
        casters={"confidence_floor_bps": int, "max_leverage": float},
    )
    assert resolved["confidence_floor_bps"] == 8000        # env won
    assert resolved["max_leverage"] == profile["max_leverage"]  # untouched


def test_passthrough_bad_env_value_falls_back_to_profile():
    curator = CuratorAgent(PROFILES_PATH)
    profile = curator.active_profile()
    resolved = apply_env_overrides(
        profile,
        {"confidence_floor_bps": "not-a-number"},
        casters={"confidence_floor_bps": int},
    )
    assert resolved["confidence_floor_bps"] == profile["confidence_floor_bps"]


def test_passthrough_unknown_knob_is_ignored():
    curator = CuratorAgent(PROFILES_PATH)
    resolved = apply_env_overrides(curator.active_profile(), {"HACK_INJECTED": "0"})
    assert "HACK_INJECTED" not in resolved


def test_every_switch_is_recorded_in_history():
    curator = CuratorAgent(PROFILES_PATH)
    curator.request_switch("aggressive_yolo", "bad", cycle=1, drawdown_breach=False)
    curator.request_switch("conservative", "good", cycle=curator.cooldown_cycles + 1, drawdown_breach=False)
    outcomes = [h["outcome"] for h in curator.state.switch_history]
    assert outcomes == ["rejected_not_on_allowlist", "applied"]