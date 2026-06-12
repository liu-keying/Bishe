from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from aiohttp import web

from .b_pull import run_b_pull
from .b_reliability import ReliabilityConfig
from .b_worker import run_b_worker
from .crypto_box import b64d
from .token_util import TokenClaims, verify_token


@dataclass(frozen=True)
class BGateConfig:
    host: str
    port: int
    redis_url: str
    queue_key: str
    poll_interval_s: float
    segment_interval_s: float
    token_secret: str
    control_host: str
    control_port: int
    reliability: ReliabilityConfig
    queue_key_rev: str = "bishe:overlay:queue:rev"


def _claims_from_token(cfg: BGateConfig, token: str) -> TokenClaims:
    return verify_token(secret=cfg.token_secret, token=token)


async def _post_stop_notice(url: str, *, who: str) -> None:
    # 最小实现：只通知，不要求对方一定实现该接口
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False, trust_env=False) as client:
            await client.post(url, json={"ok": True, "who": who})
    except Exception:
        return


def _reset_gate_state(app: web.Application) -> None:
    app["reg"] = {"a": False, "c": False}
    app["claims"] = None
    app["link_psk"] = None
    app["link_token"] = ""
    app["pull_task"] = None
    app["worker_task"] = None
    app["pull_task_rev"] = None
    app["worker_task_rev"] = None


async def handle_register(request: web.Request) -> web.Response:
    cfg: BGateConfig = request.app["cfg"]
    body = await request.json()
    role = str(body.get("role") or "").strip().lower()
    token = str(body.get("token") or "").strip()

    if role not in ("a", "c"):
        return web.json_response({"ok": False, "error": "role must be 'a' or 'c'"}, status=400)
    if not token:
        return web.json_response({"ok": False, "error": "missing token"}, status=400)

    try:
        claims = _claims_from_token(cfg, token)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"bad token: {e!r}"}, status=400)

    reg = request.app["reg"]
    reg[role] = True
    request.app["claims"] = claims
    request.app["link_token"] = token

    psk_b64 = str(body.get("psk_b64") or "").strip()
    if psk_b64:
        try:
            raw_psk = b64d(psk_b64)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"bad psk_b64: {e!r}"}, status=400)
        if len(raw_psk) != 32:
            return web.json_response({"ok": False, "error": "psk_b64 must decode to 32 bytes"}, status=400)
        prev = request.app.get("link_psk")
        if prev is not None and prev != raw_psk:
            return web.json_response({"ok": False, "error": "psk mismatch between A and C register"}, status=400)
        request.app["link_psk"] = raw_psk

    # 两边都登记成功 -> gate 同时启动正向 + 反向 pull/worker
    if reg.get("a") and reg.get("c") and request.app.get("pull_task") is None:
        a_url = (claims.a_url or "").strip()
        c_url = (claims.c_url or "").strip()
        if not a_url:
            return web.json_response({"ok": False, "error": "missing a_url in token"}, status=400)
        if not c_url:
            return web.json_response({"ok": False, "error": "missing c_url in token"}, status=400)

        psk_val = request.app.get("link_psk")
        token_val = str(request.app.get("link_token") or "")

        # 正向 (A→C): 从 A 的 /hls 拉 → 转发到 C 的 /recv
        logging.info(
            "B-gate 注册完成，启动正向 pull+worker: a_url=%s -> c_url=%s  link_token=%s psk=%s",
            a_url,
            c_url,
            bool(token_val),
            psk_val is not None,
        )
        async def _safe_task(name: str, coro):
            try:
                await coro
            except asyncio.CancelledError:
                logging.info("B-gate task %s 被取消", name)
                raise
            except Exception:
                logging.exception("B-gate task %s 异常退出，5秒后重试", name)
                await asyncio.sleep(5)
                # 不重新抛出，让 task 自然结束（外部无法重启）

        request.app["pull_task"] = asyncio.create_task(
            _safe_task("pull_fwd", run_b_pull(
                a_base_url=a_url,
                session_id="bishe-1",
                redis_url=cfg.redis_url,
                queue_key=cfg.queue_key,
                poll_interval_s=cfg.poll_interval_s,
                segment_interval_s=cfg.segment_interval_s,
                hls_prefix="/hls",
            ))
        )
        request.app["worker_task"] = asyncio.create_task(
            _safe_task("worker_fwd", run_b_worker(
                redis_url=cfg.redis_url,
                queue_key=cfg.queue_key,
                c_base_url=c_url,
                psk=psk_val,
                link_token=token_val,
                control_host=cfg.control_host,
                control_port=cfg.control_port,
                reliability=cfg.reliability,
                recv_endpoint="/recv",
                direction_label="fwd",
            ))
        )

        # 反向 (C→A): 从 C 的 /hls-rev 拉 → 转发到 A 的 /overlay/recv-tunnel
        logging.info(
            "B-gate 注册完成，启动反向 pull+worker: c_url=%s -> a_url=%s",
            c_url,
            a_url,
        )
        request.app["pull_task_rev"] = asyncio.create_task(
            _safe_task("pull_rev", run_b_pull(
                a_base_url=c_url,  # 从 C 拉取
                session_id="bishe-rev-1",
                redis_url=cfg.redis_url,
                queue_key=cfg.queue_key_rev,
                poll_interval_s=cfg.poll_interval_s,
                segment_interval_s=cfg.segment_interval_s,
                hls_prefix="/hls-rev",
            ))
        )
        request.app["worker_task_rev"] = asyncio.create_task(
            _safe_task("worker_rev", run_b_worker(
                redis_url=cfg.redis_url,
                queue_key=cfg.queue_key_rev,
                c_base_url=a_url,  # 转发到 A
                psk=psk_val,
                link_token=token_val,
                control_host=cfg.control_host,
                control_port=cfg.control_port,
                reliability=cfg.reliability,
                recv_endpoint="/overlay/recv-tunnel",
                direction_label="rev",
            ))
        )

    return web.json_response(
        {
            "ok": True,
            "role": role,
            "registered": dict(reg),
            "pull_started": request.app.get("pull_task") is not None,
        }
    )


