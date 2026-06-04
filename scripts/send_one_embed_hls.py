#!/usr/bin/env python3
"""发一条 embed-hls，轮询 C /stats 直到收到（非压测，用于联调 B-gate 链路）。"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

# 与 bench_overlay_embed_hls_scheme_b 同目录，复用单次 POST
sys.path.insert(0, os.path.dirname(__file__))
from bench_overlay_embed_hls_scheme_b import _drain_until_stable, _load_segments, _one_post_scheme_b  # noqa: E402


async def _main_async(args: argparse.Namespace) -> int:
    segments = _load_segments(segments_glob=args.segments, limit=int(args.segments_limit))
    k = int(args.k)
    if k < 1:
        raise SystemExit("--k 必须 >= 1")
    if k > len(segments):
        raise SystemExit(f"--k 不能大于分片数（k={k}, segments={len(segments)}）")

    c_reset = f"{args.c_url.rstrip('/')}/stats/reset"
    c_stats = f"{args.c_url.rstrip('/')}/stats"

    async with httpx.AsyncClient(timeout=args.timeout_s, verify=False, trust_env=False) as client:
        if args.reset_stats:
            r = await client.post(c_reset)
            r.raise_for_status()

        print(
            f"POST 1 条 -> A {args.a_url}  "
            f"segments={len(segments)} hidden_bytes={args.hidden_bytes} k={k}"
        )
        t0 = time.time()
        await _one_post_scheme_b(
            client,
            a_url=args.a_url,
            c_url=args.c_url,
            segments=segments,
            hidden_bytes=int(args.hidden_bytes),
            k=k,
            g_bytes=int(args.g_bytes or 0),
            g_bits=int(args.g_bits or 0),
            pad_bytes=int(args.pad_bytes or 0),
            extract=str(args.extract or ""),
        )
        post_s = time.time() - t0
        print(f"A 已返回（{post_s:.2f}s），等待 C 收齐…")

        out = await _drain_until_stable(
            client,
            c_stats_url=c_stats,
            target_unique=1,
            drain_timeout_s=max(1.0, float(args.drain_timeout_s)),
            idle_s=max(0.5, float(args.idle_s)),
            poll_s=max(0.2, float(args.poll_s)),
        )

    unique = int(out.get("unique_msg_ids") or 0)
    e2e = out.get("e2e_ms") or {}
    elapsed = time.time() - t0
    print("--- C /stats ---")
    print(f"unique_msg_ids={unique} recv_count={out.get('recv_count')} loss={max(0, 1 - unique)}")
    print(f"e2e_ms p50={e2e.get('p50')} p95={e2e.get('p95')} p99={e2e.get('p99')}")
    print(f"总耗时 {elapsed:.2f}s（含等待 C）")
    return 0 if unique >= 1 else 1


def main() -> int:
    p = argparse.ArgumentParser(description="发 1 条 embed-hls 并等待 C 收到（联调用，非压测）")
    p.add_argument("--a-url", default="http://127.0.0.1:8000")
    p.add_argument(
        "--c-url",
        default="http://127.0.0.1:8002",
        help="C 地址；HTTPS 时如 https://127.0.0.1:8443（须与 A --c-url 一致）",
    )
    p.add_argument("--segments", default=r".\scripts\_test_ts\hls_seg*.ts")
    p.add_argument("--segments-limit", type=int, default=6, help="0=不限制")
    p.add_argument("--hidden-bytes", type=int, default=1024)
    p.add_argument("--k", type=int, default=2)
    p.add_argument("--g-bytes", type=int, default=0)
    p.add_argument("--g-bits", type=int, default=0)
    p.add_argument("--pad-bytes", type=int, default=0)
    p.add_argument("--extract", default="")
    p.add_argument("--timeout-s", type=float, default=300.0)
    p.add_argument("--drain-timeout-s", type=float, default=120.0)
    p.add_argument("--idle-s", type=float, default=3.0)
    p.add_argument("--poll-s", type=float, default=0.5)
    p.add_argument("--no-reset-stats", dest="reset_stats", action="store_false", help="不重置 C 统计")
    p.set_defaults(reset_stats=True)
    args = p.parse_args()

    import asyncio

    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
