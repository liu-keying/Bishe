from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path

from aiohttp import web

from .common import now_ms


async def handle_recv(request: web.Request) -> web.Response:
    payload = await request.read()

    t0_ms = int(request.headers.get("X-T0-MS", "0") or "0")
    b_send_ms = int(request.headers.get("X-B-SEND-MS", "0") or "0")
    t_recv = now_ms()

    e2e_ms = (t_recv - t0_ms) if t0_ms else None
    b2c_ms = (t_recv - b_send_ms) if b_send_ms else None

    session_id = request.headers.get("X-Session", "")
    seq = request.headers.get("X-Seq", "")

    logging.info(
        "C 收到 session=%s seq=%s bytes=%d e2e_ms=%s b2c_ms=%s",
        session_id,
        seq,
        len(payload),
        e2e_ms,
        b2c_ms,
    )

    return web.json_response(
        {
            "ok": True,
            "session_id": session_id,
            "seq": seq,
            "bytes": len(payload),
            "t_recv_ms": t_recv,
            "e2e_ms": e2e_ms,
            "b2c_ms": b2c_ms,
        }
    )


async def handle_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_stop_notice(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    logging.info("C 收到 stop-notice: %s", body)
    return web.json_response({"ok": True})


async def run_c_server(
    *,
    host: str,
    port: int,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    app = web.Application()
    app.router.add_post("/recv", handle_recv)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/stop-notice", handle_stop_notice)

    runner = web.AppRunner(app)
    await runner.setup()

    ssl_ctx: ssl.SSLContext | None = None
    if ssl_certfile and ssl_keyfile:
        if not Path(ssl_certfile).is_file() or not Path(ssl_keyfile).is_file():
            raise SystemExit(f"SSL 证书或私钥不存在: cert={ssl_certfile} key={ssl_keyfile}")
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(ssl_certfile, ssl_keyfile)

    site = web.TCPSite(runner, host=host, port=port, ssl_context=ssl_ctx)
    scheme = "https" if ssl_ctx else "http"
    logging.info("C 服务端启动: %s://%s:%d", scheme, host, port)
    await site.start()

    while True:
        await asyncio.sleep(3600)

