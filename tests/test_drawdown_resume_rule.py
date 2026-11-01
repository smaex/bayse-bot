"""One drawdown resume rule, shared by risk.check_drawdown and the user loop."""
import risk as risk_mod
from risk import RiskManager


def _paused_manager(peak=10_000.0):
    r = RiskManager()
    r.peak_balance = peak
    r.paused = True
    r.pause_reason = "drawdown"
    return r


def test_resume_threshold_is_stop_times_single_fraction():
    assert RiskManager.drawdown_resume_threshold() == risk_mod.MAX_DRAWDOWN_STOP * risk_mod.DRAWDOWN_RESUME_FRACTION


def test_drawdown_pause_holds_while_still_above_resume_threshold():
    r = _paused_manager()
    r.check_drawdown(9_600.0)  # 4% drawdown, above the 2.5% resume line
    assert r.paused is True


def test_drawdown_pause_releases_below_resume_threshold():
    r = _paused_manager()
    r.check_drawdown(9_800.0)  # 2% drawdown
    assert r.paused is False
    assert r.pause_reason == ""


def test_changing_the_fraction_changes_every_resume_path(monkeypatch):
    monkeypatch.setattr(risk_mod, "DRAWDOWN_RESUME_FRACTION", 0.5)
    r = _paused_manager()
    r.check_drawdown(9_600.0)  # 4%: now below the 5% line
    assert r.paused is False


def test_operator_pause_is_never_cleared_by_drawdown_recovery():
    r = _paused_manager()
    r.pause_reason = "manual"
    r.check_drawdown(9_900.0)
    assert r.paused is True
