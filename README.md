# 应用层隐匿通道 Demo（Python）

基于 HLS 视频流的应用层隐匿通信系统，支持两种运行模式：

| 模式 | 说明 |
|------|------|
| **隐匿数据注入** | 发送方将任意二进制数据加密嵌入 TS 分片，网关拉流提取后转发 服务端 解密 |
| **SOCKS5 代理** | client 作为 SOCKS5 入口，通过隐匿通道透明代理任意 TCP 流量（HTTP/HTTPS/…） |

---

## 架构

| 节点 | 默认端口 | 作用 |
|------|----------|------|
| **control** | 8009 | 控制面：client/server 密钥交换（RSA-OAEP + AES-256-GCM），下发 PSK + Token |
| **gateway** | 8010 (数据面) / 8011 (控制面) | 数据面网关：client、server 注册后启动双向拉流与转发 |
| **client** | 8000 (+ SOCKS5 1080) | HLS 源站 + 隐匿嵌入；可选 SOCKS5 代理入口 |
| **server** | 8002 | 接收端：解密统计；可选 TCP 出口（代理模式） |
| **Redis** | 6379 | 消息队列 + 密文分片拼装缓冲 |

### 数据流

```
# 模式 1: 隐匿数据注入（单向 client→server）
发送方 → POST client(/overlay/embed-hls) → client 加密嵌入 TS
       → gateway GET client(HLS) → 提取 → POST server(/recv) → server 解密

# 模式 2: SOCKS5 代理（双向）
客户端 → SOCKS5 client → 正向 HLS(/hls) → gateway → POST server(/recv)
                                                      ↓
                                                  server → TCP → 目标
                                                      ↓
客户端 ← client ← gateway ← 反向 HLS(/hls-rev) ← server 嵌入响应 ←┘
```

---

## 环境

- Python 3.10+（建议 3.11/3.12）
- Redis

```bash
pip install -r requirements.txt
```

```bash
docker run --rm -p 6379:6379 redis:7
```

---

## 分布式部署（deploy.py）

单机开发可以手动开 5 个终端。**多机部署**使用 `deploy.py`，一条命令完成全部操作。

### 快速开始

```bash
# 1. 复制示例配置，修改 IP
cp deploy_config.example.json deploy_config.json
vim deploy_config.json

# 2. 确认各机器 SSH 免密登录
ssh root@10.0.0.1 echo ok
ssh root@10.0.0.2 echo ok
ssh root@10.0.0.3 echo ok

# 3. 部署 + 启动（自动同步代码、安装依赖、启动全部节点）
python3 deploy.py up --config deploy_config.json

# 启动后控制机实时显示各节点日志，Ctrl+C 退出日志流（节点继续运行）
```

### 配置文件

```jsonc
{
  "token_secret": "bishe-dev-secret",   // control 和 gateway 必须一致
  "ssh_user": "root",                   // SSH 登录用户
  "ssh_key": "~/.ssh/id_rsa",          // SSH 私钥路径（可选，默认用 ssh-agent）
  "remote_dir": "/opt/bishe",          // 各机器上的部署目录
  "python": "python3",                 // 远程 Python 解释器

  "redis": {                           // 已有的 Redis 实例（不会自动启动）
    "host": "10.0.0.1",
    "port": 6379
  },

  "nodes": {                           // 四节点分配到哪些机器（可同机可不同机）
    "control": {"host": "10.0.0.1", "port": 8009},
    "gateway": {"host": "10.0.0.1", "port": 8010, "control_port": 8011},
    "server":  {"host": "10.0.0.2", "port": 8002},
    "client":  {"host": "10.0.0.3", "port": 8000, "socks_port": 0}
  },

  "mode": "inject",                    // "inject" = 隐匿注入 / "socks5" = SOCKS5 代理
  "session": "bishe-1",
  "queue": "bishe:overlay:queue",
  "queue_rev": "bishe:overlay:queue:rev",
  "reliability": true
}
```

### 全部子命令

| 命令 | 作用 |
|------|------|
| `python3 deploy.py up` | 同步代码 → 安装依赖 → 启动四节点 → 实时日志 |
| `python3 deploy.py down` | 停止全部节点 |
| `python3 deploy.py sync` | 仅同步代码，不重启（改代码后快速更新） |
| `python3 deploy.py status` | 检查各节点 /health 状态 |

### 脚本做了什么

`up` 命令的执行流程：

1. **解析配置** — 读取 JSON，自动推导各节点 URL
2. **同步代码** — 按 host 去重，并行 rsync 项目文件 + pip install 依赖
3. **按序启动** — control → gateway → server → client，每步先杀旧进程再 nohup 启动，写 PID 文件
4. **健康检查** — 每节点启动后 curl `/health` 确认存活
5. **日志流** — `ssh ... tail -F` 拉取四节点日志到控制机，带颜色前缀区分，Ctrl+C 退出

