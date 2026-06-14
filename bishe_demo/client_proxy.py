from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass

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
    fake_ts_segment,
)
from .tunnel import CTL_CONNECT, CTL_CONNECTED, CTL_ERROR, CTL_FIN, TunnelCtl, TunnelData


@dataclass
class PendingPullJob:
    seq: int
    t0_ms: int
    server_url: str
    c_url: str  # backward compat alias
    ua: str
    client_recv_ms: int
    a_recv_ms: int  # backward compat alias
    content_type: str
    blob: bytes
    """入队时 client 的会话 ID；拉片 URL 须与该值一致，媒体出队后 client 会轮换到下一 session。"""
    emit_session: str = ""
    hls_index: int = 0
    hls_total: int = 1
    cipher_group: str = ""
    cipher_k: int = 0
    cipher_index: int = 0
    cipher_bytes: int = 0
    overlay_phase: str = "media"  # media | pad
    stego_frag_body_len: int = 0


def parse_initial_session_seq(session_arg: str | None) -> int:
    """--session 形如 bishe-3 时取起始序号；其它字符串回退为 1。"""
    if not session_arg or not str(session_arg).strip():
        return 1
    t = str(session_arg).strip()
    m = re.fullmatch(r"bishe-(\d+)", t, flags=re.IGNORECASE)
    return int(m.group(1)) if m else 1


def _segment_response(job: PendingPullJob, *, next_session: str | None = None) -> web.Response:
    headers = {
        "Content-Type": "video/mp2t",
        "X-Bishe-Kind": "media",
        "X-Bishe-Stego": STEGO_METHOD_PSK_HMAC_INPLACE,
        "X-Bishe-Overlay-Phase": (job.overlay_phase or "media").strip(),
        "X-C-URL": job.server_url,
        "X-Session": job.emit_session,
        "X-Seq": str(job.seq),
        "X-T0-MS": str(job.t0_ms),
        "X-Content-Type": job.content_type,
        "X-A-UA": job.ua,
        "X-A-Recv-MS": str(job.client_recv_ms),
    }
    if next_session:
        headers["X-Next-Session"] = next_session
    if job.hls_total > 1:
        headers["X-Bishe-HLS-Index"] = str(job.hls_index)
        headers["X-Bishe-HLS-Total"] = str(job.hls_total)
    if int(job.cipher_k or 0) > 0:
        headers["X-Bishe-Cipher-Group"] = str(job.cipher_group or "")
        headers["X-Bishe-Cipher-K"] = str(int(job.cipher_k))
        headers["X-Bishe-Cipher-Index"] = str(int(job.cipher_index))
        if int(job.cipher_bytes or 0) > 0:
            headers["X-Bishe-Cipher-Bytes"] = str(int(job.cipher_bytes))
        if int(job.stego_frag_body_len or 0) > 0:
            headers["X-Chunk-Size"] = str(int(job.stego_frag_body_len))
    return web.Response(status=200, body=job.blob, headers=headers)


def _hls_url_session_allowed(app: web.Application, url_sid: str) -> bool:
    if url_sid == app["session_id"]:
        return True
    return any(j.emit_session == url_sid for j in app["pull_queue"])


async def handle_hls_master(request: web.Request) -> web.Response:
    """HLS 主列表：指向 index.m3u8。"""
    app = request.app
    url_sid = request.match_info["session_id"]
    if not _hls_url_session_allowed(app, url_sid):
        return web.Response(status=404)
    body = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=2048000,CODECS="avc1.42e01e,mp4a.40.2"\n'
        "index.m3u8\n"
    )
    return web.Response(
        status=200,
        body=body.encode("utf-8"),
        headers={
            "Content-Type": "application/vnd.apple.mpegurl",
            "Cache-Control": "no-cache",
        },
    )


def _playlist_job_prefix(q: deque[PendingPullJob], url_sid: str) -> list[PendingPullJob]:
    """队首起连续且 emit_session 与 url_sid 一致的任务（不消费队列）。"""
    out: list[PendingPullJob] = []
    for j in q:
        if j.emit_session != url_sid:
            break
        out.append(j)
    return out


