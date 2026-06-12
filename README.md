# 应用层隐匿通道 Demo（Python）

基于 HLS 视频流的应用层隐匿通信系统，支持两种运行模式：

| 模式 | 说明 |
|------|------|
| **隐匿数据注入** | 发送方将任意二进制数据加密嵌入 TS 分片，B 拉流提取后转发 C 解密 |
| **SOCKS5 代理** | A 作为 SOCKS5 入口，通过隐匿通道透明代理任意 TCP 流量（HTTP/HTTPS/…） |

---

## 架构

| 节点 | 默认端口 | 作用 |
|------|----------|------|
| **E** | 8009 | 控制面：A/C 密钥交换（RSA-OAEP + AES-256-GCM），下发 PSK + Token |
| **B-gate** | 8010 (数据面) / 8011 (控制面) | 数据面网关：A、C 注册后启动双向拉流与转发 |
| **A** | 8000 (+ SOCKS5 1080) | HLS 源站 + 隐匿嵌入；可选 SOCKS5 代理入口 |
| **C** | 8002 | 接收端：解密统计；可选 TCP 出口（代理模式） |
| **Redis** | 6379 | 消息队列 + 密文分片拼装缓冲 |

### 数据流

```
# 模式 1: 隐匿数据注入（单向 A→C）
发送方 → POST A(/overlay/embed-hls) → A 加密嵌入 TS
       → B-gate GET A(HLS) → 提取 → POST C(/recv) → C 解密

# 模式 2: SOCKS5 代理（双向）
客户端 → SOCKS5 A → 正向 HLS(/hls) → B-gate → POST C(/recv)
                                                      ↓
                                                  C → TCP → 目标
                                                      ↓
客户端 ← A ← B-gate ← 反向 HLS(/hls-rev) ← C 嵌入响应 ←┘
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

## 模式 1：隐匿数据注入

### 启动（5 个终端）

```bash
# 1) Redis
docker run --rm -p 6379:6379 redis:7

# 2) E（控制面）
python main.py e --host 127.0.0.1 --port 8009 --token-secret bishe-dev-secret

# 3) B-gate
python main.py b-gate --host 127.0.0.1 --port 8010 \
  --redis redis://127.0.0.1:6379/0 --queue bishe:overlay:queue \
  --token-secret bishe-dev-secret

# 4) C（接收端）
python main.py c --host 127.0.0.1 --port 8002 \
  --e-url http://127.0.0.1:8009 --a-url http://127.0.0.1:8000 \
  --b-gate-url http://127.0.0.1:8010

# 5) A（源站）
python main.py a --host 127.0.0.1 --port 8000 --session bishe-1 \
  --e-url http://127.0.0.1:8009 --c-url http://127.0.0.1:8002 \
  --b-gate-url http://127.0.0.1:8010
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

A 启动时加 `--socks-port` 即可开启 SOCKS5 代理，通过隐匿通道透明转发任意 TCP 流量。

### 启动

```bash
# 1) Redis
docker run --rm -p 6379:6379 redis:7

# 2) E
python main.py e --host 127.0.0.1 --port 8009 --token-secret bishe-dev-secret

# 3) B-gate（需要两个 Redis 队列：正向 + 反向）
python main.py b-gate --host 127.0.0.1 --port 8010 \
  --redis redis://127.0.0.1:6379/0 \
  --queue bishe:overlay:queue \
  --queue-rev bishe:overlay:queue:rev \
  --token-secret bishe-dev-secret

# 4) C（出口节点，向目标发起真实 TCP 连接）
python main.py c --host 127.0.0.1 --port 8002 \
  --e-url http://127.0.0.1:8009 --a-url http://127.0.0.1:8000 \
  --b-gate-url http://127.0.0.1:8010

# 5) A（入口节点，--socks-port 1080 开启 SOCKS5 代理）
python main.py a --host 127.0.0.1 --port 8000 --socks-port 1080 \
  --session bishe-1 \
  --e-url http://127.0.0.1:8009 --c-url http://127.0.0.1:8002 \
  --b-gate-url http://127.0.0.1:8010
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
| `--no-socks` | `false` | 显式禁用 SOCKS5（A/C 角色） |
| `--queue-rev` | `bishe:overlay:queue:rev` | 反向 Redis 队列 key |
| `--socks-buffer-size` | `32768` | TCP 读缓冲字节数 |
| `--socks-connect-timeout` | `30` | 等待 C 建连超时秒数 |
| `--socks-idle-timeout` | `300` | 空闲连接超时秒数 |
| `--socks-max-conns` | `50` | 最大并发隧道连接数 |

---

## C 使用 HTTPS（可选）

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"

python main.py c --host 127.0.0.1 --port 8443 --ssl-cert cert.pem --ssl-key key.pem \
  --e-url http://127.0.0.1:8009 --a-url http://127.0.0.1:8000 \
  --b-gate-url http://127.0.0.1:8010
```

A 的 `--c-url` 相应改为 `https://127.0.0.1:8443`。

---

## 可靠性回执（可选）

C 解密后通知 B，B 超时重发：

```bash
# C 加 --b-callback-url
python main.py c ... --b-callback-url http://127.0.0.1:8011/overlay/recv-ack

# B-gate 不加 --no-reliability（默认启用可靠性）
python main.py b-gate ...
```

关闭可靠性（简单演示用）：

```bash
python main.py b-gate ... --no-reliability
```

---

## 停止链路

```bash
curl -X POST http://127.0.0.1:8010/stop \
  -H "Content-Type: application/json" \
  -d '{"role":"a","token":"<token>"}'
```

---

## 端口一览

| 服务 | 默认端口 |
|------|----------|
| A | 8000 |
| A (SOCKS5) | 1080 |
| C | 8002 |
| E | 8009 |
| B-gate | 8010 |
| B-gate 控制面 | 8011 |
| Redis | 6379 |

---

## 项目结构

```
bishe_demo/
├── tunnel.py          # SOCKS5 隧道数据模型 + C 侧 TCP 出口
├── hls_shared.py      # 共享 HLS 服务（A 正向 /hls、C 反向 /hls-rev 复用）
├── a_client_proxy.py  # A 节点：HLS 源站 + 隐匿嵌入 + SOCKS5 入口
├── b_gate_pull.py     # B-gate：注册编排，启动双向 pull+worker
├── b_pull.py          # B-pull：HLS 客户端拉流
├── b_worker.py        # B-worker：stego 提取 + 密文拼装 + 转发
├── b_reliability.py   # 可靠性：C 回执、超时重发
├── c_server.py        # C 节点：接收解密 + 反向 HLS + TCP 出口
├── e_server.py        # E 节点：密钥协商 + token 签发
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

- 握手阶段通过正向 HLS 发送 `TunnelCtl(connect)` 到 C
- C 建立到目标的 TCP 连接后通过反向 HLS 回传 `TunnelCtl(connected)`
- 数据阶段：双向 TCP 字节流经 PSK1 AEAD 加密后嵌入 HLS 分片转发
- 每个 TCP 连接分配唯一 `conn_id`（UUID），贯穿正反向通道

### 会话管理

A 每消费一片分片递增 `session_id`（`bishe-1` → `bishe-2` → …），B-gate 通过 `X-Next-Session` 响应头自动跟随轮换。