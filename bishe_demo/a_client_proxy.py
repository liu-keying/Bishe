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
    STEGO_MAGIC,
    STEGO_METHOD_APPEND,
    STEGO_METHOD_PSK_HMAC_INPLACE,
    STEGO_TAG_LEN,
    SUPPORTED_STEGO_METHODS,
    embed_pad_inplace,
    embed_psk_inplace,
    embed_trailer,
)


# 188B 伪 TS 包（同步字节 0x47），作外层「类分片」填充
_FAKE_TS_PACKET = b"\x47\x40\x00\x10" + b"\x00" * 184
# 嵌入后的整段字节再切分，模拟多段 HLS GET（字节边界，非 ffmpeg 真切片）
MEDIA_CHUNK_BYTES = 256 * 1024


@dataclass
class PendingPullJob:
    seq: int
    t0_ms: int
    kind: str  # direct | media
    c_url: str
    ua: str
    a_recv_ms: int
    content_type: str
    """direct: 用户原始字节；media: 已嵌入隐匿数据的完整媒体字节"""
    blob: bytes
    extract_method: str = STEGO_METHOD_PSK_HMAC_INPLACE
    """入队时 A 的会话 ID；拉片 URL 须与该值一致，媒体出队后 A 会轮换到下一 session。"""
    emit_session: str = ""
    media_group_id: str = ""
    chunk_index: int = 0
    chunk_total: int = 1
    # 真 HLS 多段时 0..hls_total-1；仅最后一片含隐匿；1/1 兼容旧单段 embed
    hls_index: int = 0
    hls_total: int = 1
    # 将一次 AEAD 密文拆成 k 份分散到多个 TS 分片尾部时携带（k=1 表示旧行为）
    cipher_group: str = ""
    cipher_k: int = 0
    cipher_index: int = 0
    # encrypt_hidden 生成的真实密文总长度（未做 k*g 填充前）
    cipher_bytes: int = 0
    overlay_phase: str = "media"  # media | pad
    # psk_hmac_inplace：密文片段长度（不含 tag），供 B 按 offset 提取
    stego_frag_body_len: int = 0


@dataclass(frozen=True)
class AConfig:
    host: str
    port: int


def parse_initial_session_seq(session_arg: str | None) -> int:
    """--session 形如 bishe-3 时取起始序号；其它字符串（如旧版 bishe-fixed-session）回退为 1。"""
    if not session_arg or not str(session_arg).strip():
        return 1
    t = str(session_arg).strip()
    m = re.fullmatch(r"bishe-(\d+)", t, flags=re.IGNORECASE)
    return int(m.group(1)) if m else 1


