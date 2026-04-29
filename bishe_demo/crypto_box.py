from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def b64d(s: str) -> bytes:
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def generate_rsa_keypair(*, bits: int = 2048) -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=int(bits))
    return priv, priv.public_key()


def rsa_pub_to_pem(pub: rsa.RSAPublicKey) -> bytes:
    return pub.public_bytes(encoding=Encoding.PEM, format=PublicFormat.SubjectPublicKeyInfo)


def rsa_priv_to_pem(priv: rsa.RSAPrivateKey) -> bytes:
    return priv.private_bytes(encoding=Encoding.PEM, format=PrivateFormat.PKCS8, encryption_algorithm=NoEncryption())


def load_rsa_pub_pem(pem: bytes) -> rsa.RSAPublicKey:
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    k = load_pem_public_key(pem)
    if not isinstance(k, rsa.RSAPublicKey):
        raise ValueError("public key is not RSA")
    return k


def load_rsa_priv_pem(pem: bytes) -> rsa.RSAPrivateKey:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    k = load_pem_private_key(pem, password=None)
    if not isinstance(k, rsa.RSAPrivateKey):
        raise ValueError("private key is not RSA")
    return k


def rsa_oaep_wrap(*, recipient_pub: rsa.RSAPublicKey, key32: bytes) -> bytes:
    if len(key32) != 32:
        raise ValueError("key32 must be 32 bytes")
    return recipient_pub.encrypt(
        key32,
        padding.OAEP(mgf=padding.MGF1(algorithm=SHA256()), algorithm=SHA256(), label=None),
    )


def rsa_oaep_unwrap(*, recipient_priv: rsa.RSAPrivateKey, wrapped: bytes) -> bytes:
    key = recipient_priv.decrypt(
        wrapped,
        padding.OAEP(mgf=padding.MGF1(algorithm=SHA256()), algorithm=SHA256(), label=None),
    )
    if len(key) != 32:
        raise ValueError("bad unwrapped key length")
    return key


def aead_encrypt(*, key32: bytes, plaintext: bytes, aad: bytes = b"") -> tuple[bytes, bytes]:
    if len(key32) != 32:
        raise ValueError("key32 must be 32 bytes")
    nonce = os.urandom(12)
    ct = AESGCM(key32).encrypt(nonce, plaintext, aad)
    return nonce, ct


def aead_decrypt(*, key32: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes:
    if len(key32) != 32:
        raise ValueError("key32 must be 32 bytes")
    return AESGCM(key32).decrypt(nonce, ciphertext, aad)

