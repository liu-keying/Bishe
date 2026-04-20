from __future__ import annotations

import hashlib

# 隐匿数据附在「视频字节流」末尾：MAGIC + u32(len) + sha256(payload) + payload
# 与课题中「从何处拆解」对应：B 侧按 append_marker 从尾部解析。
STEGO_MAGIC = b"BISHE\x01"

STEGO_METHOD_APPEND = "append_marker"
SUPPORTED_STEGO_METHODS = frozenset({STEGO_METHOD_APPEND})

TS_SYNC = 0x47
TS_PACKET_SIZE = 188


def embed_trailer(video: bytes, hidden: bytes) -> bytes:
    """将隐匿字节附在视频字节后（伪装成连续码流/容器尾部）。"""
    n = len(hidden)
    if n > 0xFFFFFFFF:
        raise ValueError("hidden payload too large")
    digest = hashlib.sha256(hidden).digest()
    return video + STEGO_MAGIC + n.to_bytes(4, "big") + digest + hidden


def extract_trailer(video: bytes) -> bytes:
    """从视频字节中按尾部 MAGIC 拆解出隐匿 payload。"""
    # 允许尾部存在额外字节（例如分片补齐的随机 padding）。为避免 padding 中偶然出现 MAGIC，
    # 采用“从尾部向前”寻找最后一个可成功解析的 MAGIC。
    end = len(video)
    while True:
        i = video.rfind(STEGO_MAGIC, 0, end)
        if i < 0:
            raise ValueError("未找到隐匿标记 STEGO_MAGIC，可能未按 append_marker 嵌入或数据损坏")
        p = i + len(STEGO_MAGIC)
        if p + 4 + 32 > len(video):
            end = i
            continue
        n = int.from_bytes(video[p : p + 4], "big")
        p += 4
        expect = video[p : p + 32]
        p += 32
        if p + n > len(video):
            end = i
            continue
        hidden = video[p : p + n]
        got = hashlib.sha256(hidden).digest()
        if got != expect:
            end = i
            continue
        return hidden