async def handle_stop(request: web.Request) -> web.Response:
    cfg: BGateConfig = request.app["cfg"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    role = str(body.get("role") or "").strip().lower()
    token = str(body.get("token") or "").strip()
    if role not in ("a", "c"):
        return web.json_response({"ok": False, "error": "role must be 'a' or 'c'"}, status=400)
    if not token:
        return web.json_response({"ok": False, "error": "missing token"}, status=400)
    try:
        claims = _claims_from_token(cfg, token)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"bad token: {e!r}"}, status=400)

    pull_task = request.app.get("pull_task")
    worker_task = request.app.get("worker_task")
    pull_task_rev = request.app.get("pull_task_rev")
    worker_task_rev = request.app.get("worker_task_rev")
    for t in (pull_task, worker_task, pull_task_rev, worker_task_rev):
        if t:
            t.cancel()

    # 通知 A/C（尽力而为）
    await _post_stop_notice(claims.a_url.rstrip("/") + "/overlay/stop-notice", who="b-gate")
    await _post_stop_notice(claims.c_url.rstrip("/") + "/stop-notice", who="b-gate")

    _reset_gate_state(request.app)

    return web.json_response({"ok": True, "stopped_by": role})


async def handle_health(request: web.Request) -> web.Response:
    reg = request.app["reg"]
    claims: TokenClaims | None = request.app.get("claims")
    return web.json_response(
        {
            "ok": True,
            "registered": dict(reg),
            "a_url": claims.a_url if claims else "",
            "c_url": claims.c_url if claims else "",
            "pull_started": request.app.get("pull_task") is not None,
            "worker_started": request.app.get("worker_task") is not None,
            "pull_started_rev": request.app.get("pull_task_rev") is not None,
            "worker_started_rev": request.app.get("worker_task_rev") is not None,
        }
    )


async def run_b_gate_pull(
    *,
    host: str,
    port: int,
    redis_url: str,
    queue_key: str,
    poll_interval_s: float,
    segment_interval_s: float = 0.0,
    token_secret: str,
    control_host: str = "127.0.0.1",
    control_port: int | None = None,
    reliability: ReliabilityConfig | None = None,
    queue_key_rev: str = "bishe:overlay:queue:rev",
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cport = int(control_port) if control_port is not None else int(port) + 1
    rcfg = reliability if reliability is not None else ReliabilityConfig()
    cfg = BGateConfig(
        host=host,
        port=port,
        redis_url=redis_url,
        queue_key=queue_key,
        poll_interval_s=poll_interval_s,
        segment_interval_s=max(0.0, float(segment_interval_s)),
        token_secret=token_secret,
        control_host=control_host,
        control_port=cport,
        reliability=rcfg,
        queue_key_rev=queue_key_rev,
    )

    app = web.Application()
    app["cfg"] = cfg
    app["reg"] = {"a": False, "c": False}
    app["claims"] = None
    app["link_psk"] = None
    app["link_token"] = ""
    app["pull_task"] = None
    app["worker_task"] = None
    app["pull_task_rev"] = None
    app["worker_task_rev"] = None
    app.router.add_get("/health", handle_health)
    app.router.add_post("/register", handle_register)
    app.router.add_post("/stop", handle_stop)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    logging.info("B-gate 启动: http://%s:%d (POST /register role=a|c)", host, port)
    await site.start()

    while True:
        await asyncio.sleep(3600)


def env_token_secret() -> str:
    return os.environ.get("BISHE_TOKEN_SECRET", "bishe-dev-secret")

