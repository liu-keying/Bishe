"""PSK load + PSK1 AEAD framing for Zhang baseline (reuses bishe_demo.psk_aead)."""

from __future__ import annotations

import os
import re
from pathlib import Path

from bishe_demo.psk_aead import decrypt_hidden, encrypt_hidden

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def load_psk_hex(*, psk_hex: str, psk_file: str) -> bytes:
    raw = (psk_hex or "").strip()
    if not raw and (psk_file or "").strip():
        raw = Path(psk_file).read_text(encoding="utf-8").strip()
    if not raw:
        env = (os.environ.get("BISHE_PSK_HEX") or "").strip()
        raw = env
    if not raw:
        raise ValueError("missing PSK: pass --psk-hex, --psk-file, or set BISHE_PSK_HEX (64 hex chars -> 32 bytes)")
    if not _HEX64.fullmatch(raw):
        raise ValueError("psk must be 64 hex characters (32 bytes) for AES-256-GCM")
    return bytes.fromhex(raw)


def aead_bytes(*, aad_text: str) -> bytes:
    return (aad_text or "zhang-http-header-cc/v1").encode("utf-8")


def seal_plaintext(*, psk: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Random 16B prefix + plaintext, then PSK1 encrypt (same layout as main demo)."""
    inner = os.urandom(16) + plaintext
    return encrypt_hidden(psk=psk, hidden=inner, aad=aad)


def open_ciphertext(*, psk: bytes, wire: bytes, aad: bytes) -> bytes:
    inner = decrypt_hidden(psk=psk, payload=wire, aad=aad)
    if len(inner) < 16:
        raise ValueError("decrypted frame too short")
    return inner[16:]
