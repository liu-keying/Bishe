from __future__ import annotations

import hashlib
import hmac
import os
import random
import struct

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


def _ts_packet_header(pid: int, pusi: bool = False, has_adaptation: bool = False,
                      has_payload: bool = True, cc: int = 0) -> bytes:
    """构造 4 字节 TS 包头。"""
    header = 0x47 << 24  # sync byte
    if pusi:
        header |= 1 << 17  # payload unit start indicator
    header |= (pid & 0x1FFF) << 8
    # adaptation field control: 1=payload only, 2=adaptation only, 3=both
    afc = 1  # default: payload only
    if has_adaptation and has_payload:
        afc = 3
    elif has_adaptation:
        afc = 2
    header |= afc << 4
    header |= (cc & 0xF)
    return struct.pack(">I", header)


def _pat_packet(program_num: int, pmt_pid: int, cc: int = 0) -> bytes:
    """生成一个 PAT 包（PID 0x0000）。"""
    header = _ts_packet_header(pid=0, pusi=True, cc=cc)
    # pointer_field
    payload = b"\x00"
    # PAT table: table_id=0x00, section_syntax_indicator=1
    section = struct.pack(">BBHHB", 0x00, 0xB0, 0x0D, program_num, 0x00)
    section += struct.pack(">HHH", program_num, 0xE000 | pmt_pid, 0)
    # CRC32 placeholder (无所谓，这里用随机)
    section += os.urandom(4)
    # 补齐到 184 字节 payload
    payload += section + os.urandom(184 - 1 - len(section))
    return header + payload


def _pmt_packet(pmt_pid: int, pcr_pid: int, video_pids: list[int],
                cc: int = 0) -> bytes:
    """生成一个 PMT 包。"""
    header = _ts_packet_header(pid=pmt_pid, pusi=True, cc=cc)
    payload = b"\x00"  # pointer_field
    # PMT table header
    section = struct.pack(">BBH", 0x02, 0xB0, 0x00)  # section_length placeholder
    section += struct.pack(">HHB", 1, 0xE000 | pcr_pid, 0)
    # stream descriptors
    for vpid in video_pids:
        section += struct.pack(">BHH", 0x1B, 0xE000 | vpid, 0)
    section = section[:3] + struct.pack(">H", len(section) - 3) + section[5:]
    # CRC32 placeholder
    section += os.urandom(4)
    payload += section + os.urandom(184 - 1 - len(section))
    return header + payload


def _pes_packet(pid: int, pusi: bool = False, cc: int = 0) -> bytes:
    """生成一个视频 PES 包（随机负载）。"""
    header = _ts_packet_header(pid=pid, pusi=pusi, cc=cc)
    payload = os.urandom(184)
    if pusi:
        buf = bytearray(184)
        # PES start code + stream_id (video=0xE0)
        buf[0:4] = (0x00, 0x00, 0x01, 0xE0)
        # PES packet length (0 表示 unbounded)
        buf[4:6] = (0x00, 0x00)
        # PES header flags: '10'=PTS+DTS present, '10'=PTS only
        has_dts = random.choice([True, False])
        buf[6] = 0x80 | (0x40 if has_dts else 0x80)  # PTS + optional DTS
        # PES header data length
        buf[7] = 10 if has_dts else 5
        # PTS (33-bit, 5 bytes)
        pts = random.randint(0, 0x1FFFFFFFF)
        buf[8] = (0x20 if has_dts else 0x30) | ((pts >> 29) & 0x0E) | 0x01
        buf[9] = (pts >> 22) & 0xFF
        buf[10] = ((pts >> 14) & 0xFE) | 0x01
        buf[11] = (pts >> 7) & 0xFF
        buf[12] = ((pts << 1) & 0xFE) | 0x01
        if has_dts:
            dts = max(0, pts - random.randint(1000, 30000))
            buf[13] = (0x10 | ((dts >> 29) & 0x0E) | 0x01)
            buf[14] = (dts >> 22) & 0xFF
            buf[15] = ((dts >> 14) & 0xFE) | 0x01
            buf[16] = (dts >> 7) & 0xFF
            buf[17] = ((dts << 1) & 0xFE) | 0x01
            buf[18:] = os.urandom(184 - 18)
        else:
            buf[13:] = os.urandom(184 - 13)
        payload = bytes(buf)
    return header + payload


def fake_ts_segment(size: int) -> bytes:
    """生成伪装 TS 分片，外观为合法的 MPEG-TS 流。

    包含 PAT(pid 0) → PMT → 多个视频 PES 包(随机 PID)，每个包 188 字节
    以 0x47 同步头对齐。用于 SOCKS5 模式下替代真实 TS 文件。
    """
    if size < TS_PACKET_SIZE:
        return os.urandom(size)
    num_pkts = (size + TS_PACKET_SIZE - 1) // TS_PACKET_SIZE  # 向上取整
    pkts: list[bytes] = []
    pmt_pid = random.randint(0x20, 0x3F)
    pcr_pid = random.randint(0x40, 0x5F)
    video_pids = [random.randint(0x100, 0x1FF) for _ in range(random.randint(1, 3))]
    cc_counters: dict[int, int] = {}

    for i in range(num_pkts):
        if i == 0:
            pkts.append(_pat_packet(program_num=1, pmt_pid=pmt_pid, cc=_next_cc(cc_counters, 0)))
        elif i == 1:
            pkts.append(_pmt_packet(pmt_pid=pmt_pid, pcr_pid=pcr_pid,
                                     video_pids=video_pids, cc=_next_cc(cc_counters, pmt_pid)))
        else:
            pid = random.choice(video_pids)
            pusi = (i == 2)  # 第一个视频包带 PES header
            pkts.append(_pes_packet(pid=pid, pusi=pusi, cc=_next_cc(cc_counters, pid)))
    return b"".join(pkts)


def _next_cc(counters: dict[int, int], pid: int) -> int:
    c = counters.get(pid, random.randint(0, 15))
    counters[pid] = (c + 1) & 0xF
    return c


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
