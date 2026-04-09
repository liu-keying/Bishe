from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import deque
from dataclasses import dataclass

from aiohttp import web

from .common import now_ms
from .stego import SUPPORTED_STEGO_METHODS, embed_trailer, embed_ts_packets


# 188B 伪 TS 包（同步字节 0x47），作外层「类分片」填充
_FAKE_TS_PACKET = b"\x47\x40\x00\x10" + b"\x00" * 184
# 嵌入后的整段字节再切分，模拟多段 HLS GET（字节边界，非 ffmpeg 真切片）
MEDIA_CHUNK_BYTES = 256 * 1024


@dataclass
class PendingPullJob:
    seq: int
    t0_ms: int
    kind: str  # direct | control | media
    c_url: str
    ua: str
    a_recv_ms: int
    content_type: str
    """direct: 用户原始字节；media: 已 embed_trailer 的完整媒体字节"""
    blob: bytes
    """control: JSON 内层（将再被外层 embed_trailer 或 embed_ts_packets 包一层）"""
    control_inner: bytes | None = None
    extract_method: str = "append_marker"
    """入队时 A 的会话 ID；拉片 URL 须与该值一致，媒体出队后 A 会轮换到下一 session。"""
    emit_session: str = ""
    media_group_id: str = ""
    chunk_index: int = 0
    chunk_total: int = 1
    # 真 HLS 多段时 0..hls_total-1；仅最后一片含隐匿；1/1 兼容旧单段 embed
    hls_index: int = 0
    hls_total: int = 1


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
    em = (job.extract_method or "append_marker").strip()
    if job.kind == "media":
        body = job.blob
        bishe_kind = "media"
    elif job.kind == "control":
        inner = job.control_inner or b"{}"
        if em == "ts_private":
            body = embed_ts_packets(inner)
        else:
            body = embed_trailer(_FAKE_TS_PACKET, inner)
        bishe_kind = "control"
    else:
        body = embed_trailer(_FAKE_TS_PACKET, job.blob)
        bishe_kind = "direct"
        em = "append_marker"

    headers = {
        "Content-Type": "video/mp2t",
        "X-Bishe-Kind": bishe_kind,
        "X-Bishe-Stego": em,
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
    if head and url_sid != head.emit_session:
        return web.Response(status=404)
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
            app["session_seq"] = int(app["session_seq"]) + 1
            app["session_id"] = f"bishe-{app['session_seq']}"
            next_session = app["session_id"]

    return _segment_response(job, next_session=next_session)


def _enqueue(app: web.Application, job: PendingPullJob) -> None:
    q: deque[PendingPullJob] = app["pull_queue"]
    q.append(job)


# POST /proxy（direct）已关闭：隐匿数据仅通过 /overlay/control + /overlay/embed（视频载体）。


async def handle_overlay_control(request: web.Request) -> web.Response:
    session_id: str = request.app["session_id"]
    seq: int = request.app["seq"]
    request.app["seq"] = seq + 1

    body = await request.read()
    try:
        obj = json.loads(body.decode("utf-8") if body else "{}")
    except Exception as e:
        raw_preview = body[:200].decode("utf-8", errors="replace")
        return web.json_response(
            {"ok": False, "error": f"invalid json: {e!r}", "raw_preview": raw_preview},
            status=400,
        )

    c_url = (
        str(obj.get("c_url") or "").strip()
        or (request.query.get("c") or "").strip()
        or (request.headers.get("X-C-URL", "").strip())
    )
    extract = obj.get("extract") or {"method": "append_marker"}
    if not isinstance(extract, dict):
        return web.json_response({"ok": False, "error": "extract must be an object"}, status=400)
    method = str(extract.get("method") or "append_marker").strip()
    if method not in SUPPORTED_STEGO_METHODS:
        return web.json_response(
            {
                "ok": False,
                "error": f"unsupported extract.method: {method} (supported: {sorted(SUPPORTED_STEGO_METHODS)})",
            },
            status=400,
        )

    inner = json.dumps(
        {"c_url": c_url, "extract": {"method": method}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    job = PendingPullJob(
        seq=seq,
        t0_ms=now_ms(),
        kind="control",
        c_url=c_url,
        ua=request.headers.get("User-Agent", ""),
        a_recv_ms=now_ms(),
        content_type="application/json",
        blob=b"",
        control_inner=inner,
        extract_method=method,
        emit_session=request.app["session_id"],
    )
    _enqueue(request.app, job)
    # 下一条 /overlay/embed 若未带 extract 查询参数，自动与同一次实验的 control 对齐，避免 ts_private / append_marker 混用
    request.app["pending_media_extract"] = method

    return web.json_response(
        {
            "ok": True,
            "queued_for_pull": True,
            "session_id": session_id,
            "seq": seq,
            "phase": "control",
            "c_url": c_url,
            "extract": {"method": method},
        }
    )


async def handle_overlay_embed(request: web.Request) -> web.Response:
    try:
        session_id: str = request.app["session_id"]

        c_url = (request.query.get("c") or "").strip() or (request.headers.get("X-C-URL", "").strip())
        q_extract = (request.query.get("extract") or request.query.get("e") or "").strip()
        if q_extract:
            extract_method = q_extract
        else:
            extract_method = (request.app.get("pending_media_extract") or "append_marker").strip()
        if extract_method not in SUPPORTED_STEGO_METHODS:
            return web.json_response(
                {
                    "ok": False,
                    "error": f"unsupported extract query (supported: {sorted(SUPPORTED_STEGO_METHODS)})",
                },
                status=400,
            )

        video: bytes | None = None
        hidden: bytes | None = None
        if request.content_type and "multipart/form-data" in request.content_type:
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                name = part.name or ""
                if name == "video":
                    video = await part.read()
                elif name == "hidden":
                    hidden = await part.read()
        else:
            return web.json_response(
                {"ok": False, "error": "use multipart/form-data with fields video and hidden"},
                status=400,
            )

        if not video:
            return web.json_response({"ok": False, "error": "missing multipart field video"}, status=400)
        if hidden is None:
            return web.json_response({"ok": False, "error": "missing multipart field hidden"}, status=400)

        try:
            if extract_method == "ts_private":
                embedded = video + embed_ts_packets(hidden)
            else:
                embedded = embed_trailer(video, hidden)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"embed failed: {e!r}"}, status=400)

        t0_ms = now_ms()
        ua = request.headers.get("User-Agent", "")
        recv_ms = now_ms()
        emit_sess = request.app["session_id"]
        chunk_sz = MEDIA_CHUNK_BYTES
        q_chunk = (request.query.get("chunk_bytes") or "").strip()
        if q_chunk.isdigit() and int(q_chunk) >= 1024:
            chunk_sz = min(int(q_chunk), 16 * 1024 * 1024)

        if len(embedded) <= chunk_sz:
            seq = request.app["seq"]
            request.app["seq"] = seq + 1
            job = PendingPullJob(
                seq=seq,
                t0_ms=t0_ms,
                kind="media",
                c_url=c_url,
                ua=ua,
                a_recv_ms=recv_ms,
                content_type="video/mp4",
                blob=embedded,
                control_inner=None,
                extract_method=extract_method,
                emit_session=emit_sess,
                media_group_id="",
                chunk_index=0,
                chunk_total=1,
            )
            _enqueue(request.app, job)
            media_chunks = 1
            first_seq = last_seq = seq
            group_id: str | None = None
        else:
            gid = uuid.uuid4().hex
            chunks = [embedded[i : i + chunk_sz] for i in range(0, len(embedded), chunk_sz)]
            n = len(chunks)
            first_seq = request.app["seq"]
            for i, blob in enumerate(chunks):
                seq = request.app["seq"]
                request.app["seq"] = seq + 1
                job = PendingPullJob(
                    seq=seq,
                    t0_ms=t0_ms,
                    kind="media",
                    c_url=c_url,
                    ua=ua,
                    a_recv_ms=recv_ms,
                    content_type="video/mp4",
                    blob=blob,
                    control_inner=None,
                    extract_method=extract_method,
                    emit_session=emit_sess,
                    media_group_id=gid,
                    chunk_index=i,
                    chunk_total=n,
                )
                _enqueue(request.app, job)
            last_seq = request.app["seq"] - 1
            media_chunks = n
            group_id = gid

        request.app["pending_media_extract"] = None

        body_out: dict = {
            "ok": True,
            "queued_for_pull": True,
            "session_id": session_id,
            "phase": "media",
            "video_bytes": len(video),
            "hidden_bytes": len(hidden),
            "embedded_bytes": len(embedded),
            "c_url": c_url,
            "extract": {"method": extract_method},
            "media_chunks": media_chunks,
            "seq_from": first_seq,
            "seq_to": last_seq,
            "chunk_bytes": chunk_sz,
        }
        if group_id:
            body_out["media_group_id"] = group_id
        body_out["seq"] = last_seq
        return web.json_response(body_out)
    except Exception as e:
        logging.exception("A /overlay/embed 处理异常: %r", e)
        return web.json_response({"ok": False, "error": f"internal error: {e!r}"}, status=500)


async def handle_overlay_embed_hls(request: web.Request) -> web.Response:
    """
    多段真实 TS（如 ffmpeg 输出）：multipart 多个同名字段 segment（按提交顺序），
    仅在最后一段末尾嵌入 hidden；前面各段原样出队供播放器/B 按 HLS 顺序拉取。
    """
    try:
        session_id: str = request.app["session_id"]

        c_url = (request.query.get("c") or "").strip() or (request.headers.get("X-C-URL", "").strip())
        q_extract = (request.query.get("extract") or request.query.get("e") or "").strip()
        if q_extract:
            extract_method = q_extract
        else:
            extract_method = (request.app.get("pending_media_extract") or "append_marker").strip()
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

        n = len(segment_parts)
        t0_ms = now_ms()
        ua = request.headers.get("User-Agent", "")
        recv_ms = now_ms()
        emit_sess = request.app["session_id"]
        chunk_sz = MEDIA_CHUNK_BYTES
        q_chunk = (request.query.get("chunk_bytes") or "").strip()
        if q_chunk.isdigit() and int(q_chunk) >= 1024:
            chunk_sz = min(int(q_chunk), 16 * 1024 * 1024)

        first_seq = request.app["seq"]
        group_id: str | None = None
        last_seq = first_seq

        for i in range(n - 1):
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
                blob=segment_parts[i],
                control_inner=None,
                extract_method=extract_method,
                emit_session=emit_sess,
                media_group_id="",
                chunk_index=0,
                chunk_total=1,
                hls_index=i,
                hls_total=n,
            )
            _enqueue(request.app, job)

        last_raw = segment_parts[-1]
        try:
            if extract_method == "ts_private":
                embedded_last = last_raw + embed_ts_packets(hidden)
            else:
                embedded_last = embed_trailer(last_raw, hidden)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"embed failed: {e!r}"}, status=400)

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
                control_inner=None,
                extract_method=extract_method,
                emit_session=emit_sess,
                media_group_id="",
                chunk_index=0,
                chunk_total=1,
                hls_index=n - 1,
                hls_total=n,
            )
            _enqueue(request.app, job)
            media_chunks = 1
        else:
            gid = uuid.uuid4().hex
            group_id = gid
            chunks = [embedded_last[i : i + chunk_sz] for i in range(0, len(embedded_last), chunk_sz)]
            nc = len(chunks)
            for ci, blob in enumerate(chunks):
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
                    control_inner=None,
                    extract_method=extract_method,
                    emit_session=emit_sess,
                    media_group_id=gid,
                    chunk_index=ci,
                    chunk_total=nc,
                    hls_index=n - 1,
                    hls_total=n,
                )
                _enqueue(request.app, job)
            media_chunks = nc

        request.app["pending_media_extract"] = None

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
            "embedded_last_bytes": len(embedded_last),
            "hidden_bytes": len(hidden),
        }
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


async def run_a_proxy(*, host: str, port: int, session_id: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    app = web.Application(client_max_size=256 * 1024 * 1024)
    app["cfg"] = AConfig(host=host, port=port)
    seq0 = parse_initial_session_seq(session_id)
    app["session_seq"] = seq0
    app["session_id"] = f"bishe-{seq0}"
    app["seq"] = 1
    app["pull_queue"] = deque[PendingPullJob]()
    app["pending_media_extract"] = None

    app.router.add_get("/health", handle_health)
    app.router.add_post("/overlay/stop-notice", handle_stop_notice)
    # app.router.add_post("/proxy", handle_proxy)  # direct 已禁用，见文件内说明
    app.router.add_post("/overlay/control", handle_overlay_control)
    app.router.add_post("/overlay/embed", handle_overlay_embed)
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
