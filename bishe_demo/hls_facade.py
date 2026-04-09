from __future__ import annotations

from urllib.parse import urljoin


def parse_master_playlist_variant_uri(body: str, playlist_url: str) -> str | None:
    """从 master m3u8 中取第一条媒体列表 URI（相对路径相对 playlist_url 解析）。"""
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ".m3u8" in s.lower():
            return urljoin(playlist_url, s)
    return None


def parse_media_playlist_first_segment_uri(body: str, playlist_url: str) -> str | None:
    """从媒体 m3u8 中取第一条 .ts 分片 URI。"""
    uris = parse_media_playlist_all_segment_uris(body, playlist_url)
    return uris[0] if uris else None


def parse_media_playlist_all_segment_uris(body: str, playlist_url: str) -> list[str]:
    """从媒体 m3u8 中按出现顺序取出所有 .ts 分片 URI（相对路径相对 playlist_url 解析）。"""
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower().endswith(".ts"):
            out.append(urljoin(playlist_url, s))
    return out
