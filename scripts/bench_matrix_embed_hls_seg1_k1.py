"""
HLS 消融压测：固定 segments_limit=1、k=1，三档 hidden_bytes。

  python scripts/bench_matrix_embed_hls_seg1_k1.py --segments "D:\\PyProjects\\Bishe\\*.ts"

默认输出：scripts/bench_matrix_embed_hls_seg1_k1_h1024_4096_65536.csv

前提：A / B-pull / B-worker / C / Redis 已按 README 启动。
"""

from __future__ import annotations

import argparse
import csv
import glob
import shlex
import subprocess
import sys
import time
from pathlib import Path


def _parse_bench_output(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for tok in line.replace(",", " ").split():
            if "=" in tok and tok.count("=") == 1:
                k, v = tok.split("=", 1)
                if k and v:
                    out[k.strip()] = v.strip()
    return out


def _run_one(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")


def _segment_count(*, segments_glob: str, limit: int) -> int:
    paths = sorted(glob.glob(segments_glob))
    if not paths:
        return 0
    if limit > 0:
        paths = paths[:limit]
    return len(paths)


def main() -> int:
    p = argparse.ArgumentParser(
        description="HLS embed 压测：segments_limit=1, k=1, hidden_bytes=1024/4096/65536"
    )
    p.add_argument("--python", default=sys.executable)
    p.add_argument(
        "--out",
        default="bench_matrix_embed_hls_seg1_k1_h1024_4096_65536.csv",
        help="输出 CSV（默认写在 scripts/ 下）",
    )
    p.add_argument("--segments", default=r".\*.ts", help=r'TS glob，如 "D:\PyProjects\Bishe\*.ts"')
    p.add_argument("--segments-limit", type=int, default=1, help="固定为 1（仅取排序后第一个分片）")
    p.add_argument("--k", type=int, default=1, help="密文分片数，固定为 1")
    p.add_argument("--hidden-bytes-list", default="1024,4096,65536")
    p.add_argument("--repeats", type=int, default=3)

    p.add_argument("--a-url", default="http://127.0.0.1:8000")
    p.add_argument("--c-url", default="http://127.0.0.1:8002")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--duration-s", type=float, default=10.0)
    p.add_argument("--timeout-s", type=float, default=120.0)
    p.add_argument("--drain-timeout-s", type=float, default=900.0)
    p.add_argument("--idle-s", type=float, default=60.0)
    p.add_argument("--poll-s", type=float, default=1.0)
    p.add_argument("--pad-bytes", type=int, default=0)
    p.add_argument("--g-bytes", type=int, default=0)
    p.add_argument("--extract", default="")
    args = p.parse_args()

    seg_limit = max(1, int(args.segments_limit))
    k = int(args.k)
    if k < 1:
        raise SystemExit("--k 必须 >= 1")
    if k > seg_limit:
        raise SystemExit(f"--k 不能大于 --segments-limit（k={k}, segments_limit={seg_limit}）")

    n_seg = _segment_count(segments_glob=args.segments, limit=seg_limit)
    if n_seg < 1:
        raise SystemExit(f"no segments matched: {args.segments!r}")

    hidden_list = [int(x) for x in str(args.hidden_bytes_list).split(",") if str(x).strip()]
    repeats = max(1, int(args.repeats))

    scripts_dir = Path(__file__).resolve().parent
    bench = scripts_dir / "bench_overlay_embed_hls_scheme_b.py"
    if not bench.is_file():
        raise SystemExit(f"missing: {bench}")

    out_path = scripts_dir / str(args.out)
    rows: list[dict[str, str]] = []
    t_batch0 = time.time()

    metric_keys = [
        "segments",
        "segments_bytes",
        "hidden_bytes",
        "k",
        "g_bytes",
        "g_bits",
        "pad_bytes",
        "extract",
        "concurrency",
        "duration_s",
        "elapsed_s",
        "sent_ok",
        "sent_err",
        "post_qps",
        "recv_count",
        "unique_msg_ids",
        "dup_count",
        "e2e_ms_p50",
        "p95",
        "p99",
        "throughput_avg_Bps",
        "window_Bps",
        "window_s",
        "expected_unique",
        "got_unique",
        "loss",
        "loss_rate",
    ]

    total = len(hidden_list) * repeats
    cur = 0

    print(
        f"config: segments_limit={seg_limit} k={k} n_segments={n_seg} "
        f"hidden_bytes={hidden_list} repeats={repeats}"
    )

    for hidden_bytes in hidden_list:
        for r in range(repeats):
            cur += 1
            base = {
                "matrix": "embed_hls",
                "a_url": args.a_url,
                "c_url": args.c_url,
                "segments_glob": args.segments,
                "segments_limit": str(seg_limit),
                "hidden_bytes": str(hidden_bytes),
                "k": str(k),
                "pad_bytes": str(int(args.pad_bytes)),
                "g_bytes": str(int(args.g_bytes)),
                "extract": str(args.extract or ""),
                "repeat": str(r + 1),
                "bench": "bench_overlay_embed_hls_scheme_b.py",
            }
            print(
                f"[{cur}/{total}] RUN hidden_bytes={hidden_bytes} repeat={r + 1}/{repeats} "
                f"seg_limit={seg_limit} k={k}",
                flush=True,
            )

            cmd = [
                str(args.python),
                str(bench),
                "--segments",
                str(args.segments),
                "--segments-limit",
                str(seg_limit),
                "--a-url",
                str(args.a_url),
                "--c-url",
                str(args.c_url),
                "--hidden-bytes",
                str(hidden_bytes),
                "--k",
                str(k),
                "--concurrency",
                str(int(args.concurrency)),
                "--duration-s",
                str(float(args.duration_s)),
                "--timeout-s",
                str(float(args.timeout_s)),
                "--drain-timeout-s",
                str(float(args.drain_timeout_s)),
                "--idle-s",
                str(float(args.idle_s)),
                "--poll-s",
                str(float(args.poll_s)),
                "--pad-bytes",
                str(int(args.pad_bytes)),
            ]
            if int(args.g_bytes) > 0:
                cmd += ["--g-bytes", str(int(args.g_bytes))]
            if str(args.extract or "").strip():
                cmd += ["--extract", str(args.extract).strip()]

            t0 = time.time()
            rc, out = _run_one(cmd)
            dt = time.time() - t0
            parsed = _parse_bench_output(out)

            row = {**base, "rc": str(rc), "runner_elapsed_s": f"{dt:.3f}"}
            for mk in metric_keys:
                if mk in parsed:
                    row[mk] = parsed[mk]
            row["raw_preview"] = out[-800:].replace("\n", "\\n")
            rows.append(row)

            print(f"[{cur}/{total}] DONE rc={rc} runner_s={dt:.1f}", flush=True)
            if rc != 0:
                print("FAILED:", shlex.join(cmd))
                print(out[-2000:])

    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    print(f"wrote {len(rows)} rows -> {out_path}")
    print(f"batch_elapsed_s={time.time() - t_batch0:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
