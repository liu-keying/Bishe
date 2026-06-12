"""
B 侧可靠性：C 回执、向 C 超时重发、密文分片组装超时通知 A 重传。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from aiohttp import web
from redis.asyncio import Redis

from .common import now_ms

PENDING_C_PREFIX = "bishe:pending_c:"
FRAG_WATCH_PREFIX = "bishe:frag_watch:"
CIPHER_B_ASM_PREFIX = "bishe:cipher_b_asm:"


def delivery_id(session_id: str, cipher_group: str) -> str:
    return f"{session_id}:{cipher_group}"


def _pending_key(did: str) -> str:
    return f"{PENDING_C_PREFIX}{did}"


def _frag_watch_key(session_id: str, cipher_group: str) -> str:
    return f"{FRAG_WATCH_PREFIX}{session_id}:{cipher_group}"


def _cipher_asm_key(session_id: str, cipher_group: str) -> str:
    return f"{CIPHER_B_ASM_PREFIX}{session_id}:{cipher_group}:data"


@dataclass(frozen=True)
class ReliabilityConfig:
    enabled: bool = True
    c_ack_timeout_s: float = 30.0
    c_resend_max: int = 3
    frag_assembly_timeout_s: float = 120.0
    watchdog_interval_s: float = 5.0


async def notify_a_error_notice(
    client: httpx.AsyncClient,
    *,
    a_url: str,
    session_id: str,
    reason: str,
    detail: str = "",
    seq: int = 0,
) -> None:
    if not a_url:
        return
    try:
        await client.post(
            f"{a_url.rstrip('/')}/overlay/error-notice",
            json={
                "session_id": session_id,
                "emit_session": session_id,
                "seq": seq,
                "reason": reason,
                "detail": detail,
            },
            headers={"X-Overlay": "1"},
        )
    except Exception:
        logging.exception("通知 A error-notice 失败 reason=%s session=%s", reason, session_id)


async def register_frag_watch(
    redis: Redis,
    *,
    session_id: str,
    cipher_group: str,
    cipher_k: int,
    a_url: str,
    rcfg: ReliabilityConfig,
) -> None:
    if not rcfg.enabled:
        return
    wkey = _frag_watch_key(session_id, cipher_group)
    existing = await redis.get(wkey)
    if existing:
        return
    doc = {
        "session_id": session_id,
        "cipher_group": cipher_group,
        "cipher_k": cipher_k,
        "a_url": a_url,
        "first_ms": now_ms(),
    }
    ttl = int(rcfg.frag_assembly_timeout_s * 3) + 60
    await redis.set(wkey, json.dumps(doc, ensure_ascii=False).encode("utf-8"), ex=ttl)


async def clear_frag_watch(redis: Redis, *, session_id: str, cipher_group: str) -> None:
    await redis.delete(_frag_watch_key(session_id, cipher_group))


async def register_pending_c_delivery(
    redis: Redis,
    *,
    did: str,
    session_id: str,
    cipher_group: str,
    a_url: str,
    c_url: str,
    payload: bytes,
    headers: dict[str, str],
    rcfg: ReliabilityConfig,
    recv_endpoint: str = "/recv",
) -> None:
    if not rcfg.enabled:
        return
    doc = {
        "delivery_id": did,
        "session_id": session_id,
        "cipher_group": cipher_group,
        "a_url": a_url,
        "c_url": c_url,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "headers": headers,
        "attempt": 1,
        "last_send_ms": now_ms(),
        "acked": False,
        "http_ok": False,
        "recv_endpoint": recv_endpoint,
    }
    ttl = int(max(rcfg.c_ack_timeout_s, rcfg.frag_assembly_timeout_s) * rcfg.c_resend_max) + 120
    await redis.set(_pending_key(did), json.dumps(doc, ensure_ascii=False).encode("utf-8"), ex=ttl)


async def post_to_c(
    client: httpx.AsyncClient,
    *,
    c_url: str,
    payload: bytes,
    headers: dict[str, str],
    did: str,
    redis: Redis,
    session_id: str,
    cipher_group: str,
    a_url: str,
    rcfg: ReliabilityConfig,
    recv_endpoint: str = "/recv",
) -> tuple[int, str]:
    hdrs = {**headers, "X-Delivery-Id": did}
    url = f"{c_url.rstrip('/')}{recv_endpoint}"
    if rcfg.enabled:
        await register_pending_c_delivery(
            redis,
            did=did,
            session_id=session_id,
            cipher_group=cipher_group,
            a_url=a_url,
            c_url=c_url,
            payload=payload,
            headers=hdrs,
            rcfg=rcfg,
        )
    try:
        r = await client.post(url, content=payload, headers=hdrs)
        status = r.status_code
        text = r.text[:500]
    except Exception as e:
        logging.warning("POST C 异常 delivery_id=%s: %r", did, e)
        return 0, repr(e)
    if status == 400:
        await redis.delete(_pending_key(did))
    elif status == 200 and rcfg.enabled:
        raw = await redis.get(_pending_key(did))
        if raw:
            doc = json.loads(raw.decode("utf-8"))
            doc["http_ok"] = True
            doc["last_send_ms"] = now_ms()
            ttl = int(max(rcfg.c_ack_timeout_s, rcfg.frag_assembly_timeout_s) * rcfg.c_resend_max) + 120
            await redis.set(_pending_key(did), json.dumps(doc, ensure_ascii=False).encode("utf-8"), ex=ttl)
    elif status not in (200, 400):
        pass
    return status, text


async def handle_recv_ack(
    request: web.Request,
    *,
    redis: Redis,
    http_client: httpx.AsyncClient,
    rcfg: ReliabilityConfig,
) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    did = str(body.get("delivery_id") or "").strip()
    ok = body.get("ok") is True
    reason = str(body.get("reason") or "").strip()
    if not did:
        return web.json_response({"ok": False, "error": "missing delivery_id"}, status=400)

    raw = await redis.get(_pending_key(did))
    if raw:
        doc = json.loads(raw.decode("utf-8"))
        session_id = str(doc.get("session_id") or "")
        a_url = str(doc.get("a_url") or "")
        if ok:
            await redis.delete(_pending_key(did))
            logging.info("C 回执成功，清除 pending delivery_id=%s", did)
        else:
            await redis.delete(_pending_key(did))
            await notify_a_error_notice(
                http_client,
                a_url=a_url,
                session_id=session_id,
                reason=reason or "decrypt_failed",
                detail=str(body.get("detail") or ""),
            )
            logging.warning(
                "C 回执失败 -> 已请求 A 重传 delivery_id=%s reason=%s",
                did,
                reason,
            )
    else:
        if not ok:
            session_id = str(body.get("session_id") or "").split(":", 1)[0]
            a_url = str(body.get("a_url") or "").strip()
            if session_id and a_url:
                await notify_a_error_notice(
                    http_client,
                    a_url=a_url,
                    session_id=session_id,
                    reason=reason or "decrypt_failed",
                    detail=str(body.get("detail") or ""),
                )
        logging.info("recv-ack delivery_id=%s ok=%s (无 pending 或已处理)", did, ok)

    return web.json_response({"ok": True, "delivery_id": did, "accepted": ok})


async def _resend_pending(
    client: httpx.AsyncClient,
    redis: Redis,
    raw: bytes,
    rcfg: ReliabilityConfig,
) -> None:
    doc = json.loads(raw.decode("utf-8"))
    if doc.get("acked"):
        return
    did = str(doc["delivery_id"])
    last_ms = int(doc.get("last_send_ms") or 0)
    if now_ms() - last_ms < int(rcfg.c_ack_timeout_s * 1000):
        return
    attempt = int(doc.get("attempt") or 1)
    if attempt >= rcfg.c_resend_max:
        await redis.delete(_pending_key(did))
        session_id = str(doc.get("session_id") or "")
        a_url = str(doc.get("a_url") or "")
        await notify_a_error_notice(
            client,
            a_url=a_url,
            session_id=session_id,
            reason="c_ack_exhausted",
            detail=f"delivery_id={did} attempts={attempt}",
        )
        logging.warning("C 回执超时且重发耗尽 -> A 重传 delivery_id=%s", did)
        return

    payload = base64.b64decode(str(doc["payload_b64"]).encode("ascii"))
    headers = dict(doc.get("headers") or {})
    c_url = str(doc["c_url"])
    url = f"{c_url.rstrip('/')}{doc.get('recv_endpoint') or '/recv'}"
    attempt += 1
    doc["attempt"] = attempt
    doc["last_send_ms"] = now_ms()
    ttl = int(max(rcfg.c_ack_timeout_s, rcfg.frag_assembly_timeout_s) * rcfg.c_resend_max) + 120
    await redis.set(_pending_key(did), json.dumps(doc, ensure_ascii=False).encode("utf-8"), ex=ttl)
    try:
        r = await client.post(url, content=payload, headers=headers)
        logging.info(
            "C 超时重发 delivery_id=%s attempt=%d status=%s",
            did,
            attempt,
            r.status_code,
        )
        if r.status_code in (200, 400):
            raw2 = await redis.get(_pending_key(did))
            if raw2 and r.status_code == 200:
                d2 = json.loads(raw2.decode("utf-8"))
                d2["http_ok"] = True
                d2["last_send_ms"] = now_ms()
                ttl2 = int(max(rcfg.c_ack_timeout_s, rcfg.frag_assembly_timeout_s) * rcfg.c_resend_max) + 120
                await redis.set(_pending_key(did), json.dumps(d2, ensure_ascii=False).encode("utf-8"), ex=ttl2)
            elif r.status_code == 400:
                await redis.delete(_pending_key(did))
    except Exception:
        logging.exception("C 超时重发失败 delivery_id=%s", did)


async def _check_frag_watches(client: httpx.AsyncClient, redis: Redis, rcfg: ReliabilityConfig) -> None:
    async for key in redis.scan_iter(match=f"{FRAG_WATCH_PREFIX}*", count=50):
        raw = await redis.get(key)
        if not raw:
            continue
        doc = json.loads(raw.decode("utf-8"))
        first_ms = int(doc.get("first_ms") or 0)
        if now_ms() - first_ms < int(rcfg.frag_assembly_timeout_s * 1000):
            continue
        session_id = str(doc.get("session_id") or "")
        cipher_group = str(doc.get("cipher_group") or "")
        cipher_k = int(doc.get("cipher_k") or 0)
        a_url = str(doc.get("a_url") or "")
        dkey = _cipher_asm_key(session_id, cipher_group)
        n_parts = await redis.hlen(dkey)
        if n_parts >= cipher_k > 0:
            await redis.delete(key)
            continue
        await notify_a_error_notice(
            client,
            a_url=a_url,
            session_id=session_id,
            reason="frag_assembly_timeout",
            detail=f"group={cipher_group[:16]} got={n_parts}/{cipher_k}",
        )
        await redis.delete(dkey)
        await redis.delete(key)
        logging.warning(
            "密文分片组装超时 -> A 重传 session=%s group=%s… %d/%d",
            session_id,
            cipher_group[:16],
            n_parts,
            cipher_k,
        )


async def reliability_watchdog_loop(
    redis: Redis,
    http_client: httpx.AsyncClient,
    rcfg: ReliabilityConfig,
) -> None:
    if not rcfg.enabled:
        while True:
            await asyncio.sleep(3600)
    while True:
        try:
            await _check_frag_watches(http_client, redis, rcfg)
            async for key in redis.scan_iter(match=f"{PENDING_C_PREFIX}*", count=50):
                raw = await redis.get(key)
                if raw:
                    await _resend_pending(http_client, redis, raw, rcfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("reliability watchdog 异常")
        await asyncio.sleep(rcfg.watchdog_interval_s)


def make_control_app(
    *,
    redis: Redis,
    http_client: httpx.AsyncClient,
    rcfg: ReliabilityConfig,
) -> web.Application:
    app = web.Application()

    async def _recv_ack(request: web.Request) -> web.Response:
        return await handle_recv_ack(request, redis=redis, http_client=http_client, rcfg=rcfg)

    app.router.add_post("/overlay/recv-ack", _recv_ack)
    app.router.add_get("/health", lambda _: web.json_response({"ok": True, "role": "b-control"}))
    return app


async def run_b_control_server(
    *,
    host: str,
    port: int,
    redis: Redis,
    http_client: httpx.AsyncClient,
    rcfg: ReliabilityConfig,
) -> web.AppRunner:
    app = make_control_app(redis=redis, http_client=http_client, rcfg=rcfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logging.info("B 控制面（recv-ack）: http://%s:%d/overlay/recv-ack", host, port)
    return runner
