"""Small observability server used by the platform health checks.

This server is deliberately independent of Telegram polling.  ``/live`` only
answers whether the process and event loop are alive; ``/ready`` answers
whether startup completed and the bot can service users.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from pathlib import Path

from aiohttp import web

import health

log = logging.getLogger("server")
_DASHBOARD = Path(__file__).with_name("dashboard.html")

# Shared state updated by bot.py.  Do not put credentials in this object.
stats_cache = {
    "users": [],
    "oracles": {},
    "last_update": 0,
}


def _security_headers(response: web.StreamResponse) -> web.StreamResponse:
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        }
    )
    return response


async def handle_live(_request: web.Request) -> web.Response:
    return _security_headers(web.json_response({"status": "live"}))


async def handle_ready(_request: web.Request) -> web.Response:
    ready, reasons, snapshot = health.readiness()
    status = 200 if ready else 503
    # Health probes are public.  Expose ages, never raw exception strings or
    # arbitrary component details that could contain request data.
    component_ages = {
        name: item.get("age_sec")
        for name, item in snapshot.get("components", {}).items()
    }
    return _security_headers(
        web.json_response(
            {
                "status": "ready" if ready else "starting",
                "uptime_sec": snapshot.get("uptime_sec"),
                "components": component_ages,
                "issues": len(reasons),
            },
            status=status,
        )
    )


async def handle_dashboard(_request: web.Request) -> web.Response:
    try:
        content = await asyncio.to_thread(_DASHBOARD.read_text, encoding="utf-8")
        return _security_headers(web.Response(text=content, content_type="text/html"))
    except Exception:
        log.exception("Dashboard load failed")
        return _security_headers(web.Response(text="Dashboard unavailable", status=500))


def _dashboard_authorized(request: web.Request) -> bool:
    expected = os.environ.get("DASHBOARD_PASSWORD", "")
    supplied = request.headers.get("Authorization", "")
    if supplied.startswith("Bearer "):
        supplied = supplied[7:]
    else:
        # Retain compatibility with the existing dashboard while preferring a
        # header, but never log this query parameter.
        supplied = request.query.get("pass", "")
    return bool(expected) and hmac.compare_digest(supplied, expected)


async def handle_api_stats(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _security_headers(web.Response(status=401, text="Unauthorized"))
    return _security_headers(web.json_response(stats_cache))


async def start_server(tg_app=None, port: int = 8080) -> None:
    """Run until cancelled, then release the listening socket cleanly."""
    del tg_app  # Webhooks are intentionally disabled; this deployment polls.
    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/ping", handle_live)  # backwards-compatible liveness
    app.router.add_get("/live", handle_live)
    app.router.add_get("/ready", handle_ready)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/api/stats", handle_api_stats)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    health.touch("http_server", port=port)
    log.info("Health-check and dashboard server listening on port %s", port)
    try:
        await asyncio.Event().wait()
    finally:
        health.fail("http_server", "server stopped")
        await runner.cleanup()
