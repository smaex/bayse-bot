import logging
from aiohttp import web

log = logging.getLogger("server")

async def handle_ping(request):
    return web.json_response({"status": "alive", "service": "bayse-bot"})

async def start_server(port: int = 8080):
    app = web.Application()
    app.add_routes([web.get("/ping", handle_ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Health-check server running on port {port}")
