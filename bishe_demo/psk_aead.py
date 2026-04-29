from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"PSK1"  # versioned marker
NONCE_LEN = 12


def encrypt_hidden(*, psk: bytes, hidden: bytes, aad: bytes) -> bytes:
    """
    Payload format (keeps first 16B as msg_id plaintext for existing C stats):
      msg_id(16) || MAGIC(4) || nonce(12) || aesgcm(psk).encrypt(nonce, hidden[16:], aad=aad)

    If hidden < 16, msg_id is padded with zeros; receiver will strip to original length is not supported.
    (Bench payloads are >=16 by design.)
    """
    if len(psk) != 32:
        raise ValueError("psk must be 32 bytes (AES-256-GCM)")
    msg_id = hidden[:16].ljust(16, b"\x00")
    body = hidden[16:]
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(psk).encrypt(nonce, body, aad)
    return msg_id + MAGIC + nonce + ct


def decrypt_hidden(*, psk: bytes, payload: bytes, aad: bytes) -> bytes:
    if len(psk) != 32:
        raise ValueError("psk must be 32 bytes (AES-256-GCM)")
    if len(payload) < 16 + len(MAGIC) + NONCE_LEN + 16:  # +tag
        raise ValueError("payload too short")
    msg_id = payload[:16]
    if payload[16 : 16 + len(MAGIC)] != MAGIC:
        raise ValueError("not a PSK1 payload")
    p = 16 + len(MAGIC)
    nonce = payload[p : p + NONCE_LEN]
    ct = payload[p + NONCE_LEN :]
    body = AESGCM(psk).decrypt(nonce, ct, aad)
    return msg_id + body

