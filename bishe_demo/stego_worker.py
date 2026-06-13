from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis

from .reliability import (
    ReliabilityConfig,
    clear_frag_watch,
    delivery_id,
    post_to_server,
    register_frag_watch,
    reliability_watchdog_loop,
    run_control_server,
)
from .common import OverlayEnvelope, now_ms
from .stego import STEGO_METHOD_PSK_HMAC_INPLACE, extract_psk_inplace, extract_trailer


@dataclass(frozen=True)
class StegoWorkerConfig:
    redis_url: str
    queue_key: str
    server_base_url: str
    blpop_timeout_s: int = 5
    psk: bytes | None = None
    link_token: str = ""
    control_host: str = "127.0.0.1"
    control_port: int = 8011
    reliability: ReliabilityConfig | None = None
    recv_endpoint: str = "/recv"
    direction_label: str = "fwd"


ASM_PREFIX = "bishe:overlay:asm:"

# 方案 B：密文分片在 gateway 侧拼成整包 AEAD 后再 POST 给 server（server 只做单次 decrypt）
CIPHER_B_ASM_PREFIX = "bishe:cipher_b_asm:"

_cipher_asm_lock = asyncio.Lock()


def _extract_stego_payload(*, video: bytes, meta: dict, cfg: StegoWorkerConfig) -> bytes:
    em = str(meta.get("extract_method") or STEGO_METHOD_PSK_HMAC_INPLACE).strip()
    if em == STEGO_METHOD_PSK_HMAC_INPLACE:
        if not (cfg.link_token or "").strip():
            raise ValueError("psk_hmac_inplace requires link_token (shared secret token)")
        sfb = int(meta.get("stego_frag_body_len") or 0)
        if sfb <= 0:
            raise ValueError("missing or invalid stego_frag_body_len for psk_hmac_inplace")
        hi = int(meta.get("hls_index") or 0)
        ci = int(meta.get("cipher_index") or 0)
        return extract_psk_inplace(
            video,
            token=cfg.link_token.strip(),
            hls_index=hi,
            frag_idx=ci,
            frag_body_len=sfb,
        )
    return extract_trailer(video)


def _looks_like_psk1(payload: bytes) -> bool:
    # encrypt_hidden: msg_id(16) || "PSK1"(4) || nonce(12) || ct||tag
    return len(payload) >= 20 and payload[16:20] == b"PSK1"


def _asm_data_key(session_id: str, group_id: str) -> str:
    return f"{ASM_PREFIX}{session_id}:{group_id}:data"


def _cipher_b_asm_key(session_id: str, cipher_group: str) -> str:
    return f"{CIPHER_B_ASM_PREFIX}{session_id}:{cipher_group}:data"


