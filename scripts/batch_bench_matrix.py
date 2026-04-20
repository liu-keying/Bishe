from __future__ import annotations

import argparse
import csv
import shlex
import subprocess
import sys
import time
from pathlib import Path


def _parse_bench_output(text: str) -> dict:
    """
    Parse stdout of bench_overlay_embed_hls.py / bench_overlay_embed.py.
    We rely on stable "key=value" tokens in their output lines.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # normalize: split by spaces, capture k=v
        for tok in line.replace(",", " ").split():
            if "=" in tok and tok.count("=") == 1:
                k, v = tok.split("=", 1)
                if k and v:
                    out[k.strip()] = v.strip()
    return out


def _run_one(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")


def main() -> int:
    p = argparse.ArgumentParser(description="批量跑 bench 组合并输出 CSV（用于画曲线）")
    p.add_argument(
        "--mode",
        choices=["embed-hls", "embed"],
        default="embed-hls",
        help="embed-hls=调用 bench_overlay_embed_hls.py；embed=调用 bench_overlay_embed.py",
    )
    p.add_argument("--python", default=sys.executable, help="python 可执行文件路径")
    p.add_argument("--out", default="bench_results.csv", help="输出 CSV 文件名（在 scripts/ 下）")

    # common
    p.add_argument("--a-url", default="http://127.0.0.1:8000")
    p.add_argument("--c-url", default="http://127.0.0.1:8002")
    p.add_argument("--duration-s", type=float, default=5.0)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--drain-timeout-s", type=float, default=900.0)
    p.add_argument("--idle-s", type=float, default=60.0)

    # matrix axes
    p.add_argument("--segments", default=r".\hls_seg*.ts", help="仅 embed-hls：glob")
    p.add_argument("--segments-limits", default="3,9,18", help="仅 embed-hls：逗号分隔，如 3,9,18")
    p.add_argument("--hidden-bytes-list", default="1024,4096,65536", help="逗号分隔，如 1024,4096,65536")
    p.add_argument("--repeats", type=int, default=3, help="每个组合重复次数")

    # embed-only
    p.add_argument("--cover-bytes", type=int, default=1024 * 1024, help="仅 embed：随机载体大小")

    args = p.parse_args()

    scripts_dir = Path(__file__).resolve().parent
    if args.mode == "embed-hls":
        bench = scripts_dir / "bench_overlay_embed_hls.py"
        if not bench.is_file():
            raise SystemExit(f"missing: {bench}")
        seg_limits = [int(x) for x in str(args.segments_limits).split(",") if str(x).strip()]
    else:
        bench = scripts_dir / "bench_overlay_embed.py"
        if not bench.is_file():
            raise SystemExit(f"missing: {bench}")
        seg_limits = []

    hidden_list = [int(x) for x in str(args.hidden_bytes_list).split(",") if str(x).strip()]
    repeats = max(1, int(args.repeats))

    out_path = scripts_dir / str(args.out)

    rows: list[dict] = []
    t_batch0 = time.time()

    def add_row(base: dict, parsed: dict, rc: int, elapsed_s: float, raw_preview: str) -> None:
        row = {**base}
        row["rc"] = rc
        row["runner_elapsed_s"] = f"{elapsed_s:.3f}"
        # pick key metrics if present
        for k in [
            "segments",
            "segments_bytes",
            "hidden_bytes",
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
        ]:
            if k in parsed:
                row[k] = parsed[k]
        row["raw_preview"] = raw_preview
        rows.append(row)

    # run matrix
    for hidden_bytes in hidden_list:
        if args.mode == "embed-hls":
            for seg_limit in seg_limits:
                for r in range(repeats):
                    base = {
                        "mode": args.mode,
                        "segments_glob": args.segments,
                        "segments_limit": seg_limit,
                        "hidden_bytes": hidden_bytes,
                        "repeat": r + 1,
                    }
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
                        "--concurrency",
                        str(args.concurrency),
                        "--duration-s",
                        str(args.duration_s),
                        "--hidden-bytes",
                        str(hidden_bytes),
                        "--drain-timeout-s",
                        str(args.drain_timeout_s),
                        "--idle-s",
                        str(args.idle_s),
                    ]
                    t0 = time.time()
                    rc, out = _run_one(cmd)
                    dt = time.time() - t0
                    parsed = _parse_bench_output(out)
                    add_row(base, parsed, rc, dt, out[-400:].replace("\n", "\\n"))
                    if rc != 0:
                        print("FAILED:", shlex.join(cmd))
                        print(out[-2000:])
                        break
        else:
            for r in range(repeats):
                base = {
                    "mode": args.mode,
                    "cover_bytes": args.cover_bytes,
                    "hidden_bytes": hidden_bytes,
                    "repeat": r + 1,
                }
                cmd = [
                    str(args.python),
                    str(bench),
                    "--a-url",
                    str(args.a_url),
                    "--c-url",
                    str(args.c_url),
                    "--concurrency",
                    str(args.concurrency),
                    "--duration-s",
                    str(args.duration_s),
                    "--hidden-bytes",
                    str(hidden_bytes),
                    "--cover-bytes",
                    str(args.cover_bytes),
                    "--drain-timeout-s",
                    str(args.drain_timeout_s),
                    "--idle-s",
                    str(args.idle_s),
                ]
                t0 = time.time()
                rc, out = _run_one(cmd)
                dt = time.time() - t0
                parsed = _parse_bench_output(out)
                add_row(base, parsed, rc, dt, out[-400:].replace("\n", "\\n"))
                if rc != 0:
                    print("FAILED:", shlex.join(cmd))
                    print(out[-2000:])
                    break

    # write csv
    cols: list[str] = []
    for row in rows:
        for k in row.keys():
            if k not in cols:
                cols.append(k)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    print(f"wrote {len(rows)} rows -> {out_path}")
    print(f"batch_elapsed_s={time.time() - t_batch0:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