async def handle_hls_playlist(request: web.Request) -> web.Response:
    """媒体播放列表：peek 队列，列出当前 session 下待发 seg-*.ts。"""
    app = request.app
    url_sid = request.match_info["session_id"]
    if not _hls_url_session_allowed(app, url_sid):
        return web.Response(status=404)

    q: deque[PendingPullJob] = app["pull_queue"]
    head = q[0] if q else None
    if head and url_sid != head.emit_session:
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:6",
        ]
        text = "\n".join(lines) + "\n"
        return web.Response(
            status=200,
            body=text.encode("utf-8"),
            headers={
                "Content-Type": "application/vnd.apple.mpegurl",
                "Cache-Control": "no-cache",
            },
        )
    if not head and url_sid != app["session_id"]:
        return web.Response(status=404)

    jobs = _playlist_job_prefix(q, url_sid)

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:6",
    ]
    if jobs:
        lines.append("#EXT-X-PLAYLIST-TYPE:VOD")
        lines.append(f"#EXT-X-MEDIA-SEQUENCE:{max(0, jobs[0].seq - 1)}")
        for j in jobs:
            lines.append("#EXTINF:6.000,")
            lines.append(f"seg-{j.seq:06d}.ts")
        lines.append("#EXT-X-ENDLIST")
    text = "\n".join(lines) + "\n"
    return web.Response(
        status=200,
        body=text.encode("utf-8"),
        headers={
            "Content-Type": "application/vnd.apple.mpegurl",
            "Cache-Control": "no-cache",
        },
    )


async def handle_hls_segment(request: web.Request) -> web.Response:
    """GET seg 与队首 seq/session 一致时 popleft 并返回 TS。"""
    app = request.app
    url_sid = request.match_info["session_id"]
    if not _hls_url_session_allowed(app, url_sid):
        return web.Response(status=404)

    seg = int(request.match_info["seg"])
    q: deque[PendingPullJob] = app["pull_queue"]
    if not q or q[0].seq != seg or q[0].emit_session != url_sid:
        return web.Response(status=404)

    job = q.popleft()
    next_session: str | None = None
    last_hls = job.hls_total <= 1 or job.hls_index == job.hls_total - 1
    if last_hls:
        next_map: dict[str, str] = app.get("session_next") or {}
        next_session = next_map.get(job.emit_session) or ""
        if next_session:
            next_map.pop(job.emit_session, None)
            app["session_next"] = next_map

    return _segment_response(job, next_session=next_session)


def _enqueue(app: web.Application, job: PendingPullJob) -> None:
    app["pull_queue"].append(job)


def _remember_for_retry(app: web.Application, job: PendingPullJob) -> None:
    """网关 校验失败时按 session 重传 media 片。"""
    if job.overlay_phase != "media":
        return
    by_sess = app.get("retry_media_by_session")
    if not isinstance(by_sess, dict):
        by_sess = {}
    emit_sess = (job.emit_session or "").strip()
    if emit_sess not in by_sess or not isinstance(by_sess.get(emit_sess), list):
        by_sess[emit_sess] = []
    entry = {
        "server_url": job.server_url,
        "content_type": job.content_type,
        "blob": job.blob,
        "hls_index": job.hls_index,
        "hls_total": job.hls_total,
        "overlay_phase": job.overlay_phase,
        "cipher_group": job.cipher_group,
        "cipher_k": job.cipher_k,
        "cipher_index": job.cipher_index,
        "cipher_bytes": job.cipher_bytes,
        "stego_frag_body_len": job.stego_frag_body_len,
    }
    by_sess[emit_sess].append(entry)
    app["retry_media_by_session"] = by_sess
    app["retry_media"] = by_sess[emit_sess]


def _clear_retry_state(app: web.Application) -> None:
    app["retry_media"] = []
    app["retry_media_by_session"] = {}


