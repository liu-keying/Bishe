from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
import uuid
from pathlib import Path

import httpx
from aiohttp import web

from .common import now_ms
from .crypto_box import aead_decrypt, b64d, b64e, generate_rsa_keypair, rsa_oaep_unwrap, rsa_pub_to_pem
from .hls_shared import register_hls_routes, setup_hls_state
from .psk_aead import decrypt_hidden, encrypt_hidden
from .stego import (
    STEGO_METHOD_PSK_HMAC_INPLACE,
    STEGO_TAG_LEN,
    embed_pad_inplace,
    embed_psk_inplace,
)
from .tunnel import (
    CTL_CONNECT,
    CTL_CONNECTED,
    CTL_ERROR,
    CTL_FIN,
    TunnelCtl,
    TunnelData,
    TunnelExit,
    TUNNEL_MAGIC,
    MAGIC_OFFSET,
)


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


async def _notify_gateway_recv_ack(
    app: web.Application,
    *,
    delivery_id: str,
    ok: bool,
    session_id: str,
    reason: str = "",
    detail: str = "",
) -> None:
    gw_url = str(app.get("gateway_callback_url") or "").strip()
    if not gw_url:
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
            await client.post(gw_url, json=body, headers={"X-Overlay": "1"})
    except Exception:
        logging.exception("server -> gateway recv-ack 失败 delivery_id=%s ok=%s", delivery_id, ok)


def _embed_to_rev_hls(app: web.Application, payload: bytes) -> None:
    """将隧道响应消息加密后嵌入 server 的反向 HLS 分片队列。

    使用与 client 相同的 psk_hmac_inplace 方法。
    """
    psk: bytes | None = app.get("psk")
    link_token = str(app.get("link_token") or "").strip()
    if psk is None or not link_token:
        logging.warning("SOCKS5(server): PSK/link_token 未就绪，丢弃反向消息")
        return

    session_id = str(app.get("session_id_rev") or "")
    aad = session_id.encode("utf-8")
    hidden = encrypt_hidden(psk=psk, hidden=payload, aad=aad)

    orig_len = len(hidden)  # 记录原始 AEAD 密文长度（截断用）
    g_bytes = max(32768, len(hidden))
    if len(hidden) < g_bytes:
        hidden = hidden + os.urandom(g_bytes - len(hidden))
    part = hidden[:g_bytes]

    seq = int(app.get("seq_rev") or 1)
    app["seq_rev"] = seq + 1
    emit_sess = str(app.get("session_id_rev") or "")

    try:
        blob = embed_psk_inplace(
            b"\x00" * (g_bytes + STEGO_TAG_LEN),
            token=link_token,
            hls_index=0,
            frag_idx=0,
            cipher_fragment=part,
        )
    except Exception as e:
        logging.warning("SOCKS5(server): embed 失败: %r", e)
        return

    # PendingPullJob 需要这些字段，这里构造最小版本
    from dataclasses import dataclass as _dc

    @_dc
    class _RevJob:
        seq: int = 0
        t0_ms: int = 0
        server_url: str = ""
        c_url: str = ""
        ua: str = "rev"
        client_recv_ms: int = 0
        a_recv_ms: int = 0
        content_type: str = "application/octet-stream"
        blob: bytes = b""
        emit_session: str = ""
        hls_index: int = 0
        hls_total: int = 1
        cipher_group: str = ""
        cipher_k: int = 1
        cipher_index: int = 0
        cipher_bytes: int = 0
        overlay_phase: str = "media"
        stego_frag_body_len: int = 0

    import uuid as _uuid

    job = _RevJob(
        seq=seq,
        blob=blob,
        emit_session=emit_sess,
        cipher_group=_uuid.uuid4().hex,  # 必须非空，worker 才处理
        cipher_bytes=orig_len,  # 原始 AEAD 长度（不含填充），worker 据此截断
        stego_frag_body_len=g_bytes,
    )
    app["pull_queue_rev"].append(job)
    logging.debug("SOCKS5(server): 响应已入反向 HLS 队列 seq=%d bytes=%d", seq, len(hidden))


async def _tunnel_read_loop(app: web.Application) -> None:
    """持续从 TunnelExit 读取目标响应数据，嵌入反向 HLS。"""
    buffer_size = 32768
    while True:
        exit_: TunnelExit = app.get("tunnel_exit")
        if exit_ is None:
            await asyncio.sleep(1)
            continue

        conn_ids = exit_.get_active_conns()
        if not conn_ids:
            await asyncio.sleep(0.1)
            continue

        for conn_id in conn_ids:
            try:
                data = await exit_.recv(conn_id, buffer_size)
            except Exception:
                continue
            if data is None:
                continue
            if not data:
                # EOF
                ctl = TunnelCtl(conn_id=conn_id, ctl_type=CTL_FIN)
                _embed_to_rev_hls(app, ctl.to_bytes())
                await exit_.close(conn_id)
                continue

            seq_map = app.setdefault("tunnel_seq_rev", {})
            seq = seq_map.get(conn_id, 0) + 1
            seq_map[conn_id] = seq

            td = TunnelData(conn_id=conn_id, seq=seq, data=data)
            _embed_to_rev_hls(app, td.to_bytes())

        await asyncio.sleep(0.05)  # 简短间隙，避免 CPU 空转


