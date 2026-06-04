"""
Sender for Zhang-style HTTP header covert channel (User-Agent + Cookie).

Run from repo root:
  python comparison/zhang_http_header_cc/sender.py --url http://127.0.0.1:8011/ --payload hidden.bin
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx

from comparison.zhang_http_header_cc.protocol import (
    b64url_encode,
    build_cookie_header,
    build_user_agent,
    new_msg_id,
    split_b64_for_headers,
)
from comparison.zhang_http_header_cc.psk_util import aead_bytes, load_psk_hex, seal_plaintext


def _default_ua_prefix() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )


async def send_payload(
    *,
    base_url: str,
    payload: bytes,
    psk: bytes,
    aad: bytes,
    cookie_chars: int,
    ua_chars: int,
    ua_prefix: str,
    verify_tls: bool,
    timeout_s: float,
) -> tuple[str, int]:
    """
    Returns (transport_msg_id, requests_sent).

    On the wire (HTTP headers) we carry AEAD ciphertext bytes produced by the same
    PSK1 layout as bishe_demo.psk_aead (AES-256-GCM).
    """
    ua_prefix = (ua_prefix or "").strip() or _default_ua_prefix()
    base_url = str(base_url).rstrip("/") + "/"
    msg_id = new_msg_id()
    wire = seal_plaintext(psk=psk, plaintext=payload, aad=aad)
    b64_full = b64url_encode(wire)
    per_req = max(1, int(cookie_chars) + int(ua_chars))
    chunks: list[str] = []
    for i in range(0, len(b64_full), per_req):
        chunks.append(b64_full[i : i + per_req])
    total = len(chunks)
    if total <= 0:
        total = 1
        chunks = [""]

    client_kw = {"timeout": timeout_s, "verify": verify_tls}
    async with httpx.AsyncClient(**client_kw) as client:
        sent = 0
        for seq, piece in enumerate(chunks):
            a, b = split_b64_for_headers(piece, cookie_chars=cookie_chars, ua_chars=ua_chars)
            fake_sess = secrets.token_hex(8)
            headers = {
                "User-Agent": build_user_agent(
                    ua_prefix=ua_prefix,
                    msg_id=msg_id,
                    seq=seq,
                    total=total,
                    zh_b=b,
                ),
                "Cookie": build_cookie_header(
                    msg_id=msg_id,
                    seq=seq,
                    total=total,
                    zh_a=a,
                    fake_sess=fake_sess,
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": base_url,
            }
            r = await client.get(base_url, headers=headers)
            r.raise_for_status()
            sent += 1
        return msg_id, sent


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Zhang-style HTTP header CC sender (GET + headers).")
    p.add_argument("--url", default="http://127.0.0.1:8011/", help="Sink base URL, e.g. http://127.0.0.1:8011/")
    p.add_argument("--payload", default="", help="Path to binary file to send (or omit with --text)")
    p.add_argument("--text", default="", help="Inline UTF-8 text payload (alternative to --payload)")
    p.add_argument("--cookie-chars", type=int, default=900, help="Max base64url chars placed in Cookie part")
    p.add_argument("--ua-chars", type=int, default=500, help="Max base64url chars placed in UA tail part")
    p.add_argument("--ua-prefix", default="", help="Override UA prefix (default: plausible Chrome UA)")
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--psk-hex", default="", help="32-byte PSK as 64 hex chars (or set BISHE_PSK_HEX / --psk-file)")
    p.add_argument("--psk-file", default="", help="File containing 64 hex chars of PSK")
    p.add_argument(
        "--aad",
        default="zhang-http-header-cc/v1",
        help="AEAD associated data (UTF-8); receiver must use the same string",
    )
    args = p.parse_args()

    if str(args.text or "").strip():
        payload = str(args.text).encode("utf-8")
    elif str(args.payload or "").strip():
        payload = Path(args.payload).read_bytes()
    else:
        payload = os.urandom(32)

    ua_prefix = str(args.ua_prefix).strip() or _default_ua_prefix()
    psk = load_psk_hex(psk_hex=str(args.psk_hex), psk_file=str(args.psk_file))
    aad = aead_bytes(aad_text=str(args.aad))
    msg_id, n = await send_payload(
        base_url=str(args.url),
        payload=payload,
        psk=psk,
        aad=aad,
        cookie_chars=int(args.cookie_chars),
        ua_chars=int(args.ua_chars),
        ua_prefix=ua_prefix,
        verify_tls=True,
        timeout_s=float(args.timeout_s),
    )
    logging.info(
        "sent msg_id=%s requests=%d plaintext_bytes=%d aead=PSK1/AES-GCM",
        msg_id,
        n,
        len(payload),
    )
    print(f"msg_id={msg_id} requests={n} plaintext_bytes={len(payload)}")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