def _segment_response(job: PendingPullJob, *, next_session: str | None = None) -> web.Response:
    em = (job.extract_method or STEGO_METHOD_PSK_HMAC_INPLACE).strip()
    if job.kind == "media":
        body = job.blob
        bishe_kind = "media"
    else:
        body = embed_trailer(_FAKE_TS_PACKET, job.blob)
        bishe_kind = "direct"
        em = "append_marker"

    headers = {
        "Content-Type": "video/mp2t",
        "X-Bishe-Kind": bishe_kind,
        "X-Bishe-Stego": em,
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
    if bishe_kind == "media" and job.chunk_total > 1 and job.media_group_id:
        headers["X-Bishe-Media-Group"] = job.media_group_id
        headers["X-Bishe-Chunk-Index"] = str(job.chunk_index)
        headers["X-Bishe-Chunk-Total"] = str(job.chunk_total)
    if bishe_kind == "media" and job.hls_total > 1:
        headers["X-Bishe-HLS-Index"] = str(job.hls_index)
        headers["X-Bishe-HLS-Total"] = str(job.hls_total)
    if bishe_kind == "media" and int(job.cipher_k or 0) > 0:
        headers["X-Bishe-Cipher-Group"] = str(job.cipher_group or "")
        headers["X-Bishe-Cipher-K"] = str(int(job.cipher_k))
        headers["X-Bishe-Cipher-Index"] = str(int(job.cipher_index))
        if int(job.cipher_bytes or 0) > 0:
            headers["X-Bishe-Cipher-Bytes"] = str(int(job.cipher_bytes))
        if em == STEGO_METHOD_PSK_HMAC_INPLACE and int(job.stego_frag_body_len or 0) > 0:
            headers["X-Chunk-Size"] = str(int(job.stego_frag_body_len))
    return web.Response(status=200, body=body, headers=headers)


def _hls_url_session_allowed(app: web.Application, url_sid: str) -> bool:
    if url_sid == app["session_id"]:
        return True
    return any(j.emit_session == url_sid for j in app["pull_queue"])


async def handle_hls_master(request: web.Request) -> web.Response:
    """HLS 主列表：指向 index.m3u8（单码率，伪装成点播/直播入口）。"""
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
    """
    媒体播放列表：**peek** 队列，一次性列出当前会话下待发的前缀中所有 seg-XXXXXX.ts（不消费队列）。
    带 #EXT-X-PLAYLIST-TYPE:VOD 与 #EXT-X-ENDLIST，接近点播 HLS；无待发时仅返回头，模拟直播等待。
    """
    app = request.app
    url_sid = request.match_info["session_id"]
    if not _hls_url_session_allowed(app, url_sid):
        return web.Response(status=404)

    q: deque[PendingPullJob] = app["pull_queue"]
    head = q[0] if q else None
    # 若队首仍是旧 session，则对新 session 返回“空列表”而不是 404：
    # b-pull 将继续轮询，直到该 session 的分片真正入队到队首。
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
    """
    HLS 分片：GET 的 seg 必须与队首任务 seq 一致，成功则 **popleft** 并返回类 TS 二进制。
    媒介帧出队后轮换 session，并通过 X-Next-Session 通知 B-pull。
    """
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
    if job.kind == "media":
        last_chunk = job.chunk_total <= 1 or job.chunk_index == job.chunk_total - 1
        last_hls = job.hls_total <= 1 or job.hls_index == job.hls_total - 1
        if last_chunk and last_hls:
            # session 的轮换由 /overlay/embed-hls 在入队时完成；这里仅把“该 session 的下一跳”告知拉片端。
            # 注意：全局 session_id 可能已被并发入队推进到更大值，不能直接拿它当 next。
            next_map: dict[str, str] = app.get("session_next") or {}
            next_session = next_map.get(job.emit_session) or ""
            if next_session:
                # 已完成该 session 的最后一片，next 映射可释放
                next_map.pop(job.emit_session, None)
                app["session_next"] = next_map

    return _segment_response(job, next_session=next_session)


def _enqueue(app: web.Application, job: PendingPullJob) -> None:
    q: deque[PendingPullJob] = app["pull_queue"]
    q.append(job)


def _remember_for_retry(app: web.Application, job: PendingPullJob) -> None:
    """
    记录最近一次 media（用于 B 校验失败后请求 A 重传）。
    只做最小实现：覆盖式保存最后一组，避免无限增长占内存。
    """
    if job.kind == "media":
        # 兼容两种回调定位方式：
        # - 按 emit_session 精确重传（推荐，避免并发/多组时重传错组）
        # - 回退到“最近一次”重传（历史行为）
        by_sess = app.get("retry_media_by_session")
        if not isinstance(by_sess, dict):
            by_sess = {}
        emit_sess = (job.emit_session or "").strip()
        if emit_sess not in by_sess or not isinstance(by_sess.get(emit_sess), list):
            by_sess[emit_sess] = []
        entry = {
            "c_url": job.c_url,
            "extract_method": job.extract_method,
            "content_type": job.content_type,
            "blob": job.blob,
            "hls_index": job.hls_index,
            "hls_total": job.hls_total,
            "chunk_index": job.chunk_index,
            "chunk_total": job.chunk_total,
            "media_group_id": job.media_group_id,
            "overlay_phase": job.overlay_phase,
            "cipher_group": job.cipher_group,
            "cipher_k": job.cipher_k,
            "cipher_index": job.cipher_index,
            "cipher_bytes": job.cipher_bytes,
            "stego_frag_body_len": job.stego_frag_body_len,
        }
        by_sess[emit_sess].append(entry)
        app["retry_media_by_session"] = by_sess

        # 保留旧字段：总是指向“最近一次追加的 media”
        app["retry_media"] = by_sess[emit_sess]
        return


def _clear_retry_state(app: web.Application) -> None:
    app["retry_media"] = []
    app["retry_media_by_session"] = {}


# POST /proxy（direct）已关闭：隐匿数据仅通过 /overlay/embed-hls（多段 TS 载体）。


async def handle_overlay_embed_hls(request: web.Request) -> web.Response:
    """
    多段真实 TS（如 ffmpeg 输出）：multipart 多个同名字段 segment（按提交顺序）。

    默认（k=1）：仅在最后一段末尾嵌入一次 AEAD 密文（旧行为）。

    方案 B（k>1）：对整段 hidden 做一次 AEAD 得到 cipher，再将 cipher 均分为 k 份，
    随机选择 k 个分片索引（按 HLS 顺序仍保持 0..n-1），在每个选中分片末尾分别 embed 对应分片密文；
    未选中的分片可选择追加固定长度伪 TS 填充（overlay_phase=pad），用于模拟“非携带分片”的字节形态。
    """
    try:
        session_id: str = request.app["session_id"]

        c_url = (request.query.get("c") or "").strip() or (request.headers.get("X-C-URL", "").strip())
        extract_method = (
            (request.query.get("extract") or request.query.get("e") or "").strip() or STEGO_METHOD_PSK_HMAC_INPLACE
        )
        if extract_method not in SUPPORTED_STEGO_METHODS:
            return web.json_response(
                {
                    "ok": False,
                    "error": f"unsupported extract query (supported: {sorted(SUPPORTED_STEGO_METHODS)})",
                },
                status=400,
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

        plain_hidden = hidden
        plain_len = len(plain_hidden)

        psk: bytes | None = request.app.get("psk")
        if psk is None:
            return web.json_response({"ok": False, "error": "PSK not ready (start A with --e-url and wait kex)"}, status=503)
        if extract_method == STEGO_METHOD_PSK_HMAC_INPLACE:
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
        # HLS overlay：chunk_sz 以第一个分片长度为准（用于最后一片加 hidden 后的再切分）
        chunk_sz = len(segment_parts[0]) or MEDIA_CHUNK_BYTES

        # k=1：保持旧行为（仅最后一片 embed；过大则按 chunk_sz 再切分）
        k_raw = (request.query.get("k") or request.query.get("cipher_k") or "").strip()
        k = int(k_raw) if k_raw.isdigit() else 1
        if k < 1:
            return web.json_response({"ok": False, "error": "k must be >= 1"}, status=400)
        if k > n:
            return web.json_response({"ok": False, "error": f"k must be <= number of segments (k={k}, n={n})"}, status=400)

        # 数据面 AEAD：AAD 仅绑定到会话标识，避免与分片拆分/重组策略耦合
        aad = str(emit_sess).encode("utf-8")
        try:
            hidden = encrypt_hidden(psk=psk, hidden=hidden, aad=aad)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"encrypt hidden failed: {e!r}"}, status=400)

        g_bytes = 0
        pad_raw = (request.query.get("pad_bytes") or "").strip()
        # 默认不追加填充；仅在显式指定 pad_bytes>0 时追加伪 TS 字节（用于实验对比）
        pad_bytes = int(pad_raw) if pad_raw.isdigit() else 0
        pad_bytes = max(0, min(pad_bytes, 16 * 1024 * 1024))

        first_seq = request.app["seq"]
        group_id: str | None = None
        last_seq = first_seq
        cipher_group_id: str | None = None

        def _split_equal(data: bytes, parts: int) -> list[bytes]:
            if parts <= 0:
                raise ValueError("parts must be positive")
            m = len(data)
            base = m // parts
            rem = m % parts
            out: list[bytes] = []
            off = 0
            for i in range(parts):
                ln = base + (1 if i < rem else 0)
                out.append(data[off : off + ln])
                off += ln
            return out

        _clear_retry_state(request.app)

        if k == 1:
            # 旧路径：前 n-1 段原样；最后一段 embed（可能再切分）
            for i in range(n - 1):
                seq = request.app["seq"]
                request.app["seq"] = seq + 1
                last_seq = seq
                blob0 = segment_parts[i]
                phase0 = "media"
                if pad_bytes > 0:
                    pad = (_FAKE_TS_PACKET * ((pad_bytes + len(_FAKE_TS_PACKET) - 1) // len(_FAKE_TS_PACKET)))[:pad_bytes]
                    blob0 = blob0 + pad
                    phase0 = "pad"
                job = PendingPullJob(
                    seq=seq,
                    t0_ms=t0_ms,
                    kind="media",
                    c_url=c_url,
                    ua=ua,
                    a_recv_ms=recv_ms,
                    content_type="video/mp2t",
                    blob=blob0,
                    extract_method=extract_method,
                    emit_session=emit_sess,
                    media_group_id="",
                    chunk_index=0,
                    chunk_total=1,
                    hls_index=i,
                    hls_total=n,
                    cipher_group="",
                    cipher_k=0,
                    cipher_index=0,
                    overlay_phase=phase0,
                )
                _enqueue(request.app, job)

            last_raw = segment_parts[-1]
            try:
                if extract_method == STEGO_METHOD_APPEND:
                    embedded_last = embed_trailer(last_raw, hidden)
                else:
                    embedded_last = embed_psk_inplace(
                        last_raw,
                        token=link_token,
                        hls_index=n - 1,
                        frag_idx=0,
                        cipher_fragment=hidden,
                    )
            except Exception as e:
                return web.json_response({"ok": False, "error": f"embed failed: {e!r}"}, status=400)

            stego_body = len(hidden)
            if extract_method == STEGO_METHOD_PSK_HMAC_INPLACE and len(embedded_last) > chunk_sz:
                return web.json_response(
                    {
                        "ok": False,
                        "error": (
                            "psk_hmac_inplace: last segment after embed exceeds first-segment chunk_sz; "
                            "use larger TS segments or append_marker for oversized last segment."
                        ),
                    },
                    status=400,
                )

            if len(embedded_last) <= chunk_sz:
                seq = request.app["seq"]
                request.app["seq"] = seq + 1
                last_seq = seq
                job = PendingPullJob(
                    seq=seq,
                    t0_ms=t0_ms,
                    kind="media",
                    c_url=c_url,
                    ua=ua,
                    a_recv_ms=recv_ms,
                    content_type="video/mp2t",
                    blob=embedded_last,
                    extract_method=extract_method,
                    emit_session=emit_sess,
                    media_group_id="",
                    chunk_index=0,
                    chunk_total=1,
                    hls_index=n - 1,
                    hls_total=n,
                    cipher_group="",
                    cipher_k=0,
                    cipher_index=0,
                    overlay_phase="media",
                    stego_frag_body_len=stego_body,
                )
                _enqueue(request.app, job)
                _remember_for_retry(request.app, job)
                media_chunks = 1
            else:
                gid = uuid.uuid4().hex
                group_id = gid
                chunks = [embedded_last[i : i + chunk_sz] for i in range(0, len(embedded_last), chunk_sz)]
                nc = len(chunks)
                for ci, blob in enumerate(chunks):
                    if len(blob) < chunk_sz:
                        blob = blob + os.urandom(chunk_sz - len(blob))
                    seq = request.app["seq"]
                    request.app["seq"] = seq + 1
                    last_seq = seq
                    job = PendingPullJob(
                        seq=seq,
                        t0_ms=t0_ms,
                        kind="media",
                        c_url=c_url,
                        ua=ua,
                        a_recv_ms=recv_ms,
                        content_type="video/mp2t",
                        blob=blob,
                        extract_method=extract_method,
                        emit_session=emit_sess,
                        media_group_id=gid,
                        chunk_index=ci,
                        chunk_total=nc,
                        hls_index=n - 1,
                        hls_total=n,
                        cipher_group="",
                        cipher_k=0,
                        cipher_index=0,
                        overlay_phase="media",
                        stego_frag_body_len=stego_body,
                    )
                    _enqueue(request.app, job)
                    _remember_for_retry(request.app, job)
                media_chunks = nc
        else:
            # 方案 B（新版设计）：
            # - 整包密文按固定大小 g（bits/bytes）切成 k 份，随机散入 k 个 TS 分片尾部；
            # - 其余分片尾部随机补齐同等“流量增量”，以维持体积特征；
            # - 不再与首分片长度 chunk_sz 对齐/比较（也不做再切块）。
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
                # 默认：每份至少能容纳当前密文均分后的长度（向上取整）
                g_bytes = (len(cipher) + k - 1) // k

            g_bytes = max(1, min(int(g_bytes), 16 * 1024 * 1024))
            pad_psk_len = STEGO_TAG_LEN + g_bytes
            pad_append_len = len(STEGO_MAGIC) + 4 + g_bytes

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
                    blob0 = segment_parts[i]
                    if extract_method == STEGO_METHOD_APPEND:
                        blob0 = blob0 + os.urandom(pad_append_len)
                    else:
                        blob0 = embed_pad_inplace(
                            blob0,
                            token=link_token,
                            hls_index=i,
                            pad_len=pad_psk_len,
                        )
                    job = PendingPullJob(
                        seq=seq,
                        t0_ms=t0_ms,
                        kind="media",
                        c_url=c_url,
                        ua=ua,
                        a_recv_ms=recv_ms,
                        content_type="video/mp2t",
                        blob=blob0,
                        extract_method=extract_method,
                        emit_session=emit_sess,
                        media_group_id="",
                        chunk_index=0,
                        chunk_total=1,
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
                    if extract_method == STEGO_METHOD_APPEND:
                        blob = embed_trailer(segment_parts[i], piece)
                    else:
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
                    kind="media",
                    c_url=c_url,
                    ua=ua,
                    a_recv_ms=recv_ms,
                    content_type="video/mp2t",
                    blob=blob,
                    extract_method=extract_method,
                    emit_session=emit_sess,
                    media_group_id="",
                    chunk_index=0,
                    chunk_total=1,
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

        # 一组 HLS 媒体入队完成后，轮换到下一 session，避免并发提交导致多组共享同一 emit_session。
        # 同时记录 emit_sess -> next_sess 映射，供最后一片通过 X-Next-Session 引导 b-pull 顺序跟随。
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
            "extract": {"method": extract_method},
            "seq_from": first_seq,
            "seq_to": last_seq,
            "cipher_k": k,
            "pad_bytes": pad_bytes,
            "g_bytes": int(g_bytes) if k > 1 else 0,
            "g_bits": int(g_bytes) * 8 if k > 1 else 0,
            "plain_hidden_bytes": plain_len,
            "cipher_bytes": len(hidden),
        }
        if cipher_group_id:
            body_out["cipher_group"] = cipher_group_id
        if group_id:
            body_out["media_group_id"] = group_id
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
    """
    B 校验/拆解失败的回调：A 收到后将最近一次 media 重新入队，供 B 重新拉取。
    最小实现：重发“上一组”，并发送到当前 session（由 A 的轮换机制决定下一次拉取的会话）。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    logging.warning("A 收到 error-notice: %s", body)

    # 优先按 B 提供的 session_id 精确定位那一组 media，避免并发/多组时重传错组。
    sid = str(body.get("session_id") or body.get("emit_session") or body.get("X-Session") or "").strip()
    by_sess = request.app.get("retry_media_by_session") or {}
    media: list[dict] = []
    if sid and isinstance(by_sess, dict):
        m0 = by_sess.get(sid)
        if isinstance(m0, list):
            media = m0

    # 回退：保持旧行为（重传最近一次）
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

    # 重发 media（按记录的顺序）
    for m in media:
        seq = request.app["seq"]
        request.app["seq"] = seq + 1
        last_seq = seq
        job_media = PendingPullJob(
            seq=seq,
            t0_ms=t0_ms,
            kind="media",
            c_url=str(m.get("c_url") or ""),
            ua=ua,
            a_recv_ms=recv_ms,
            content_type=str(m.get("content_type") or "application/octet-stream"),
            blob=m.get("blob") or b"",
            extract_method=str(m.get("extract_method") or STEGO_METHOD_PSK_HMAC_INPLACE),
            emit_session=emit_sess,
            media_group_id=str(m.get("media_group_id") or ""),
            chunk_index=int(m.get("chunk_index") or 0),
            chunk_total=int(m.get("chunk_total") or 1),
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
    app["cfg"] = AConfig(host=host, port=port)
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
    # app.router.add_post("/proxy", handle_proxy)  # direct 已禁用，见文件内说明
    app.router.add_post("/overlay/embed-hls", handle_overlay_embed_hls)
    app.router.add_get("/hls/{session_id}/master.m3u8", handle_hls_master)
    app.router.add_get("/hls/{session_id}/index.m3u8", handle_hls_playlist)
    app.router.add_get("/hls/{session_id}/seg-{seg}.ts", handle_hls_segment)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    logging.info(
        "A 节点启动(HLS 伪装): http://%s:%d session_id=%s（媒介分片出队后自动 bishe-2,3,...）— master / index / seg",
        host,
        port,
        app["session_id"],
    )
    await site.start()

    while True:
        await asyncio.sleep(3600)
