import asyncio
import json
import logging
import time
from collections import deque
import websockets
import config

log = logging.getLogger("feeds.hardened")

class HardenedWebsocketPool:
    def __init__(self, url, pool_size: int, sub_payload: dict, message_handler, dedup_key_fn):
        self.url = url  # Can be a string or list of strings
        self.pool_size = pool_size
        self.sub_payload = sub_payload
        self.message_handler = message_handler
        self.dedup_key_fn = dedup_key_fn
        self.current_url_idx = 0
        
        self.tasks = {}
        self.is_running = False
        
        # Deduplication cache: key -> timestamp of first arrival
        self.seen_messages = {}
        
        # Connection metrics
        self.conn_start_times = {}  # conn_id -> timestamp
        self.last_tick_time = {}    # conn_id -> timestamp
        self.jitter_ema = {}        # conn_id -> float (arrival delay relative to fastest)
        self.respawn_counts = []    # list of timestamps of respawns

    async def start(self):
        self.is_running = True
        # Layer 5: Stagger connection starts evenly across a 1-second window
        stagger_delay = 1.0 / self.pool_size
        for conn_id in range(self.pool_size):
            self.jitter_ema[conn_id] = 0.0
            self.last_tick_time[conn_id] = 0.0
            task = asyncio.create_task(self._conn_loop(conn_id))
            self.tasks[conn_id] = task
            log.info(f"HardenedWS Pool: Staggering connection {conn_id} by {conn_id * stagger_delay:.2f}s")
            await asyncio.sleep(stagger_delay)
            
        # Start the culling background task
        asyncio.create_task(self._cull_loop())

    async def stop(self):
        self.is_running = False
        for conn_id, task in list(self.tasks.items()):
            task.cancel()
        self.tasks.clear()

    async def _conn_loop(self, conn_id: int):
        backoff = 1
        while self.is_running:
            self.conn_start_times[conn_id] = time.time()
            self.last_tick_time[conn_id] = 0.0
            is_first_tick = True  # Layer 4: Drop the first tick from every new connection
            
            url = self.url[self.current_url_idx] if isinstance(self.url, list) else self.url
            try:
                log.debug(f"HardenedWS Pool: Connection {conn_id} connecting to {url}")
                async with websockets.connect(url, ping_interval=20) as ws:
                    log.info(f"HardenedWS Pool: Connection {conn_id} successfully connected to {url}")
                    backoff = 1
                    
                    if self.sub_payload:
                        await ws.send(json.dumps(self.sub_payload))
                        
                    async for raw in ws:
                        arrival_time = time.time()
                        self.last_tick_time[conn_id] = arrival_time
                        
                        # Layer 4: Drop first tick to avoid stale snapshots
                        if is_first_tick:
                            is_first_tick = False
                            log.debug(f"HardenedWS Pool: Connection {conn_id} dropped first tick snapshot.")
                            continue
                            
                        # Handle potential multi-line streaming messages (e.g. TwelveData splits by newline)
                        for line in raw.strip().split("\n"):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                msg = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                                
                            key = self.dedup_key_fn(msg)
                            if not key:
                                # If it doesn't represent a standard price tick, pass to handler directly
                                self.message_handler(msg)
                                continue
                                
                            # Deduplication check (Layer 2)
                            if key in self.seen_messages:
                                first_arrival = self.seen_messages[key]
                                delay = arrival_time - first_arrival
                                
                                # Layer 6: Track arrival delay relative to the fastest socket via EMA
                                alpha = 0.1
                                self.jitter_ema[conn_id] = alpha * delay + (1.0 - alpha) * self.jitter_ema[conn_id]
                                continue
                            
                            # First time seeing this update!
                            self.seen_messages[key] = arrival_time
                            self.jitter_ema[conn_id] = 0.9 * self.jitter_ema[conn_id] # Encourage fast delivery
                            
                            # Clean up old seen keys to prevent memory leaks
                            if len(self.seen_messages) > 1000:
                                cutoff = arrival_time - 10.0
                                for k, t in list(self.seen_messages.items()):
                                    if t < cutoff:
                                        self.seen_messages.pop(k, None)
                                        
                            # Dispatch to message handler
                            self.message_handler(msg)
                            
            except asyncio.CancelledError:
                log.info(f"HardenedWS Pool: Connection {conn_id} cancelled.")
                break
            except Exception as e:
                if "451" in str(e) and isinstance(self.url, list) and len(self.url) > 1:
                    old_idx = self.current_url_idx
                    self.current_url_idx = (self.current_url_idx + 1) % len(self.url)
                    log.warning(f"HardenedWS Pool: Geoblocked (status 451). Rotating endpoint to {self.url[self.current_url_idx]}")
                log.warning(f"HardenedWS Pool: Connection {conn_id} disconnected/error: {e}. Reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _cull_loop(self):
        """Layer 6: Periodic Jitter EMA monitoring and slowest connection culling."""
        while self.is_running:
            await asyncio.sleep(config.WS_CULL_INTERVAL_SEC)
            
            now = time.time()
            eligible = []
            for conn_id in range(self.pool_size):
                start_t = self.conn_start_times.get(conn_id, 0)
                # Must be alive for at least 8 seconds and have received at least one tick
                if now - start_t >= 8.0 and self.last_tick_time.get(conn_id, 0.0) > 0.0:
                    eligible.append(conn_id)
                    
            if not eligible:
                continue
                
            # Clean up old respawn timestamps from the rolling 60-second limit
            self.respawn_counts = [t for t in self.respawn_counts if now - t < 60.0]
            
            # Find the worst performing connection (highest jitter EMA score)
            worst_conn = max(eligible, key=lambda cid: self.jitter_ema.get(cid, 0.0))
            worst_score = self.jitter_ema.get(worst_conn, 0.0)
            
            limit_sec = config.WS_MAX_JITTER_MS / 1000.0
            if worst_score > limit_sec:
                # Max 20 respawns per minute
                if len(self.respawn_counts) < 20:
                    log.info(f"HardenedWS Pool: Culling worst connection {worst_conn} (Jitter EMA: {worst_score:.4f}s > {limit_sec}s)")
                    self.tasks[worst_conn].cancel()
                    self.tasks[worst_conn] = asyncio.create_task(self._conn_loop(worst_conn))
                    self.respawn_counts.append(now)
                else:
                    log.warning("HardenedWS Pool: Culling suppressed due to minute rate limit.")


# ── Layer 1 (Window Validation) and Layer 3 (Delta Guard) ──────────────────────

def validate_ticks_window(asset: str, price_history, duration_sec: float = 5.0, min_ticks: int = None, max_jump_pct: float = None) -> bool:
    """
    Layer 1: Monitor final seconds before window opens.
    Requires at least min_ticks per token, with no single jump above max_jump_pct.
    """
    if min_ticks is None:
        min_ticks = config.WS_MIN_TICKS_WINDOW
    if max_jump_pct is None:
        max_jump_pct = config.WS_MAX_TICK_JUMP_PCT
        
    now = time.time()
    # price_history is a list or deque of (timestamp, price) tuples
    recent_ticks = [price for t, price in price_history if now - t <= duration_sec]
    
    if len(recent_ticks) < min_ticks:
        log.warning(f"Layer 1 Validation Failed: {asset} has only {len(recent_ticks)} ticks in last {duration_sec}s (need {min_ticks})")
        return False
        
    # Check for consecutive jumps
    for i in range(1, len(recent_ticks)):
        prev_p = recent_ticks[i - 1]
        curr_p = recent_ticks[i]
        if prev_p > 0:
            jump = abs(curr_p - prev_p) / prev_p
            if jump > max_jump_pct:
                log.warning(f"Layer 1 Validation Failed: {asset} price jumped too fast: {jump:.4%} > {max_jump_pct:.4%}")
                return False
                
    return True


def check_delta_limit(symbol: str, price: float, baseline_price: float, limit_pct: float = None) -> bool:
    """
    Layer 3: Stale Data Guard.
    Reject tick if price exceeds delta limit relative to baseline price.
    """
    if limit_pct is None:
        limit_pct = config.WS_WARMUP_DELTA_LIMIT
        
    if baseline_price <= 0.0:
        return True
        
    delta = abs(price - baseline_price) / baseline_price
    if delta > limit_pct:
        log.warning(f"Layer 3 Guard: Rejected tick for {symbol} ({price:.4f}). Deviated {delta:.4%} > limit {limit_pct:.4%} from baseline ({baseline_price:.4f})")
        return False
        
    return True
