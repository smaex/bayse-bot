import database
import logging

log = logging.getLogger("recorder")

def record_market_snapshot(markets: list):
    """Saves a snapshot of all active markets with their current AMM prices to the DB."""
    data = [
        {
            "market_id": m.get("market_id"),
            "asset": m.get("asset"),
            "timeframe": m.get("timeframe"),
            "threshold": m.get("threshold"),
            "yes_price": m.get("yes_price"),
            "no_price": m.get("no_price"),
            "secs_to_close": m.get("secs_to_close"),
            "expiry_time": m.get("expiry_time")
        }
        for m in markets
    ]
    database.save_recording("market_snapshot", data)

def record_spot_tick(asset: str, price: float):
    """Saves a spot price update to the DB."""
    data = {"price": price}
    database.save_recording("spot_tick", data, asset=asset)

log.info("Persistent Database Recorder initialized.")
