"""Daily gain lock with giveback limit (2026-11-01 request).

Wins must not reset the loss baseline. Instead, once the day's realised gain
reaches an arm level, the bot keeps at least a set fraction of its peak gain.
If the day gives back below that floor, new entries pause until the next
trading day or an explicit /resume. Losses always count against the day.
"""

import pytest

import ops_controls as ops


ARM = 0.02    # arm at 2% of day start balance
KEEP = 0.5    # keep at least half of the peak gain


def _state(session, peak, start=2698.43):
    return ops.gain_lock_state(session, peak, start, ARM, KEEP)


def test_no_lock_below_arm_level():
    new_peak, locked, floor = _state(session=30.0, peak=0.0)  # arm = 53.97
    assert new_peak == 30.0
    assert locked is False
    assert floor is None


def test_arms_once_peak_reaches_arm_level_and_reports_floor():
    new_peak, locked, floor = _state(session=60.0, peak=0.0)
    assert new_peak == 60.0
    assert locked is False, "at the peak the lock is armed but not yet triggered"
    assert floor == pytest.approx(30.0)


def test_triggers_when_gain_gives_back_below_floor():
    new_peak, locked, floor = _state(session=29.0, peak=60.0)
    assert new_peak == 60.0, "peak never falls"
    assert locked is True
    assert floor == pytest.approx(30.0)


def test_does_not_trigger_above_floor():
    _, locked, _ = _state(session=45.0, peak=60.0)
    assert locked is False


def test_peak_only_rises():
    new_peak, _, _ = _state(session=10.0, peak=60.0)
    assert new_peak == 60.0
    new_peak, _, _ = _state(session=90.0, peak=60.0)
    assert new_peak == 90.0


def test_losses_are_counted_once_armed():
    # Gained ₦60, then lost to ₦-20: a loss, and below the floor.
    _, locked, _ = _state(session=-20.0, peak=60.0)
    assert locked is True


def test_disabled_when_arm_fraction_not_positive():
    new_peak, locked, floor = ops.gain_lock_state(500.0, 900.0, 2698.43, 0.0, KEEP)
    assert locked is False and floor is None


def test_no_lock_without_a_valid_start_balance():
    _, locked, _ = ops.gain_lock_state(60.0, 60.0, 0.0, ARM, KEEP)
    assert locked is False


def test_config_defaults_are_conservative():
    import config
    assert 0.0 < config.GAIN_LOCK_ARM_FRACTION <= 0.05
    assert 0.0 < config.GAIN_LOCK_KEEP_FRACTION < 1.0


def test_gain_giveback_lifts_at_new_trading_day():
    import bot
    assert "gain_giveback" in bot._SESSION_PAUSE_REASONS
