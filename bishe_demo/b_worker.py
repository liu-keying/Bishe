from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis

from .common import OverlayEnvelope, now_ms
from .stego import extract_trailer, extract_ts_private


@dataclass(frozen=True)
class BWorkerConfig:
    redis_url: str
    queue_key: str
    c_base_url: str
    blpop_timeout_s: int = 5


CTRL_KEY_PREFIX = "bishe:overlay:ctrl:"
ASM_PREFIX = "bishe:overlay:asm:"


def _ctrl_key(session_id: str) -> str:
    return f"{CTRL_KEY_PREFIX}{session_id}"


def _asm_meta_key(session_id: str, group_id: str) -> str:
    return f"{ASM_PREFIX}{session_id}:{group_id}:meta"


def _asm_data_key(session_id: str, group_id: str) -> str:
    return f"{ASM_PREFIX}{session_id}:{group_id}:data"


async def forward_one(
    client: httpx.AsyncClient,
    cfg: BWorkerConfig,
    env: OverlayEnvelope,
    redis: Redis,
) -> bool:
    send_ms = now_ms()
    meta = env.meta or {}
    phase = str(meta.get("overlay_phase") or "").strip()

    if phase == "control":
        c_url = str(meta.get("c_url") or "").strip() or cfg.c_base_url
        extract = meta.get("extract") or {"method": "append_marker"}
        if not isinstance(extract, dict):
            raise RuntimeError("control: meta.extract 必须是对象")
        data = json.dumps({"c_url": c_url, "extract": extract}, ensure_ascii=False, separators=(",", ":"))
        await redis.set(_ctrl_key(env.session_id), data.encode("utf-8"), ex=3600)
        logging.info("已缓存隐匿控制信息 session=%s seq=%s", env.session_id, env.seq)
        return False

    if phase == "media":
        chunk_total = int(meta.get("chunk_total") or 1)
        chunk_index = int(meta.get("chunk_index") or 0)
        media_group_id = str(meta.get("media_group_id") or "").strip()
        hls_total = int(meta.get("hls_total") or 1)
        hls_index = int(meta.get("hls_index") or 0)
        meta_method = str(meta.get("extract_method") or meta.get("stego") or "").strip()
        meta_c_url = str(meta.get("c_url") or "").strip()

        if hls_total > 1 and hls_index < hls_total - 1:
            if chunk_total > 1:
                raise RuntimeError("中间 HLS 分片不应使用字节分块（chunk_total>1）")
            logging.info(
                "HLS 纯媒体分片（无隐匿）已跳过 session=%s seq=%s hls=%d/%d",
                env.session_id,
                env.seq,
                hls_index,
                hls_total - 1,
            )
            return False

        if chunk_total <= 1:
            method = meta_method
            c_url = meta_c_url or cfg.c_base_url
            if not method:
                raw_ctrl = await redis.get(_ctrl_key(env.session_id))
                if raw_ctrl is None:
                    raise RuntimeError(
                        "media 到达但缺少控制信息：请先发送 overlay_phase=control，或在 A 的响应头带 X-Bishe-Stego 并在 meta 里传递"
                    )
                ctrl = json.loads(raw_ctrl.decode("utf-8"))
                method = str(((ctrl.get("extract") or {}).get("method")) or "append_marker")
                c_url = str(ctrl.get("c_url") or "").strip() or c_url
            video = env.unpack_payload()
            if method == "append_marker":
                payload = extract_trailer(video)
            elif method == "ts_private":
                payload = extract_ts_private(video)
            else:
                raise RuntimeError(f"不支持的拆解方式: {method}")
            if not c_url:
                raise RuntimeError("未配置目标 C：control 中 c_url 为空且未设置 b-worker --c-url")
            # 兼容旧控制帧模式：若使用 ctrl_key 推导出来的 c_url/method，则消费掉该 control
            if not meta_method:
                await redis.delete(_ctrl_key(env.session_id))

            url = f"{c_url.rstrip('/')}/recv"
            headers = {
                "Content-Type": "application/octet-stream",
                "X-Overlay": "1",
                "X-Overlay-Extract": method,
                "X-Session": env.session_id,
                "X-Seq": str(env.seq),
                "X-T0-MS": str(env.t0_ms),
                "X-B-SEND-MS": str(send_ms),
            }
            r = await client.post(url, content=payload, headers=headers)
            if r.status_code != 200:
                raise RuntimeError(f"C 返回异常: status={r.status_code} body={r.text[:200]}")
            return True

        if not media_group_id:
            raise RuntimeError("多分片媒体缺少 meta.media_group_id")
        sid = env.session_id
        mkey = _asm_meta_key(sid, media_group_id)
        dkey = _asm_data_key(sid, media_group_id)
        piece = env.unpack_payload()

        if chunk_index == 0:
            # 优先使用数据面 meta（无 control 帧模式）；否则回退到旧 ctrl_key
            if meta_method:
                raw_meta = json.dumps(
                    {"c_url": meta_c_url, "extract": {"method": meta_method}},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                await redis.set(mkey, raw_meta, ex=7200)
            else:
                raw_ctrl = await redis.get(_ctrl_key(sid))
                if raw_ctrl is None:
                    raise RuntimeError("分片媒体首包到达但缺少 control（或 control 已被消费）")
                await redis.set(mkey, raw_ctrl, ex=7200)
                await redis.delete(_ctrl_key(sid))
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

        raw_ctrl = await redis.get(mkey)
        if raw_ctrl is None:
            raise RuntimeError("分片组装缺少缓存的 control 元数据")
        ctrl = json.loads(raw_ctrl.decode("utf-8"))
        method = str((ctrl.get("extract") or {}).get("method") or "append_marker")
        c_url = str(ctrl.get("c_url") or "").strip() or cfg.c_base_url
        if not c_url:
            raise RuntimeError("未配置目标 C：control 中 c_url 为空且未设置 b-worker --c-url")

        blobs: list[bytes] = []
        for i in range(chunk_total):
            b = await redis.hget(dkey, str(i))
            if b is None:
                raise RuntimeError(f"分片缺失 index={i}")
            blobs.append(b)
        video = b"".join(blobs)

        if method == "append_marker":
            payload = extract_trailer(video)
        elif method == "ts_private":
            payload = extract_ts_private(video)
        else:
            raise RuntimeError(f"不支持的拆解方式: {method}")

        await redis.delete(dkey)
        await redis.delete(mkey)

        url = f"{c_url.rstrip('/')}/recv"
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Overlay": "1",
            "X-Overlay-Extract": method,
            "X-Session": sid,
            "X-Seq": str(env.seq),
            "X-T0-MS": str(env.t0_ms),
            "X-B-SEND-MS": str(send_ms),
        }
        r = await client.post(url, content=payload, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"C 返回异常: status={r.status_code} body={r.text[:200]}")
        return True

    # 默认：直接转发原始 payload（原 demo 行为）
    c_url = str(meta.get("c_url") or "").strip() or cfg.c_base_url
    if not c_url:
        raise RuntimeError("未配置目标 C：请在 A->B 的封装 meta 里带 c_url，或启动 b-worker 时提供 --c-url")

    url = f"{c_url.rstrip('/')}/recv"
    payload = env.unpack_payload()

    headers = {
        "Content-Type": env.content_type or "application/octet-stream",
        "X-Overlay": "1",
        "X-Session": env.session_id,
        "X-Seq": str(env.seq),
        "X-T0-MS": str(env.t0_ms),
        "X-B-SEND-MS": str(send_ms),
    }

    r = await client.post(url, content=payload, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"C 返回异常: status={r.status_code} body={r.text[:200]}")
    return True


async def run_b_worker(*, redis_url: str, queue_key: str, c_base_url: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = BWorkerConfig(redis_url=redis_url, queue_key=queue_key, c_base_url=c_base_url)

    redis = Redis.from_url(redis_url, decode_responses=False)
    await redis.ping()
    logging.info("B worker 已连接 Redis: %s (queue=%s) -> C=%s", redis_url, queue_key, c_base_url)

    async with httpx.AsyncClient(timeout=120.0, verify=False) as client:
        try:
            while True:
                item = await redis.blpop(queue_key, timeout=cfg.blpop_timeout_s)
                if item is None:
                    continue
                _, data = item
                try:
                    env = OverlayEnvelope.from_json_bytes(data)
                    env2 = OverlayEnvelope.pack(
                        session_id=env.session_id,
                        seq=env.seq,
                        payload=env.unpack_payload(),
                        content_type=env.content_type,
                        meta={**env.meta, "b_worker_pop_ms": now_ms()},
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
                        else:
                            logging.info("转发成功 session=%s seq=%d bytes=%d", env2.session_id, env2.seq, n)
                except Exception as e:
                    logging.exception("转发失败（丢弃该条）: %r", e)
                    await asyncio.sleep(0.1)
        finally:
            await redis.aclose()

