from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis

from .common import OverlayEnvelope, make_hls_master_path, now_ms
from .hls_facade import parse_master_playlist_variant_uri, parse_media_playlist_all_segment_uris
from .stego import extract_trailer


@dataclass(frozen=True)
class BPullConfig:
    a_base_url: str
    session_id: str
    redis_url: str
    queue_key: str
    poll_interval_s: float
    segment_interval_s: float = 0.0
    hls_prefix: str = "/hls"


def _envelope_from_get_response(r: httpx.Response, fallback_session: str, a_base_url: str) -> OverlayEnvelope:
    body = r.content
    kind = (r.headers.get("X-Bishe-Kind") or "direct").strip()
    session_id = (r.headers.get("X-Session") or "").strip() or fallback_session
    seq = int(r.headers.get("X-Seq") or "0")
    t0_ms = int(r.headers.get("X-T0-MS") or "0")
    c_url_hdr = (r.headers.get("X-C-URL") or "").strip()
    stego_hdr = (r.headers.get("X-Bishe-Stego") or "").strip()
    ctype = (r.headers.get("X-Content-Type") or "application/octet-stream").strip()
    ua = (r.headers.get("X-A-UA") or "").strip()
    a_recv_ms = int(r.headers.get("X-A-Recv-MS") or "0")

    b_pull_ms = now_ms()
    base_meta: dict = {
        "c_url": c_url_hdr,
        "a_url": a_base_url,
        "ua": ua,
        "a_recv_ms": a_recv_ms,
        "b_pull_ms": b_pull_ms,
    }

    if kind == "media":
        overlay_phase = (r.headers.get("X-Bishe-Overlay-Phase") or "media").strip()
        meta = {
            **base_meta,
            "overlay_phase": overlay_phase,
        }
        if stego_hdr:
            meta["extract_method"] = stego_hdr
        cg = (r.headers.get("X-Bishe-Cipher-Group") or "").strip()
        if cg:
            meta["cipher_group"] = cg
        ck = (r.headers.get("X-Bishe-Cipher-K") or "").strip()
        if ck.isdigit():
            meta["cipher_k"] = int(ck)
        ci = (r.headers.get("X-Bishe-Cipher-Index") or "").strip()
        if ci.isdigit():
            meta["cipher_index"] = int(ci)
        cb = (r.headers.get("X-Bishe-Cipher-Bytes") or "").strip()
        if cb.isdigit():
            meta["cipher_bytes"] = int(cb)
        sfb = (r.headers.get("X-Chunk-Size") or "").strip()
        if sfb.isdigit():
            meta["stego_frag_body_len"] = int(sfb)
        mg = (r.headers.get("X-Bishe-Media-Group") or "").strip()
        if mg:
            meta["media_group_id"] = mg
        ct_raw = (r.headers.get("X-Bishe-Chunk-Total") or "1").strip()
        ci_raw = (r.headers.get("X-Bishe-Chunk-Index") or "0").strip()
        meta["chunk_total"] = int(ct_raw) if ct_raw.isdigit() else 1
        meta["chunk_index"] = int(ci_raw) if ci_raw.isdigit() else 0
        ht_raw = (r.headers.get("X-Bishe-HLS-Total") or "").strip()
        hi_raw = (r.headers.get("X-Bishe-HLS-Index") or "").strip()
        meta["hls_total"] = int(ht_raw) if ht_raw.isdigit() else 1
        meta["hls_index"] = int(hi_raw) if hi_raw.isdigit() else 0
        return OverlayEnvelope.pack(
            session_id=session_id,
            seq=seq,
            payload=body,
            content_type=ctype or "video/mp4",
            meta=meta,
            t0_ms=t0_ms,
        )

    if kind == "control":
        raise ValueError("control frames are disabled")

    raw = extract_trailer(body)
    meta = {**base_meta, "c_url": c_url_hdr or base_meta.get("c_url", "")}
    return OverlayEnvelope.pack(
        session_id=session_id,
        seq=seq,
        payload=raw,
        content_type=ctype or "application/octet-stream",
        meta=meta,
        t0_ms=t0_ms,
    )


