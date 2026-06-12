from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


def new_session_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class OverlayEnvelope:
    """
    把"消息"伪装成"媒体分片/上传请求"的载体。

    - payload_b64：真实要传的字节内容（demo 用 base64；后续你可换成更贴近视频分片的封装）
    - t0_ms：A 侧进入 overlay 的时间戳，用于端到端延迟测量
    """

    v: int
    session_id: str
    seq: int
    t0_ms: int
    content_type: str
    payload_b64: str
    meta: dict[str, Any]

    @staticmethod
    def pack(
        *,
        session_id: str,
        seq: int,
        payload: bytes,
        content_type: str = "application/octet-stream",
        meta: dict[str, Any] | None = None,
        t0_ms: int | None = None,
    ) -> "OverlayEnvelope":
        return OverlayEnvelope(
            v=1,
            session_id=session_id,
            seq=seq,
            t0_ms=now_ms() if t0_ms is None else int(t0_ms),
            content_type=content_type,
            payload_b64=base64.b64encode(payload).decode("ascii"),
            meta={} if meta is None else dict(meta),
        )

    def unpack_payload(self) -> bytes:
        return base64.b64decode(self.payload_b64.encode("ascii"))

    def to_json_bytes(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def from_json_bytes(b: bytes) -> "OverlayEnvelope":
        obj = json.loads(b.decode("utf-8"))
        return OverlayEnvelope(
            v=int(obj["v"]),
            session_id=str(obj["session_id"]),
            seq=int(obj["seq"]),
            t0_ms=int(obj["t0_ms"]),
            content_type=str(obj.get("content_type") or "application/octet-stream"),
            payload_b64=str(obj["payload_b64"]),
            meta=dict(obj.get("meta") or {}),
        )


def make_hls_like_path(session_id: str, seq: int, prefix: str = "/hls") -> str:
    # 模拟 HLS 分片命名风格：/hls/<session>/seg-000001.ts（仅作文档/展示用）
    p = prefix.rstrip("/")
    return f"{p}/{session_id}/seg-{seq:06d}.ts"


def make_hls_master_path(session_id: str, prefix: str = "/hls") -> str:
    p = prefix.rstrip("/")
    return f"{p}/{session_id}/master.m3u8"


def make_hls_media_playlist_path(session_id: str, prefix: str = "/hls") -> str:
    """媒体播放列表（列出当前可拉的一条分片，模拟直播滑动窗口）。"""
    p = prefix.rstrip("/")
    return f"{p}/{session_id}/index.m3u8"

