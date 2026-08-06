
    async def cancel_all(self, client, market_id: str = None):
        """Cancel all open maker orders (called on vol spike or market close)."""
        targets = {market_id: self.open_orders[market_id]} if market_id and market_id in self.open_orders else dict(self.open_orders)
        for mid, info in list(targets.items()):
            try:
                await client.cancel_order(info["order_id"])
                log.info(f"MAKER cancelled order {info['order_id']} on {mid}")
            except Exception as e:
                log.warning(f"MAKER cancel failed for {info['order_id']}: {e}")
            self.open_orders.pop(mid, None)
    def track_order(self, market_id: str, order_id: str, placed_price: float,
                    binance_price: float, amount: float, outcome_id: str):
        """Called by executor after a LIMIT order is placed."""
        self.open_orders[market_id] = {
            "order_id":       order_id,
            "placed_price":   placed_price,
            "binance_at_place": binance_price,
            "amount":         amount,
            "outcome_id":     outcome_id,
            "placed_at":      time.time(),
        }
    def should_requote(self, market_id: str) -> bool:
        """True if Binance has moved enough that our quote is stale."""
        info = self.open_orders.get(market_id)
        if not info:
            return False
        asset  = None
        # We don't store asset in open_orders, so re-check via direct_spot
        # by comparing any asset's price move as a proxy.
        for a in ("BTC", "ETH", "SOL"):
            price_now, t = feeds_direct.get_direct_price(a)
            if price_now and (time.time() - t) < 5:
                base = info.get("binance_at_place", price_now)
                if base > 0 and abs(price_now - base) / base > REQUOTE_THRESHOLD:
                    return True
        return False
# Singleton used by executor.py and bot.py
maker_strategy = MakerStrategy()
