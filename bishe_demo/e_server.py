from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

from .stego import SUPPORTED_STEGO_METHODS
from .token_util import issue_token


async def handle_issue(request: web.Request) -> web.Response:
    secret = str(request.app["secret"])
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response({"ok": False, "error": f"invalid json: {e!r}"}, status=400)
    a_url = str(body.get("a_url") or "").strip()
    c_url = str(body.get("c_url") or "").strip()
    extract_method = str(body.get("extract_method") or "append_marker").strip()
    try:
        ttl_s = int(body.get("ttl_s") or 600)
    except Exception:
        return web.json_response({"ok": False, "error": "ttl_s must be int"}, status=400)

    if extract_method not in SUPPORTED_STEGO_METHODS:
        return web.json_response({"ok": False, "error": f"unsupported extract_method: {extract_method}"}, status=400)
    if not a_url:
        return web.json_response({"ok": False, "error": "missing a_url"}, status=400)
    if not c_url:
        return web.json_response({"ok": False, "error": "missing c_url"}, status=400)
    if ttl_s < 10 or ttl_s > 24 * 3600:
        return web.json_response({"ok": False, "error": "ttl_s out of range"}, status=400)

    token = issue_token(
        secret=secret,
        a_url=a_url,
        c_url=c_url,
        extract_method=extract_method,
        ttl_s=ttl_s,
    )
    return web.json_response(
        {
            "ok": True,
            "a_url": a_url,
            "c_url": c_url,
            "extract_method": extract_method,
            "ttl_s": ttl_s,
            "token": token,
        }
    )


async def handle_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def run_e_server(*, host: str, port: int, token_secret: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application()
    app["secret"] = token_secret
    app.router.add_get("/health", handle_health)
    app.router.add_post("/issue", handle_issue)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    logging.info("E 节点启动: http://%s:%d (issue token)", host, port)
    await site.start()
    while True:
        await asyncio.sleep(3600)


def env_token_secret() -> str:
    return os.environ.get("BISHE_TOKEN_SECRET", "bishe-dev-secret")