### 注意事项

- **Redis 不自动管理**：配置中的 Redis 地址需提前启动好，脚本不会帮你起 Redis
- **`token_secret` 要一致**：control 用它签发 Token，gateway 用它验签
- **`0.0.0.0` 监听**：脚本自动用 `--host 0.0.0.0`，确保远程可访问；注意防火墙
- **`deploy_config.json` 已加入 `.gitignore`**：防止密钥泄露

---

## 模式 1：隐匿数据注入

### 启动（5 个终端）

```bash
# 1) Redis
docker run --rm -p 6379:6379 redis:7

# 2) control（控制面）
python main.py control --host 127.0.0.1 --port 8009 --token-secret bishe-dev-secret

# 3) gateway
python main.py gateway --host 127.0.0.1 --port 8010 \
  --redis redis://127.0.0.1:6379/0 --queue bishe:overlay:queue \
  --token-secret bishe-dev-secret

# 4) server（接收端）
python main.py server --host 127.0.0.1 --port 8002 \
  --control-url http://127.0.0.1:8009 --client-url http://127.0.0.1:8000 \
  --gateway-url http://127.0.0.1:8010

# 5) client（源站）
python main.py client --host 127.0.0.1 --port 8000 --session bishe-1 \
  --control-url http://127.0.0.1:8009 --server-url http://127.0.0.1:8002 \
  --gateway-url http://127.0.0.1:8010
```

### 发送隐匿数据

```bash
curl -X POST "http://127.0.0.1:8000/overlay/embed-hls?k=1" \
  -F "segment=@hls_seg000.ts" \
  -F "hidden=@hidden.bin"
```

### 查看结果

```bash
curl http://127.0.0.1:8002/stats
# recv_count / recv_bytes 增加即表示端到端打通
```

### 密文分片参数

| 参数 | 含义 |
|------|------|
| `k` | 密文分片份数，`1 ≤ k ≤ n`；不写默认 `k=n` |
| `g_bytes` / `g_bits` | 每片密文槽位长度（字节）；默认 `⌈len(密文)/k⌉` |

`k=1` 时整段密文只占 1 个随机选中的 TS；`k=n` 时每片各带一段密文。

---

## 模式 2：SOCKS5 代理

client 启动时加 `--socks-port` 即可开启 SOCKS5 代理，通过隐匿通道透明转发任意 TCP 流量。

### 启动

```bash
# 1) Redis
docker run --rm -p 6379:6379 redis:7

# 2) control
python main.py control --host 127.0.0.1 --port 8009 --token-secret bishe-dev-secret

# 3) gateway（需要两个 Redis 队列：正向 + 反向）
python main.py gateway --host 127.0.0.1 --port 8010 \
  --redis redis://127.0.0.1:6379/0 \
  --queue bishe:overlay:queue \
  --queue-rev bishe:overlay:queue:rev \
  --token-secret bishe-dev-secret

# 4) server（出口节点，向目标发起真实 TCP 连接）
python main.py server --host 127.0.0.1 --port 8002 \
  --control-url http://127.0.0.1:8009 --client-url http://127.0.0.1:8000 \
  --gateway-url http://127.0.0.1:8010

# 5) client（入口节点，--socks-port 1080 开启 SOCKS5 代理）
python main.py client --host 127.0.0.1 --port 8000 --socks-port 1080 \
  --session bishe-1 \
  --control-url http://127.0.0.1:8009 --server-url http://127.0.0.1:8002 \
  --gateway-url http://127.0.0.1:8010
```

### 使用

```bash
# HTTP
curl --socks5 127.0.0.1:1080 http://httpbin.org/get

# HTTPS
curl --socks5 127.0.0.1:1080 https://www.example.com

# 浏览器：配置 SOCKS5 代理 127.0.0.1:1080
```

### SOCKS5 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--socks-port` | `0` | SOCKS5 端口，0=禁用 |
| `--socks-listen` | `127.0.0.1` | SOCKS5 监听地址 |
| `--no-socks` | `false` | 显式禁用 SOCKS5（client/server 角色） |
| `--queue-rev` | `bishe:overlay:queue:rev` | 反向 Redis 队列 key |
| `--socks-buffer-size` | `32768` | TCP 读缓冲字节数 |
| `--socks-connect-timeout` | `30` | 等待 server 建连超时秒数 |
| `--socks-idle-timeout` | `300` | 空闲连接超时秒数 |
| `--socks-max-conns` | `50` | 最大并发隧道连接数 |

---

## 分布式部署

各节点可部署在不同主机上，只需保证网络互通。以下为典型部署指南。

### 网络连通性

