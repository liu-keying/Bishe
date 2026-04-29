from __future__ import annotations

import hashlib
import hmac
import os

# --- 旧方案：尾部 MAGIC + u32(len) + payload（便于调试）---
STEGO_MAGIC = b"BISHE\x01"

STEGO_METHOD_APPEND = "append_marker"
# 方案 2：分片内伪随机偏移 + HMAC 动态标记 + 密文片段（等长替换，不改变分片总长）
STEGO_METHOD_PSK_HMAC_INPLACE = "psk_hmac_inplace"

SUPPORTED_STEGO_METHODS = frozenset({STEGO_METHOD_APPEND, STEGO_METHOD_PSK_HMAC_INPLACE})

TS_SYNC = 0x47
TS_PACKET_SIZE = 188

# 动态标记截断长度 t（字节）
STEGO_TAG_LEN = 16


def embed_trailer(video: bytes, hidden: bytes) -> bytes:
    """将隐匿字节附在视频字节后（伪装成连续码流/容器尾部）。"""
    n = len(hidden)
    if n > 0xFFFFFFFF:
        raise ValueError("hidden payload too large")
    return video + STEGO_MAGIC + n.to_bytes(4, "big") + hidden


def extract_trailer(video: bytes) -> bytes:
    """从视频字节中按尾部 MAGIC 拆解出隐匿 payload。"""
    end = len(video)
    while True:
        i = video.rfind(STEGO_MAGIC, 0, end)
        if i < 0:
            raise ValueError("未找到隐匿标记 STEGO_MAGIC，可能未按 append_marker 嵌入或数据损坏")
        p = i + len(STEGO_MAGIC)
        if p + 4 > len(video):
            end = i
            continue
        n = int.from_bytes(video[p : p + 4], "big")
        p += 4
        if p + n > len(video):
            end = i
            continue
        return video[p : p + n]


def _derive_tag(*, token: str, hls_index: int, frag_idx: int) -> bytes:
    key = token.encode("utf-8")
    msg = b"v1|" + str(hls_index).encode() + b"|" + str(frag_idx).encode()
    return hmac.new(key, msg, hashlib.sha256).digest()[:STEGO_TAG_LEN]


def _derive_offset(
    *,
    token: str,
    hls_index: int,
    slot: str,
    seg_len: int,
    payload_len: int,
) -> int:
    """slot 为分片内槽位标识：密文片段序号 str(frag_idx)，或承载填充时的 \"pad\"。"""
    key = token.encode("utf-8")
    msg = b"o1|" + str(hls_index).encode() + b"|" + slot.encode("utf-8")
    raw = hmac.new(key, msg, hashlib.sha256).digest()
    val = int.from_bytes(raw[:8], "big")
    max_start = seg_len - payload_len
    if max_start < 0:
        raise ValueError(f"segment too short for stego: seg_len={seg_len} need>={payload_len}")
    return val % (max_start + 1)


def embed_psk_inplace(
    video: bytes,
    *,
    token: str,
    hls_index: int,
    frag_idx: int,
    cipher_fragment: bytes,
) -> bytes:
    """
    在分片内 offset 处做等长替换：写入 tag || cipher_fragment，不改变 len(video)。
    tag = Trunc_t(HMAC(PSK, token||hls_index||frag_idx))。
    """
    tag = _derive_tag(token=token, hls_index=hls_index, frag_idx=frag_idx)
    payload = tag + cipher_fragment
    plen = len(payload)
    off = _derive_offset(
        token=token,
        hls_index=hls_index,
        slot=str(frag_idx),
        seg_len=len(video),
        payload_len=plen,
    )
    return video[:off] + payload + video[off + plen :]


def embed_pad_inplace(
    video: bytes,
    *,
    token: str,
    hls_index: int,
    pad_len: int,
) -> bytes:
    """非承载分片：在独立派生的 offset 处写入等长随机字节，体积与承载分片一致。"""
    junk = os.urandom(pad_len)
    off = _derive_offset(
        token=token,
        hls_index=hls_index,
        slot="pad",
        seg_len=len(video),
        payload_len=pad_len,
    )
    return video[:off] + junk + video[off + pad_len :]


def extract_psk_inplace(
    video: bytes,
    *,
    token: str,
    hls_index: int,
    frag_idx: int,
    frag_body_len: int,
) -> bytes:
    """
    按与嵌入相同的规则计算 offset，读取 tag||body；校验 tag 后返回密文片段（不含 tag）。
    frag_body_len：仅密文片段字节数（不含 STEGO_TAG_LEN）。
    """
    tag = _derive_tag(token=token, hls_index=hls_index, frag_idx=frag_idx)
    plen = STEGO_TAG_LEN + frag_body_len
    off = _derive_offset(
        token=token,
        hls_index=hls_index,
        slot=str(frag_idx),
        seg_len=len(video),
        payload_len=plen,
    )
    if off + plen > len(video):
        raise ValueError("stego read out of range")
    chunk = video[off : off + plen]
    if chunk[:STEGO_TAG_LEN] != tag:
        raise ValueError("stego tag mismatch")
    return chunk[STEGO_TAG_LEN:]
