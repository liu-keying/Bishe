"""
Zhang et al. [7] style HTTP header covert channel framing (demo / baseline).

Reference (user-provided):
  Zhang R, Gan Y, Yin Y F. Research on Construction Methods for Network Covert
  Channels Based on HTTP[J]. Advanced Materials Research, 2012, 220-223:2528-2533.

Encoding: split each base64url chunk across Cookie (field `zh_a`) and User-Agent
suffix (field `zh_b`) so both header families carry entropy; receiver merges
`zh_a + zh_b` then decodes base64url to recover the original chunk stream.
"""

from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


_UA_TAIL_RE = re.compile(
    r"\|m=(?P<msg>[0-9a-fA-F-]{36})\|s=(?P<s>\d+)\|t=(?P<t>\d+)\|b=(?P<b>[A-Za-z0-9_-]*)$"
)
_COOKIE_MSG_RE = re.compile(r"(?:^|;\s*)m=(?P<msg>[0-9a-fA-F-]{36})(?:;|$)", flags=re.IGNORECASE)
_COOKIE_A_RE = re.compile(r"(?:^|;\s*)zh_a=(?P<a>[A-Za-z0-9_-]*)(?:;|$)", flags=re.IGNORECASE)


def split_b64_for_headers(b64: str, *, cookie_chars: int, ua_chars: int) -> tuple[str, str]:
    """
    Split a base64url string into two parts. First `cookie_chars` go to Cookie,
    remainder to UA tail (may be empty).
    """
    b64 = b64 or ""
    cookie_chars = max(0, int(cookie_chars))
    ua_chars = max(0, int(ua_chars))
    a = b64[:cookie_chars]
    rest = b64[cookie_chars:]
    b = rest[:ua_chars] if ua_chars else rest
    # If still overflow, caller must use smaller chunks / more requests.
    overflow = rest[len(b) :] if ua_chars else rest
    if overflow:
        raise ValueError("chunk too large for configured header split; reduce raw chunk size")
    return a, b


def build_cookie_header(*, msg_id: str, seq: int, total: int, zh_a: str, fake_sess: str) -> str:
    msg_id = str(msg_id).strip()
    return f"PHPSESSID={fake_sess}; m={msg_id}; s={int(seq)}; t={int(total)}; zh_a={zh_a}"


def build_user_agent(*, ua_prefix: str, msg_id: str, seq: int, total: int, zh_b: str) -> str:
    prefix = (ua_prefix or "").rstrip()
    tail = f"|m={msg_id}|s={int(seq)}|t={int(total)}|b={zh_b}"
    return f"{prefix}{tail}"


@dataclass(frozen=True)
class ParsedHeaders:
    msg_id: str
    seq: int
    total: int
    zh_a: str
    zh_b: str

    def merged_b64(self) -> str:
        return f"{self.zh_a}{self.zh_b}"


def parse_from_headers(*, user_agent: str, cookie: str) -> ParsedHeaders | None:
    ua = user_agent or ""
    ck = cookie or ""
    m_ua = _UA_TAIL_RE.search(ua)
    if not m_ua:
        return None
    msg = m_ua.group("msg")
    s = int(m_ua.group("s"))
    t = int(m_ua.group("t"))
    b = m_ua.group("b") or ""

    m_ck = _COOKIE_MSG_RE.search(ck)
    if not m_ck or m_ck.group("msg").lower() != msg.lower():
        return None
    m_a = _COOKIE_A_RE.search(ck)
    if not m_a:
        return None
    a = m_a.group("a") or ""
    return ParsedHeaders(msg_id=msg, seq=s, total=t, zh_a=a, zh_b=b)


def new_msg_id() -> str:
    return str(uuid.uuid4())
