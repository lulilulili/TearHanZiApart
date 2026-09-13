# f553ce7 协作验证（移交单 #3）

验证对象：`f553ce730d4f69d9553c02e32e7c79ac8075cfb0`。
协议：[`docs/协作验证协议.md`](../../docs/协作验证协议.md)。

本目录只包含验证侧产物。算法源来自 `git archive` 的固定快照；字体、
MakeMeAHanzi/拆字数据复制到快照数据目录，B 库独立，不与工作树共享写入。
`identity.json` 记录完整模块/规则、数据、字体和字集 SHA-256。

958 字与 320 字直接取自该提交附带的运行目录 `chars.txt`。协议正文提及的
`verifyOut/sampleChars.txt`、`sampleChars30.txt` 在本工作树不存在，故采用提交
已经固定的实际字集，未另选样本。958 字的 SHA-256 为
`639ab392bba57e8d202677fd20eb003861378626f010bd1ec509b4045f6f2cda`。

## 标准门

| 门 | 验收要求 | 本轮结果 |
|---|---|---|
| 鸿蒙 SC 958 | 不低于提交声明 920/957 = 96.13% | **920/957 = 96.13%，通过** |
| bench | 65 例，verified 回退/错误 = 0 | **65 例，0 破坏，通过** |
| simhei 320 | 基线 283/319；±2 字噪声容限，逐字检查 | **用户要求暂缓，未验收** |
| Noto SC 320 | 上轮 238/320，本提交声明 239/320；逐字检查 | **用户要求暂缓，未验收** |

全量只在协议要求的里程碑运行；本轮移交单 #3 未单独设全量达标数。
本次标准门不把 958 抽样外推为 9,570 字的全量成绩。

2026-09-13 用户要求“跨字验收暂且不管了”，因此终止本任务跨字体批次。
simhei 留下 137 条完整记录，Noto 尚未开始；这些部分记录只供续跑，不用于
通过率或泛化结论。`status.json` 明确记录 `crossDeferredByUser=true`。

SC 抽样相对提交附带的同字集结果，无修复/回退/失败码迁移；失败码分布
TYPE 18、AREA 19、ORDER 7、OVERLAP 2；ERROR/UNION/SPLIT 均为 0。
软指标 compQuota 0、compOut 33、orderX 95、reclass 4064，均与提交结果一致。
逐字差异见 [`sample-comparison.json`](sample-comparison.json)。

将上轮 `acceptance-e4c9dfd` 全量归档限定到同一 958 字后，历史对比为
918→920：翻绿“搏睥缴贾”，翻红“還鄖”；详见
[`sample-vs-historical-e4c9dfd.json`](sample-vs-historical-e4c9dfd.json)。后两字
已做下文同机新旧代码复核，不能将历史翻红直接认定为 G8.5 引入的回退。
相对历史抽样，compOut 32→33、orderX 99→95、reclass 4055→4064。

bench 的 65 例中仅 5 例金标准标记为 verified（鸿蒙 SC“口中天日国”）。
0 破坏表示该门满足要求，不表示其余 unverified/known_bad 已经分割正确；
完整逐例 IoU 差异保留在 `bench.log`，本轮未重新 seed 或改变 golden 状态。
本次输出的目检页为 [`bench-review.html`](bench-review.html)。

## 命令与结果校验

在隔离源码与数据目录中调用原有入口（`<out>` 指向本目录）：

```powershell
python -X utf8 -u tools/verify_batch.py --root <snapshot> --preset sample --chars-file <out>/sample-chars.txt --jobs 6 --out <out>/sample --no-resume
python -X utf8 -u -m strokelab.bench --root <snapshot> --check
python -X utf8 -u tools/verify_batch.py --root <snapshot> --preset cross --chars-file <out>/cross-chars.txt --jobs 6 --out <out>/cross --no-resume
```

`status.json` 记录各阶段命令、退出码和耗时，日志分别为 `sample.log`、
`bench.log`、`cross.log`。不依据批次脚本的零退出码直接判通过：该脚本在
单字失败时也可能返回 0，需要独立检查逐字结果。