async def handle_overlay_embed_hls(request: web.Request) -> web.Response:
    """
    multipart：多个 segment + hidden。
    整包 AEAD 后密文拆 k 份随机散入 k 个 TS；其余分片 embed_pad_inplace。
    """
    try:
        session_id: str = request.app["session_id"]

        server_url = (request.query.get("server") or request.query.get("c") or "").strip() or (
            request.headers.get("X-C-URL", "").strip()
        )

        segment_parts: list[bytes] = []
        hidden: bytes | None = None
        if request.content_type and "multipart/form-data" in request.content_type:
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                name = part.name or ""
                if name == "segment":
                    segment_parts.append(await part.read())
                elif name == "hidden":
                    hidden = await part.read()
        else:
            return web.json_response(
                {
                    "ok": False,
                    "error": "use multipart/form-data with fields segment (repeat, ordered) and hidden",
                },
                status=400,
            )

        if len(segment_parts) < 1:
            return web.json_response({"ok": False, "error": "at least one multipart field segment required"}, status=400)
        if hidden is None:
            return web.json_response({"ok": False, "error": "missing multipart field hidden"}, status=400)

        plain_len = len(hidden)

        psk: bytes | None = request.app.get("psk")
        if psk is None:
            return web.json_response({"ok": False, "error": "PSK not ready (start client with --control-url and wait kex)"}, status=503)
        if not str(request.app.get("link_token") or "").strip():
            return web.json_response(
                {"ok": False, "error": "link_token not ready (psk_hmac_inplace requires control kex to obtain token)"},
                status=503,
            )
        link_token = str(request.app.get("link_token") or "").strip()
        n = len(segment_parts)
        t0_ms = now_ms()
        ua = request.headers.get("User-Agent", "")
        recv_ms = now_ms()
        emit_sess = request.app["session_id"]
        chunk_sz = len(segment_parts[0])

        k_raw = (request.query.get("k") or request.query.get("cipher_k") or "").strip()
        k = int(k_raw) if k_raw.isdigit() else n
        if k < 1:
            return web.json_response({"ok": False, "error": "k must be >= 1"}, status=400)
        if k > n:
            return web.json_response({"ok": False, "error": f"k must be <= number of segments (k={k}, n={n})"}, status=400)

        aad = str(emit_sess).encode("utf-8")
        try:
            hidden = encrypt_hidden(psk=psk, hidden=hidden, aad=aad)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"encrypt hidden failed: {e!r}"}, status=400)

        first_seq = request.app["seq"]
        last_seq = first_seq
        cipher_group_id: str | None = None

        _clear_retry_state(request.app)

        cipher = hidden
        cipher_bytes_real = len(cipher)

        g_bits_raw = (request.query.get("g_bits") or request.query.get("g") or "").strip()
        g_bytes_raw = (request.query.get("g_bytes") or "").strip()
        g_bytes: int | None = None
        if g_bytes_raw.isdigit():
            g_bytes = int(g_bytes_raw)
        elif g_bits_raw.isdigit():
            gb = int(g_bits_raw)
            g_bytes = (gb + 7) // 8
        if g_bytes is None or g_bytes <= 0:
            g_bytes = (len(cipher) + k - 1) // k

        g_bytes = max(1, min(int(g_bytes), 16 * 1024 * 1024))
        pad_psk_len = STEGO_TAG_LEN + g_bytes

        total_cipher_bytes = k * g_bytes
        if len(cipher) > total_cipher_bytes:
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "cipher too large for chosen (k,g) capacity: "
                        f"cipher_bytes={len(cipher)} > k*g_bytes={total_cipher_bytes} (k={k} g_bytes={g_bytes}). "
                        "请增大 g（或 g_bytes/g_bits），或增大 k，或减小 hidden。"
                    ),
                },
                status=400,
            )

        if len(cipher) < total_cipher_bytes:
            cipher = cipher + os.urandom(total_cipher_bytes - len(cipher))

        parts = [cipher[i * g_bytes : (i + 1) * g_bytes] for i in range(k)]
        chosen = sorted(random.sample(range(n), k))
        assign = {idx: parts[j] for j, idx in enumerate(chosen)}
        idx_map = {idx: j for j, idx in enumerate(chosen)}
        cipher_group = uuid.uuid4().hex
        cipher_group_id = cipher_group

        media_chunks = 0
        for i in range(n):
            seq = request.app["seq"]
            request.app["seq"] = seq + 1
            last_seq = seq
            piece = assign.get(i)
            if piece is None:
                blob0 = embed_pad_inplace(
                    segment_parts[i],
                    token=link_token,
                    hls_index=i,
                    pad_len=pad_psk_len,
                )
                job = PendingPullJob(
                    seq=seq,
                    t0_ms=t0_ms,
                    server_url=server_url,
                    c_url=server_url,
                    ua=ua,
                    client_recv_ms=recv_ms,
                    a_recv_ms=recv_ms,
                    content_type="video/mp2t",
                    blob=blob0,
                    emit_session=emit_sess,
                    hls_index=i,
                    hls_total=n,
                    cipher_group=cipher_group,
                    cipher_k=k,
                    cipher_index=0,
                    cipher_bytes=cipher_bytes_real,
                    overlay_phase="pad",
                    stego_frag_body_len=0,
                )
                _enqueue(request.app, job)
                continue

            try:
                blob = embed_psk_inplace(
                    segment_parts[i],
                    token=link_token,
                    hls_index=i,
                    frag_idx=idx_map[i],
                    cipher_fragment=piece,
                )
            except Exception as e:
                return web.json_response({"ok": False, "error": f"embed failed: {e!r}"}, status=400)

            job = PendingPullJob(
                seq=seq,
                t0_ms=t0_ms,
                server_url=server_url,
                c_url=server_url,
                ua=ua,
                client_recv_ms=recv_ms,
                a_recv_ms=recv_ms,
                content_type="video/mp2t",
                blob=blob,
                emit_session=emit_sess,
                hls_index=i,
                hls_total=n,
                cipher_group=cipher_group,
                cipher_k=k,
                cipher_index=idx_map[i],
                cipher_bytes=cipher_bytes_real,
                overlay_phase="media",
                stego_frag_body_len=g_bytes,
            )
            _enqueue(request.app, job)
            _remember_for_retry(request.app, job)
            media_chunks += 1

        request.app["session_seq"] = int(request.app["session_seq"]) + 1
        next_sess = f"bishe-{request.app['session_seq']}"
        next_map: dict[str, str] = request.app.get("session_next") or {}
        next_map[str(emit_sess)] = next_sess
        request.app["session_next"] = next_map
        request.app["session_id"] = next_sess

        body_out: dict = {
            "ok": True,
            "queued_for_pull": True,
            "session_id": session_id,
            "phase": "media_hls",
            "hls_segments": n,
            "last_segment_chunks": media_chunks,
            "chunk_bytes": chunk_sz,
            "server_url": server_url,
            "extract": {"method": STEGO_METHOD_PSK_HMAC_INPLACE},
            "seq_from": first_seq,
            "seq_to": last_seq,
            "cipher_k": k,
            "g_bytes": int(g_bytes),
            "g_bits": int(g_bytes) * 8,
            "plain_hidden_bytes": plain_len,
            "cipher_bytes": len(hidden),
        }
        if cipher_group_id:
            body_out["cipher_group"] = cipher_group_id
        body_out["seq"] = last_seq
        return web.json_response(body_out)
    except Exception as e:
        logging.exception("client /overlay/embed-hls 处理异常: %r", e)
        return web.json_response({"ok": False, "error": f"internal error: {e!r}"}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "session_id": request.app["session_id"]})


