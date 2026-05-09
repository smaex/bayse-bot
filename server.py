import logging
import os
from aiohttp import web

log = logging.getLogger("server")

# Shared state updated by bot.py
stats_cache = {
    "users": [],
    "oracles": {},
    "last_update": 0
}

async def handle_ping(request):
    return web.Response(text="pong", status=200)

async def handle_dashboard(request):
    try:
        with open("dashboard.html", "r") as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error loading dashboard: {e}", status=500)

async def handle_api_stats(request):
    # Simple password protection via query param
    password = request.query.get("pass")
    expected = os.environ.get("DASHBOARD_PASSWORD")
    
    if expected and password != expected:
        return web.Response(status=401, text="Unauthorized")

    return web.json_response(stats_cache)

async def start_server(port=8080):
    app = web.Application()
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/api/stats", handle_api_stats)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    log.info(f"Health-check & Dashboard server starting on port {port}...")
    await site.start()
