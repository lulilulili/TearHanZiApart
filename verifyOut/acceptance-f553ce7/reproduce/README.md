# 本轮运行脚本归档

这里保留实际运行的验证侧脚本，不属于算法主干。脚本按原运行布局解析路径：
在独立 checkout 中，将本目录 `.py` 复制到仓库的 `.local/protocol-f553ce7/`。
先准备与 `../identity.json` 指纹一致的字体和数据，再运行：

```powershell
python -X utf8 .local/protocol-f553ce7/run_acceptance.py prepare
python -X utf8 .local/protocol-f553ce7/run_acceptance.py run
python -X utf8 .local/protocol-f553ce7/final_check.py
```

使用新的独立 checkout，避免覆盖本次归档；脚本拒绝覆盖已有快照与阶段日志。
若同一轮进程中断，使用 `run --resume`，它校验源码、字体、数据、字集指纹，
检查已有逐字记录，保留已完成阶段，再续跑缺失记录。本轮在 686/958 处中断后
按此方式续跑；累计墙钟时间含中断，阶段 `seconds` 是续跑阶段耗时。

同机新旧定点对照分别使用 `--revision e4c9dfd`、`--revision f553ce7`：

```powershell
python -X utf8 .local/protocol-f553ce7/audit_cases.py --revision e4c9dfd --label old-targets --chars 還鄖橥碱搏博縛鎛睥啤碑稗萆蜱郫陴髀埤鱄事聿重量善畫甫聞門問間回國囚
python -X utf8 .local/protocol-f553ce7/audit_cases.py --revision f553ce7 --label head-targets --chars 還鄖橥碱搏博縛鎛睥啤碑稗萆蜱郫陴髀埤鱄事聿重量善畫甫聞門問間回國囚
python -X utf8 .local/protocol-f553ce7/audit_cases.py --revision e4c9dfd --label old-family-rest --chars 濞輻首鼽鼾齄導颦礴痺缴贾
python -X utf8 .local/protocol-f553ce7/audit_cases.py --revision f553ce7 --label head-family-rest --chars 濞輻首鼽鼾齄導颦礴痺缴贾
python -X utf8 .local/protocol-f553ce7/topology_audit.py
```

定点对照使用各版本独立 B 库，重用同一份字体和输入数据；标准批次则复制现有
B 库到隔离目录，并由原加载器校验签名。两种缓存起点不可混称为全程冷缓存。
原始逐笔结果留在 `.local/`，归档 JSONL 保存失败信息、动作记录与路径 SHA-256。

本轮跨字体阶段随后按用户要求停止：simhei 137 条、Noto 未开始。
`final_check.py` 读取明确的 `crossDeferredByUser` 状态，只验收用户保留的范围，
不会将这 137 条当作完整 320 字门。若未来恢复原范围，应先在新验收记录中
取消暂缓状态、完成全部跨字体结果，再运行完整检查；不要改写本次历史结论。
