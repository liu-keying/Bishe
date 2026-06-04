"""
对比实验：Zhang 等 [7] 风格 HTTP 头字段信道（User-Agent + Cookie）端到端耗时与吞吐。

前提：已启动 server.py（写 JSONL）。

运行（仓库根目录）：
  python comparison/zhang_http_header_cc/bench_zhang_header_cc.py ^
    --url http://127.0.0.1:8011/ --log scripts/zhang_cc.jsonl --hidden-bytes 4096 --repeats 5
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from comparison.zhang_http_header_cc.psk_util import aead_bytes, load_psk_hex
from comparison.zhang_http_header_cc.recv_from_log import iter_jsonl, recover_plaintext_from_records
from comparison.zhang_http_header_cc.sender import send_payload


def _percentile(sorted_x: list[float], q: float) -> float:
    """Linear interpolation on sorted samples, q in [0, 1]."""
    if not sorted_x:
        return 0.0
    n = len(sorted_x)
    if n == 1:
        return float(sorted_x[0])
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    w = pos - lo
    return float(sorted_x[lo]) * (1.0 - w) + float(sorted_x[hi]) * w


async def _one_round(
    *,
    base_url: str,
    log_path: Path,
    payload: bytes,
    psk: bytes,
    aad: bytes,
    cookie_chars: int,
    ua_chars: int,
    timeout_s: float,
) -> tuple[float, int, int, bool]:
    """
    Returns (e2e_elapsed_s, requests, plaintext_bytes, ok_match).

    e2e_elapsed_s: 从本轮开始到「日志可见 + 解密并校验明文」结束（与 HLS bench 的端到端语义对齐，便于制表对比）。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.is_file():
        t0_size = log_path.stat().st_size
    else:
        t0_size = 0

    t0 = time.perf_counter()
    msg_id, n = await send_payload(
        base_url=base_url,
        payload=payload,
        psk=psk,
        aad=aad,
        cookie_chars=cookie_chars,
        ua_chars=ua_chars,
        ua_prefix="",
        verify_tls=True,
        timeout_s=timeout_s,
    )

    # Wait until appended bytes visible (Windows / shared FS friendly)
    deadline = time.perf_counter() + float(timeout_s)
    while time.perf_counter() < deadline:
        if log_path.is_file() and log_path.stat().st_size > t0_size:
            break
        await asyncio.sleep(0.05)

    rows = iter_jsonl(log_path)
    ok = False
    try:
        got = recover_plaintext_from_records(rows, msg_id=msg_id, psk=psk, aad=aad)
        ok = got == payload
    except Exception:
        ok = False
    elapsed = time.perf_counter() - t0
    return elapsed, n, len(payload), ok


async def _amain() -> None:
    p = argparse.ArgumentParser(description="Bench Zhang-style HTTP header CC (send + parse log recover).")
    p.add_argument("--url", default="http://127.0.0.1:8011/")
    p.add_argument("--log", default="scripts/zhang_cc.jsonl", help="Must match server.py --log")
    p.add_argument("--hidden-bytes", type=int, default=4096)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--cookie-chars", type=int, default=900)
    p.add_argument("--ua-chars", type=int, default=500)
    p.add_argument("--timeout-s", type=float, default=60.0)
    p.add_argument("--psk-hex", default="", help="32-byte PSK as 64 hex chars (or set BISHE_PSK_HEX / --psk-file)")
    p.add_argument("--psk-file", default="", help="File containing 64 hex chars of PSK")
    p.add_argument(
        "--aad",
        default="zhang-http-header-cc/v1",
        help="AEAD associated data (UTF-8); must match sender --aad",
    )
    args = p.parse_args()

    log_path = Path(args.log)
    if not log_path.is_absolute():
        log_path = (_REPO_ROOT / log_path).resolve()

    psk = load_psk_hex(psk_hex=str(args.psk_hex), psk_file=str(args.psk_file))
    aad = aead_bytes(aad_text=str(args.aad))
    n_bytes = max(0, int(args.hidden_bytes))
    repeats = max(1, int(args.repeats))

    lat: list[float] = []
    reqs: list[int] = []
    oks = 0
    ok_flags: list[bool] = []
    for i in range(repeats):
        payload = os.urandom(n_bytes) if n_bytes > 0 else b""
        elapsed, n_req, nbytes, ok = await _one_round(
            base_url=str(args.url),
            log_path=log_path,
            payload=payload,
            psk=psk,
            aad=aad,
            cookie_chars=int(args.cookie_chars),
            ua_chars=int(args.ua_chars),
            timeout_s=float(args.timeout_s),
        )
        lat.append(elapsed)
        reqs.append(n_req)
        ok_flags.append(ok)
        oks += 1 if ok else 0
        thr = (nbytes / elapsed) if elapsed > 0 else 0.0
        e2e_ms = elapsed * 1000.0
        loss_i = 0 if ok else 1
        print(
            "repeat=",
            i + 1,
            " ok=",
            ok,
            " loss=",
            loss_i,
            " e2e_ms=",
            f"{e2e_ms:.3f}",
            " elapsed_s=",
            f"{elapsed:.4f}",
            " requests=",
            n_req,
            " hidden_bytes=",
            nbytes,
            " throughput_avg_Bps=",
            f"{thr:.3f}",
            sep="",
        )

    mean = sum(lat) / len(lat)
    lat_sorted = sorted(lat)
    p50_s = _percentile(lat_sorted, 0.50)
    p95_s = _percentile(lat_sorted, 0.95)
    p99_s = _percentile(lat_sorted, 0.99)
    loss = repeats - oks
    loss_rate = (loss / repeats) if repeats else 0.0
    succ_time = sum(lat[i] for i in range(repeats) if ok_flags[i])
    succ_bytes = n_bytes * oks
    thr_goodput = (succ_bytes / succ_time) if succ_time > 0 else 0.0
    thr_attempt = (n_bytes * repeats / sum(lat)) if lat and sum(lat) > 0 else 0.0

    # 与 scripts/bench_overlay_embed_hls_scheme_b.py 终端输出字段对齐，便于并列制表
    print(
        "--- Zhang HTTP-header baseline (align keys with HLS bench) ---",
        f"hidden_bytes={n_bytes} repeats={repeats}",
        sep="\n",
    )
    print(
        f"expected_unique={repeats} got_unique={oks} loss={loss} loss_rate={loss_rate:.4f}",
    )
    print(
        f"e2e_ms_p50={p50_s * 1000.0:.3f} e2e_ms_p95={p95_s * 1000.0:.3f} e2e_ms_p99={p99_s * 1000.0:.3f} "
        f"elapsed_s_mean={mean:.4f}",
    )
    print(
        f"throughput_avg_Bps={thr_goodput:.3f} throughput_attempt_Bps={thr_attempt:.3f} "
        f"requests_mean={sum(reqs)/len(reqs):.3f}"
    )
    print(
        "summary ",
        "hidden_bytes=",
        n_bytes,
        " repeats=",
        repeats,
        " ok_rate=",
        f"{oks/repeats:.3f}",
        " loss_rate=",
        f"{loss_rate:.4f}",
        " requests_mean=",
        f"{sum(reqs)/len(reqs):.3f}",
        " elapsed_s_mean=",
        f"{mean:.4f}",
        " elapsed_s_p50=",
        f"{p50_s:.4f}",
        sep="",
    )


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
