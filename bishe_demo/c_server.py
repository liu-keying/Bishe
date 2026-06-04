from __future__ import annotations

import asyncio
import logging
import ssl
import uuid
from pathlib import Path

import httpx
from aiohttp import web

from .common import now_ms
from .crypto_box import aead_decrypt, b64d, b64e, generate_rsa_keypair, rsa_oaep_unwrap, rsa_pub_to_pem
from .psk_aead import decrypt_hidden
from .stego import STEGO_METHOD_PSK_HMAC_INPLACE


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


async def _notify_b_recv_ack(
    app: web.Application,
    *,
    delivery_id: str,
    ok: bool,
    session_id: str,
    reason: str = "",
    detail: str = "",
) -> None:
    b_url = str(app.get("b_callback_url") or "").strip()
    if not b_url:
        return
    body = {
        "delivery_id": delivery_id,
        "ok": ok,
        "session_id": session_id,
        "reason": reason,
        "detail": detail,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False, trust_env=False) as client:
            await client.post(b_url, json=body, headers={"X-Overlay": "1"})
    except Exception:
        logging.exception("C -> B recv-ack 失败 delivery_id=%s ok=%s", delivery_id, ok)


async def handle_recv(request: web.Request) -> web.Response:
    payload = await request.read()
    psk: bytes | None = request.app.get("psk")
    if psk is None:
        return web.json_response({"ok": False, "error": "PSK not ready (start C with --e-url and wait kex)"}, status=503)

    delivery_id_hdr = str(request.headers.get("X-Delivery-Id", "") or "").strip()
    session_id_hdr = str(request.headers.get("X-Session", "") or "")

    delivered: set[str] = request.app.setdefault("delivered_ids", set())
    if delivery_id_hdr and delivery_id_hdr in delivered:
        if delivery_id_hdr:
            asyncio.create_task(
                _notify_b_recv_ack(
                    request.app,
                    delivery_id=delivery_id_hdr,
                    ok=True,
                    session_id=session_id_hdr,
                    reason="duplicate_delivery",
                )
            )
        return web.json_response(
            {"ok": True, "duplicate": True, "delivery_id": delivery_id_hdr},
            status=200,
        )

    try:
        aad = session_id_hdr.encode("utf-8")
        payload = decrypt_hidden(psk=psk, payload=payload, aad=aad)
    except Exception as e:
        if delivery_id_hdr:
            asyncio.create_task(
                _notify_b_recv_ack(
                    request.app,
                    delivery_id=delivery_id_hdr,
                    ok=False,
                    session_id=session_id_hdr,
                    reason="decrypt_failed",
                    detail=repr(e),
                )
            )
        return web.json_response({"ok": False, "error": f"decrypt failed: {e!r}"}, status=400)

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

    if delivery_id_hdr:
        delivered.add(delivery_id_hdr)
        asyncio.create_task(
            _notify_b_recv_ack(
                request.app,
                delivery_id=delivery_id_hdr,
                ok=True,
                session_id=session_id,
                reason="ok",
            )
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
    request.app["delivered_ids"] = set()
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
    e_url: str = "",
    a_url: str = "",
    b_gate_url: str = "",
    psk_hex: str = "",
    b_callback_url: str = "",
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # /recv 可能承载“整段密文”或更大的 trailer，默认 1MB 会触发 413
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app["stats"] = {"start_ms": now_ms(), "recv_count": 0, "dup_count": 0, "recv_bytes": 0, "events": [], "seen_msg_ids": set()}
    app["psk"] = None
    app["link_token"] = ""
    app["b_callback_url"] = (b_callback_url or "").strip()
    if app["b_callback_url"]:
        logging.info("C 启用 B 回执: %s", app["b_callback_url"])

    psk_hex2 = (psk_hex or "").strip()
    if psk_hex2:
        if len(psk_hex2) != 64:
            raise SystemExit("--psk-hex 须为 64 位十六进制（32 字节 AES-256 密钥）")
        app["psk"] = bytes.fromhex(psk_hex2)
        logging.info("C 使用静态 PSK（--psk-hex / BISHE_PSK_HEX）")

    e_url2 = (e_url or "").strip()
    a_url2 = (a_url or "").strip()
    if e_url2 and a_url2:
        scheme = "https" if ssl_certfile and ssl_keyfile else "http"
        c_base = f"{scheme}://{host}:{port}"
        priv, pub = generate_rsa_keypair(bits=2048)
        pub_b64 = b64e(rsa_pub_to_pem(pub))
        link = f"{a_url2.rstrip('/')}|{c_base.rstrip('/')}|{STEGO_METHOD_PSK_HMAC_INPLACE}"
        aad = link.encode("utf-8")
        async with httpx.AsyncClient(timeout=10.0, verify=False, trust_env=False) as client:
            while True:
                r = await client.post(
                    f"{e_url2.rstrip('/')}/issue",
                    json={
                        "role": "c",
                        "a_url": a_url2,
                        "c_url": c_base,
                        "extract_method": STEGO_METHOD_PSK_HMAC_INPLACE,
                        "ttl_s": 600,
                        "pubkey": pub_b64,
                    },
                )
                r.raise_for_status()
                obj = r.json()
                if obj.get("ok") is not True:
                    raise SystemExit(f"E /issue failed: {obj!r}")
                if obj.get("pending"):
                    await asyncio.sleep(float(obj.get("retry_after_s") or 0.5))
                    continue
                bundle = obj.get("bundle") or {}
                import json

                ek_psk = b64d(str(bundle.get("ek_psk") or ""))
                nonce = b64d(str(bundle.get("nonce") or ""))
                ct = b64d(str(bundle.get("ciphertext") or ""))
                psk = rsa_oaep_unwrap(recipient_priv=priv, wrapped=ek_psk)
                pt = aead_decrypt(key32=psk, nonce=nonce, ciphertext=ct, aad=aad)
                data = json.loads(pt.decode("utf-8"))
                psk_b64 = b64e(psk)
                token = str(data.get("token") or "")
                if not psk_b64:
                    raise SystemExit(f"E bundle missing psk_b64: {data!r}")
                if not token:
                    raise SystemExit(f"E bundle missing token: {data!r}")
                app["psk"] = b64d(psk_b64)
                app["link_token"] = token
                logging.info("C 已从 E 获取 PSK（数据面解密启用）")
                bg = (b_gate_url or "").strip()
                if bg:
                    rr = await client.post(
                        f"{bg.rstrip('/')}/register",
                        json={"role": "c", "token": token, "psk_b64": b64e(app["psk"])},
                    )
                    rr.raise_for_status()
                    logging.info("C 已向 B-gate 注册（role=c）")
                break
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

