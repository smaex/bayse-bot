"""Orphaned complete-set leg: the exit policy sells it once the retry window closes."""

import bot


def test_orphan_exit_due_only_for_a_flagged_unpaired_position():
    assert bot._orphan_exit_due({"orphan_exit_due": True}, paired=None) is True
    assert bot._orphan_exit_due({"orphan_exit_due": True}, paired={"x": 1}) is False
    assert bot._orphan_exit_due({}, paired=None) is False
    assert bot._orphan_exit_due({"orphan_exit_due": False}, paired=None) is False
