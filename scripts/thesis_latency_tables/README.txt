时延汇总表（来源：scripts/bench_matrix_embed_hls.csv）

维度
  hidden_bytes_B : 1024, 4096, 65536
  k              : 1, 2, 3, 5
  segments_limit : 6, 9, 12, 15
  每格 3 次重复；表中为各次 e2e_ms_p50 / p95 / p99 的均值

正文推荐（分片数量对时延影响，对其余 k 平均）
  latency_segments_effect_by_hidden_all_k.csv
      三档 hidden × 分片 6/9/12/15，含相对 seg6 的 p50 增量与百分比

  latency_hidden1024_segments_avg_across_k.csv
      仅 1024B：seg6≈22360ms → seg15≈22708ms（数百 ms 级波动）

  latency_segments_x_hidden_p50_avg_k.csv
      宽表：行=分片数，列=三档 hidden 的 p50（ms）

全因子
  latency_full_long.csv — 48 格长表

固定 seg=6
  latency_seg6_hidden_x_k_p50_ms.csv — 与吞吐 seg6 表同构

与 5.2 吞吐对比
  latency_vs_throughput_by_seg_avg_k.csv

指标
  mean_e2e_ms_p50 : C /stats 的 e2e_ms.p50 均值
  mean_p95_ms     : 同上 p95

生成
  python scripts/gen_thesis_latency_tables.py
