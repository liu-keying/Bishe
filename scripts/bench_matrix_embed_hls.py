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
    """
    Parse stdout of bench_overlay_embed_hls*.py.
    We rely on stable "key=value" tokens in their output lines.
    """
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


def _split_int_list(s: str) -> list[int]:
    return [int(x) for x in str(s).split(",") if str(x).strip()]


def _segment_count(*, segments_glob: str, limit: int) -> int:
    """与 bench_overlay_embed_hls_scheme_b._load_segments 一致：排序 glob，再按 limit 截断。"""
    paths = sorted(glob.glob(segments_glob))
    if not paths:
        return 0
    if limit > 0:
        paths = paths[:limit]
    return len(paths)


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "矩阵压测：统一调用 bench_overlay_embed_hls_scheme_b.py（k>=1）。\n"
            "输出 CSV，字段来自 bench 脚本 stdout 的 key=value 解析。"
        )
    )
    p.add_argument("--python", default=sys.executable, help="python 可执行文件路径")
    p.add_argument("--out", default="bench_matrix_embed_hls.csv", help="输出 CSV 文件名（默认写在 scripts/ 下）")

    p.add_argument("--a-url", default="http://127.0.0.1:8000")
    p.add_argument("--c-url", default="http://127.0.0.1:8002")
    p.add_argument("--segments", required=True, help=r'glob，如 "D:\data\hls\*.ts"（按文件名排序）')

    p.add_argument(
        "--k-list",
        default="1,2,3,5",
        help="逗号分隔，如 1,2,3,5（每个 k 必须 <= 当次 --segments-limit 下的分片数 n；不满足的组合会自动跳过）",
    )
    p.add_argument("--hidden-bytes-list", default="1024,4096,65536", help="逗号分隔，如 1024,4096,65536")
    p.add_argument("--segments-limits", default="3,9,18", help="逗号分隔，如 3,9,18（传给 --segments-limit；0 表示不限制）")
    p.add_argument(
        "--pad-bytes-list",
        default="0",
        help=(
            "逗号分隔，如 0 或 0,188。说明：当前 A 的 /overlay/embed-hls 实现里 pad_bytes 不参与嵌入，仅回 JSON；"
            "默认只扫 0，避免无意义加倍矩阵规模。"
        ),
    )
    p.add_argument("--g-bytes-list", default="0", help="逗号分隔，如 0,512（0 表示自动推导）")

    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--duration-s", type=float, default=10.0)
    p.add_argument("--timeout-s", type=float, default=120.0)
    p.add_argument("--drain-timeout-s", type=float, default=300.0)
    p.add_argument("--idle-s", type=float, default=10.0)
    p.add_argument("--poll-s", type=float, default=1.0)

    p.add_argument("--extract", default="", help="可选 append_marker（传给 bench_overlay_embed_hls_scheme_b.py）")
    p.add_argument("--repeats", type=int, default=3, help="每个组合重复次数（用于均值/方差；当前脚本先原样记录多次）")

    args = p.parse_args()

    scripts_dir = Path(__file__).resolve().parent
    bench_kb = scripts_dir / "bench_overlay_embed_hls_scheme_b.py"
    if not bench_kb.is_file():
        raise SystemExit(f"missing: {bench_kb}")

    ks = _split_int_list(args.k_list)
    hiddens = _split_int_list(args.hidden_bytes_list)
    seg_limits = _split_int_list(args.segments_limits)
    pads = _split_int_list(args.pad_bytes_list)
    gbytes = _split_int_list(args.g_bytes_list)
    repeats = max(1, int(args.repeats))

    seg_counts: dict[int, int] = {sl: _segment_count(segments_glob=args.segments, limit=sl) for sl in seg_limits}
    if all(seg_counts[sl] <= 0 for sl in seg_limits):
        raise SystemExit(f"no segments matched glob: {args.segments!r}")

    out_path = scripts_dir / str(args.out)
    rows: list[dict[str, str]] = []

    def add_row(base: dict[str, str], parsed: dict[str, str], rc: int, elapsed_s: float, raw: str) -> None:
        row: dict[str, str] = {**base}
        row["rc"] = str(rc)
        row["runner_elapsed_s"] = f"{elapsed_s:.3f}"
        for k in [
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
        ]:
            if k in parsed:
                row[k] = parsed[k]
        row["raw_preview"] = raw[-800:].replace("\n", "\\n")
        rows.append(row)

    t_batch0 = time.time()
    total_cells = len(hiddens) * len(seg_limits) * len(pads) * len(gbytes) * len(ks) * repeats
    cur_cell = 0

    for hidden_bytes in hiddens:
        for seg_limit in seg_limits:
            for pad_bytes in pads:
                for gb in gbytes:
                    for k in ks:
                        for r in range(repeats):
                            cur_cell += 1
                            base = {
                                "matrix": "embed_hls",
                                "a_url": args.a_url,
                                "c_url": args.c_url,
                                "segments_glob": args.segments,
                                "segments_number": str(seg_limit),
                                "segments_limit": str(seg_limit),
                                "hidden_bytes": str(hidden_bytes),
                                "k": str(k),
                                "pad_bytes": str(pad_bytes),
                                "g_bytes": str(gb),
                                "extract": str(args.extract or ""),
                                "repeat": str(r + 1),
                            }

                            n_seg = seg_counts[seg_limit]
                            if k > n_seg:
                                print(
                                    f"[matrix {cur_cell}/{total_cells}] SKIP hidden={hidden_bytes} "
                                    f"seg_limit={seg_limit} pad={pad_bytes} g_bytes={gb} k={k} repeat={r + 1}/{repeats} "
                                    f"(k>{n_seg} n_segments)",
                                    flush=True,
                                )
                                add_row(
                                    {
                                        **base,
                                        "bench": "bench_overlay_embed_hls_scheme_b.py",
                                        "note": f"skipped_k_gt_n (k={k} n={n_seg})",
                                    },
                                    {},
                                    -1,
                                    0.0,
                                    "",
                                )
                                continue

                            elapsed_batch = time.time() - t_batch0
                            print(
                                f"[matrix {cur_cell}/{total_cells}] RUN hidden={hidden_bytes} seg_limit={seg_limit} "
                                f"pad={pad_bytes} g_bytes={gb} k={k} repeat={r + 1}/{repeats} "
                                f"(batch_elapsed_s={elapsed_batch:.1f})",
                                flush=True,
                            )

                            cmd = [
                                str(args.python),
                                str(bench_kb),
                                "--segments",
                                str(args.segments),
                                "--segments-limit",
                                str(seg_limit),
                                "--a-url",
                                str(args.a_url),
                                "--c-url",
                                str(args.c_url),
                                "--concurrency",
                                str(int(args.concurrency)),
                                "--duration-s",
                                str(float(args.duration_s)),
                                "--hidden-bytes",
                                str(int(hidden_bytes)),
                                "--k",
                                str(int(k)),
                                "--pad-bytes",
                                str(int(pad_bytes)),
                                "--timeout-s",
                                str(float(args.timeout_s)),
                                "--drain-timeout-s",
                                str(float(args.drain_timeout_s)),
                                "--idle-s",
                                str(float(args.idle_s)),
                                "--poll-s",
                                str(float(args.poll_s)),
                            ]
                            if int(gb) > 0:
                                cmd += ["--g-bytes", str(int(gb))]
                            if str(args.extract or "").strip():
                                cmd += ["--extract", str(args.extract).strip()]

                            t0 = time.time()
                            rc, out = _run_one(cmd)
                            dt = time.time() - t0
                            print(
                                f"[matrix {cur_cell}/{total_cells}] DONE rc={rc} runner_s={dt:.1f} "
                                f"hidden={hidden_bytes} seg_limit={seg_limit} k={k} repeat={r + 1}/{repeats}",
                                flush=True,
                            )
                            parsed = _parse_bench_output(out)
                            add_row(
                                {
                                    **base,
                                    "bench": "bench_overlay_embed_hls_scheme_b.py",
                                },
                                parsed,
                                rc,
                                dt,
                                out,
                            )
                            if rc != 0:
                                print("FAILED:", shlex.join(cmd))
                                print(out[-2000:])

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
