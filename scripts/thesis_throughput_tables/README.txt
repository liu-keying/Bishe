吞吐量汇总表（来源：scripts/bench_matrix_embed_hls.csv）

维度
  hidden_bytes_B : 1024, 4096, 65536
  k              : 1, 2, 3, 5
  segments_limit : 6, 9, 12, 15
  每格 3 次重复，表中为均值（部分文件含标准差）

文件说明
  throughput_full_long.csv
      推荐：48 行全因子长表，含吞吐、post_qps、sent_ok、e2e_p50

  throughput_all_configs_summary.csv
      与 full_long 相同，便于导入 Excel 做透视

  throughput_seg{N}_hidden_x_k_Bps.csv  (N=6,9,12,15)
      宽表：行=hidden，列=k，固定分片数

  throughput_hidden{H}_seg_x_k_Bps.csv  (H=1024,4096,65536)
      宽表：行=segments_limit，列=k，固定载荷

  throughput_seg6_hidden_x_k_Bps.csv
      仅 seg=6 的 3×4 主表（正文常用）

指标
  mean_throughput_Bps : C /stats 的 throughput_bytes_per_s.avg 均值
  mean_post_qps       : bench 的 sent_ok/elapsed（含 drain，非纯发送窗）

分片数影响（结论：增加 HLS 分片数，吞吐下降；小载荷更明显）
  throughput_k1_hidden_x_segments_Bps.csv
      k=1，行=hidden，列=seg6/9/12/15 及相对 seg6 的百分比
  throughput_k1_segments_x_hidden_Bps.csv
      k=1，行=segments_limit，列=三档 hidden（正文表推荐）
  throughput_hidden1024_segments_x_k_Bps.csv
      hidden=1024，行=seg，列=k（说明小载荷下各 k 均下降）
  throughput_segments_effect_by_hidden_all_k.csv
      各 hidden 在 seg6→15 上全 k 平均及降幅百分比
  throughput_k1_segments_long.csv
      k=1 长表，含标准差与 post_qps