验证侧新增 `tools/check_protocol_results.py`，拒绝重复、缺项、缺字状态变化，
并逐字列出修复/回退/失败码迁移。它不修改 `strokelab.verify` 的任何规则。
聚合数字门通过以后，回退字和迁移字仍须进行对抗复核。

实际运行脚本与续跑说明保留于 [`reproduce/`](reproduce/README.md)，依赖版本
见 `dependencies.json`。标准批次使用隔离目录中的签名校验缓存，定点 A/B
使用各版本独立冷缓存，两者不混称为全程冷启动。

## 定点归因与对抗审查

45 个不同字的同机新旧版本对照全部完成：16/45 → 30/45，确认 14 字修复，
没有 PASS→FAIL 回退。修复字为“博啤搏睥碑稗縛萆蜱郫鎛陴缴贾”。
两批逐字对照见 `audit/target-diff.json` 与 `audit/family-rest-diff.json`。

- D 型探测保留 `offBar==1`，本轮“碱”未触发且通过。执行器对计划内正主
  仍检查重置后 `strokeGroupCost<=40`；D/C 型免代价检查的实际对象是被逐笔。
  空组断言不检查逐笔身份正确，不能把它当作被逐笔的语义正确性证明。
- `ladderTouched` 同时包含动作的旧组、新组，G9 会跳过这些整组，范围
  不限于移动笔。已有定点测试未发现健康边界字回退，但这项宽豁免必须在
  后续新字族中继续审查；本轮没有改写算法来消除该风险。

- `e4c9dfd` 到 `f553ce7^` 的 `strokelab/` 无 diff，四个中间提交仅有工具、
  文档或结果。不能把同机旧结果差异归因为这四个提交的算法变化。
- 同机冷缓存复核：旧版与新版均复现“還”AREA、“鄖”TYPE，且新版两字
  `ladderRealign=[]`；两字逐笔路径 SHA-256 也完全一致。“橥、碱”的逐笔
  路径同样完全一致。历史归档与同机重跑的差异仍须区分环境/输入/缓存，
  不能据此宣称已确定具体漂移来源。
- `outlineCenterline` 在矩形、环、十字、短 T、不连通区域、空区域的
  新旧输出逐点相同（6/6），抽取出的图构建语句 AST 也相同。SC、simhei、
  Noto 三字体的“日口田中王土”拓扑声明独立复现 18/18，且这 18 例的
  新旧中轴输出也逐点相同。新增
  `medialJunctions` 对 MultiPolygon 抛出
  `AttributeError`，短真实 T 分支在原尺度漏检、放大 2 倍后检出。
  它尚未接入仲裁；在补全输入契约与尺度/组级标定之前不得升为硬门。
  短 T 反例为 `box(0,400,600,460).union(box(270,200,330,430))`；不连通
  反例为 `box(0,0,100,50).union(box(200,0,300,50))`。完整结果见
  [`audit/topology.json`](audit/topology.json)。
- `ladderRealign` 会在二遍被采纳后继承第一遍记录；“睥”实测记录中的
  第 5 笔目标组为 5，最终二遍组为 8。该字段是历史动作，不能直接作为
  最终归属断言，也不能单独用于统计最终执行采纳率。

逐笔目检图：[`visual-review.png`](visual-review.png)（同时保留 SVG）。
“搏、睥”的横条归属改善可见；“髀”TYPE→OVERLAP、“埤”减少 ORDER 但
保留 TYPE/AREA，均不得记为整字修复。

## 状态

用户调整后的本轮范围已完成：SC 抽样、bench、45 字同机对照与对抗审查通过，
跨字体门暂缓，移交单 #3 不标记为全部门已勾销。未改生产算法或 golden，
确认的残余与风险已交接到下一步计划。最终完整性核验见
[`final-check.json`](final-check.json)：快照/字体/数据/字集指纹未变，SC 的逐字
失败详情与全部指标也和提交附带结果一致。验证侧检查工具 7 项测试通过。
