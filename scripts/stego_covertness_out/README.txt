stego_covertness_ts 输出
segments: 'scripts/_test_ts/hls_seg*.ts' limit=6
psk_runs: 1
chi2 homogeneity: active-byte m×2 table, merge adjacent bins until E>=5, df=merged_bins-1
KL/JS: D_KL(stego||cover) 与 JS(cover,stego)，单位 bit；越小越隐蔽
keys_manifest.json: 各次 PSK/token
metrics_summary_across_psk.csv: 多密钥汇总（论文表）
