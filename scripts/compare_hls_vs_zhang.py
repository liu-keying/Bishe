"""
HLS 隐写方案 vs Zhang HTTP 头基线：并列对比，输出制表 CSV。

  python scripts/compare_hls_vs_zhang.py ^
    --segments "D:\\PyProjects\\Bishe\\*.ts" --segments-limit 6 --k 3 ^
    --hidden-bytes 1024,4096,65536 --hls-repeats 3 --zhang-repeats 3

依赖：comparison/zhang_http_header_cc/server.py 已启动且 --log 与 --zhang-log 一致。
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _parse_kv_output(text: str) -> dict[str, str]:
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


def _run(cmd: list[str], *, timeout_s: float | None) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=str(_REPO),
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + ("\n" + e.stderr if e.stderr else "")
        return -1, out + f"\n[timeout after {timeout_s}s]"
    return p.returncode, (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")


def _f(parsed: dict[str, str], key: str) -> float | None:
    v = parsed.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description="HLS embed vs Zhang HTTP 头基线（clearnet）")
    p.add_argument("--out", default="scripts/thesis_hls_vs_zhang_seg6_k3_compare.csv")

    p.add_argument("--a-url", default="http://127.0.0.1:8000")
    p.add_argument("--c-url", default="http://127.0.0.1:8002")
    p.add_argument("--zhang-url", default="http://127.0.0.1:8011/")
    p.add_argument("--zhang-log", default="scripts/zhang_cc.jsonl")
    p.add_argument("--psk-hex", default="")
    p.add_argument("--psk-file", default="")

    p.add_argument("--segments", default=r".\*.ts")
    p.add_argument("--segments-limit", type=int, default=6)
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--hidden-bytes", default="1024,4096,65536")
    p.add_argument("--hls-repeats", type=int, default=3)
    p.add_argument("--zhang-repeats", type=int, default=3)
    p.add_argument("--hls-duration-s", type=float, default=10.0)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--timeout-s", type=float, default=120.0)
    p.add_argument("--drain-timeout-s", type=float, default=900.0)
    p.add_argument("--idle-s", type=float, default=60.0)
    p.add_argument("--subprocess-timeout-s", type=float, default=0.0)
    p.add_argument("--skip-hls", action="store_true")
    p.add_argument("--skip-zhang", action="store_true")
    p.add_argument("--python", default=sys.executable)
    args = p.parse_args()

    hidden_list = [int(x) for x in str(args.hidden_bytes).split(",") if str(x).strip()]
    hls_bench = _REPO / "scripts" / "bench_overlay_embed_hls_scheme_b.py"
    zhang_bench = _REPO / "comparison" / "zhang_http_header_cc" / "bench_zhang_header_cc.py"
    if not args.skip_hls and not hls_bench.is_file():
        raise SystemExit(f"missing: {hls_bench}")
    if not args.skip_zhang and not zhang_bench.is_file():
        raise SystemExit(f"missing: {zhang_bench}")

    sub_timeout = float(args.subprocess_timeout_s) if float(args.subprocess_timeout_s) > 0 else None
    rows: list[dict] = []

    print(f"A={args.a_url} C={args.c_url} zhang={args.zhang_url}")

    for hidden_bytes in hidden_list:
        row: dict = {
            "hidden_bytes": hidden_bytes,
            "segments_limit": int(args.segments_limit),
            "k": int(args.k),
        }

        if not args.skip_hls:
            hls_lat_p50: list[float] = []
            hls_lat_p95: list[float] = []
            hls_lat_p99: list[float] = []
            hls_thr: list[float] = []
            hls_loss: list[float] = []
            for rep in range(max(1, int(args.hls_repeats))):
                cmd = [
                    str(args.python),
                    str(hls_bench),
                    "--segments",
                    str(args.segments),
                    "--segments-limit",
                    str(int(args.segments_limit)),
                    "--a-url",
                    str(args.a_url),
                    "--c-url",
                    str(args.c_url),
                    "--hidden-bytes",
                    str(hidden_bytes),
                    "--k",
                    str(int(args.k)),
                    "--duration-s",
                    str(float(args.hls_duration_s)),
                    "--concurrency",
                    str(int(args.concurrency)),
                    "--timeout-s",
                    str(float(args.timeout_s)),
                    "--drain-timeout-s",
                    str(float(args.drain_timeout_s)),
                    "--idle-s",
                    str(float(args.idle_s)),
                ]
                print(f"[HLS] hidden={hidden_bytes} repeat={rep + 1} ...")
                rc, out = _run(cmd, timeout_s=sub_timeout)
                parsed = _parse_kv_output(out)
                if rc != 0:
                    print(out[-3000:])
                    raise SystemExit(f"HLS bench failed rc={rc} hidden={hidden_bytes}")
                for key, lst in [
                    ("e2e_ms_p50", hls_lat_p50),
                    ("p95", hls_lat_p95),
                    ("p99", hls_lat_p99),
                    ("throughput_avg_Bps", hls_thr),
                    ("loss_rate", hls_loss),
                ]:
                    v = _f(parsed, key)
                    if v is not None:
                        lst.append(v)
            if hls_lat_p50:
                row["hls_e2e_ms_p50"] = round(sum(hls_lat_p50) / len(hls_lat_p50), 3)
                row["hls_e2e_ms_p95"] = round(sum(hls_lat_p95) / len(hls_lat_p95), 3) if hls_lat_p95 else ""
                row["hls_e2e_ms_p99"] = round(sum(hls_lat_p99) / len(hls_lat_p99), 3) if hls_lat_p99 else ""
                row["hls_throughput_avg_Bps"] = round(sum(hls_thr) / len(hls_thr), 3) if hls_thr else ""
                row["hls_loss_rate"] = round(sum(hls_loss) / len(hls_loss), 4) if hls_loss else ""

        if not args.skip_zhang:
            z_lat_p50: list[float] = []
            z_lat_p95: list[float] = []
            z_lat_p99: list[float] = []
            z_thr: list[float] = []
            z_loss: list[float] = []
            for rep in range(max(1, int(args.zhang_repeats))):
                cmd = [
                    str(args.python),
                    str(zhang_bench),
                    "--url",
                    str(args.zhang_url),
                    "--log",
                    str(args.zhang_log),
                    "--hidden-bytes",
                    str(hidden_bytes),
                    "--repeats",
                    "1",
                    "--timeout-s",
                    str(float(args.timeout_s)),
                ]
                if str(args.psk_hex).strip():
                    cmd += ["--psk-hex", str(args.psk_hex).strip()]
                if str(args.psk_file).strip():
                    cmd += ["--psk-file", str(args.psk_file).strip()]
                print(f"[Zhang] hidden={hidden_bytes} repeat={rep + 1} ...")
                rc, out = _run(cmd, timeout_s=sub_timeout)
                parsed = _parse_kv_output(out)
                if rc != 0:
                    print(out[-3000:])
                    raise SystemExit(f"Zhang bench failed rc={rc} hidden={hidden_bytes}")
                for key, lst in [
                    ("e2e_ms_p50", z_lat_p50),
                    ("e2e_ms_p95", z_lat_p95),
                    ("e2e_ms_p99", z_lat_p99),
                    ("throughput_avg_Bps", z_thr),
                    ("loss_rate", z_loss),
                ]:
                    v = _f(parsed, key)
                    if v is not None:
                        lst.append(v)
            if z_lat_p50:
                row["zhang_e2e_ms_p50"] = round(sum(z_lat_p50) / len(z_lat_p50), 3)
                row["zhang_e2e_ms_p95"] = round(sum(z_lat_p95) / len(z_lat_p95), 3) if z_lat_p95 else ""
                row["zhang_e2e_ms_p99"] = round(sum(z_lat_p99) / len(z_lat_p99), 3) if z_lat_p99 else ""
                row["zhang_throughput_avg_Bps"] = round(sum(z_thr) / len(z_thr), 3) if z_thr else ""
                row["zhang_loss_rate"] = round(sum(z_loss) / len(z_loss), 4) if z_loss else ""

        rows.append(row)
        print("  ->", row)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = (_REPO / out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cols: list[str] = []
    for row in rows:
        for k in row:
            if k not in cols:
                cols.append(k)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    print(f"wrote {len(rows)} rows -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
