"""
Minimal "web server" that appends one JSON line per HTTP request (synthetic access log).

Run from repo root:
  python comparison/zhang_http_header_cc/server.py --host 127.0.0.1 --port 8011 --log zhang_cc.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from aiohttp import web


def build_app(*, log_path: Path) -> web.Application:
    app = web.Application()

    async def access_sink(request: web.Request) -> web.Response:
        rec = {
            "ts": time.time(),
            "remote": request.remote,
            "method": request.method,
            "path": request.path_qs,
            "user_agent": request.headers.get("User-Agent", ""),
            "cookie": request.headers.get("Cookie", ""),
            "referer": request.headers.get("Referer", ""),
        }
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def _append() -> None:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line)

        await asyncio.to_thread(_append)
        return web.Response(text="OK\n", content_type="text/plain", charset="utf-8")

    app.router.add_route("*", "/{tail:.*}", access_sink)
    return app


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Zhang-style HTTP header CC: logging sink server (JSONL).")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8011)
    p.add_argument("--log", default="zhang_cc.jsonl", help="JSONL log path (append-only)")
    args = p.parse_args()

    log_path = Path(args.log).resolve()
    app = build_app(log_path=log_path)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=str(args.host), port=int(args.port))
    await site.start()
    logging.info("Zhang HTTP-header CC sink listening on http://%s:%s log=%s", args.host, args.port, log_path)
    await asyncio.Event().wait()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
