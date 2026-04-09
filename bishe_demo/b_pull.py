from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis

from .common import OverlayEnvelope, make_hls_master_path, now_ms
from .hls_facade import parse_master_playlist_variant_uri, parse_media_playlist_all_segment_uris
from .stego import extract_trailer, extract_ts_private


@dataclass(frozen=True)
class BPullConfig:
    a_base_url: str
    session_id: str
    redis_url: str
    queue_key: str
    poll_interval_s: float


def _envelope_from_get_response(r: httpx.Response, fallback_session: str) -> OverlayEnvelope:
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
        "ua": ua,
        "a_recv_ms": a_recv_ms,
        "b_pull_ms": b_pull_ms,
    }

    if kind == "media":
        meta = {
            **base_meta,
            "overlay_phase": "media",
        }
        if stego_hdr:
            meta["extract_method"] = stego_hdr
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
        stego = (r.headers.get("X-Bishe-Stego") or "append_marker").strip()
        if stego == "ts_private":
            inner = extract_ts_private(body)
        else:
            inner = extract_trailer(body)
        ctrl = json.loads(inner.decode("utf-8"))
        c_url = str(ctrl.get("c_url") or c_url_hdr).strip()
        extract = ctrl.get("extract") or {"method": "append_marker"}
        meta = {
            **base_meta,
            "c_url": c_url,
            "overlay_phase": "control",
            "extract": extract if isinstance(extract, dict) else {"method": "append_marker"},
        }
        return OverlayEnvelope.pack(
            session_id=session_id,
            seq=seq,
            payload=b"",
            content_type="application/json",
            meta=meta,
            t0_ms=t0_ms,
        )

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
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = BPullConfig(
        a_base_url=a_base_url.rstrip("/"),
        session_id=session_id,
        redis_url=redis_url,
        queue_key=queue_key,
        poll_interval_s=poll_interval_s,
    )

    redis = Redis.from_url(redis_url, decode_responses=False)
    await redis.ping()
    live_session = [cfg.session_id]
    master_url = f"{cfg.a_base_url}{make_hls_master_path(live_session[0])}"
    logging.info(
        "B pull 启动(类 HLS 客户端): master=%s -> index.m3u8 -> seg-*.ts -> Redis %s",
        master_url,
        cfg.queue_key,
    )

    media_playlist_url: str | None = None
    try:
        async with httpx.AsyncClient(timeout=120.0, verify=False) as client:
            while True:
                if media_playlist_url is None:
                    master_url = f"{cfg.a_base_url}{make_hls_master_path(live_session[0])}"
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
                for seg_url in seg_urls:
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
                        env = _envelope_from_get_response(rs, live_session[0])
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
