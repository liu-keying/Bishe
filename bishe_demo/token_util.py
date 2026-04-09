from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


@dataclass(frozen=True)
class TokenClaims:
    a_url: str
    c_url: str
    extract_method: str
    exp_ms: int

    def is_expired(self) -> bool:
        return int(time.time() * 1000) > int(self.exp_ms)


def issue_token(
    *, secret: str, a_url: str, c_url: str, extract_method: str, ttl_s: int = 600
) -> str:
    exp_ms = int(time.time() * 1000) + int(ttl_s) * 1000
    payload = {
        "v": 1,
        "a_url": a_url,
        "c_url": c_url,
        "extract_method": extract_method,
        "exp_ms": exp_ms,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return f"{_b64url_encode(body)}.{_b64url_encode(sig)}"


def verify_token(*, secret: str, token: str) -> TokenClaims:
    try:
        body_b64, sig_b64 = token.split(".", 1)
    except ValueError as e:
        raise ValueError("invalid token format") from e
    body = _b64url_decode(body_b64)
    sig = _b64url_decode(sig_b64)
    exp_sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, exp_sig):
        raise ValueError("invalid token signature")
    obj = json.loads(body.decode("utf-8"))
    claims = TokenClaims(
        a_url=str(obj.get("a_url") or ""),
        c_url=str(obj.get("c_url") or ""),
        extract_method=str(obj.get("extract_method") or ""),
        exp_ms=int(obj.get("exp_ms") or 0),
    )
    if not claims.a_url:
        raise ValueError("missing a_url")
    if claims.is_expired():
        raise ValueError("token expired")
    return claims