async def handle_recv(request: web.Request) -> web.Response:
    payload = await request.read()
    psk: bytes | None = request.app.get("psk")
    if psk is None:
        return web.json_response({"ok": False, "error": "PSK not ready (start server with --control-url and wait kex)"}, status=503)

    delivery_id_hdr = str(request.headers.get("X-Delivery-Id", "") or "").strip()
    session_id_hdr = str(request.headers.get("X-Session", "") or "")

    delivered: set[str] = request.app.setdefault("delivered_ids", set())
    if delivery_id_hdr and delivery_id_hdr in delivered:
        if delivery_id_hdr:
            asyncio.create_task(
                _notify_gateway_recv_ack(
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
        plain = decrypt_hidden(psk=psk, payload=payload, aad=aad)
    except Exception as e:
        if delivery_id_hdr:
            asyncio.create_task(
                _notify_gateway_recv_ack(
                    request.app,
                    delivery_id=delivery_id_hdr,
                    ok=False,
                    session_id=session_id_hdr,
                    reason="decrypt_failed",
                    detail=repr(e),
                )
            )
        return web.json_response({"ok": False, "error": f"decrypt failed: {e!r}"}, status=400)

    # --- 隧道消息处理（SOCKS5） ---
    if TunnelCtl.is_tunnel_ctl(plain):
        return await _handle_tunnel_ctl(request, plain)
    if TunnelData.is_tunnel_data(plain):
        return await _handle_tunnel_data(request, plain)

    # 以下为原有隐匿数据处理
    payload = plain
    t0_ms = int(request.headers.get("X-T0-MS", "0") or "0")
    gw_send_ms = int(request.headers.get("X-GW-SEND-MS", "0") or "0")
    t_recv = now_ms()

    e2e_ms = (t_recv - t0_ms) if t0_ms else None
    gw2server_ms = (t_recv - gw_send_ms) if gw_send_ms else None

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
        "server 收到 session=%s seq=%s bytes=%d e2e_ms=%s gw2server_ms=%s",
        session_id,
        seq,
        len(payload),
        e2e_ms,
        gw2server_ms,
    )

    if delivery_id_hdr:
        delivered.add(delivery_id_hdr)
        asyncio.create_task(
            _notify_gateway_recv_ack(
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
            "gw2server_ms": gw2server_ms,
        }
    )


async def _handle_tunnel_ctl(request: web.Request, plain: bytes) -> web.Response:
    """处理隧道控制消息（server 侧）。"""
    ctl = TunnelCtl.from_bytes(plain)
    if ctl is None:
        return web.json_response({"ok": False, "error": "bad tunnel ctl"}, status=400)

    exit_: TunnelExit = request.app.get("tunnel_exit")
    if exit_ is None:
        return web.json_response({"ok": False, "error": "tunnel not enabled"}, status=503)

    if ctl.ctl_type == CTL_CONNECT:
        ok = await exit_.connect(ctl.conn_id, ctl.host, ctl.port)
        resp = TunnelCtl(
            conn_id=ctl.conn_id,
            ctl_type=CTL_CONNECTED if ok else CTL_ERROR,
            error="" if ok else f"connect {ctl.host}:{ctl.port} failed",
        )
        _embed_to_rev_hls(request.app, resp.to_bytes())
        return web.json_response({"ok": True, "conn_id": ctl.conn_id, "connected": ok})

    if ctl.ctl_type == CTL_FIN:
        await exit_.close(ctl.conn_id)
        return web.json_response({"ok": True, "conn_id": ctl.conn_id, "closed": True})

    return web.json_response({"ok": False, "error": f"unknown ctl_type: {ctl.ctl_type}"}, status=400)


async def _handle_tunnel_data(request: web.Request, plain: bytes) -> web.Response:
    """处理隧道数据消息（server 侧）：写入目标 TCP 连接。"""
    td = TunnelData.from_bytes(plain)
    if td is None:
        return web.json_response({"ok": False, "error": "bad tunnel data"}, status=400)

    exit_: TunnelExit = request.app.get("tunnel_exit")
    if exit_ is None:
        return web.json_response({"ok": False, "error": "tunnel not enabled"}, status=503)

    ok = await exit_.send(td.conn_id, td.data)
    if not ok:
        return web.json_response({"ok": False, "error": "conn not found or write failed"}, status=404)

    if td.fin:
        await exit_.close(td.conn_id)

    return web.json_response({"ok": True, "conn_id": td.conn_id, "bytes": len(td.data)})


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
    logging.info("server 收到 stop-notice: %s", body)
    return web.json_response({"ok": True})


async def run_server(
    *,
    host: str,
    port: int,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
    control_url: str = "",
    client_url: str = "",
    gateway_url: str = "",
    psk_hex: str = "",
    gateway_callback_url: str = "",
    socks_enabled: bool = False,
    socks_max_conns: int = 50,
    socks_idle_timeout: float = 300.0,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # /recv 可能承载"整段密文"或更大的 trailer，默认 1MB 会触发 413
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app["stats"] = {"start_ms": now_ms(), "recv_count": 0, "dup_count": 0, "recv_bytes": 0, "events": [], "seen_msg_ids": set()}
    app["psk"] = None
    app["link_token"] = ""
    app["gateway_callback_url"] = (gateway_callback_url or "").strip()
    if app["gateway_callback_url"]:
        logging.info("server 启用 gateway 回执: %s", app["gateway_callback_url"])

    # SOCKS5 隧道状态（server 侧出口）
    app["tunnel_exit"] = TunnelExit(max_conns=socks_max_conns, idle_timeout_s=socks_idle_timeout)
    app["tunnel_enabled"] = socks_enabled
    app["tunnel_seq_rev"]: dict[str, int] = {}

    # 反向 HLS 状态（server 提供 HLS 服务，供 gateway 拉取响应数据回传 client）
    setup_hls_state(app, "bishe-rev-1")
    # 覆盖为固定 session ID（"bishe-rev-1" 不符合 bishe-<N> 格式，setup_hls_state 解析后会变成 bishe-1）
    app["session_id"] = "bishe-rev-1"
    app["session_id_rev"] = "bishe-rev-1"
    app["seq_rev"] = app["seq"]
    app["pull_queue_rev"] = app["pull_queue"]   # 别名，供 _embed_to_rev_hls 使用

    if socks_enabled:
        logging.info("server 启用 SOCKS5 隧道出口 (max_conns=%d)", socks_max_conns)

    psk_hex2 = (psk_hex or "").strip()
    if psk_hex2:
        if len(psk_hex2) != 64:
            raise SystemExit("--psk-hex 须为 64 位十六进制（32 字节 AES-256 密钥）")
        app["psk"] = bytes.fromhex(psk_hex2)
        logging.info("server 使用静态 PSK（--psk-hex / BISHE_PSK_HEX）")

    control_url2 = (control_url or "").strip()
    client_url2 = (client_url or "").strip()
    if control_url2 and client_url2:
        scheme = "https" if ssl_certfile and ssl_keyfile else "http"
        server_base = f"{scheme}://{host}:{port}"
        priv, pub = generate_rsa_keypair(bits=2048)
        pub_b64 = b64e(rsa_pub_to_pem(pub))
        link = f"{client_url2.rstrip('/')}|{server_base.rstrip('/')}|{STEGO_METHOD_PSK_HMAC_INPLACE}"
        aad = link.encode("utf-8")
        async with httpx.AsyncClient(timeout=10.0, verify=False, trust_env=False) as client:
            while True:
                r = await client.post(
                    f"{control_url2.rstrip('/')}/issue",
                    json={
                        "role": "server",
                        "client_url": client_url2,
                        "server_url": server_base,
                        "a_url": client_url2,
                        "c_url": server_base,
                        "extract_method": STEGO_METHOD_PSK_HMAC_INPLACE,
                        "ttl_s": 600,
                        "pubkey": pub_b64,
                    },
                )
                r.raise_for_status()
                obj = r.json()
                if obj.get("ok") is not True:
                    raise SystemExit(f"control /issue failed: {obj!r}")
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
                    raise SystemExit(f"control bundle missing psk_b64: {data!r}")
                if not token:
                    raise SystemExit(f"control bundle missing token: {data!r}")
                app["psk"] = b64d(psk_b64)
                app["link_token"] = token
                logging.info("server 已从 control 获取 PSK（数据面解密启用）")
                gw = (gateway_url or "").strip()
                if gw:
                    rr = await client.post(
                        f"{gw.rstrip('/')}/register",
                        json={"role": "server", "token": token, "psk_b64": b64e(app["psk"])},
                    )
                    rr.raise_for_status()
                    logging.info("server 已向 gateway 注册（role=server）")
                break
    app.router.add_post("/recv", handle_recv)
    app.router.add_get("/stats", handle_stats)
    app.router.add_post("/stats/reset", handle_reset_stats)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/stop-notice", handle_stop_notice)

    # 反向 HLS 路由（供 gateway 拉取 server→client 响应数据）
    register_hls_routes(app, "/hls-rev")

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
    logging.info("server 服务端启动: %s://%s:%d (recv + rev-hls + stats)", scheme, host, port)
    await site.start()

    # 隧道读循环（持续从目标 TCP 读取响应数据）
    if socks_enabled:
        asyncio.create_task(_tunnel_read_loop(app))

    while True:
        await asyncio.sleep(3600)