async def forward_one(
    client: httpx.AsyncClient,
    cfg: StegoWorkerConfig,
    env: OverlayEnvelope,
    redis: Redis,
) -> bool:
    send_ms = now_ms()
    meta = env.meta or {}
    phase = str(meta.get("overlay_phase") or "").strip()
    # 伪填充分片只随 HLS 出队，不应走默认"整包转发 server"分支（否则会对整段 TS 做 PSK1 误判刷屏）
    if phase == "pad":
        return False

    if phase == "media":
        chunk_total = int(meta.get("chunk_total") or 1)
        chunk_index = int(meta.get("chunk_index") or 0)
        media_group_id = str(meta.get("media_group_id") or "").strip()
        hls_total = int(meta.get("hls_total") or 1)
        hls_index = int(meta.get("hls_index") or 0)
        meta_server_url = str(meta.get("server_url") or meta.get("c_url") or "").strip()
        overlay_phase = str(meta.get("overlay_phase") or "media").strip()
        cipher_group = str(meta.get("cipher_group") or "").strip()
        cipher_k = int(meta.get("cipher_k") or 0)

        if overlay_phase == "pad":
            # client 侧追加的伪填充分片：不入队转发
            return False

        if hls_total > 1 and hls_index < hls_total - 1:
            if chunk_total > 1:
                raise RuntimeError("中间 HLS 分片不应使用字节分块（chunk_total>1）")
            # 兼容提示：历史上可能只有最后一片含隐匿；当前实现下中间分片也可能携带密文分片（取决于 client 的 embed 策略）
            if not cipher_group and cipher_k <= 1:
                logging.info(
                    "HLS 纯媒体分片（无隐匿）已跳过 session=%s seq=%s hls=%d/%d",
                    env.session_id,
                    env.seq,
                    hls_index,
                    hls_total - 1,
                )
                return False

        if chunk_total <= 1:
            server_url = meta_server_url or cfg.server_base_url
            video = env.unpack_payload()
            try:
                payload = _extract_stego_payload(video=video, meta=meta, cfg=cfg)
            except ValueError as e:
                source_url = str(meta.get("source_url") or meta.get("a_url") or "").strip()
                if source_url:
                    try:
                        await client.post(
                            f"{source_url.rstrip('/')}/overlay/error-notice",
                            json={
                                "session_id": env.session_id,
                                "seq": env.seq,
                                "reason": "extract_failed",
                                "detail": str(e),
                            },
                            headers={"X-Overlay": "1"},
                        )
                    except Exception:
                        logging.exception("通知 client error-notice 失败")
                raise
            if not server_url:
                raise RuntimeError("未配置目标 server：control 中 server_url 为空且未设置 gateway-worker --server-url")

            cg = str(meta.get("cipher_group") or "").strip()
            ck = int(meta.get("cipher_k") or 0)
            if cg and ck >= 1:
                ci = int(meta.get("cipher_index") or 0)
                cipher_bytes_real = int(meta.get("cipher_bytes") or 0)
                if ci < 0 or ci >= ck:
                    raise RuntimeError(f"bad cipher_index={ci} for cipher_k={ck}")
                dkey = _cipher_b_asm_key(env.session_id, cg)
                async with _cipher_asm_lock:
                    # 方案 B：密文被均分为 k 份。只有第 0 片应以 "PSK1" 开头（整包 AEAD 格式头），
                    # 可用作"防误判"哨兵：若第 0 片不是 PSK1，则多半是误提取（MAGIC 撞车）或数据损坏。
                    if ci == 0 and not _looks_like_psk1(payload):
                        logging.warning(
                            "cipher 分片 idx=0 非 PSK1，疑似误提取/损坏：session=%s group=%s… bytes=%d（丢弃该片）",
                            env.session_id,
                            cg[:16],
                            len(payload),
                        )
                        await redis.delete(dkey)
                        return False
                    await redis.hset(dkey, mapping={str(ci): payload})
                    await redis.expire(dkey, 7200)
                    n_parts = await redis.hlen(dkey)
                    rcfg = cfg.reliability
                    if rcfg and rcfg.enabled:
                        source_url0 = str(meta.get("source_url") or meta.get("a_url") or "").strip()
                        await register_frag_watch(
                            redis,
                            session_id=env.session_id,
                            cipher_group=cg,
                            cipher_k=ck,
                            client_url=source_url0,
                            rcfg=rcfg,
                        )
                    if n_parts < ck:
                        logging.info(
                            "cipher 分片缓冲 session=%s group=%s… idx=%d/%d hlen=%d",
                            env.session_id,
                            cg[:16],
                            ci,
                            ck - 1,
                            n_parts,
                        )
                        return False
                    if rcfg and rcfg.enabled:
                        await clear_frag_watch(redis, session_id=env.session_id, cipher_group=cg)
                    blobs: list[bytes] = []
                    for i in range(ck):
                        b = await redis.hget(dkey, str(i))
                        if b is None:
                            raise RuntimeError(f"cipher fragment missing i={i}")
                        blobs.append(b)
                    payload = b"".join(blobs)
                    await redis.delete(dkey)
                # client 侧可能把密文补齐到 k*g_bytes（便于固定片大小）；这里按真实密文长度截断，否则 GCM tag 会 InvalidTag。
                if cipher_bytes_real > 0:
                    payload = payload[:cipher_bytes_real]
                if not _looks_like_psk1(payload):
                    logging.warning(
                        "cipher 组装完成但非 PSK1，疑似误提取/分片错配：session=%s group=%s… bytes=%d（丢弃）",
                        env.session_id,
                        cg[:16],
                        len(payload),
                    )
                    return False
                logging.info(
                    "cipher 已在 gateway 组装完成 session=%s group=%s… bytes=%d -> server",
                    env.session_id,
                    cg[:16],
                    len(payload),
                )
            else:
                # 无 cipher_group：不再支持"仅最后一片 trailer 携带整包密文"的旧路径；这类分片直接跳过。
                logging.info(
                    "无 cipher_group，跳过转发：session=%s seq=%s hls=%d/%d",
                    env.session_id,
                    env.seq,
                    hls_index,
                    hls_total,
                )
                return False

            ext_hdr = str(meta.get("extract_method") or STEGO_METHOD_PSK_HMAC_INPLACE).strip()
            headers = {
                "Content-Type": "application/octet-stream",
                "X-Overlay": "1",
                "X-Overlay-Extract": ext_hdr,
                "X-Session": env.session_id,
                "X-Seq": str(env.seq),
                "X-T0-MS": str(env.t0_ms),
                "X-GW-SEND-MS": str(send_ms),
                "X-Bishe-HLS-Index": str(hls_index),
                "X-Bishe-HLS-Total": str(hls_total),
                "X-Bishe-Cipher-Index": str(int(meta.get("cipher_index") or 0)),
            }
            source_url0 = str(meta.get("source_url") or meta.get("a_url") or "").strip()
            rcfg = cfg.reliability
            did = delivery_id(env.session_id, cg) if cg else delivery_id(env.session_id, str(env.seq))
            if rcfg and rcfg.enabled:
                status, body = await post_to_server(
                    client,
                    server_url=server_url,
                    payload=payload,
                    headers=headers,
                    did=did,
                    redis=redis,
                    session_id=env.session_id,
                    cipher_group=cg,
                    client_url=source_url0,
                    rcfg=rcfg,
                    recv_endpoint=cfg.recv_endpoint,
                )
                if status == 200:
                    return True
                if status == 400:
                    return False
                raise RuntimeError(f"server 返回异常: status={status} body={body[:200]}")
            url = f"{server_url.rstrip('/')}{cfg.recv_endpoint}"
            r = await client.post(url, content=payload, headers=headers)
            if r.status_code != 200:
                raise RuntimeError(f"server 返回异常: status={r.status_code} body={r.text[:200]}")
            return True

        if not media_group_id:
            raise RuntimeError("多分片媒体缺少 meta.media_group_id")
        sid = env.session_id
        dkey = _asm_data_key(sid, media_group_id)
        piece = env.unpack_payload()

        if chunk_index == 0:
            await redis.hset(dkey, mapping={str(chunk_index): piece})
            await redis.expire(dkey, 7200)
        else:
            await redis.hset(dkey, mapping={str(chunk_index): piece})
        n_parts = await redis.hlen(dkey)
        logging.info(
            "媒体分片缓冲 session=%s group=%s idx=%d/%d hlen=%d",
            sid,
            media_group_id,
            chunk_index,
            chunk_total - 1,
            n_parts,
        )
        if n_parts < chunk_total:
            return False
        server_url = meta_server_url or cfg.server_base_url
        if not server_url:
            raise RuntimeError("未配置目标 server：请在 client 的响应头带 X-C-URL 或启动 gateway-worker 时提供 --server-url")

        blobs: list[bytes] = []
        for i in range(chunk_total):
            b = await redis.hget(dkey, str(i))
            if b is None:
                raise RuntimeError(f"分片缺失 index={i}")
            blobs.append(b)
        video = b"".join(blobs)

        try:
            payload = _extract_stego_payload(video=video, meta=meta, cfg=cfg)
        except ValueError as e:
            source_url = str(meta.get("source_url") or meta.get("a_url") or "").strip()
            if source_url:
                try:
                    await client.post(
                        f"{source_url.rstrip('/')}/overlay/error-notice",
                        json={
                            "session_id": sid,
                            "seq": env.seq,
                            "reason": "extract_failed",
                            "detail": str(e),
                            "media_group_id": media_group_id,
                        },
                        headers={"X-Overlay": "1"},
                    )
                except Exception:
                    logging.exception("通知 client error-notice 失败")
            raise

        await redis.delete(dkey)

        # 组装后同样按 overlay meta 走"是否为密文/分片"的筛选与组装逻辑，避免误提取 trailer 直接打到 server。
        cg = str(meta.get("cipher_group") or "").strip()
        ck = int(meta.get("cipher_k") or 0)
        if cg and ck >= 1:
            ci = int(meta.get("cipher_index") or 0)
            cipher_bytes_real = int(meta.get("cipher_bytes") or 0)
            if ci < 0 or ci >= ck:
                raise RuntimeError(f"bad cipher_index={ci} for cipher_k={ck}")
            dkey2 = _cipher_b_asm_key(sid, cg)
            async with _cipher_asm_lock:
                if ci == 0 and not _looks_like_psk1(payload):
                    logging.warning(
                        "cipher 分片(idx=0)非 PSK1（chunk-asm 后），疑似误提取/损坏：session=%s group=%s… bytes=%d（丢弃该片）",
                        sid,
                        cg[:16],
                        len(payload),
                    )
                    await redis.delete(dkey2)
                    return False
                await redis.hset(dkey2, mapping={str(ci): payload})
                await redis.expire(dkey2, 7200)
                n2 = await redis.hlen(dkey2)
                rcfg2 = cfg.reliability
                if rcfg2 and rcfg2.enabled:
                    source_url2 = str(meta.get("source_url") or meta.get("a_url") or "").strip()
                    await register_frag_watch(
                        redis,
                        session_id=sid,
                        cipher_group=cg,
                        cipher_k=ck,
                        client_url=source_url2,
                        rcfg=rcfg2,
                    )
                if n2 < ck:
                    logging.info(
                        "cipher 分片缓冲（chunk-asm 后）session=%s group=%s… idx=%d/%d hlen=%d",
                        sid,
                        cg[:16],
                        ci,
                        ck - 1,
                        n2,
                    )
                    return False
                if rcfg2 and rcfg2.enabled:
                    await clear_frag_watch(redis, session_id=sid, cipher_group=cg)
                blobs2: list[bytes] = []
                for i in range(ck):
                    b = await redis.hget(dkey2, str(i))
                    if b is None:
                        raise RuntimeError(f"cipher fragment missing i={i}")
                    blobs2.append(b)
                payload = b"".join(blobs2)
                await redis.delete(dkey2)
            if cipher_bytes_real > 0:
                payload = payload[:cipher_bytes_real]
            if not _looks_like_psk1(payload):
                logging.warning(
                    "cipher 组装完成但非 PSK1（chunk-asm 后），疑似误提取/错配：session=%s group=%s… bytes=%d（丢弃）",
                    sid,
                    cg[:16],
                    len(payload),
                )
                return False
            logging.info(
                "cipher 已在 gateway 组装完成（chunk-asm 后）session=%s group=%s… bytes=%d -> server",
                sid,
                cg[:16],
                len(payload),
            )
        else:
            logging.info(
                "无 cipher_group，跳过转发（chunk-asm 后）：session=%s seq=%s bytes=%d",
                sid,
                env.seq,
                len(payload),
            )
            return False

        hi = int(meta.get("hls_index") or 0)
        ht = int(meta.get("hls_total") or 1)
        ext_hdr2 = str(meta.get("extract_method") or STEGO_METHOD_PSK_HMAC_INPLACE).strip()
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Overlay": "1",
            "X-Overlay-Extract": ext_hdr2,
            "X-Session": sid,
            "X-Seq": str(env.seq),
            "X-T0-MS": str(env.t0_ms),
            "X-GW-SEND-MS": str(send_ms),
            "X-Bishe-HLS-Index": str(hi),
            "X-Bishe-HLS-Total": str(ht),
            "X-Bishe-Cipher-Index": str(int(meta.get("cipher_index") or 0)),
        }
        source_url3 = str(meta.get("source_url") or meta.get("a_url") or "").strip()
        rcfg3 = cfg.reliability
        did3 = delivery_id(sid, cg) if cg else delivery_id(sid, str(env.seq))
        if rcfg3 and rcfg3.enabled:
            status, body = await post_to_server(
                client,
                server_url=server_url,
                payload=payload,
                headers=headers,
                did=did3,
                redis=redis,
                session_id=sid,
                cipher_group=cg,
                client_url=source_url3,
                rcfg=rcfg3,
                recv_endpoint=cfg.recv_endpoint,
            )
            if status == 200:
                return True
            if status == 400:
                return False
            raise RuntimeError(f"server 返回异常: status={status} body={body[:200]}")
        url = f"{server_url.rstrip('/')}{cfg.recv_endpoint}"
        r = await client.post(url, content=payload, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"server 返回异常: status={r.status_code} body={r.text[:200]}")
        return True

    # 默认：直接转发原始 payload（原 demo 行为）
    server_url = str(meta.get("server_url") or meta.get("c_url") or "").strip() or cfg.server_base_url
    if not server_url:
        raise RuntimeError("未配置目标 server：请在 client->gateway 的封装 meta 里带 server_url，或启动 gateway-worker 时提供 --server-url")

    url = f"{server_url.rstrip('/')}{cfg.recv_endpoint}"
    payload = env.unpack_payload()

    # 兜底保护：只要要发给 server 的不是 PSK1 AEAD 包，就直接丢弃。
    # 否则（例如 hls-puller 误把某些"非媒体/非密文"任务塞进队列）会导致 server 被大量 400 轰炸。
    if not _looks_like_psk1(payload):
        logging.info(
            "默认转发分支检测到非 PSK1 payload，跳过：session=%s seq=%s bytes=%d",
            env.session_id,
            env.seq,
            len(payload),
        )
        return False

    headers = {
        "Content-Type": env.content_type or "application/octet-stream",
        "X-Overlay": "1",
        "X-Session": env.session_id,
        "X-Seq": str(env.seq),
        "X-T0-MS": str(env.t0_ms),
        "X-GW-SEND-MS": str(send_ms),
    }

    r = await client.post(url, content=payload, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"server 返回异常: status={r.status_code} body={r.text[:200]}")
    return True


