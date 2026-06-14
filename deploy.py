#!/usr/bin/env python3
"""分布式部署脚本：通过 SSH 将各节点部署到多台机器并启动，实时流式回传日志。

用法:
  python3 deploy.py up      --config deploy_config.json    # 部署并启动，实时流式输出日志
  python3 deploy.py down    --config deploy_config.json    # 停止全部节点
  python3 deploy.py sync    --config deploy_config.json    # 仅同步代码（不启动）
  python3 deploy.py status  --config deploy_config.json    # 查看各节点状态
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------

REQUIRED_NODES = ["control", "gateway", "server", "client"]
START_ORDER = ["control", "gateway", "server", "client"]
STOP_ORDER = ["client", "server", "gateway", "control"]


class DeployConfig:
    def __init__(self, path: str):
        with open(path) as f:
            raw = json.load(f)

        self.token_secret = (raw.get("token_secret") or "").strip()
        if not self.token_secret:
            self.token_secret = secrets.token_hex(32)
            print(f"  token_secret 未配置，已自动生成: {self.token_secret}")
        self.ssh_user = raw.get("ssh_user", "root")
        self.ssh_key = raw.get("ssh_key") or None
        self.ssh_password = raw.get("ssh_password") or None
        raw_dir = raw.get("remote_dir", "bishe")
        if raw_dir.startswith("/") or raw_dir.startswith("~"):
            self.remote_dir = raw_dir
        else:
            # 相对路径 → 远程 $HOME/xxx
            self.remote_dir = f"$HOME/{raw_dir}"
        self.venv = raw.get("venv", f"{self.remote_dir}/.venv")
        self.python = f"{self.venv}/bin/python3"
        self.session = raw.get("session", "bishe-1")
        self.queue = raw.get("queue", "bishe:overlay:queue")
        self.queue_rev = raw.get("queue_rev", "bishe:overlay:queue:rev")
        self.reliability = raw.get("reliability", True)
        self.mode = raw.get("mode", "inject")
        self.redis = raw.get("redis") or {}
        gw_host = (raw.get("nodes", {}).get("gateway", {}) or {}).get("host", "")
        self.redis_managed = self.redis.get("managed", self.redis.get("host", gw_host) == gw_host)
        self.redis_host = self.redis.get("host", gw_host)
        self.redis_port = str(self.redis.get("port", 6379))

        self.nodes: dict[str, dict] = raw.get("nodes", {})
        for role in REQUIRED_NODES:
            if role not in self.nodes:
                raise SystemExit(f"配置缺少必填节点: {role}")

        for role, nd in self.nodes.items():
            nd.setdefault("role", role)
            nd.setdefault("host", "127.0.0.1")
            nd.setdefault("port", _default_port(role))
            nd["url"] = f"http://{nd['host']}:{nd['port']}"

    # ---- URL helpers ----

    @property
    def redis_url(self) -> str:
        host = self.redis_host
        gw_host = self.nodes.get("gateway", {}).get("host", "")
        if host == gw_host:
            host = "127.0.0.1"  # 同机走本地回环，不受 protected-mode 限制
        return f"redis://{host}:{self.redis_port}/0"

    @property
    def redis_host_port(self) -> str:
        return f"{self.redis_host}:{self.redis_port}"

    def node_url(self, role: str) -> str:
        return self.nodes[role]["url"]

    # ---- 每节点 SSH 凭据 (可覆盖全局设置) ----

    def _ssh_user_for(self, role: str) -> str:
        return self.nodes[role].get("ssh_user") or self.ssh_user

    def _ssh_password_for(self, role: str) -> str | None:
        return self.nodes[role].get("ssh_password") or self.ssh_password

    def _ssh_key_for(self, role: str) -> str | None:
        return self.nodes[role].get("ssh_key") or self.ssh_key

    def _ssh_opts_for(self, role: str) -> list[str]:
        opts = ["-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
        key = self._ssh_key_for(role)
        if key:
            opts.extend(["-i", os.path.expanduser(key)])
        return opts

    def _ssh_prefix(self, role: str) -> list[str]:
        """返回 ssh 命令前缀，含可选的 sshpass。"""
        pwd = self._ssh_password_for(role)
        if pwd:
            return ["sshpass", "-p", pwd, "ssh"] + self._ssh_opts_for(role)
        return ["ssh"] + self._ssh_opts_for(role)

    def ssh_target(self, role: str) -> str:
        nd = self.nodes[role]
        return f"{self._ssh_user_for(role)}@{nd['host']}"

    # ---- 各节点 CLI 参数 ----

    def args_for(self, role: str) -> str:
        return getattr(self, f"_args_{role}")()

    def _args_control(self) -> str:
        nd = self.nodes["control"]
        return (
            f"control --host {nd['host']} --port {nd['port']}"
            f" --token-secret {self.token_secret}"
        )

    def _args_gateway(self) -> str:
        nd = self.nodes["gateway"]
        control_port = nd.get("control_port", 8011)
        parts = [
            f"gateway --host {nd['host']} --port {nd['port']}",
            f"--redis {self.redis_url}",
            f"--queue {self.queue}",
            f"--token-secret {self.token_secret}",
            f"--control-port {control_port}",
        ]
        if self.mode == "socks5":
            parts.append(f"--queue-rev {self.queue_rev}")
        if not self.reliability:
            parts.append("--no-reliability")
        return " ".join(parts)

    def _args_server(self) -> str:
        nd = self.nodes["server"]
        parts = [
            f"server --host {nd['host']} --port {nd['port']}",
            f"--control-url {self.node_url('control')}",
            f"--client-url {self.node_url('client')}",
            f"--gateway-url {self.node_url('gateway')}",
        ]
        if not self.reliability:
            parts.append("--no-reliability")
        else:
            gw_control_port = self.nodes["gateway"].get("control_port", 8011)
            gw_host = self.nodes["gateway"]["host"]
            parts.append(
                f"--gateway-callback-url http://{gw_host}:{gw_control_port}/overlay/recv-ack"
            )
        if nd.get("ssl_cert") and nd.get("ssl_key"):
            parts.append(f"--ssl-cert {nd['ssl_cert']} --ssl-key {nd['ssl_key']}")
        if self.mode == "socks5":
            parts.append("--socks-max-conns 50")
        else:
            parts.append("--no-socks")
        return " ".join(parts)

    def _args_client(self) -> str:
        nd = self.nodes["client"]
        parts = [
            f"client --host {nd['host']} --port {nd['port']}",
            f"--session {self.session}",
            f"--control-url {self.node_url('control')}",
            f"--server-url {self.node_url('server')}",
            f"--gateway-url {self.node_url('gateway')}",
        ]
        if self.mode == "socks5":
            socks_port = int(nd.get("socks_port") or 1080)
            parts.append(f"--socks-port {socks_port}")
        return " ".join(parts)


def _default_port(role: str) -> int:
    return {"control": 8009, "gateway": 8010, "server": 8002, "client": 8000}.get(role, 8000)


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"



# ---------------------------------------------------------------------------
# SSH 原语 (每个函数都接受 role, 以便使用该节点的凭据)
# ---------------------------------------------------------------------------

def _ssh(cfg: DeployConfig, role: str, cmd: str, *, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
    """在远程执行命令，stdout/stderr 透传。check=False 时不因非零退出码抛异常。"""
    target = cfg.ssh_target(role)
    prefix = cfg._ssh_prefix(role)
    full = prefix + [target, cmd]
    print(f"  [{target}] $ {cmd[:120]}{'...' if len(cmd) > 120 else ''}")
    return subprocess.run(full, check=check, timeout=timeout)


def _ssh_capture(cfg: DeployConfig, role: str, cmd: str) -> str:
    """远程执行并捕获 stdout。"""
    target = cfg.ssh_target(role)
    prefix = cfg._ssh_prefix(role)
    r = subprocess.run(prefix + [target, cmd], capture_output=True, text=True)
    return r.stdout.strip()


def _rsync_to(cfg: DeployConfig, role: str, resolved_dir: str) -> None:
    """rsync 项目到远程主机的指定绝对路径。"""
    target = cfg.ssh_target(role)
    opts = cfg._ssh_opts_for(role)
    pwd = cfg._ssh_password_for(role)
    if pwd:
        ssh_cmd = f"sshpass -p {sh_quote(pwd)} ssh {' '.join(opts)}"
    else:
        ssh_cmd = f"ssh {' '.join(opts)}"

    project_root = Path(__file__).resolve().parent
    src_dir = str(project_root) + "/"
    cmd = (
        f"rsync -az -e {sh_quote(ssh_cmd)}"
        f" --exclude='__pycache__' --exclude='.git' --exclude='.venv'"
        f" --exclude='*.pyc' --exclude='deploy_config.json'"
        f" --exclude='logs/' --exclude='run/'"
        f" {src_dir} {target}:{sh_quote(resolved_dir)}/"
    )
    print(f"  rsync -> {target}:{resolved_dir}/")
    subprocess.run(cmd, shell=True, check=True)


# ---------------------------------------------------------------------------
# 日志拉取
# ---------------------------------------------------------------------------

def _pull_log(cfg: DeployConfig, role: str, local_dir: str) -> str:
    """通过 scp 拉取单个节点的日志到控制机文件。返回本地文件路径。"""
    target = cfg.ssh_target(role)
    nd = cfg.nodes[role]
    rdir = nd.get("_resolved_dir") or cfg.remote_dir
    remote_log = f"{rdir}/logs/{role}.log"

    os.makedirs(local_dir, exist_ok=True)
    local_file = os.path.join(local_dir, f"{role}.log")
    tmp_file = local_file + ".tmp"

    opts = cfg._ssh_opts_for(role)
    pwd = cfg._ssh_password_for(role)
    if pwd:
        scp_prefix = ["sshpass", "-p", pwd, "scp"] + opts
    else:
        scp_prefix = ["scp"] + opts

    try:
        subprocess.run(scp_prefix + [f"{target}:{remote_log}", tmp_file],
                       capture_output=True, timeout=10)
        os.replace(tmp_file, local_file)  # 原子替换，避免损坏
    except Exception:
        pass  # 静默跳过，下次重试
    return local_file


# ---------------------------------------------------------------------------
# 操作
# ---------------------------------------------------------------------------

def do_sync(cfg: DeployConfig) -> None:
    """同步代码到所有主机（同一主机只传一次）。"""
    hosts: dict[str, str] = {}  # host -> representative role
    for role in REQUIRED_NODES:
        h = cfg.nodes[role]["host"]
        if h not in hosts:
            hosts[h] = role

    def sync_one(host: str) -> None:
        rep_role = hosts[host]
        # 解析远程绝对路径（$HOME 和 ~ 在各命令中行为不一致，统一展开）
        resolved_dir = _ssh_capture(cfg, rep_role, f"echo {cfg.remote_dir}")
        if not resolved_dir:
            resolved_dir = cfg.remote_dir
        venv_dir = _ssh_capture(cfg, rep_role, f"echo {cfg.venv}") or cfg.venv

        # 创建目录
        r = _ssh(cfg, rep_role,
                 f"mkdir -p {resolved_dir} {resolved_dir}/logs {resolved_dir}/run",
                 check=False)
        if r.returncode != 0:
            raise SystemExit(
                f"\n无法在 {host} 上创建 {resolved_dir}（权限不足）。\n"
                f"请先手动执行: ssh {cfg.ssh_target(rep_role)} 'mkdir -p {resolved_dir}/{{logs,run}}'\n"
                f"或修改 deploy_config.json 中的 remote_dir 为该用户可写的目录。"
            )

        # rsync
        try:
            _rsync_to(cfg, rep_role, resolved_dir)
        except subprocess.CalledProcessError:
            raise SystemExit(f"rsync 到 {host} 失败，检查 SSH 连通性")

        # venv
        _ssh(cfg, rep_role,
             f"python3 -m venv --help >/dev/null 2>&1 || "
             f"(sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv)",
             check=False)
        _ssh(cfg, rep_role,
             f"test -d {venv_dir} || python3 -m venv {venv_dir}")
        _ssh(cfg, rep_role,
             f"cd {resolved_dir} && {venv_dir}/bin/pip install -q -r requirements.txt")

        # 缓存解析后的路径，供 start 使用
        cfg.nodes[rep_role]["_resolved_dir"] = resolved_dir
        cfg.nodes[rep_role]["_venv_dir"] = venv_dir

    print(f"\n=== 同步代码到 {len(hosts)} 台主机 ===")
    with ThreadPoolExecutor(max_workers=min(4, len(hosts))) as pool:
        list(pool.map(sync_one, hosts.keys()))
    print("同步完成\n")


def do_start_one(cfg: DeployConfig, role: str) -> None:
    """启动单个节点。"""
    args = cfg.args_for(role)
    nd = cfg.nodes[role]
    rdir = nd.get("_resolved_dir") or cfg.remote_dir
    vdir = nd.get("_venv_dir") or cfg.venv
    python = f"{vdir}/bin/python3"

    # 先杀掉旧进程（pkill + fuser 兜底，确保端口释放）
    _ssh(cfg, role,
         f"(timeout 5 pkill -F {rdir}/run/{role}.pid 2>/dev/null || "
         f"fuser -k {nd['port']}/tcp 2>/dev/null || true); "
         f"sleep 1",
         timeout=10, check=False)

    # 用 ssh -f 在后台启动（比 setsid 更可靠）
    target = cfg.ssh_target(role)
    prefix = cfg._ssh_prefix(role)
    start_cmd = (
        f"cd {rdir} && "
        f"nohup {python} main.py {args} </dev/null >{rdir}/logs/{role}.log 2>&1 & "
        f"echo $! > {rdir}/run/{role}.pid"
    )
    full = prefix + ["-f", target, start_cmd]
    print(f"  [{target}] $ {start_cmd[:120]}{'...' if len(start_cmd) > 120 else ''}")
    subprocess.run(full, timeout=15)


def do_stop_one(cfg: DeployConfig, role: str) -> str:
    """停止单个节点。"""
    nd = cfg.nodes[role]
    # 先解析远程绝对路径（down 是新进程，没有 _resolved_dir 缓存）
    rdir = nd.get("_resolved_dir") or _ssh_capture(cfg, role, f"echo {cfg.remote_dir}") or cfg.remote_dir
    return _ssh_capture(
        cfg, role,
        f"kill $(cat {rdir}/run/{role}.pid 2>/dev/null) 2>/dev/null && echo stopped || echo 'not running'"
    )


def do_redis_start(cfg: DeployConfig) -> None:
    """在 gateway 机器上启动 Redis（优先 apt，备选 docker）。"""
    if not cfg.redis_managed:
        return
    role = "gateway"
    print(f"--- Redis ({cfg.redis_host_port}) ---")

    # 先试 apt（更可靠，不受墙影响）
    r = _ssh_capture(cfg, role,
                     f"redis-cli -p {cfg.redis_port} ping 2>/dev/null")
    if r == "PONG":
        print("  Redis: 已在运行")
        return

    # apt 安装并启动（sudo -S 通过 stdin 传密码）
    pwd = cfg._ssh_password_for(role) or ""
    sudo = f"echo {sh_quote(pwd)} | sudo -S" if pwd else "sudo -n"
    _ssh(cfg, role,
         f"{sudo} apt-get update -qq 2>/dev/null; "
         f"{sudo} apt-get install -y -qq redis-server 2>/dev/null; "
         f"{sudo} sed -i 's/^bind .*/bind 0.0.0.0/' /etc/redis/redis.conf 2>/dev/null; "
         f"{sudo} systemctl restart redis-server 2>/dev/null; "
         f"echo done",
         check=False, timeout=120)

    r = _ssh_capture(cfg, role, f"redis-cli -p {cfg.redis_port} ping 2>/dev/null")
    if r == "PONG":
        print("  Redis: OK (apt)")
        return

    # apt 失败，回退到 docker
    print("  apt 不可用，尝试 docker ...")
    _ssh(cfg, role,
         f"docker rm -f bishe-redis 2>/dev/null; "
         f"docker run -d --restart unless-stopped --name bishe-redis "
         f"-p {cfg.redis_port}:6379 redis:7 2>/dev/null; "
         f"echo done",
         check=False, timeout=180)

    for _ in range(5):
        r = _ssh_capture(cfg, role, "docker exec bishe-redis redis-cli ping 2>/dev/null")
        if r == "PONG":
            print("  Redis: OK (docker)")
            return
        time.sleep(2)
    print("  Redis: 未能确认就绪，继续部署（gateway 会自动重连）")


def do_redis_stop(cfg: DeployConfig) -> None:
    """停止 gateway 机器上的 Redis。"""
    if not cfg.redis_managed:
        return
    role = "gateway"
    pwd = cfg._ssh_password_for(role) or ""
    sudo = f"echo {sh_quote(pwd)} | sudo -S" if pwd else "sudo -n"
    print(f"--- 停止 Redis ---")
    _ssh(cfg, role,
         f"{sudo} systemctl stop redis-server 2>/dev/null; "
         f"docker stop bishe-redis 2>/dev/null; "
         f"echo done",
         check=False)


def do_health(cfg: DeployConfig, role: str) -> str:
    """检查节点健康状态（最多重试 5 次，每次等 2 秒）。"""
    url = f"{cfg.node_url(role)}/health"
    for attempt in range(5):
        try:
            r = subprocess.run(["curl", "-sf", "--max-time", "3", url],
                               capture_output=True, text=True)
            if r.returncode == 0:
                return "OK"
        except Exception:
            pass
        if attempt < 4:
            time.sleep(2)
    return "FAIL"


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_up(cfg: DeployConfig) -> None:
    print("=" * 60)
    print("Bishe 分布式部署")
    print(f"  control : {cfg.node_url('control')}")
    print(f"  gateway : {cfg.node_url('gateway')}  (redis: {cfg.redis_host_port})")
    print(f"  server  : {cfg.node_url('server')}")
    print(f"  client  : {cfg.node_url('client')}")
    print(f"  mode    : {cfg.mode}  |  reliability: {cfg.reliability}")
    print("=" * 60)

    # 1. 同步代码
    do_sync(cfg)

    # 2. 启动 Redis
    do_redis_start(cfg)

    # 3. 按序启动各节点
    print("\n=== 启动节点 ===")
    for role in START_ORDER:
        nd = cfg.nodes[role]
        print(f"\n--- {role} ({nd['host']}:{nd['port']}) ---")
        do_start_one(cfg, role)
        status = do_health(cfg, role)
        marker = "✓" if status == "OK" else "✗"
        print(f"  {marker} {role}: {status}")

    # 3. 定期拉取日志到控制机文件
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bishe-logs")
    print(f"\n✓ 集群启动完成，日志实时拉取到 {log_dir}/（Ctrl+C 停止）\n")
    try:
        while True:
            for role in START_ORDER:
                _pull_log(cfg, role, log_dir)
            time.sleep(5)
    except KeyboardInterrupt:
        print(f"\n已停止日志拉取，节点仍在运行（用 'down' 停止）")


def cmd_down(cfg: DeployConfig) -> None:
    print("=== 停止全部节点 ===")
    for role in STOP_ORDER:
        out = do_stop_one(cfg, role)
        print(f"  {role}: {out}")
    do_redis_stop(cfg)
    print("已停止")


def cmd_sync(cfg: DeployConfig) -> None:
    do_sync(cfg)
    print("同步完成（未重启节点）")


def cmd_status(cfg: DeployConfig) -> None:
    print(f"{'节点':<10} {'URL':<30} {'状态':<10}")
    print("-" * 50)
    for role in START_ORDER:
        status = do_health(cfg, role)
        marker = "✓ OK" if status == "OK" else f"✗ {status}"
        print(f"{role:<10} {cfg.node_url(role):<30} {marker}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Bishe 分布式部署工具")
    p.add_argument("command", choices=["up", "down", "sync", "status"],
                   help="up=部署+启动+日志 / down=停止 / sync=仅同步代码 / status=查看状态")
    p.add_argument("--config", default="deploy_config.json",
                   help="配置文件路径 (default: deploy_config.json)")
    args = p.parse_args()

    if not os.path.exists(args.config):
        raise SystemExit(
            f"配置文件不存在: {args.config}\n"
            f"请参考 deploy_config.example.json 创建"
        )

    cfg = DeployConfig(args.config)

    if args.command == "up":
        cmd_up(cfg)
    elif args.command == "down":
        cmd_down(cfg)
    elif args.command == "sync":
        cmd_sync(cfg)
    elif args.command == "status":
        cmd_status(cfg)


if __name__ == "__main__":
    main()
