from __future__ import annotations

# 隐匿数据附在「视频字节流」末尾：MAGIC + u32(len) + payload
# 与课题中「从何处拆解」对应：B 侧按 append_marker 从尾部解析。
STEGO_MAGIC = b"BISHE\x01"

STEGO_METHOD_APPEND = "append_marker"
STEGO_METHOD_TS_PRIVATE = "ts_private"
SUPPORTED_STEGO_METHODS = frozenset({STEGO_METHOD_APPEND, STEGO_METHOD_TS_PRIVATE})

TS_SYNC = 0x47
TS_PACKET_SIZE = 188
TS_PAYLOAD_SIZE = TS_PACKET_SIZE - 4
# 专用 PID（演示用），载荷为连续字节：MAGIC + u32(len) + hidden + 0xFF 填充
TS_PID_BISHE = 0x0100


def embed_trailer(video: bytes, hidden: bytes) -> bytes:
    """将隐匿字节附在视频字节后（伪装成连续码流/容器尾部）。"""
    n = len(hidden)
    if n > 0xFFFFFFFF:
        raise ValueError("hidden payload too large")
    return video + STEGO_MAGIC + n.to_bytes(4, "big") + hidden


def extract_trailer(video: bytes) -> bytes:
    """从视频字节中按尾部 MAGIC 拆解出隐匿 payload。"""
    i = video.rfind(STEGO_MAGIC)
    if i < 0:
        raise ValueError("未找到隐匿标记 STEGO_MAGIC，可能未按 append_marker 嵌入或数据损坏")
    p = i + len(STEGO_MAGIC)
    if p + 4 > len(video):
        raise ValueError("隐匿长度字段不完整")
    n = int.from_bytes(video[p : p + 4], "big")
    p += 4
    if p + n > len(video):
        raise ValueError("隐匿长度与数据不符")
    return video[p : p + n]


def _ts_header(*, pusi: bool, cc: int) -> bytes:
    pid = TS_PID_BISHE
    b0 = TS_SYNC
    b1 = ((0x40 if pusi else 0x00) & 0x40) | ((pid >> 8) & 0x1F)
    b2 = pid & 0xFF
    b3 = 0x10 | (cc & 0x0F)
    return bytes([b0, b1, b2, b3])


def embed_ts_packets(hidden: bytes) -> bytes:
    """将隐匿数据放进若干 MPEG-TS 包的 payload（AFC=01，每包 184B 载荷）。"""
    inner = STEGO_MAGIC + len(hidden).to_bytes(4, "big") + hidden
    out = bytearray()
    cc = 0
    first = True
    offset = 0
    while offset < len(inner):
        chunk = inner[offset : offset + TS_PAYLOAD_SIZE]
        if len(chunk) < TS_PAYLOAD_SIZE:
            chunk = chunk + b"\xFF" * (TS_PAYLOAD_SIZE - len(chunk))
        out.extend(_ts_header(pusi=first, cc=cc))
        out.extend(chunk)
        cc = (cc + 1) & 0x0F
        first = False
        offset += TS_PAYLOAD_SIZE
    return bytes(out)


def _ts_header_fields(pkt: bytes) -> tuple[int, int, int, int, int]:
    if len(pkt) != TS_PACKET_SIZE or pkt[0] != TS_SYNC:
        raise ValueError("invalid TS packet")
    b1, b2, b3 = pkt[1], pkt[2], pkt[3]
    tei = (b1 >> 7) & 1
    pusi = (b1 >> 6) & 1
    pid = ((b1 & 0x1F) << 8) | b2
    afc = (b3 >> 4) & 0x3
    cc = b3 & 0x0F
    return tei, pusi, pid, afc, cc


def _ts_payload_bytes(pkt: bytes) -> bytes:
    _tei, _pusi, _pid, afc, _cc = _ts_header_fields(pkt)
    if afc == 0b01:
        return pkt[4:TS_PACKET_SIZE]
    if afc == 0b11:
        alen = pkt[4]
        start = 5 + alen
        if start > TS_PACKET_SIZE:
            raise ValueError("TS adaptation 长度异常")
        return pkt[start:TS_PACKET_SIZE]
    raise ValueError("仅支持含 payload 的 TS 包(AFC=01/11)")


def _reassemble_bishe_ts_segment(segment: bytes) -> bytes:
    if len(segment) % TS_PACKET_SIZE != 0:
        raise ValueError("TS 段长度不是 188 的整数倍")
    npack = len(segment) // TS_PACKET_SIZE
    buf = bytearray()
    exp_cc: int | None = None
    for i in range(npack):
        pkt = segment[i * TS_PACKET_SIZE : (i + 1) * TS_PACKET_SIZE]
        tei, pusi, pid, afc, cc = _ts_header_fields(pkt)
        if tei:
            raise ValueError("TS transport_error")
        if pid != TS_PID_BISHE:
            raise ValueError("TS PID 不匹配")
        if afc not in (0b01, 0b11):
            raise ValueError("TS AFC 不支持")
        if i == 0 and pusi != 1:
            raise ValueError("首包需 PUSI=1")
        if i > 0 and pusi != 0:
            raise ValueError("续包需 PUSI=0")
        if exp_cc is None:
            exp_cc = cc
        else:
            if cc != ((exp_cc + 1) & 0x0F):
                raise ValueError("TS continuity 不连续")
            exp_cc = cc
        buf.extend(_ts_payload_bytes(pkt))
    raw = bytes(buf)
    if not raw.startswith(STEGO_MAGIC):
        raise ValueError("TS 载荷内层缺少 MAGIC")
    p = len(STEGO_MAGIC)
    if p + 4 > len(raw):
        raise ValueError("TS 载荷长度字段不完整")
    n = int.from_bytes(raw[p : p + 4], "big")
    p += 4
    if p + n > len(raw):
        raise ValueError("TS 隐匿长度与数据不符")
    hidden = raw[p : p + n]
    tail = raw[p + n :]
    if tail.strip(b"\xFF") != b"":
        raise ValueError("TS 隐匿尾部存在非填充数据")
    return hidden


def extract_ts_private(blob: bytes) -> bytes:
    """从字节流末尾解析出本 demo 写入的 TS 包序列中的隐匿 payload（与 embed_ts_packets 对偶）。"""
    n = len(blob)
    max_k = n // TS_PACKET_SIZE
    # 必须从 k=1 往上试：若从大 k 往下试，大 MP4 时 max_k 极大，单次失败虽快但
    # 曾实现为对整段 seg 做完整校验时会变成 O(max_k^2)，表现为「永远跑不完」；
    # 隐匿 TS 通常只有少量包，升序试可在第一次命中时立即返回。
    for k in range(1, max_k + 1):
        start = n - k * TS_PACKET_SIZE
        seg = blob[start:]
        try:
            return _reassemble_bishe_ts_segment(seg)
        except ValueError:
            continue
    raise ValueError("未找到有效的隐匿 TS 序列(PID=0x0100)，可能未使用 ts_private 或数据损坏")
