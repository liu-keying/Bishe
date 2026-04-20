from __future__ import annotations

import argparse
import asyncio
import glob
import os
import time
import uuid

import httpx


def _load_segments(*, segments_glob: str, limit: int) -> list[bytes]:
    paths = sorted(glob.glob(segments_glob))
    if not paths:
        raise FileNotFoundError(f"no segments matched: {segments_glob!r}")
    if limit > 0:
        paths = paths[:limit]
    out: list[bytes] = []
    for p in paths:
        with open(p, "rb") as f:
            out.append(f.read())
    return out


async def _drain_until_stable(
    client: httpx.AsyncClient,
    *,
    c_stats_url: str,
    target_unique: int,
    drain_timeout_s: float,
    idle_s: float,
    poll_s: float,
) -> dict:
    t_start = time.time()
    last_unique = -1
    last_change = t_start
    out: dict = {}
    while True:
        r = await client.get(c_stats_url, params={"window_s": "5"})
        r.raise_for_status()
        out = r.json()
        unique = int(out.get("unique_msg_ids") or 0)
        if unique != last_unique:
            last_unique = unique
            last_change = time.time()
        if unique >= target_unique:
            return out
        now = time.time()
        if now - t_start >= drain_timeout_s:
            return out
        if now - last_change >= idle_s:
            return out
        await asyncio.sleep(poll_s)


async def _one_post(
    client: httpx.AsyncClient,
    *,
    a_url: str,
    c_url: str,
    segments: list[bytes],
    hidden_bytes: int,
) -> None:
    msg_id = uuid.uuid4().bytes  # 16B -> C 用于 unique/dup 统计
    if hidden_bytes < 16:
        hidden = msg_id[:hidden_bytes]
    else:
        hidden = msg_id + os.urandom(hidden_bytes - 16)

    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for i, seg in enumerate(segments):
        files.append(("segment", (f"seg{i:03d}.ts", seg, "video/mp2t")))
    files.append(("hidden", ("hidden.bin", hidden, "application/octet-stream")))

    url = f"{a_url.rstrip('/')}/overlay/embed-hls"
    params = {"c": c_url}
    r = await client.post(url, params=params, files=files)
    r.raise_for_status()


async def main_async() -> int:
    p = argparse.ArgumentParser(description="压测 /overlay/embed-hls（真实 ts 分片；端到端统计在 C /stats）")
    p.add_argument("--a-url", default="http://127.0.0.1:8000")
    p.add_argument("--c-url", default="http://127.0.0.1:8002")
    p.add_argument("--segments", required=True, help=r'glob，如 ".\\hls_seg*.ts"（按文件名排序）')
    p.add_argument("--segments-limit", type=int, default=0, help="可选，只取前 N 个分片（0=不限制）")
    p.add_argument("--hidden-bytes", type=int, default=4096)
    p.add_argument("--concurrency", type=int, default=1, help="并发（HLS 多分片上传较重，建议 1~3）")
    p.add_argument("--duration-s", type=float, default=10.0)
    p.add_argument("--timeout-s", type=float, default=120.0)
    p.add_argument("--drain-timeout-s", type=float, default=300.0, help="发完后等待链路清空的最长时间")
    p.add_argument("--idle-s", type=float, default=10.0, help="若该时间内 unique 无增长，认为已稳定")
    p.add_argument("--poll-s", type=float, default=1.0, help="轮询 C /stats 间隔")
    args = p.parse_args()

    conc = max(1, int(args.concurrency))
    dur_s = max(0.1, float(args.duration_s))
    hidden_bytes = max(1, int(args.hidden_bytes))

    segments = _load_segments(segments_glob=args.segments, limit=int(args.segments_limit))
    seg_total_bytes = sum(len(s) for s in segments)

    c_reset = f"{args.c_url.rstrip('/')}/stats/reset"
    c_stats = f"{args.c_url.rstrip('/')}/stats"

    sent_ok = 0
    sent_err = 0
    start = time.time()
    stop_at = start + dur_s

    limits = httpx.Limits(max_connections=conc * 2, max_keepalive_connections=conc * 2)
    async with httpx.AsyncClient(timeout=args.timeout_s, limits=limits) as client:
        rr = await client.post(c_reset)
        rr.raise_for_status()

        sem = asyncio.Semaphore(conc)

        async def worker_loop() -> None:
            nonlocal sent_ok, sent_err
            while time.time() < stop_at:
                async with sem:
                    try:
                        await _one_post(
                            client,
                            a_url=args.a_url,
                            c_url=args.c_url,
                            segments=segments,
                            hidden_bytes=hidden_bytes,
                        )
                        sent_ok += 1
                    except Exception:
                        sent_err += 1

        tasks = [asyncio.create_task(worker_loop()) for _ in range(conc)]
        await asyncio.gather(*tasks)

        out = await _drain_until_stable(
            client,
            c_stats_url=c_stats,
            target_unique=sent_ok,
            drain_timeout_s=max(0.1, float(args.drain_timeout_s)),
            idle_s=max(0.1, float(args.idle_s)),
            poll_s=max(0.1, float(args.poll_s)),
        )

    unique = int(out.get("unique_msg_ids") or 0)
    expected = sent_ok
    loss = max(0, expected - unique)
    loss_rate = (loss / expected) if expected else 0.0

    elapsed = max(0.001, time.time() - start)
    qps = sent_ok / elapsed

    print("=== bench_overlay_embed_hls ===")
    print(f"A: {args.a_url}  C: {args.c_url}")
    print(f"segments={len(segments)} segments_bytes={seg_total_bytes} hidden_bytes={hidden_bytes}")
    print(f"concurrency={conc} duration_s={dur_s:.3f} elapsed_s={elapsed:.3f}")
    print(f"sent_ok={sent_ok} sent_err={sent_err} post_qps={qps:.3f}")
    print("--- C stats (/stats) ---")
    print(f"recv_count={out.get('recv_count')} unique_msg_ids={out.get('unique_msg_ids')} dup_count={out.get('dup_count')}")
    e2e = out.get("e2e_ms") or {}
    print(f"e2e_ms_p50={e2e.get('p50')} p95={e2e.get('p95')} p99={e2e.get('p99')}")
    thr = out.get("throughput_bytes_per_s") or {}
    print(f"throughput_avg_Bps={thr.get('avg')} window_Bps={thr.get('window')} window_s={thr.get('window_s')}")
    print("--- final loss (drained) ---")
    print(f"expected_unique={expected} got_unique={unique} loss={loss} loss_rate={loss_rate:.4f}")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())

