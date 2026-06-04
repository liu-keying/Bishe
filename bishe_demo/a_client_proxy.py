from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import uuid
from collections import deque
from dataclasses import dataclass

import httpx
from aiohttp import web

from .common import now_ms
from .crypto_box import aead_decrypt, b64d, b64e, generate_rsa_keypair, rsa_oaep_unwrap, rsa_pub_to_pem
from .psk_aead import encrypt_hidden
from .stego import (
    STEGO_METHOD_PSK_HMAC_INPLACE,
    STEGO_TAG_LEN,
    embed_pad_inplace,
    embed_psk_inplace,
)


@dataclass
class PendingPullJob:
    seq: int
    t0_ms: int
    c_url: str
    ua: str
    a_recv_ms: int
    content_type: str
    blob: bytes
    """入队时 A 的会话 ID；拉片 URL 须与该值一致，媒体出队后 A 会轮换到下一 session。"""
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
        "X-C-URL": job.c_url,
        "X-Session": job.emit_session,
        "X-Seq": str(job.seq),
        "X-T0-MS": str(job.t0_ms),
        "X-Content-Type": job.content_type,
        "X-A-UA": job.ua,
        "X-A-Recv-MS": str(job.a_recv_ms),
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
    """B 校验失败时按 session 重传 media 片。"""
    if job.overlay_phase != "media":
        return
    by_sess = app.get("retry_media_by_session")
    if not isinstance(by_sess, dict):
        by_sess = {}
    emit_sess = (job.emit_session or "").strip()
    if emit_sess not in by_sess or not isinstance(by_sess.get(emit_sess), list):
        by_sess[emit_sess] = []
    entry = {
        "c_url": job.c_url,
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

        c_url = (request.query.get("c") or "").strip() or (request.headers.get("X-C-URL", "").strip())

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
            return web.json_response({"ok": False, "error": "PSK not ready (start A with --e-url and wait kex)"}, status=503)
        if not str(request.app.get("link_token") or "").strip():
            return web.json_response(
                {"ok": False, "error": "link_token not ready (psk_hmac_inplace requires E kex to obtain token)"},
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
                    c_url=c_url,
                    ua=ua,
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
                c_url=c_url,
                ua=ua,
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
            "c_url": c_url,
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
        logging.exception("A /overlay/embed-hls 处理异常: %r", e)
        return web.json_response({"ok": False, "error": f"internal error: {e!r}"}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "session_id": request.app["session_id"]})


async def handle_stop_notice(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    logging.info("A 收到 stop-notice: %s", body)
    return web.json_response({"ok": True})


async def handle_error_notice(request: web.Request) -> web.Response:
    """B 校验失败：按 session 重入队 media。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    logging.warning("A 收到 error-notice: %s", body)

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
            c_url=str(m.get("c_url") or ""),
            ua=ua,
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


async def run_a_proxy(
    *,
    host: str,
    port: int,
    session_id: str | None = None,
    e_url: str = "",
    c_url: str = "",
    b_gate_url: str = "",
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    app = web.Application(client_max_size=256 * 1024 * 1024)
    seq0 = parse_initial_session_seq(session_id)
    app["session_seq"] = seq0
    app["session_id"] = f"bishe-{seq0}"
    app["session_next"] = {}
    app["seq"] = 1
    app["pull_queue"] = deque[PendingPullJob]()
    app["retry_media"] = []
    app["psk"] = None
    app["link_token"] = ""

    e_url2 = (e_url or "").strip()
    c_url2 = (c_url or "").strip()
    if e_url2 and c_url2:
        a_base = f"http://{host}:{port}"
        priv, pub = generate_rsa_keypair(bits=2048)
        pub_b64 = b64e(rsa_pub_to_pem(pub))
        link = f"{a_base.rstrip('/')}|{c_url2.rstrip('/')}|{STEGO_METHOD_PSK_HMAC_INPLACE}"
        aad = link.encode("utf-8")
        async with httpx.AsyncClient(timeout=10.0, verify=False, trust_env=False) as client:
            while True:
                r = await client.post(
                    f"{e_url2.rstrip('/')}/issue",
                    json={
                        "role": "a",
                        "a_url": a_base,
                        "c_url": c_url2,
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
                logging.info("A 已从 E 获取 PSK（数据面加密启用）")
                bg = (b_gate_url or "").strip()
                if bg:
                    rr = await client.post(
                        f"{bg.rstrip('/')}/register",
                        json={"role": "a", "token": token, "psk_b64": b64e(psk)},
                    )
                    rr.raise_for_status()
                    logging.info("A 已向 B-gate 注册（role=a）")
                break

    app.router.add_get("/health", handle_health)
    app.router.add_post("/overlay/stop-notice", handle_stop_notice)
    app.router.add_post("/overlay/error-notice", handle_error_notice)
    app.router.add_post("/overlay/embed-hls", handle_overlay_embed_hls)
    app.router.add_get("/hls/{session_id}/master.m3u8", handle_hls_master)
    app.router.add_get("/hls/{session_id}/index.m3u8", handle_hls_playlist)
    app.router.add_get("/hls/{session_id}/seg-{seg}.ts", handle_hls_segment)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    logging.info(
        "A 节点启动(HLS 伪装): http://%s:%d session_id=%s — master / index / seg",
        host,
        port,
        app["session_id"],
    )
    await site.start()

    while True:
        await asyncio.sleep(3600)