async def run_b_pull(
    *,
    a_base_url: str,
    session_id: str,
    redis_url: str,
    queue_key: str,
    poll_interval_s: float = 0.25,
    segment_interval_s: float = 0.0,
    hls_prefix: str = "/hls",
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = BPullConfig(
        a_base_url=a_base_url.rstrip("/"),
        session_id=session_id,
        redis_url=redis_url,
        queue_key=queue_key,
        poll_interval_s=poll_interval_s,
        segment_interval_s=max(0.0, float(segment_interval_s)),
        hls_prefix=hls_prefix.rstrip("/") or "/hls",
    )

    redis = Redis.from_url(redis_url, decode_responses=False)
    await redis.ping()
    live_session = [cfg.session_id]
    master_url = f"{cfg.a_base_url}{make_hls_master_path(live_session[0], prefix=cfg.hls_prefix)}"
    seg_iv = cfg.segment_interval_s
    logging.info(
        "B pull 启动(类 HLS 客户端): master=%s -> index.m3u8 -> seg-*.ts -> Redis %s "
        "(segment_interval_s=%s hls_prefix=%s)",
        master_url,
        cfg.queue_key,
        seg_iv if seg_iv > 0 else "burst",
        cfg.hls_prefix,
    )

    media_playlist_url: str | None = None
    try:
        async with httpx.AsyncClient(timeout=120.0, verify=False, trust_env=False) as client:
            while True:
                if media_playlist_url is None:
                    master_url = f"{cfg.a_base_url}{make_hls_master_path(live_session[0], prefix=cfg.hls_prefix)}"
                    try:
                        rm = await client.get(master_url)
                    except httpx.HTTPError as e:
                        logging.warning("GET master 失败: %s", e)
                        await asyncio.sleep(cfg.poll_interval_s)
                        continue
                    if rm.status_code != 200:
                        logging.warning("GET master 状态 %s", rm.status_code)
                        await asyncio.sleep(cfg.poll_interval_s)
                        continue
                    media_playlist_url = parse_master_playlist_variant_uri(rm.text, str(rm.url))
                    if not media_playlist_url:
                        logging.warning("master.m3u8 中未解析到媒体列表")
                        await asyncio.sleep(cfg.poll_interval_s)
                        continue
                    logging.info("已解析媒体列表 URL: %s", media_playlist_url)

                try:
                    ri = await client.get(media_playlist_url)
                except httpx.HTTPError as e:
                    logging.warning("GET index 失败: %s", e)
                    await asyncio.sleep(cfg.poll_interval_s)
                    continue
                if ri.status_code != 200:
                    logging.warning("GET index.m3u8 状态 %s", ri.status_code)
                    await asyncio.sleep(cfg.poll_interval_s)
                    continue

                seg_urls = parse_media_playlist_all_segment_uris(ri.text, str(ri.url))
                if not seg_urls:
                    await asyncio.sleep(cfg.poll_interval_s)
                    continue

                logging.info("index.m3u8 解析到 %d 个分片 URI，将顺序 GET", len(seg_urls))
                saw_next_session = False
                for seg_i, seg_url in enumerate(seg_urls):
                    if cfg.segment_interval_s > 0 and seg_i > 0:
                        await asyncio.sleep(cfg.segment_interval_s)
                    try:
                        rs = await client.get(seg_url)
                    except httpx.HTTPError as e:
                        logging.warning("GET 分片失败: %s", e)
                        await asyncio.sleep(cfg.poll_interval_s)
                        saw_next_session = False
                        break
                    if rs.status_code == 404:
                        await asyncio.sleep(cfg.poll_interval_s)
                        saw_next_session = False
                        break
                    if rs.status_code != 200:
                        logging.warning("GET 分片状态 %s: %s", rs.status_code, seg_url)
                        await asyncio.sleep(cfg.poll_interval_s)
                        saw_next_session = False
                        break

                    try:
                        env = _envelope_from_get_response(rs, live_session[0], cfg.a_base_url)
                        await redis.rpush(cfg.queue_key, env.to_json_bytes())
                        env_seq = int(rs.headers.get("X-Seq") or "0")
                        hdr_sess = (rs.headers.get("X-Session") or "").strip() or live_session[0]
                        logging.info(
                            "HLS 拉片并入队 session=%s X-Seq=%d kind=%s",
                            hdr_sess,
                            env_seq,
                            rs.headers.get("X-Bishe-Kind", "direct"),
                        )
                        nxt = (rs.headers.get("X-Next-Session") or "").strip()
                        if nxt:
                            live_session[0] = nxt
                            media_playlist_url = None
                            saw_next_session = True
                            logging.info("已跟随 A 轮换 session -> %s，将重新 GET master", nxt)
                            break
                    except Exception:
                        logging.exception("解析分片响应并入队失败")
                        await asyncio.sleep(cfg.poll_interval_s)
                        saw_next_session = False
                        break

                if saw_next_session:
                    continue
    finally:
        await redis.aclose()
