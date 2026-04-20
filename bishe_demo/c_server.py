from __future__ import annotations

import asyncio
import logging
import ssl
import uuid
from pathlib import Path

from aiohttp import web

from .common import now_ms


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if p <= 0:
        return float(sorted_vals[0])
    if p >= 100:
        return float(sorted_vals[-1])
    # nearest-rank (1-indexed)
    k = int((p / 100.0) * len(sorted_vals) + 0.999999999)
    k = max(1, min(len(sorted_vals), k))
    return float(sorted_vals[k - 1])


async def handle_recv(request: web.Request) -> web.Response:
    payload = await request.read()

    t0_ms = int(request.headers.get("X-T0-MS", "0") or "0")
    b_send_ms = int(request.headers.get("X-B-SEND-MS", "0") or "0")
    t_recv = now_ms()

    e2e_ms = (t_recv - t0_ms) if t0_ms else None
    b2c_ms = (t_recv - b_send_ms) if b_send_ms else None

    session_id = request.headers.get("X-Session", "")
    seq = request.headers.get("X-Seq", "")

    msg_id = ""
    if len(payload) >= 16:
        try:
            msg_id = str(uuid.UUID(bytes=payload[:16]))
        except Exception:
            msg_id = ""

    stats = request.app["stats"]
    stats["recv_count"] = int(stats.get("recv_count") or 0) + 1
    stats["recv_bytes"] = int(stats.get("recv_bytes") or 0) + len(payload)
    stats["events"].append({"t_recv_ms": t_recv, "bytes": len(payload), "e2e_ms": e2e_ms, "msg_id": msg_id})
    if msg_id:
        seen = stats["seen_msg_ids"]
        if msg_id in seen:
            stats["dup_count"] = int(stats.get("dup_count") or 0) + 1
        else:
            seen.add(msg_id)

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


async def handle_stats(request: web.Request) -> web.Response:
    stats = request.app["stats"]
    events: list[dict] = stats["events"]
    e2e_vals = sorted([float(e["e2e_ms"]) for e in events if e.get("e2e_ms") is not None])

    # throughput: bytes/sec over last window
    try:
        window_s = float((request.query.get("window_s") or "5").strip())
    except Exception:
        window_s = 5.0
    window_s = max(0.5, min(60.0, window_s))
    now = now_ms()
    cutoff = now - int(window_s * 1000)
    win_bytes = sum(int(e.get("bytes") or 0) for e in events if int(e.get("t_recv_ms") or 0) >= cutoff)
    win_bps = win_bytes / window_s

    start_ms = int(stats.get("start_ms") or now)
    dur_s = max(0.001, (now - start_ms) / 1000.0)
    avg_bps = (int(stats.get("recv_bytes") or 0)) / dur_s

    return web.json_response(
        {
            "ok": True,
            "start_ms": start_ms,
            "now_ms": now,
            "duration_s": dur_s,
            "recv_count": int(stats.get("recv_count") or 0),
            "unique_msg_ids": len(stats["seen_msg_ids"]),
            "dup_count": int(stats.get("dup_count") or 0),
            "recv_bytes": int(stats.get("recv_bytes") or 0),
            "e2e_ms": {
                "p50": _percentile(e2e_vals, 50),
                "p95": _percentile(e2e_vals, 95),
                "p99": _percentile(e2e_vals, 99),
            },
            "throughput_bytes_per_s": {
                "avg": avg_bps,
                "window_s": window_s,
                "window": win_bps,
            },
        }
    )


async def handle_reset_stats(request: web.Request) -> web.Response:
    request.app["stats"] = {"start_ms": now_ms(), "recv_count": 0, "dup_count": 0, "recv_bytes": 0, "events": [], "seen_msg_ids": set()}
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
    app["stats"] = {"start_ms": now_ms(), "recv_count": 0, "dup_count": 0, "recv_bytes": 0, "events": [], "seen_msg_ids": set()}
    app.router.add_post("/recv", handle_recv)
    app.router.add_get("/stats", handle_stats)
    app.router.add_post("/stats/reset", handle_reset_stats)
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

