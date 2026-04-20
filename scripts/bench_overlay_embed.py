from __future__ import annotations

import argparse
import asyncio
import os
import time
import uuid

import httpx


async def _drain_until_stable(
    client: httpx.AsyncClient,
    *,
    c_stats_url: str,
    target_unique: int,
    drain_timeout_s: float,
    idle_s: float,
    poll_s: float,
) -> dict:
    """
    轮询 C /stats，直到：
    - unique_msg_ids >= target_unique（全部到达），或
    - 超过 drain_timeout_s，或
    - 在 idle_s 时间内 unique_msg_ids 无增长（认为队列已基本清空/不再增长）
    """
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
    cover_bytes: bytes,
    hidden_bytes: int,
) -> None:
    msg_id = uuid.uuid4().bytes  # 16B -> C 用于 unique/dup 统计
    if hidden_bytes < 16:
        hidden = msg_id[:hidden_bytes]
    else:
        hidden = msg_id + os.urandom(hidden_bytes - 16)

    files = {
        "video": ("cover.mp4", cover_bytes, "video/mp4"),
        "hidden": ("hidden.bin", hidden, "application/octet-stream"),
    }
    url = f"{a_url.rstrip('/')}/overlay/embed"
    params = {"c": c_url}
    r = await client.post(url, params=params, files=files)
    r.raise_for_status()


async def main_async() -> int:
    p = argparse.ArgumentParser(description="压测 /overlay/embed（端到端统计在 C /stats）")
    p.add_argument("--a-url", default="http://127.0.0.1:8000")
    p.add_argument("--c-url", default="http://127.0.0.1:8002")
    p.add_argument(
        "--cover",
        default="",
        help="可选，video 载体文件路径；若为空则用随机字节生成载体（见 --cover-bytes）",
    )
    p.add_argument("--cover-bytes", type=int, default=1024 * 1024, help="未提供 --cover 时生成的载体大小")
    p.add_argument("--hidden-bytes", type=int, default=4096, help="hidden 载荷大小（>=16 才有 msg_id）")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--duration-s", type=float, default=10.0)
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--drain-timeout-s", type=float, default=60.0, help="发完后等待链路清空的最长时间")
    p.add_argument("--idle-s", type=float, default=5.0, help="若该时间内 unique 无增长，认为已稳定")
    p.add_argument("--poll-s", type=float, default=0.5, help="轮询 C /stats 间隔")
    args = p.parse_args()

    conc = max(1, int(args.concurrency))
    dur_s = max(0.1, float(args.duration_s))
    hidden_bytes = max(1, int(args.hidden_bytes))

    cover_arg = str(args.cover or "").strip()
    if cover_arg:
        cover_path = os.path.abspath(cover_arg)
        with open(cover_path, "rb") as f:
            cover_bytes = f.read()
    else:
        cover_bytes = os.urandom(max(1, int(args.cover_bytes)))

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
                            cover_bytes=cover_bytes,
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

    print("=== bench_overlay_embed ===")
    print(f"A: {args.a_url}  C: {args.c_url}")
    print(f"cover_bytes={len(cover_bytes)} hidden_bytes={hidden_bytes}")
    print(f"concurrency={conc} duration_s={dur_s:.3f} elapsed_s={elapsed:.3f}")
    print(f"sent_ok={sent_ok} sent_err={sent_err} post_qps={qps:.2f}")
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

