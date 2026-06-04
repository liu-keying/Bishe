"""
将 thesis_out（或指定目录）下的 thesis_embed_hls*.svg 导出为 PNG。

优先顺序：
  1) cairosvg（pip install cairosvg；部分 Windows 环境需额外装 GTK/cairo）
  2) Inkscape 命令行（PATH 中有 inkscape）
  3) ImageMagick v7（PATH 中有 magick）

示例：
  python scripts/thesis_svg_to_png.py
  python scripts/thesis_svg_to_png.py --dir scripts/thesis_out --scale 2

pip 报错 ValueError: check_hostname requires server_hostname
  多为当前终端里 HTTP/HTTPS 代理环境变量与 pip/SSL 不兼容（与是否用清华源无关）。
  在当前 PowerShell 会话里先清空代理再装（大小写都清一遍更稳）：

    $env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:ALL_PROXY=''
    $env:http_proxy=''; $env:https_proxy=''; $env:all_proxy=''
    pip install cairosvg

  若仍失败，可临时改用官方索引（排除镜像站与代理组合问题）：

    pip install cairosvg -i https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org

  清空环境变量后仍报同样错时，代理多半写在 pip 配置里，先查看再删掉：

    pip config list -v
    pip config unset global.proxy
    pip config unset install.proxy

  不打算用 pip 时：安装 Inkscape 或 ImageMagick 并加入 PATH，本脚本会自动走命令行导出。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _svg_pixel_size(svg_path: Path) -> tuple[int | None, int | None]:
    """从根 <svg width=\"580\" height=\"460\"> 解析像素尺寸（失败则返回 None）。"""
    try:
        head = svg_path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        return None, None
    m = re.search(r"<svg[^>]*\swidth=\"(\d+)\"[^>]*\sheight=\"(\d+)\"", head, re.I)
    if not m:
        m = re.search(r"<svg[^>]*\sheight=\"(\d+)\"[^>]*\swidth=\"(\d+)\"", head, re.I)
        if m:
            h, w = m.group(1), m.group(2)
            return int(w), int(h)
        return None, None
    return int(m.group(1)), int(m.group(2))


def _export_cairosvg(svg: Path, png: Path, scale: float) -> None:
    import cairosvg

    data = svg.read_bytes()
    cairosvg.svg2png(bytestring=data, write_to=str(png), scale=scale)


def _export_inkscape(svg: Path, png: Path, scale: float) -> None:
    exe = shutil.which("inkscape")
    if not exe:
        raise RuntimeError("inkscape not on PATH")
    w, h = _svg_pixel_size(svg)
    cmd = [exe, str(svg), "--export-type=png", f"--export-filename={png}"]
    if w is not None and h is not None:
        cmd.append(f"--export-width={max(1, int(w * scale))}")
        cmd.append(f"--export-height={max(1, int(h * scale))}")
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _export_magick(svg: Path, png: Path, scale: float) -> None:
    exe = shutil.which("magick")
    if not exe:
        raise RuntimeError("magick (ImageMagick 7) not on PATH")
    w, h = _svg_pixel_size(svg)
    density = int(96 * scale) if scale > 0 else 150
    cmd = [exe, str(svg), "-density", str(density)]
    if w is not None and h is not None:
        cmd += ["-resize", f"{max(1, int(w * scale))}x{max(1, int(h * scale))}!"]
    cmd.append(str(png))
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def export_svgs_to_png(out_dir: Path, *, scale: float = 2.0) -> tuple[list[Path], str]:
    """
    将 out_dir 下 thesis_embed_hls*.svg 转为同名 .png。
    返回 (写入的 png 路径列表, 使用的方法名)。
    """
    svgs = sorted(out_dir.glob("thesis_embed_hls*.svg"))
    if not svgs:
        return [], "none"

    backend: str | None = None
    export = None
    try:
        import cairosvg  # noqa: F401

        backend = "cairosvg"
        export = _export_cairosvg
    except ImportError:
        pass
    if export is None and shutil.which("inkscape"):
        backend = "inkscape"
        export = _export_inkscape
    if export is None and shutil.which("magick"):
        backend = "imagemagick"
        export = _export_magick
    if export is None or backend is None:
        raise RuntimeError(
            "未找到可用的 SVG→PNG 方式。请任选其一：\n"
            "  pip install cairosvg\n"
            "  或安装 Inkscape / ImageMagick(magick) 并加入 PATH\n"
            "pip 若报 check_hostname requires server_hostname：先清空环境变量里的代理，再试；\n"
            "仍失败则执行 pip config list -v，并用 pip config unset global.proxy 等去掉 pip.ini 里的 proxy。\n"
            "详见本文件模块顶部的说明。"
        )

    written: list[Path] = []
    for svg in svgs:
        png = svg.with_suffix(".png")
        export(svg, png, scale)
        written.append(png)
        print(f"wrote {png}")
    return written, backend


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dir",
        type=Path,
        default=Path(__file__).resolve().parent / "thesis_out",
        help="含 thesis_embed_hls*.svg 的目录",
    )
    ap.add_argument(
        "--scale",
        type=float,
        default=2.0,
        help="相对 SVG 声明尺寸的缩放（cairosvg 为 scale；Inkscape 为宽高×scale）",
    )
    args = ap.parse_args()
    out_dir = args.dir
    if not out_dir.is_dir():
        print(f"not a directory: {out_dir}", file=sys.stderr)
        return 1
    try:
        written, backend = export_svgs_to_png(out_dir, scale=args.scale)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"done ({len(written)} png, backend={backend})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
