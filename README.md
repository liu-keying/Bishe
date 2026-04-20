# 基于“流媒体 API 风格”的应用层隐匿通道 Demo（Python）

链路：**B 像播放器一样走 HLS 形态** — `GET master.m3u8` → `GET index.m3u8` → `GET seg-xxxxxx.ts`（分片为类 TS 二进制 + 业务头）→ **Redis** → **B-worker** → **HTTP/HTTPS** → **C**。

- **A（本地源站）**：**`POST /proxy` 已关闭**；请用 **`POST /overlay/embed`**（multipart：`video` + `hidden`）将隐匿数据附在视频字节后入队。对外仍提供 **`master.m3u8` → `index.m3u8` → `seg-*.ts`**。
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

## 发送隐匿数据（视频载体）

### 媒体帧（`video` / `hidden` 换成你的真实路径）

```powershell
cd D:\PyProjects\Bishe
curl.exe -X POST "http://127.0.0.1:8000/overlay/embed?c=http://127.0.0.1:8002" -F "video=@cover.mp4" -F "hidden=@hidden.bin"
```

若文件不在当前目录，写绝对路径，例如 `-F "video=@D:\PyProjects\Bishe\cover.mp4"`。

说明：`hidden` 经 **尾部 MAGIC+长度+SHA256(hidden)** 附在 `video` 字节之后；B 拉片后由 worker **`extract_trailer`** 校验 SHA256 并拆出再 **POST** 到 C。

## 压测（端到端：A -> B -> C）

先确保 C / B / A 都已启动，然后执行：

```powershell
cd D:\PyProjects\Bishe
# 不需要 cover.mp4：默认自动生成 1MiB 随机载体（可用 --cover-bytes 调整）
python .\scripts\bench_overlay_embed.py --a-url http://127.0.0.1:8000 --c-url http://127.0.0.1:8002 --concurrency 5 --duration-s 10 --hidden-bytes 4096
```

### 若你的载体是切好的 HLS `.ts` 分片

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

## 后续扩展（建议）

- 调整 `b-pull` 轮询与分片大小，使 TLS 外可见的时序/体积更接近真实拉片。
- 多段 fMP4、真实 m3u8 索引与 GET 组合。
- Redis Streams、多 worker、重试与幂等。