| 节点 | 需要被谁访问 | 需要访问谁 |
|------|-------------|-----------|
| **control** | client、server | 无 |
| **gateway** | client、server（注册/拉流） | client、server、Redis |
| **client** | gateway（拉流）、发送方（/overlay/embed-hls） | control、gateway |
| **server** | gateway（转发 /recv） | control、gateway、（目标 TCP） |
| **Redis** | gateway | 无 |

### 通用注意事项

- `--token-secret` 在 **control** 和 **gateway** 上必须一致
- `--host` 设为 `0.0.0.0` 监听所有网卡，或指定实际 IP
- `--control-url`、`--client-url`、`--server-url`、`--gateway-url` 使用对方可达的实际 IP/域名
- `--redis` 使用 gateway 可达的 Redis 地址
- 确保防火墙开放对应端口

### 示例一：两台机器（隐匿数据注入）

**机器 A**（`10.0.0.1`）：control + gateway + Redis  
**机器 B**（`10.0.0.2`）：client + server

```bash
# ===== 机器 A: 10.0.0.1 =====
# 1) Redis
docker run -d --restart unless-stopped -p 6379:6379 redis:7

# 2) control
python main.py control --host 0.0.0.0 --port 8009 --token-secret bishe-dev-secret

# 3) gateway（注：Redis 用本机地址，control/gateway 本身也在本机）
python main.py gateway --host 0.0.0.0 --port 8010 \
  --redis redis://127.0.0.1:6379/0 --queue bishe:overlay:queue \
  --token-secret bishe-dev-secret

# ===== 机器 B: 10.0.0.2 =====
# 4) server（所有 URL 指向机器 A 的对应服务）
python main.py server --host 0.0.0.0 --port 8002 \
  --control-url http://10.0.0.1:8009 --client-url http://10.0.0.2:8000 \
  --gateway-url http://10.0.0.1:8010

# 5) client
python main.py client --host 0.0.0.0 --port 8000 --session bishe-1 \
  --control-url http://10.0.0.1:8009 --server-url http://10.0.0.2:8002 \
  --gateway-url http://10.0.0.1:8010
```

### 示例二：三台机器（SOCKS5 代理）

**机器 A**（`10.0.0.1`）：gateway + Redis  
**机器 B**（`10.0.0.2`）：client（SOCKS5 入口）  
**机器 C**（`10.0.0.3`）：server + control

```bash
# ===== 机器 A: 10.0.0.1 (gateway) =====
docker run -d --restart unless-stopped -p 6379:6379 redis:7

python main.py gateway --host 0.0.0.0 --port 8010 \
  --redis redis://127.0.0.1:6379/0 \
  --queue bishe:overlay:queue \
  --queue-rev bishe:overlay:queue:rev \
  --token-secret bishe-dev-secret

# ===== 机器 B: 10.0.0.2 (client) =====
python main.py client --host 0.0.0.0 --port 8000 --socks-port 1080 \
  --session bishe-1 \
  --control-url http://10.0.0.3:8009 \
  --server-url http://10.0.0.3:8002 \
  --gateway-url http://10.0.0.1:8010

# ===== 机器 C: 10.0.0.3 (control + server) =====
python main.py control --host 0.0.0.0 --port 8009 --token-secret bishe-dev-secret

python main.py server --host 0.0.0.0 --port 8002 \
  --control-url http://10.0.0.3:8009 \
  --client-url http://10.0.0.2:8000 \
  --gateway-url http://10.0.0.1:8010
```

在机器 B 或同一网络的任意主机上使用 SOCKS5 代理：

```bash
curl --socks5 10.0.0.2:1080 http://httpbin.org/get
```

### 示例三：完全分布式 + HTTPS

所有节点各占一台机器，server 启用 HTTPS。

**机器 A**（`10.0.0.1`）：control  
**机器 B**（`10.0.0.2`）：gateway + Redis  
**机器 C**（`10.0.0.3`）：client  
**机器 D**（`10.0.0.4`）：server（HTTPS）

```bash
# ===== 机器 A: control =====
python main.py control --host 0.0.0.0 --port 8009 --token-secret bishe-dev-secret

# ===== 机器 B: gateway + Redis =====
docker run -d --restart unless-stopped -p 6379:6379 redis:7

python main.py gateway --host 0.0.0.0 --port 8010 \
  --redis redis://127.0.0.1:6379/0 \
  --queue bishe:overlay:queue \
  --queue-rev bishe:overlay:queue:rev \
  --token-secret bishe-dev-secret \
  --gateway-callback-url http://10.0.0.2:8011/overlay/recv-ack

# ===== 机器 C: client =====
python main.py client --host 0.0.0.0 --port 8000 --socks-port 1080 \
  --session bishe-1 \
  --control-url http://10.0.0.1:8009 \
  --server-url https://10.0.0.4:8443 \
  --gateway-url http://10.0.0.2:8010

# ===== 机器 D: server (HTTPS) =====
# 先生成证书
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
  -days 365 -nodes -subj "/CN=10.0.0.4"

python main.py server --host 0.0.0.0 --port 8443 \
  --ssl-cert cert.pem --ssl-key key.pem \
  --control-url http://10.0.0.1:8009 \
  --client-url http://10.0.0.3:8000 \
  --gateway-url http://10.0.0.2:8010 \
  --gateway-callback-url http://10.0.0.2:8011/overlay/recv-ack
```