async def run_stego_worker(
    *,
    redis_url: str,
    queue_key: str,
    server_base_url: str,
    psk: bytes | None = None,
    link_token: str = "",
    control_host: str = "127.0.0.1",
    control_port: int = 8011,
    reliability: ReliabilityConfig | None = None,
    recv_endpoint: str = "/recv",
    direction_label: str = "fwd",
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rcfg = reliability if reliability is not None else ReliabilityConfig()
    cfg = StegoWorkerConfig(
        redis_url=redis_url,
        queue_key=queue_key,
        server_base_url=server_base_url,
        psk=psk,
        link_token=link_token or "",
        control_host=control_host,
        control_port=control_port,
        reliability=rcfg,
        recv_endpoint=recv_endpoint.rstrip("/") or "/recv",
        direction_label=direction_label or "fwd",
    )

    redis = Redis.from_url(redis_url, decode_responses=False)
    await redis.ping()
    logging.info(
        "gateway worker [%s] 已连接 Redis: %s (queue=%s) -> %s%s reliability=%s",
        cfg.direction_label,
        redis_url,
        queue_key,
        server_base_url,
        cfg.recv_endpoint,
        rcfg.enabled,
    )

    async with httpx.AsyncClient(timeout=120.0, verify=False, trust_env=False) as client:
        control_runner = None
        watchdog_task = None
        if rcfg.enabled:
            control_runner = await run_control_server(
                host=control_host,
                port=control_port,
                redis=redis,
                http_client=client,
                rcfg=rcfg,
            )
            watchdog_task = asyncio.create_task(reliability_watchdog_loop(redis, client, rcfg))
        try:
            while True:
                try:
                    item = await redis.blpop(queue_key, timeout=cfg.blpop_timeout_s)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.warning("gateway worker [%s] BLPOP 异常，1s后重试: %r", cfg.direction_label, e)
                    await asyncio.sleep(1)
                    continue
                if item is None:
                    continue
                _, data = item
                logging.debug("gateway worker [%s] BLPOP 获取一条消息 len=%d", cfg.direction_label, len(data))
                try:
                    env = OverlayEnvelope.from_json_bytes(data)
                    env2 = OverlayEnvelope.pack(
                        session_id=env.session_id,
                        seq=env.seq,
                        payload=env.unpack_payload(),
                        content_type=env.content_type,
                        meta={**env.meta, "gw_worker_pop_ms": now_ms()},
                        t0_ms=env.t0_ms,
                    )
                    forwarded = await forward_one(client, cfg, env2, redis)
                    meta2 = env2.meta or {}
                    phase2 = str(meta2.get("overlay_phase") or "").strip()
                    if phase2 == "control":
                        logging.info("控制帧已处理 session=%s seq=%d", env2.session_id, env2.seq)
                    else:
                        n = len(env2.unpack_payload())
                        if phase2 == "media":
                            ct = int(meta2.get("chunk_total") or 1)
                            if forwarded:
                                logging.info(
                                    "媒体帧已拆解并转发 session=%s seq=%d embedded_bytes=%d",
                                    env2.session_id,
                                    env2.seq,
                                    n,
                                )
                            elif ct > 1:
                                logging.info(
                                    "媒体分片已缓冲 session=%s seq=%d chunk_bytes=%d",
                                    env2.session_id,
                                    env2.seq,
                                    n,
                                )
                        elif forwarded:
                            logging.info("转发成功 session=%s seq=%d bytes=%d", env2.session_id, env2.seq, n)
                except Exception as e:
                    logging.exception("转发失败（丢弃该条）: %r", e)
                    await asyncio.sleep(0.1)
        finally:
            if watchdog_task:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            if control_runner:
                await control_runner.cleanup()
            await redis.aclose()
