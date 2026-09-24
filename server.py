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

async def handle_tg_webhook(request):
    """Handle incoming Telegram updates via Webhook."""
    app = request.app.get("tg_app")
    if not app:
        return web.Response(status=500)
    
    try:
        body = await request.json()
        from telegram import Update
        update = Update.de_json(body, app.bot)
        await app.process_update(update)
        return web.Response(status=200)
    except Exception as e:
        log.error(f"Webhook Error: {e}")
        return web.Response(status=200) # Always return 200 to Telegram

async def start_server(tg_app=None, port=8080):
    app = web.Application()
    app["tg_app"] = tg_app
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/api/stats", handle_api_stats)
    app.router.add_post("/tg-webhook", handle_tg_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    log.info(f"Health-check & Dashboard server starting on port {port}...")
    await site.start()