async def handle_stop_notice(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    logging.info("client 收到 stop-notice: %s", body)
    return web.json_response({"ok": True})


async def handle_error_notice(request: web.Request) -> web.Response:
    """网关 校验失败：按 session 重入队 media。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    logging.warning("client 收到 error-notice: %s", body)

    sid = str(body.get("session_id") or body.get("emit_session") or body.get("X-Session") or "").strip()
    by_sess = request.app.get("retry_media_by_session") or {}
    media: list[dict] = []
    if sid and isinstance(by_sess, dict):
        m0 = by_sess.get(sid)
        if isinstance(m0, list):
            media = m0

    if not media:
        m1 = request.app.get("retry_media") or []
        if isinstance(m1, list):
            media = m1

    if not media:
        return web.json_response({"ok": False, "error": "no retry state"}, status=404)

    t0_ms = now_ms()
    recv_ms = now_ms()
    ua = "retry"
    emit_sess = request.app["session_id"]
    first_seq = request.app["seq"]
    last_seq = first_seq - 1

    for m in media:
        seq = request.app["seq"]
        request.app["seq"] = seq + 1
        last_seq = seq
        job_media = PendingPullJob(
            seq=seq,
            t0_ms=t0_ms,
            server_url=str(m.get("server_url") or m.get("c_url") or ""),
            c_url=str(m.get("server_url") or m.get("c_url") or ""),
            ua=ua,
            client_recv_ms=recv_ms,
            a_recv_ms=recv_ms,
            content_type=str(m.get("content_type") or "application/octet-stream"),
            blob=m.get("blob") or b"",
            emit_session=emit_sess,
            hls_index=int(m.get("hls_index") or 0),
            hls_total=int(m.get("hls_total") or 1),
            cipher_group=str(m.get("cipher_group") or ""),
            cipher_k=int(m.get("cipher_k") or 0),
            cipher_index=int(m.get("cipher_index") or 0),
            cipher_bytes=int(m.get("cipher_bytes") or 0),
            overlay_phase=str(m.get("overlay_phase") or "media"),
            stego_frag_body_len=int(m.get("stego_frag_body_len") or 0),
        )
        _enqueue(request.app, job_media)

    return web.json_response(
        {
            "ok": True,
            "requeued": True,
            "session_id": emit_sess,
            "seq_from": first_seq,
            "seq_to": last_seq,
        }
    )


# ---------------------------------------------------------------------------
# SOCKS5 隧道 —— client 侧（入口代理 + 反向接收）
# ---------------------------------------------------------------------------

async def handle_recv_tunnel(request: web.Request) -> web.Response:
    """接收 网关 回传的隧道消息（TunnelCtl / TunnelData），写入对应客户端连接。"""
    payload = await request.read()
    psk: bytes | None = request.app.get("psk")
    if psk is None:
        return web.json_response({"ok": False, "error": "PSK not ready"}, status=503)

    session_id_hdr = str(request.headers.get("X-Session", "") or "")
    try:
        aad = session_id_hdr.encode("utf-8")
        plain = decrypt_hidden(psk=psk, payload=payload, aad=aad)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"decrypt failed: {e!r}"}, status=400)

    # 判断消息类型
    ctl = TunnelCtl.from_bytes(plain)
    if ctl is not None:
        tunnel_conns: dict = request.app.get("tunnel_conns") or {}
        st = tunnel_conns.get(ctl.conn_id)
        if st is None:
            return web.json_response({"ok": True, "unknown_conn": ctl.conn_id[:16]})

        if ctl.ctl_type == CTL_CONNECTED:
            st["ready"].set()
            logging.info("SOCKS5 隧道已建立: conn=%s", ctl.conn_id[:8])
        elif ctl.ctl_type in (CTL_FIN, CTL_ERROR):
            logging.info("SOCKS5 隧道关闭: conn=%s ctl=%s", ctl.conn_id[:8], ctl.ctl_type)
            try:
                st["writer"].close()
            except Exception:
                pass
            tunnel_conns.pop(ctl.conn_id, None)
        return web.json_response({"ok": True, "conn_id": ctl.conn_id, "ctl": ctl.ctl_type})

    data_msg = TunnelData.from_bytes(plain)
    if data_msg is not None:
        tunnel_conns: dict = request.app.get("tunnel_conns") or {}
        st = tunnel_conns.get(data_msg.conn_id)
        if st is None:
            return web.json_response({"ok": True, "unknown_conn": data_msg.conn_id[:16]})
        try:
            st["writer"].write(data_msg.data)
            await st["writer"].drain()
            st["last_active"] = time.time()
        except Exception:
            tunnel_conns.pop(data_msg.conn_id, None)
            return web.json_response({"ok": True, "conn_closed": data_msg.conn_id[:16]})
        return web.json_response({"ok": True, "conn_id": data_msg.conn_id, "bytes": len(data_msg.data)})

    return web.json_response({"ok": False, "error": "unknown tunnel message format"}, status=400)


def _embed_tunnel_msg(app: web.Application, payload: bytes, *, is_ctl: bool = False) -> None:
    """将隧道消息加密后嵌入 client 的 HLS 分片队列。"""
    psk: bytes | None = app.get("psk")
    if psk is None:
        logging.warning("SOCKS5: PSK 未就绪，丢弃隧道消息")
        return
    session_id = str(app.get("session_id") or "")
    aad = session_id.encode("utf-8")
    hidden = encrypt_hidden(psk=psk, hidden=payload, aad=aad)

    # 分割为适合 TS 分片的大小（默认 32KB 以下不用切分）
    orig_len = len(hidden)  # 记录原始 AEAD 密文长度（截断用）
    g_bytes = max(32768, len(hidden))
    if len(hidden) < g_bytes:
        hidden = hidden + os.urandom(g_bytes - len(hidden))

    part = hidden[:g_bytes]
    link_token = str(app.get("link_token") or "").strip()
    if not link_token:
        logging.warning("SOCKS5: link_token 未就绪，丢弃隧道消息")
        return

    seq = int(app.get("seq") or 1)
    app["seq"] = seq + 1
    emit_sess = str(app.get("session_id") or "")

    try:
        blob = embed_psk_inplace(
            fake_ts_segment(g_bytes + STEGO_TAG_LEN),  # 伪装 TS 分片
            token=link_token,
            hls_index=0,
            frag_idx=0,
            cipher_fragment=part,
        )
    except Exception as e:
        logging.warning("SOCKS5: embed 失败: %r", e)
        return

    job = PendingPullJob(
        seq=seq,
        t0_ms=0,
        server_url="",
        c_url="",
        ua="socks5",
        client_recv_ms=0,
        a_recv_ms=0,
        content_type="application/octet-stream",
        blob=blob,
        emit_session=emit_sess,
        hls_index=0,
        hls_total=1,
        cipher_group=uuid.uuid4().hex,  # 必须非空，worker 才处理
        cipher_k=1,
        cipher_index=0,
        cipher_bytes=orig_len,  # 原始 AEAD 长度（不含填充），worker 据此截断
        overlay_phase="media",
        stego_frag_body_len=g_bytes,
    )
    app["pull_queue"].append(job)
    logging.debug("SOCKS5: 隧道消息已入 HLS 队列 seq=%d bytes=%d", seq, len(hidden))


async def handle_socks_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    app: web.Application,
    cfg: dict,
) -> None:
    """处理单个 SOCKS5 客户端连接。"""
    peer = writer.get_extra_info("peername")
    logging.info("SOCKS5 新连接: %s", peer)
    conn_id = ""
    tunnel_conns: dict = app.get("tunnel_conns") or {}

    async def _send_tunnel_ctl(ctl_type: str, host: str = "", port: int = 0) -> str:
        cid = conn_id or uuid.uuid4().hex
        ctl = TunnelCtl(conn_id=cid, ctl_type=ctl_type, host=host, port=port)
        _embed_tunnel_msg(app, ctl.to_bytes(), is_ctl=True)
        return cid

    try:
        # --- SOCKS5 握手阶段 1: 认证协商 ---
        auth_req = await asyncio.wait_for(reader.readexactly(2), timeout=10.0)
        if auth_req[0] != 0x05:
            writer.close()
            return
        nmethods = auth_req[1]
        await asyncio.wait_for(reader.readexactly(nmethods), timeout=5.0)
        writer.write(b"\x05\x00")  # 选择无认证
        await writer.drain()

        # --- SOCKS5 握手阶段 2: 连接请求 ---
        req_hdr = await asyncio.wait_for(reader.readexactly(4), timeout=10.0)
        if req_hdr[0] != 0x05 or req_hdr[1] != 0x01:  # 只支持 CONNECT
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")  # 命令不支持
            await writer.drain()
            writer.close()
            return

        atyp = req_hdr[3]
        host: str
        if atyp == 0x01:  # IPv4
            addr = await asyncio.wait_for(reader.readexactly(4), timeout=5.0)
            host = ".".join(str(b) for b in addr)
        elif atyp == 0x03:  # 域名
            len_byte = await asyncio.wait_for(reader.readexactly(1), timeout=5.0)
            name = await asyncio.wait_for(reader.readexactly(len_byte[0]), timeout=5.0)
            host = name.decode("ascii", errors="replace")
        elif atyp == 0x04:  # IPv6
            addr = await asyncio.wait_for(reader.readexactly(16), timeout=5.0)
            host = ":".join(f"{addr[i]<<8|addr[i+1]:x}" for i in range(0, 16, 2))
        else:
            writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")  # 地址类型不支持
            await writer.drain()
            writer.close()
            return

        port_bytes = await asyncio.wait_for(reader.readexactly(2), timeout=5.0)
        port = struct.unpack(">H", port_bytes)[0]
        logging.info("SOCKS5 CONNECT: %s:%d", host, port)

        # 生成连接 ID，通过隐匿通道请求 server 建连
        conn_id = uuid.uuid4().hex
        ready_event = asyncio.Event()
        st = {
            "reader": reader,
            "writer": writer,
            "ready": ready_event,
            "target_host": host,
            "target_port": port,
            "created_at": time.time(),
            "last_active": time.time(),
        }
        tunnel_conns[conn_id] = st
        app["tunnel_conns"] = tunnel_conns

        await _send_tunnel_ctl(CTL_CONNECT, host=host, port=port)

        # 等待 server 确认建连
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=cfg.get("connect_timeout", 30.0))
        except asyncio.TimeoutError:
            logging.warning("SOCKS5: 等待 server 建连超时 conn=%s", conn_id[:8])
            writer.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")  # 主机不可达
            await writer.drain()
            writer.close()
            tunnel_conns.pop(conn_id, None)
            await _send_tunnel_ctl(CTL_FIN)
            return

        # 回 SOCKS5 成功响应
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()
        logging.info("SOCKS5 隧道就绪: conn=%s → %s:%d", conn_id[:8], host, port)

        # --- 阶段 3: 数据转发（客户端 → 隐匿通道） ---
        buffer_size = cfg.get("buffer_size", 32768)
        seq = 0
        while True:
            try:
                data = await asyncio.wait_for(reader.read(buffer_size), timeout=0.1)
            except asyncio.TimeoutError:
                # 检查连接是否还在
                if conn_id not in tunnel_conns:
                    break
                continue
            except Exception:
                break

            if not data:
                # 客户端断开
                break

            seq += 1
            td = TunnelData(conn_id=conn_id, seq=seq, data=data)
            _embed_tunnel_msg(app, td.to_bytes())
            st["last_active"] = time.time()

    except Exception as e:
        logging.warning("SOCKS5 客户端异常 conn=%s: %r", conn_id[:8] if conn_id else "?", e)
    finally:
        if conn_id and conn_id in tunnel_conns:
            tunnel_conns.pop(conn_id, None)
        try:
            writer.close()
        except Exception:
            pass
        if conn_id:
            await _send_tunnel_ctl(CTL_FIN)
            logging.info("SOCKS5 客户端断开: conn=%s", conn_id[:8])


async def run_socks_listener(app: web.Application, host: str, port: int, cfg: dict) -> asyncio.AbstractServer:
    """启动 SOCKS5 监听器。"""
    async def _client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await handle_socks_client(reader, writer, app, cfg)

    server = await asyncio.start_server(_client_connected, host=host, port=port)
    logging.info("SOCKS5 代理监听: %s:%d", host, port)
    return server


async def _tunnel_cleanup_loop(app: web.Application, idle_timeout_s: float = 300.0) -> None:
    """定期清理空闲/过期的隧道连接。"""
    while True:
        await asyncio.sleep(30)
        tunnel_conns: dict = app.get("tunnel_conns") or {}
        now = time.time()
        to_remove = []
        for conn_id, st in list(tunnel_conns.items()):
            last = st.get("last_active", st.get("created_at", now))
            if now - last > idle_timeout_s:
                to_remove.append(conn_id)
        for conn_id in to_remove:
            st = tunnel_conns.pop(conn_id, None)
            if st:
                try:
                    st["writer"].close()
                except Exception:
                    pass
                # 通知 server 关闭
                ctl = TunnelCtl(conn_id=conn_id, ctl_type=CTL_FIN)
                _embed_tunnel_msg(app, ctl.to_bytes(), is_ctl=True)
                logging.info("SOCKS5 空闲超时关闭: conn=%s", conn_id[:8])


async def run_client_proxy(
    *,
    host: str,
    port: int,
    session_id: str | None = None,
    control_url: str = "",
    server_url: str = "",
    gateway_url: str = "",
    socks_host: str = "127.0.0.1",
    socks_port: int = 0,
    socks_enabled: bool = False,
    socks_buffer_size: int = 32768,
    socks_connect_timeout: float = 30.0,
    socks_idle_timeout: float = 300.0,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = web.Application(client_max_size=256 * 1024 * 1024)
    setup_hls_state(app, session_id or "bishe-1")
    app["psk"] = None
    app["link_token"] = ""
    app["tunnel_conns"] = {}
    app["socks_enabled"] = socks_enabled

    control_url2 = (control_url or "").strip()
    server_url2 = (server_url or "").strip()
    if control_url2 and server_url2:
        async def _kex_background() -> None:
            client_base = f"http://{host}:{port}"
            priv, pub = generate_rsa_keypair(bits=2048)
            pub_b64 = b64e(rsa_pub_to_pem(pub))
            link = f"{client_base.rstrip('/')}|{server_url2.rstrip('/')}|{STEGO_METHOD_PSK_HMAC_INPLACE}"
            aad = link.encode("utf-8")
            async with httpx.AsyncClient(timeout=10.0, verify=False, trust_env=False) as client:
                while True:
                    try:
                        r = await client.post(
                            f"{control_url2.rstrip('/')}/issue",
                            json={
                                "role": "client",
                                "client_url": client_base,
                                "server_url": server_url2,
                                "a_url": client_base,
                                "c_url": server_url2,
                                "extract_method": STEGO_METHOD_PSK_HMAC_INPLACE,
                                "ttl_s": 600,
                                "pubkey": pub_b64,
                            },
                        )
                        r.raise_for_status()
                    except Exception as e:
                        logging.warning("client PSK 交换失败，10s 后重试: %r", e)
                        await asyncio.sleep(10)
                        continue
                    obj = r.json()
                    if obj.get("ok") is not True:
                        logging.warning("control /issue 返回错误，10s 后重试: %r", obj)
                        await asyncio.sleep(10)
                        continue
                    if obj.get("pending"):
                        await asyncio.sleep(float(obj.get("retry_after_s") or 0.5))
                        continue
                    bundle = obj.get("bundle") or {}
                    import json
                    try:
                        ek_psk = b64d(str(bundle.get("ek_psk") or ""))
                        nonce = b64d(str(bundle.get("nonce") or ""))
                        ct = b64d(str(bundle.get("ciphertext") or ""))
                        psk = rsa_oaep_unwrap(recipient_priv=priv, wrapped=ek_psk)
                        pt = aead_decrypt(key32=psk, nonce=nonce, ciphertext=ct, aad=aad)
                        data = json.loads(pt.decode("utf-8"))
                        psk_b64 = b64e(psk)
                        token = str(data.get("token") or "")
                    except Exception as e:
                        logging.warning("client PSK 解密失败，10s 后重试: %r", e)
                        await asyncio.sleep(10)
                        continue
                    if not psk_b64 or not token:
                        logging.warning("control bundle 缺少字段，10s 后重试")
                        await asyncio.sleep(10)
                        continue
                    app["psk"] = b64d(psk_b64)
                    app["link_token"] = token
                    logging.info("client 已从 control 获取 PSK（数据面加密启用）")
                    gw = (gateway_url or "").strip()
                    if gw:
                        try:
                            async with httpx.AsyncClient(timeout=10.0, verify=False, trust_env=False) as gw_client:
                                rr = await gw_client.post(
                                    f"{gw.rstrip('/')}/register",
                                    json={"role": "client", "token": token, "psk_b64": b64e(psk)},
                                )
                                rr.raise_for_status()
                                logging.info("client 已向 gateway 注册（role=client）")
                        except Exception as e:
                            logging.warning("client 向 gateway 注册失败，10s 后重试: %r", e)
                            await asyncio.sleep(10)
                            continue
                    return  # PSK 交换成功
        asyncio.create_task(_kex_background())

    app.router.add_get("/health", handle_health)
    app.router.add_post("/overlay/stop-notice", handle_stop_notice)
    app.router.add_post("/overlay/error-notice", handle_error_notice)
    app.router.add_post("/overlay/embed-hls", handle_overlay_embed_hls)
    # SOCKS5 反向通道接收端点
    app.router.add_post("/overlay/recv-tunnel", handle_recv_tunnel)
    # HLS 路由（正向）
    register_hls_routes(app, "/hls")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    logging.info(
        "client 节点启动(HLS 伪装): http://%s:%d session_id=%s — master / index / seg",
        host,
        port,
        app["session_id"],
    )
    await site.start()

    # SOCKS5 监听器
    socks_server = None
    if socks_enabled and socks_port > 0:
        socks_cfg = {
            "buffer_size": socks_buffer_size,
            "connect_timeout": socks_connect_timeout,
        }
        socks_server = await run_socks_listener(app, socks_host, socks_port, socks_cfg)
        asyncio.create_task(_tunnel_cleanup_loop(app, idle_timeout_s=socks_idle_timeout))

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        if socks_server:
            socks_server.close()
            await socks_server.wait_closed()