### 部署策略参考

| 场景 | 推荐部署方式 |
|------|-------------|
| 本地开发/调试 | 全部 localhost + 5 个终端 |
| 内网穿透测试 | Redis + gateway + control 在一台；client 和 server 分置两端 |
| 公网隐匿代理 | client 在本地 PC；gateway + Redis 在跳板 VPS；server + control 在出口 VPS |
| 最小延迟 | Redis 与 gateway 同机部署；control 可合设于 gateway 或 server |

---

## server 使用 HTTPS（可选）

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"

python main.py server --host 127.0.0.1 --port 8443 --ssl-cert cert.pem --ssl-key key.pem \
  --control-url http://127.0.0.1:8009 --client-url http://127.0.0.1:8000 \
  --gateway-url http://127.0.0.1:8010
```

client 的 `--server-url` 相应改为 `https://127.0.0.1:8443`。

---

## 可靠性回执（可选）

server 解密后通知 gateway，gateway 超时重发：

```bash
# server 加 --gateway-callback-url
python main.py server ... --gateway-callback-url http://127.0.0.1:8011/overlay/recv-ack

# gateway 不加 --no-reliability（默认启用可靠性）
python main.py gateway ...
```

关闭可靠性（简单演示用）：

```bash
python main.py gateway ... --no-reliability
```

---

## 停止链路

```bash
curl -X POST http://127.0.0.1:8010/stop \
  -H "Content-Type: application/json" \
  -d '{"role":"client","token":"<token>"}'
```

---

## 端口一览

| 服务 | 默认端口 |
|------|----------|
| client | 8000 |
| client (SOCKS5) | 1080 |
| server | 8002 |
| control | 8009 |
| gateway | 8010 |
| gateway 控制面 | 8011 |
| Redis | 6379 |

---

## 项目结构

```
bishe_demo/
├── tunnel.py          # SOCKS5 隧道数据模型 + server 侧 TCP 出口
├── hls_shared.py      # 共享 HLS 服务（client 正向 /hls、server 反向 /hls-rev 复用）
├── client_proxy.py    # client 节点：HLS 源站 + 隐匿嵌入 + SOCKS5 入口
├── gateway.py         # gateway：注册编排，启动双向 pull+worker
├── hls_puller.py      # HLS 拉流客户端
├── stego_worker.py    # stego 提取 + 密文拼装 + 转发
├── reliability.py     # 可靠性：server 回执、超时重发
├── server.py          # server 节点：接收解密 + 反向 HLS + TCP 出口
├── control_server.py  # control 节点：密钥协商 + token 签发
├── stego.py           # 隐匿嵌入/提取算法（psk_hmac_inplace）
├── psk_aead.py        # PSK1 格式 AEAD 加解密
├── crypto_box.py      # RSA 密钥对、OAEP 封装、AEAD
├── token_util.py      # HMAC 签名 token
├── common.py          # OverlayEnvelope + HLS 路径工具
└── hls_facade.py      # m3u8 播放列表解析
main.py                # 统一入口
```

---

## 技术要点

### 隐匿嵌入（stego）

采用 `psk_hmac_inplace` 方案：
1. 用 HMAC(token, hls_index, frag_idx) 派生 16 字节 tag 和伪随机偏移量
2. 在 TS 分片内偏移处等长写入 `tag || cipher_fragment`
3. 不改变分片总长度，外观仍为合法 TS 流
4. 非承载分片写入等长随机填充，外观与承载片一致

### 端到端加密

`msg_id(16B) || "PSK1"(4B) || nonce(12B) || AES-256-GCM(body, aad=session_id)`

### SOCKS5 隧道

- 握手阶段通过正向 HLS 发送 `TunnelCtl(connect)` 到 server
- server 建立到目标的 TCP 连接后通过反向 HLS 回传 `TunnelCtl(connected)`
- 数据阶段：双向 TCP 字节流经 PSK1 AEAD 加密后嵌入 HLS 分片转发
- 每个 TCP 连接分配唯一 `conn_id`（UUID），贯穿正反向通道

### 会话管理

client 每消费一片分片递增 `session_id`（`bishe-1` → `bishe-2` → …），gateway 通过 `X-Next-Session` 响应头自动跟随轮换。
