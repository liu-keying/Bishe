from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from aiohttp import web

from .b_pull import run_b_pull
from .b_worker import run_b_worker
from .token_util import TokenClaims, verify_token


@dataclass(frozen=True)
class BGateConfig:
    host: str
    port: int
    redis_url: str
    queue_key: str
    poll_interval_s: float
    token_secret: str


def _claims_from_token(cfg: BGateConfig, token: str) -> TokenClaims:
    return verify_token(secret=cfg.token_secret, token=token)


async def _post_stop_notice(url: str, *, who: str) -> None:
    # 最小实现：只通知，不要求对方一定实现该接口
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            await client.post(url, json={"ok": True, "who": who})
    except Exception:
        return


def _reset_gate_state(app: web.Application) -> None:
    app["reg"] = {"a": False, "c": False}
    app["claims"] = None
    app["pull_task"] = None
    app["worker_task"] = None


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

    # 两边都登记成功 -> gate 同时启动 b-pull + b-worker
    if reg.get("a") and reg.get("c") and request.app.get("pull_task") is None:
        a_url = (claims.a_url or "").strip()
        c_url = (claims.c_url or "").strip()
        if not a_url:
            return web.json_response({"ok": False, "error": "missing a_url in token"}, status=400)
        if not c_url:
            return web.json_response({"ok": False, "error": "missing c_url in token"}, status=400)

        logging.info(
            "B-gate 注册完成，启动 b-pull + b-worker: a_url=%s -> c_url=%s",
            a_url,
            c_url,
        )
        request.app["pull_task"] = asyncio.create_task(
            run_b_pull(
                a_base_url=a_url,
                session_id="bishe-1",
                redis_url=cfg.redis_url,
                queue_key=cfg.queue_key,
                poll_interval_s=cfg.poll_interval_s,
            )
        )
        request.app["worker_task"] = asyncio.create_task(
            run_b_worker(redis_url=cfg.redis_url, queue_key=cfg.queue_key, c_base_url=c_url)
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
    if pull_task:
        pull_task.cancel()
    if worker_task:
        worker_task.cancel()

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
        }
    )


async def run_b_gate_pull(
    *,
    host: str,
    port: int,
    redis_url: str,
    queue_key: str,
    poll_interval_s: float,
    token_secret: str,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = BGateConfig(
        host=host,
        port=port,
        redis_url=redis_url,
        queue_key=queue_key,
        poll_interval_s=poll_interval_s,
        token_secret=token_secret,
    )

    app = web.Application()
    app["cfg"] = cfg
    app["reg"] = {"a": False, "c": False}
    app["claims"] = None
    app["pull_task"] = None
    app["worker_task"] = None
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

