# G8 序保持互换撤除实验（矩阵归并 2a）采纳字归档

生成 2026-09-14 03:06。两态逐笔切割终态 SVG（逐笔填色，bench/review.html 同款画法）+ verifyChar fails 与 G8 决策迹对比。全库枚举后回退阀已扳回执行态为默认（通过→失败 9 字，见 docs/矩阵归并设计.md 实施记录）；本归档为 2a 出口物与 2b 硬前置。

## HarmonyOS_Sans_SC.ttf 攮

- **执行态 ARB_G8_EXEC=True（默认，回退阀后）**：verifyChar=AREA+ORDER → `攮_HarmonyOS_Sans_SC_exec.svg`
  - ORDER: 8-13x
  - AREA: 6:3%→1%
  - G8 迹: `{"level": "G8", "strokes": [8, 13], "moves": [[8, 17, 11], [13, 11, 17]], "adopted": true, "evidence": {"axis": "x", "dK": 237.8, "dT": -131.7, "oldC": 67.6, "newC": 56.6}}`
- **撤除态 ARB_G8_EXEC=False（诊断实验）**：verifyChar=AREA → `攮_HarmonyOS_Sans_SC_demote.svg`
  - AREA: 5:4%→0%
  - G8 迹: `{"level": "G8", "strokes": [8, 13], "from": [17, 11], "action": "demoted", "adopted": false, "evidence": {"axis": "x", "dK": 237.8, "dT": -131.7, "oldC": 67.6, "newC": 56.6, "wouldSwap": [[8, 17, 11], [13, 11, 17]]}}`

## simsun.ttc 銲

- **执行态 ARB_G8_EXEC=True（默认，回退阀后）**：verifyChar=AREA+TYPE → `銲_simsun_exec.svg`
  - TYPE: 7:横轴偏44°
  - AREA: 1:3%→14%
  - G8 迹: `{"level": "G8", "strokes": [3, 7], "moves": [[3, 5, 4], [7, 4, 5]], "adopted": true, "evidence": {"axis": "y", "dK": -252.4, "dT": 73.5, "oldC": 148.9, "newC": 88.9}}`
- **撤除态 ARB_G8_EXEC=False（诊断实验）**：verifyChar=AREA+TYPE → `銲_simsun_demote.svg`
  - TYPE: 3:横轴偏52°
  - AREA: 1:3%→14%
  - G8 迹: `{"level": "G8", "strokes": [3, 7], "from": [5, 4], "action": "demoted", "adopted": false, "evidence": {"axis": "y", "dK": -252.4, "dT": 73.5, "oldC": 148.9, "newC": 88.9, "wouldSwap": [[3, 5, 4], [7, 4, 5]]}}`
  - G8 迹: `{"level": "G8", "strokes": [7, 12], "from": [4, 1], "action": "costVeto", "adopted": false, "evidence": {"axis": "y", "dK": 264.6, "dT": -70.8, "oldC": 100.1, "newC": 260.3}}`
