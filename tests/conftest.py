import pytest

import database


@pytest.fixture(autouse=True)
def _clear_daily_pnl_cache():
    """The daily PnL read is cached in-process; never let one test's figure leak into another."""
    database._invalidate_daily_pnl()
    yield
    database._invalidate_daily_pnl()
