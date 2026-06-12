"""
共享 HLS 服务模块。

从 a_client_proxy.py 提取，供 A（正向 /hls）和 C（反向 /hls-rev）共同复用。
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any

from aiohttp import web

from .common import now_ms
from .stego import STEGO_METHOD_PSK_HMAC_INPLACE

# ---- 重新导出 a_client_proxy 使用的 dataclass ----
# （a_client_proxy.py 中定义的 PendingPullJob 也在此处可用，
#   但为避免循环导入，hls_shared 不直接依赖 PendingPullJob，
#   handler 通过 app 状态与任务队列交互）


def parse_initial_session_seq(session_arg: str | None) -> int:
    """--session 形如 bishe-3 时取起始序号；其它字符串回退为 1。"""
    if not session_arg or not str(session_arg).strip():
        return 1
    t = str(session_arg).strip()
    m = re.fullmatch(r"bishe-(\d+)", t, flags=re.IGNORECASE)
    return int(m.group(1)) if m else 1


def setup_hls_state(app: web.Application, session_id_start: str, initial_seq: int = 1) -> None:
    """在 aiohttp app 上初始化 HLS 服务所需的状态键。"""
    seq0 = parse_initial_session_seq(session_id_start)
    app["session_seq"] = seq0
    app["session_id"] = f"bishe-{seq0}"
    app["session_next"]: dict[str, str] = {}
    app["seq"] = max(1, initial_seq)
    app["pull_queue"] = deque()
    app["retry_media"]: list[dict[str, Any]] = []
    app["retry_media_by_session"]: dict[str, list[dict[str, Any]]] = {}


def _hls_url_session_allowed(app: web.Application, url_sid: str) -> bool:
    if url_sid == app["session_id"]:
        return True
    return any(j.emit_session == url_sid for j in app["pull_queue"])


def _segment_response(job: Any, *, next_session: str | None = None) -> web.Response:
    """构建 TS 分片 HTTP 响应（带全部隐匿元数据头）。"""
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


def _playlist_job_prefix(q: deque, url_sid: str) -> list[Any]:
    """队首起连续且 emit_session 与 url_sid 一致的任务（不消费队列）。"""
    out: list[Any] = []
    for j in q:
        if j.emit_session != url_sid:
            break
        out.append(j)
    return out


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


async def handle_hls_playlist(request: web.Request) -> web.Response:
    """媒体播放列表：peek 队列，列出当前 session 下待发 seg-*.ts。"""
    app = request.app
    url_sid = request.match_info["session_id"]
    if not _hls_url_session_allowed(app, url_sid):
        return web.Response(status=404)

    q: deque = app["pull_queue"]
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
    q: deque = app["pull_queue"]
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


def register_hls_routes(app: web.Application, prefix: str) -> None:
    """在 aiohttp app 上注册 HLS 路由。

    prefix 形如 "/hls" 或 "/hls-rev"（不含尾部斜杠）。
    """
    p = prefix.rstrip("/")
    app.router.add_get(f"{p}/{{session_id}}/master.m3u8", handle_hls_master)
    app.router.add_get(f"{p}/{{session_id}}/index.m3u8", handle_hls_playlist)
    app.router.add_get(f"{p}/{{session_id}}/seg-{{seg}}.ts", handle_hls_segment)
