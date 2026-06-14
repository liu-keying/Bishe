from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time

from aiohttp import web

from .stego import SUPPORTED_STEGO_METHODS
from .token_util import issue_token
from .crypto_box import aead_encrypt, b64d, b64e, load_rsa_pub_pem, rsa_oaep_wrap


async def handle_issue(request: web.Request) -> web.Response:
    secret = str(request.app["secret"])
    try:
        body = await request.json()
    except Exception as e:
        return web.json_response({"ok": False, "error": f"invalid json: {e!r}"}, status=400)
    client_url = str(body.get("client_url") or body.get("a_url") or "").strip()
    server_url = str(body.get("server_url") or body.get("c_url") or "").strip()
    extract_method = str(body.get("extract_method") or "append_marker").strip()
    try:
        ttl_s = int(body.get("ttl_s") or 600)
    except Exception:
        return web.json_response({"ok": False, "error": "ttl_s must be int"}, status=400)

    if extract_method not in SUPPORTED_STEGO_METHODS:
        return web.json_response({"ok": False, "error": f"unsupported extract_method: {extract_method}"}, status=400)
    if not client_url:
        return web.json_response({"ok": False, "error": "missing client_url"}, status=400)
    if not server_url:
        return web.json_response({"ok": False, "error": "missing server_url"}, status=400)
    if ttl_s < 10 or ttl_s > 24 * 3600:
        return web.json_response({"ok": False, "error": "ttl_s out of range"}, status=400)

    token = issue_token(
        secret=secret,
        client_url=client_url,
        server_url=server_url,
        extract_method=extract_method,
        ttl_s=ttl_s,
    )

    # Optional: key exchange + PSK distribution.
    # If caller provides role+pubkey, control will wait for both client and server pubkeys,
    # generate a per-link PSK P (32B), and return {token,P} encrypted to caller.
    role = str(body.get("role") or "").strip().lower()
    pubkey_b64 = str(body.get("pubkey") or "").strip()
    if not role:
        return web.json_response({"ok": False, "error": "missing role ('client'|'server')"}, status=400)
    if not pubkey_b64:
        return web.json_response({"ok": False, "error": "missing pubkey"}, status=400)
    if role not in ("client", "server"):
        return web.json_response({"ok": False, "error": "role must be 'client' or 'server'"}, status=400)
    try:
        pub_raw = b64d(pubkey_b64)
        pub = load_rsa_pub_pem(pub_raw)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"bad pubkey: {e!r}"}, status=400)

    # Keyed by link identity (stable for one experiment run).
    # Note: token itself includes exp_ms; state is best-effort.
    link_key = f"{client_url}|{server_url}|{extract_method}"

    async with request.app["kex_lock"]:
        kex: dict = request.app.get("kex") or {}
        st = kex.get(link_key) or {}
        st["client_url"] = client_url
        st["server_url"] = server_url
        st["extract_method"] = extract_method
        st["ttl_s"] = ttl_s
        st["t_ms"] = int(time.time() * 1000)
        st[f"{role}_pub"] = pub

        # Generate PSK once both sides registered.
        if st.get("psk") is None and st.get("client_pub") is not None and st.get("server_pub") is not None:
            st["psk"] = secrets.token_bytes(32)  # AES-256-GCM key
            st["token"] = token
            logging.info("control kex ready link=%s (psk issued)", link_key)
        else:
            # Keep token stable once issued; otherwise, first side gets a token that might differ
            # from the second call. For simplicity, we only "finalize" when ready.
            if st.get("token") is not None:
                token = str(st["token"])

        kex[link_key] = st
        request.app["kex"] = kex

        pending = st.get("psk") is None

    if pending:
        return web.json_response(
            {
                "ok": True,
                "pending": True,
                "link": link_key,
                "retry_after_s": 0.5,
            }
        )

    psk: bytes = st["psk"]
    token2: str = str(st.get("token") or token)
    plain = {
        "v": 1,
        "token": token2,
        "client_url": client_url,
        "server_url": server_url,
        "extract_method": extract_method,
        "ttl_s": ttl_s,
    }
    import json

    pt = json.dumps(plain, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    aad = link_key.encode("utf-8")
    wrapped_psk = rsa_oaep_wrap(recipient_pub=pub, key32=psk)
    nonce, ct = aead_encrypt(key32=psk, plaintext=pt, aad=aad)
    bundle = {"ek_psk": b64e(wrapped_psk), "nonce": b64e(nonce), "ciphertext": b64e(ct)}
    return web.json_response({"ok": True, "pending": False, "link": link_key, "bundle": bundle})


async def handle_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def run_control_server(*, host: str, port: int, token_secret: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = web.Application()
    app["secret"] = token_secret
    app["kex"] = {}
    app["kex_lock"] = asyncio.Lock()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/issue", handle_issue)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    logging.info("control 节点启动: http://%s:%d (issue token)", host, port)
    await site.start()
    while True:
        await asyncio.sleep(3600)


def env_token_secret() -> str:
    return os.environ.get("BISHE_TOKEN_SECRET", "bishe-dev-secret")
