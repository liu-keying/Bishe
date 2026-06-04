from __future__ import annotations

import argparse
import asyncio
import glob
import os
import time
import uuid

import httpx

_REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
import sys

if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

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
        # 尚无到达时不要用 idle 提前结束（大包/HLS 常需数十秒）
        if unique > 0 and now - last_change >= idle_s:
            return out
        await asyncio.sleep(poll_s)


async def _one_post_scheme_b(
    client: httpx.AsyncClient,
    *,
    a_url: str,
    c_url: str,
    segments: list[bytes],
    hidden_bytes: int,
    k: int,
    g_bytes: int,
    g_bits: int,
    pad_bytes: int,
    extract: str,
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
    params: dict[str, str] = {"k": str(int(k))}
    if (c_url or "").strip():
        params["c"] = c_url.strip()
    if int(pad_bytes or 0) > 0:
        params["pad_bytes"] = str(int(pad_bytes))
    if int(g_bytes or 0) > 0:
        params["g_bytes"] = str(int(g_bytes))
    elif int(g_bits or 0) > 0:
        params["g_bits"] = str(int(g_bits))
    if (extract or "").strip():
        params["extract"] = extract.strip()

    r = await client.post(url, params=params, files=files)
    r.raise_for_status()


async def main_async() -> int:
    p = argparse.ArgumentParser(
        description=(
            "压测 /overlay/embed-hls：整包 AEAD 后密文拆 k 份随机散入 k 个 TS 分片；"
            "端到端统计在 C /stats（unique_msg_ids 等）。"
        )
    )
    p.add_argument("--a-url", default="http://127.0.0.1:8000")
    p.add_argument("--c-url", default="http://127.0.0.1:8002")
    p.add_argument("--segments", required=True, help=r'glob，如 ".\\hls_seg*.ts"（按文件名排序）')
    p.add_argument("--segments-limit", type=int, default=0, help="可选，只取前 N 个分片（0=不限制）")
    p.add_argument("--hidden-bytes", type=int, default=4096)
    p.add_argument("--k", type=int, required=True, help="密文分片数（k>=1；k=1 表示整包密文随机落入 1 个分片）")
    p.add_argument("--g-bytes", type=int, default=0, help="每份密文分片大小（bytes）。0=A 自动推导")
    p.add_argument("--g-bits", type=int, default=0, help="每份密文分片大小（bits）。优先级低于 --g-bytes")
    p.add_argument("--pad-bytes", type=int, default=0, help="可选：pad_bytes（未携带密文的 TS 尾部追加伪 TS 字节）")
    p.add_argument("--extract", default="", help="可选：append_marker（默认走 psk_hmac_inplace）")

    p.add_argument(
        "--send-count",
        type=int,
        default=0,
        help=">0 时精确发送 N 条消息后等待 C 统计（忽略 --duration-s，用于单条端到端时延）",
    )
    p.add_argument("--concurrency", type=int, default=1, help="并发（方案 B 较重，建议 1~3）")
    p.add_argument("--duration-s", type=float, default=10.0)
    p.add_argument("--timeout-s", type=float, default=120.0)
    p.add_argument("--drain-timeout-s", type=float, default=300.0, help="发完后等待链路清空的最长时间")
    p.add_argument("--idle-s", type=float, default=10.0, help="若该时间内 unique 无增长，认为已稳定")
    p.add_argument("--poll-s", type=float, default=1.0, help="轮询 C /stats 间隔")
    args = p.parse_args()

    conc = max(1, int(args.concurrency))
    send_count = max(0, int(args.send_count or 0))
    dur_s = max(0.1, float(args.duration_s))
    hidden_bytes = max(1, int(args.hidden_bytes))
    k = int(args.k)
    if k < 1:
        raise SystemExit("--k 必须 >= 1")

    segments = _load_segments(segments_glob=args.segments, limit=int(args.segments_limit))
    if k > len(segments):
        raise SystemExit(f"--k 不能大于分片数（k={k}, segments={len(segments)}）")

    seg_total_bytes = sum(len(s) for s in segments)

    c_reset = f"{args.c_url.rstrip('/')}/stats/reset"
    c_stats = f"{args.c_url.rstrip('/')}/stats"

    sent_ok = 0
    sent_err = 0
    start = time.time()
    stop_at = start + dur_s

    limits = httpx.Limits(max_connections=conc * 2, max_keepalive_connections=conc * 2)
    client_kw = {
        "timeout": args.timeout_s,
        "limits": limits,
        "verify": False,  # C 可为自签 HTTPS；与 b-worker 一致
        "trust_env": False,
    }
    async with httpx.AsyncClient(**client_kw) as client:
        rr = await client.post(c_reset)
        rr.raise_for_status()
        chk = await client.get(c_stats)
        chk.raise_for_status()
        st = chk.json()
        if int(st.get("recv_count") or 0) != 0 or int(st.get("unique_msg_ids") or 0) != 0:
            rr2 = await client.post(c_reset)
            rr2.raise_for_status()

        if send_count > 0:
            for _ in range(send_count):
                try:
                    await _one_post_scheme_b(
                        client,
                        a_url=args.a_url,
                        c_url=args.c_url,
                        segments=segments,
                        hidden_bytes=hidden_bytes,
                        k=k,
                        g_bytes=int(args.g_bytes or 0),
                        g_bits=int(args.g_bits or 0),
                        pad_bytes=int(args.pad_bytes or 0),
                        extract=str(args.extract or ""),
                    )
                    sent_ok += 1
                except Exception:
                    sent_err += 1
        else:
            sem = asyncio.Semaphore(conc)

            async def worker_loop() -> None:
                nonlocal sent_ok, sent_err
                while time.time() < stop_at:
                    async with sem:
                        try:
                            await _one_post_scheme_b(
                                client,
                                a_url=args.a_url,
                                c_url=args.c_url,
                                segments=segments,
                                hidden_bytes=hidden_bytes,
                                k=k,
                                g_bytes=int(args.g_bytes or 0),
                                g_bits=int(args.g_bits or 0),
                                pad_bytes=int(args.pad_bytes or 0),
                                extract=str(args.extract or ""),
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

    print("=== bench_overlay_embed_hls_scheme_b ===")
    print(f"A: {args.a_url}  C: {args.c_url}")
    print(
        f"segments={len(segments)} segments_bytes={seg_total_bytes} hidden_bytes={hidden_bytes} "
        f"k={k} g_bytes={int(args.g_bytes or 0)} g_bits={int(args.g_bits or 0)} pad_bytes={int(args.pad_bytes or 0)} "
        f"extract={args.extract!r}"
    )
    if send_count > 0:
        print(f"send_count={send_count} concurrency={conc} elapsed_s={elapsed:.3f}")
    else:
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
