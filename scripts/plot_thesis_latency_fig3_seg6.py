"""
根据 thesis_latency_fig3_seg6_measured.csv 绘制
「固定 HLS=6：密文分片数 k 与 p50/p95」折线图（与 thesis 原图同风格 SVG）。

用法:
  python scripts/plot_thesis_latency_fig3_seg6.py
  python scripts/plot_thesis_latency_fig3_seg6.py --csv scripts/thesis_latency_fig3_seg6_measured.csv
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
from collections import defaultdict
from pathlib import Path
from statistics import mean


def _load_plot_helpers():
    plot_py = Path(__file__).resolve().parent / "plot_bench_matrix_embed_hls_thesis.py"
    spec = importlib.util.spec_from_file_location("thesis_plot", plot_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {plot_py}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_measured(csv_path: Path) -> dict[tuple[int, int], tuple[float, float]]:
    """(hidden_bytes, k) -> (p50, p95)"""
    out: dict[tuple[int, int], tuple[float, float]] = {}
    with csv_path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            hb = str(row.get("hidden_bytes_B", "")).strip()
            if not hb or hb.startswith("#"):
                continue
            h = int(hb)
            k = int(row["k"])
            out[(h, k)] = (float(row["mean_e2e_ms_p50"]), float(row["mean_p95_ms"]))
    if not out:
        raise SystemExit(f"no data rows in {csv_path}")
    return out


def build_fig3_svg(
    *,
    data: dict[tuple[int, int], tuple[float, float]],
    ks_sorted: list[int],
    hidden_sorted: list[int],
) -> list[str]:
    plot = _load_plot_helpers()
    colors = plot._COLORS
    _esc = plot._esc_xml
    _nice = plot._nice_y_ticks
    _fmt = plot._fmt_axis_num
    _axes = plot._svg_axes_frame

    x0, y0, iw, ih = 50, 70, 480, 300
    lines = [
        f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 100}" fill="white" stroke="#ccc"/>',
        f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
        f'{_esc("固定HLS分片数=6：密文分片数 k 与 p50（实线）/ p95（虚线）")}</text>',
    ]

    mx_raw = max(v for p50, p95 in data.values() for v in (p50, p95)) * 1.08
    y_ticks = _nice(0.0, mx_raw, 6)
    y_hi = max(y_ticks[-1], mx_raw)
    plot_left = 95.0
    plot_right = x0 + iw - 15.0
    plot_top = y0 + 32.0
    plot_bottom = y0 + ih - 52.0

    if len(ks_sorted) <= 1:
        xmap = {ks_sorted[0]: (plot_left + plot_right) / 2} if ks_sorted else {}
    else:
        xmap = {
            k: plot_left + i * ((plot_right - plot_left) / (len(ks_sorted) - 1))
            for i, k in enumerate(ks_sorted)
        }

    def py_v(v: float) -> float:
        return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

    lines.extend(
        _axes(
            plot_left=plot_left,
            plot_right=plot_right,
            plot_top=plot_top,
            plot_bottom=plot_bottom,
            y_ticks=y_ticks,
            y_min=0.0,
            y_max=y_hi,
            x_labels=[(xmap[k], str(k)) for k in ks_sorted],
            x_axis_title="密文分片数 k / 个",
            y_axis_title="时延 / (ms)",
            y_tick_suffix=" ms",
        )
    )

    for h in hidden_sorted:
        col = colors.get(h, "#333")
        pts50: list[str] = []
        pts95: list[str] = []
        for k in ks_sorted:
            if (h, k) not in data:
                continue
            p50, p95 = data[(h, k)]
            pts50.append(f"{xmap[k]},{py_v(p50)}")
            pts95.append(f"{xmap[k]},{py_v(p95)}")
        if len(pts50) >= 2:
            lines.append(
                f'<polyline fill="none" stroke="{col}" stroke-width="2.5" points="{" ".join(pts50)}"/>'
            )
            lines.append(
                f'<polyline fill="none" stroke="{col}" stroke-width="2" stroke-dasharray="7,5" '
                f'opacity="0.85" points="{" ".join(pts95)}"/>'
            )
        for k in ks_sorted:
            if (h, k) not in data:
                continue
            p50, _ = data[(h, k)]
            lines.append(
                f'<circle cx="{xmap[k]}" cy="{py_v(p50)}" r="4" fill="{col}" '
                f'stroke="white" stroke-width="1"/>'
            )

    lx = plot_left
    ly = plot_bottom + 58
    for idx, h in enumerate(hidden_sorted):
        lines.append(
            f'<rect x="{lx + idx * 88}" y="{ly}" width="10" height="10" fill="{colors.get(h, "#888")}"/>'
            f'<text x="{lx + idx * 88 + 14}" y="{ly + 9}" font-size="11" font-weight="600" fill="#222">{h} B</text>'
        )
    lines.append(
        f'<text x="{lx}" y="{ly + 22}" font-size="11" font-weight="600" fill="#111">'
        f'{_esc("说明：同色为同档 hidden_bytes；粗实线 p50，虚线 p95。")}</text>'
    )
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--csv",
        type=Path,
        default=Path(__file__).resolve().parent / "thesis_latency_fig3_seg6_measured.csv",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "thesis_out",
    )
    args = ap.parse_args()

    data = load_measured(args.csv)
    ks_sorted = sorted({k for _, k in data})
    hidden_sorted = sorted({h for h, _ in data})

    plot = _load_plot_helpers()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_svg = args.out_dir / "thesis_embed_hls_latency_fig_3_seg6_p50p95.svg"
    plot._write_svg_file(out_svg, 620, 486, build_fig3_svg(data=data, ks_sorted=ks_sorted, hidden_sorted=hidden_sorted))
    print(f"wrote {out_svg}")

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; SVG only")
        return 0

    fig, ax = plt.subplots(figsize=(8.5, 5.2))
    colors = {1024: "#4C72B0", 4096: "#55A868", 65536: "#C44E52"}
    for h in hidden_sorted:
        xs = []
        p50s = []
        p95s = []
        for k in ks_sorted:
            if (h, k) not in data:
                continue
            xs.append(k)
            p50, p95 = data[(h, k)]
            p50s.append(p50)
            p95s.append(p95)
        c = colors.get(h, "#333")
        ax.plot(xs, p50s, "-o", color=c, linewidth=2.5, markersize=7, label=f"{h} B p50")
        ax.plot(xs, p95s, "--o", color=c, linewidth=2, markersize=5, alpha=0.85, label=f"{h} B p95")
    ax.set_xlabel("密文分片数 k / 个")
    ax.set_ylabel("时延 / (ms)")
    ax.set_title("固定HLS分片数=6：密文分片数 k 与 p50（实线）/ p95（虚线）")
    ax.set_xticks(ks_sorted)
    ax.grid(True, axis="y", linestyle="--", alpha=0.35)
    ax.legend(loc="upper left", fontsize=9, ncol=2)
    fig.tight_layout()
    out_png = args.out_dir / "thesis_embed_hls_latency_fig_3_seg6_p50p95.png"
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    print(f"wrote {out_png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
