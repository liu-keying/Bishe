# 基于“流媒体 API 风格”的应用层隐匿通道 Demo（Python）

链路：**B 像播放器一样走 HLS 形态** — `GET master.m3u8` → `GET index.m3u8` → `GET seg-xxxxxx.ts`（分片为类 TS 二进制 + 业务头）→ **Redis** → **B-worker** → **HTTP/HTTPS** → **C**。

- **A（本地源站）**：**`POST /proxy` 已关闭**；请用 **`POST /overlay/embed-hls`**（multipart：多个 `segment` + `hidden`）将隐匿数据附在 **最后一段 TS** 字节后入队。对外仍提供 **`master.m3u8` → `index.m3u8` → `seg-*.ts`**。
- **B**：
  - `b-pull`：解析 m3u8 文本得到 URI，**按 HLS 习惯拉列表再拉分片**，拼 `OverlayEnvelope` 后写入 Redis。
  - `b-worker`：从 Redis 取出并 **POST** 到 C（`--c-url` 可为 `https://...`，默认校验证书关闭便于自签）。
- **C**：`POST /recv` 接收；可选 **HTTPS**（`--ssl-cert` + `--ssl-key`）。

**注意**：`--session` 在 **A** 与 **b-pull** 上起始值必须一致，格式为 **`bishe-<数字>`**（默认 `bishe-1`）。**每完成一次媒体分片出队**（媒介 `seg` 从 A 出队）后，A 将当前会话递增为 `bishe-2`、`bishe-3`…，并在分片响应头里带 **`X-Next-Session`**；**b-pull** 会自动跟随。

## 依赖

- Python 3.10+（建议 3.11/3.12）
- Redis（本机或容器均可）

```bash
pip install -r requirements.txt
```

```bash
docker run --rm -p 6379:6379 redis:7
```

## 启动四个进程

### 1) 启动 C（HTTP）

```bash
python main.py c --host 127.0.0.1 --port 8002
```

### 2) 启动 B pull（轮询 GET A → Redis）

```bash
python main.py b-pull --a-url http://127.0.0.1:8000 --session bishe-1 --redis redis://127.0.0.1:6379/0 --queue bishe:overlay:queue
```

### 3) 启动 B worker（Redis → C）

```bash
python main.py b-worker --redis redis://127.0.0.1:6379/0 --queue bishe:overlay:queue --c-url http://127.0.0.1:8002
```

### 4) 启动 A（被动出流）

```bash
python main.py a --host 127.0.0.1 --port 8000 --session bishe-1
```

## 发送隐匿数据（TS 分片载体）

### 多段 TS（`segment` 可重复字段，`hidden` 一份）

```powershell
cd D:\PyProjects\Bishe
curl.exe -X POST "http://127.0.0.1:8000/overlay/embed-hls?c=http://127.0.0.1:8002" `
  -F "segment=@.\out\hls_seg000.ts" `
  -F "segment=@.\out\hls_seg001.ts" `
  -F "hidden=@hidden.bin"
```

若文件不在当前目录，写绝对路径。

说明：`hidden`（若启用 E 下发的 PSK，则会先被 **AEAD 加密**）再经 **尾部 MAGIC+长度** 附在 **最后一段 TS** 字节之后；B 拉片后由 worker **`extract_trailer`** 拆出并 **POST** 到 C，C 再用 PSK **AEAD 解密**得到原始 `hidden`。

可选（实验）：`?k=3` 将 **整段 AEAD 密文**均分为 3 份，随机选择 3 个 TS 分片分别嵌入；`?pad_bytes=188` 可在未携带密文的分片末尾追加固定长度伪 TS 填充（默认 0）。当 `k>1` 时，**B-worker** 用 Redis（`BISHE_REDIS`，与拉队相同）按 `cipher_group` 收齐分片、**拼接成整段密文**后再 POST 给 C；C 只做 **一次** AEAD 解密。

## 压测（端到端：A -> B -> C）

先确保 C / B / A 都已启动，然后执行：

```powershell
cd D:\PyProjects\Bishe
python .\scripts\bench_overlay_embed_hls.py --segments ".\out\hls_seg*.ts" --a-url http://127.0.0.1:8000 --c-url http://127.0.0.1:8002 --concurrency 1 --duration-s 10 --hidden-bytes 4096
```

说明：脚本会在发送结束后轮询 `C /stats`，直到 **全部到达** 或达到 `--drain-timeout-s`，且在 `--idle-s` 时间内无新增到达则判定稳定；最终以此计算 **最终丢失率**。

## 批量跑矩阵并输出 CSV

按 `segments-limit × hidden-bytes × repeats` 批量运行 `bench_overlay_embed_hls.py`，并写出 CSV 方便用 Excel/Origin 画曲线：

```powershell
cd D:\PyProjects\Bishe
python .\scripts\batch_bench_matrix.py --mode embed-hls --segments ".\hls_seg*.ts" --segments-limits 3,9,18 --hidden-bytes-list 1024,4096,65536 --repeats 3 --duration-s 5 --drain-timeout-s 900 --idle-s 60 --out bench_results.csv
```

汇总（按 `segments_limit,hidden_bytes` 分组输出均值/标准差/方差）：

```powershell
cd D:\PyProjects\Bishe
python .\scripts\summarize_bench_csv.py --in bench_results.csv --out bench_results_summary.csv
```

## C 使用 HTTPS（示例）

自签证书（示例）：

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"
```

```bash
python main.py c --host 127.0.0.1 --port 8443 --ssl-cert cert.pem --ssl-key key.pem
```

```bash
python main.py b-worker --c-url https://127.0.0.1:8443 ...
```

## 可靠性（C 回执 / B 超时重发 / 分片超时通知 A）

启用后需同时配置 **B 控制面** 与 **C 回执 URL**：

```powershell
# B-worker（控制面默认 8011）
python main.py b-worker --c-url http://127.0.0.1:8002 --control-port 8011

# C 须指向 B 的 recv-ack
python main.py c --port 8002 --psk-hex <64hex> --b-callback-url http://127.0.0.1:8011/overlay/recv-ack
```

行为概要：

- **C AEAD 解密失败**：`POST /overlay/recv-ack`（`ok=false`）→ B 调 A `/overlay/error-notice` 重入队媒体。
- **C 解密成功**：`ok=true` 回执 → B 清除 pending。
- **B 超时未收到回执**：按 `--c-ack-timeout-s` 重发 `POST /recv`（最多 `--c-resend-max` 次），仍失败则通知 A。
- **B 超时未收齐 k 个密文分片**：按 `--frag-assembly-timeout-s` 通知 A 重传。

关闭：`python main.py b-worker --no-reliability`（C 可不设 `--b-callback-url`）。

## 后续扩展（建议）

- 调整 `b-pull` 轮询与分片大小，使 TLS 外可见的时序/体积更接近真实拉片。
- 多段 fMP4、真实 m3u8 索引与 GET 组合。
- Redis Streams、多 worker、幂等（部分已由可靠性模块覆盖）。
