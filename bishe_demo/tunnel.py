"""
SOCKS5 隧道数据模型 + C 侧 TCP 出口连接池。

数据包格式（作为 encrypt_hidden 的 hidden 输入）：
  conn_id(16B hex) || TNL1(4B) || payload_len(4B big-endian) || JSON

encrypt_hidden 将 conn_id 保留为明文 msg_id，后续 TLS 负载加密。
decrypt_hidden 返回 conn_id + TNL1 + len + JSON，按 TNL1 魔数识别隧道消息。
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Optional

TUNNEL_MAGIC = b"TNL1"
MAGIC_OFFSET = 16  # 在 decrypt_hidden 输出中的偏移（跳过 16 字节 conn_id）

# 控制消息类型
CTL_CONNECT = "connect"
CTL_CONNECTED = "connected"
CTL_FIN = "fin"
CTL_ERROR = "error"


@dataclass
class TunnelCtl:
    """隧道控制消息：建连 / 确认 / 关闭"""

    conn_id: str  # 16 字节 hex UUID
    ctl_type: str  # "connect" | "connected" | "fin" | "error"
    host: str = ""
    port: int = 0
    error: str = ""

    def to_bytes(self) -> bytes:
        obj = {"type": "ctl", "conn_id": self.conn_id, "ctl": self.ctl_type}
        if self.host:
            obj["host"] = self.host
        if self.port:
            obj["port"] = self.port
        if self.error:
            obj["error"] = self.error
        json_bytes = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return (
            self.conn_id.encode("ascii")[:16].ljust(16, b"\x00")
            + TUNNEL_MAGIC
            + struct.pack(">I", len(json_bytes))
            + json_bytes
        )

    @staticmethod
    def from_bytes(data: bytes) -> Optional["TunnelCtl"]:
        try:
            if len(data) < MAGIC_OFFSET + len(TUNNEL_MAGIC) + 4:
                return None
            conn_id = data[:16].decode("ascii", errors="replace").rstrip("\x00")
            magic = data[MAGIC_OFFSET : MAGIC_OFFSET + len(TUNNEL_MAGIC)]
            if magic != TUNNEL_MAGIC:
                return None
            payload_len = struct.unpack(">I", data[MAGIC_OFFSET + len(TUNNEL_MAGIC) : MAGIC_OFFSET + len(TUNNEL_MAGIC) + 4])[0]
            json_start = MAGIC_OFFSET + len(TUNNEL_MAGIC) + 4
            if json_start + payload_len > len(data):
                return None
            obj = json.loads(data[json_start : json_start + payload_len].decode("utf-8"))
            if obj.get("type") != "ctl":
                return None
            return TunnelCtl(
                conn_id=str(obj.get("conn_id") or conn_id),
                ctl_type=str(obj.get("ctl") or ""),
                host=str(obj.get("host") or ""),
                port=int(obj.get("port") or 0),
                error=str(obj.get("error") or ""),
            )
        except Exception:
            return None

    @staticmethod
    def is_tunnel_ctl(data: bytes) -> bool:
        """检查 decrypt_hidden 输出是否为隧道控制消息。"""
        if len(data) < MAGIC_OFFSET + len(TUNNEL_MAGIC) + 4:
            return False
        magic = data[MAGIC_OFFSET : MAGIC_OFFSET + len(TUNNEL_MAGIC)]
        if magic != TUNNEL_MAGIC:
            return False
        try:
            payload_len_start = MAGIC_OFFSET + len(TUNNEL_MAGIC)
            payload_len = struct.unpack(">I", data[payload_len_start : payload_len_start + 4])[0]
            json_start = payload_len_start + 4
            if json_start + payload_len > len(data):
                return False
            obj = json.loads(data[json_start : json_start + payload_len].decode("utf-8"))
            return obj.get("type") == "ctl"
        except Exception:
            return False


@dataclass
class TunnelData:
    """隧道数据消息：双向字节流片段"""

    conn_id: str  # 16 字节 hex UUID
    seq: int  # 流内序号
    data: bytes  # 原始 TCP 字节
    fin: bool = False

    def to_bytes(self) -> bytes:
        obj = {
            "type": "data",
            "conn_id": self.conn_id,
            "seq": self.seq,
            "fin": self.fin,
            "data_b64": "",  # 数据太大不适合 JSON，放后面
        }
        # 数据放 JSON 后面（二进制尾部），避免 base64 膨胀
        json_bytes = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return (
            self.conn_id.encode("ascii")[:16].ljust(16, b"\x00")
            + TUNNEL_MAGIC
            + struct.pack(">I", len(json_bytes))
            + json_bytes
            + struct.pack(">I", len(self.data))
            + self.data
        )

    @staticmethod
    def from_bytes(data: bytes) -> Optional["TunnelData"]:
        try:
            if len(data) < MAGIC_OFFSET + len(TUNNEL_MAGIC) + 4:
                return None
            conn_id = data[:16].decode("ascii", errors="replace").rstrip("\x00")
            magic = data[MAGIC_OFFSET : MAGIC_OFFSET + len(TUNNEL_MAGIC)]
            if magic != TUNNEL_MAGIC:
                return None
            json_len = struct.unpack(">I", data[MAGIC_OFFSET + len(TUNNEL_MAGIC) : MAGIC_OFFSET + len(TUNNEL_MAGIC) + 4])[0]
            json_start = MAGIC_OFFSET + len(TUNNEL_MAGIC) + 4
            if json_start + json_len + 4 > len(data):
                return None
            obj = json.loads(data[json_start : json_start + json_len].decode("utf-8"))
            if obj.get("type") != "data":
                return None
            data_len_start = json_start + json_len
            data_len = struct.unpack(">I", data[data_len_start : data_len_start + 4])[0]
            data_start = data_len_start + 4
            if data_start + data_len > len(data):
                return None
            return TunnelData(
                conn_id=str(obj.get("conn_id") or conn_id),
                seq=int(obj.get("seq") or 0),
                data=data[data_start : data_start + data_len],
                fin=bool(obj.get("fin") or False),
            )
        except Exception:
            return None

    @staticmethod
    def is_tunnel_data(data: bytes) -> bool:
        """检查 decrypt_hidden 输出是否为隧道数据消息。"""
        if len(data) < MAGIC_OFFSET + len(TUNNEL_MAGIC) + 4:
            return False
        magic = data[MAGIC_OFFSET : MAGIC_OFFSET + len(TUNNEL_MAGIC)]
        if magic != TUNNEL_MAGIC:
            return False
        try:
            payload_len_start = MAGIC_OFFSET + len(TUNNEL_MAGIC)
            json_len = struct.unpack(">I", data[payload_len_start : payload_len_start + 4])[0]
            json_start = payload_len_start + 4
            if json_start + json_len > len(data):
                return False
            obj = json.loads(data[json_start : json_start + json_len].decode("utf-8"))
            return obj.get("type") == "data"
        except Exception:
            return False


class TunnelExit:
    """C 侧 TCP 出口连接池。

    管理到目标服务器的 TCP 连接，每个连接由 conn_id 索引。
    """

    def __init__(self, max_conns: int = 50, idle_timeout_s: float = 300.0):
        self._max_conns = max_conns
        self._idle_timeout_s = idle_timeout_s
        # conn_id → (StreamReader, StreamWriter)
        self._conns: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
        self._last_active: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def connect(self, conn_id: str, host: str, port: int) -> bool:
        """建立到目标的 TCP 连接。"""
        async with self._lock:
            if len(self._conns) >= self._max_conns:
                logging.warning("TunnelExit: 连接数已达上限 %d，拒绝 %s", self._max_conns, conn_id)
                return False
            if conn_id in self._conns:
                return True  # 已连接

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=15.0,
            )
        except Exception as e:
            logging.warning("TunnelExit: 连接 %s → %s:%d 失败: %r", conn_id[:8], host, port, e)
            return False

        async with self._lock:
            self._conns[conn_id] = (reader, writer)
            self._last_active[conn_id] = time.time()
        logging.info("TunnelExit: 已连接 %s → %s:%d", conn_id[:8], host, port)
        return True

    async def send(self, conn_id: str, data: bytes) -> bool:
        """写数据到目标 TCP 连接。"""
        pair = self._conns.get(conn_id)
        if pair is None:
            return False
        try:
            pair[1].write(data)
            await pair[1].drain()
            self._last_active[conn_id] = time.time()
            return True
        except Exception:
            await self.close(conn_id)
            return False

    async def recv(self, conn_id: str, max_bytes: int = 65536) -> Optional[bytes]:
        """从目标 TCP 连接读取数据（非阻塞，立即返回已有数据或 None）。"""
        pair = self._conns.get(conn_id)
        if pair is None:
            return None
        reader = pair[0]
        try:
            data = await asyncio.wait_for(reader.read(max_bytes), timeout=0.05)
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

        if data:
            self._last_active[conn_id] = time.time()
        else:
            # EOF: 目标关闭了连接
            await self.close(conn_id)
        return data if data else None

    async def close(self, conn_id: str) -> None:
        """关闭到目标的 TCP 连接。"""
        async with self._lock:
            pair = self._conns.pop(conn_id, None)
            self._last_active.pop(conn_id, None)
        if pair is not None:
            try:
                pair[1].close()
                await pair[1].wait_closed()
            except Exception:
                pass
            logging.info("TunnelExit: 已关闭 %s", conn_id[:8])

    def is_connected(self, conn_id: str) -> bool:
        return conn_id in self._conns

    def get_active_conns(self) -> list[str]:
        """返回活跃连接的 conn_id 列表。"""
        return list(self._conns.keys())

    async def cleanup_idle(self) -> int:
        """清理空闲超时的连接，返回清理数量。"""
        now = time.time()
        to_close: list[str] = []
        async with self._lock:
            for conn_id, last in list(self._last_active.items()):
                if now - last > self._idle_timeout_s:
                    to_close.append(conn_id)
        for conn_id in to_close:
            logging.info("TunnelExit: 空闲超时关闭 %s", conn_id[:8])
            await self.close(conn_id)
        return len(to_close)

    @property
    def conn_count(self) -> int:
        return len(self._conns)
