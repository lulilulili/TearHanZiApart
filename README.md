# TearHanZiApart · 汉字矢量笔画拆解

基于 [MakeMeAHanzi](https://github.com/skishore/makemeahanzi)（文鼎楷体基准）对任意矢量中文字体做**纯矢量**笔画拆解——全程无像素/SDF 判定。**算法为 Python 包 `strokelab`（唯一算法源）**，附浏览器可视化前端。

## 硬性保证

**所有笔画的并集与原字形恒等（不多不少）**：保留区段是原字形贝塞尔段经 De Casteljau 的精确切片；交叠区双重归属；shapely 布尔收口（裁剪+残差回填）作为最后一道闸。实测 9 字体 × 17 字全部严格达标（平均覆盖 99.97%、溢出 0.00%）。

## 流程（页面 S1–S6 可视化）

1. **建库**：A库 = 文鼎楷体标准笔画；B库 = 目标字体标准笔画（U+31C0–31E5 官方笔画区，或按"32类笔画×独立/少相交代表字"规则表从孤立连通组拆取，尺度不变形状描述子校验）。
2. **A↔B 映射 → D**：把 A库同类型中轴线映射进 B库笔画轮廓并精调，得到**目标字体自己的笔画形态骨架**；按楷体结构 C（笔顺/类型/位置）定位构成 D。楷体只提供结构，形态来自目标字体。
3. **D ↔ 实际路径匹配**：轮廓按曲率采样，宽度归一化距离+切向一致性归属；迭代精调（笔宽估计、直笔伸缩、折笔分段平移、主人判定整体归属、孔洞径向对应）。
4. **矢量切割**：标签跳变处沿轮廓参数二分，De Casteljau 精确切段。
5. **划分式重构**：环路追踪 + 直割线弦桥接 + 环形笔画跨轮廓并环 + shapely 布尔收口。

## 使用

### 准备数据（体积/许可原因不入库）

仓库根目录放置：

- `Fonts/` —— 任意 ttf/otf 中文字体；
- `makemeahanzi-master/` —— [MakeMeAHanzi](https://github.com/skishore/makemeahanzi)（graphics.txt、dictionary.txt）；
- `hanzi_chaizi-master/` —— [hanzi_chaizi](https://github.com/howl-anderson/hanzi_chaizi)（raw_data/chaizi-*.txt）。

（放在仓库上一级目录也可，`StrokeLab.bat` 会自动探测。）

### 可视化实验台

双击 `StrokeLab.bat`（需 Python 3；首次自动安装 fonttools + shapely），浏览器自动打开。六面板：真实子路径 / 楷体笔顺动画 / A·B 库含来源 / 多层级结构 / S1–S6 流程 / D′ 按楷体笔顺播放；任意汉字即输即拆（约 9500 字）。

### 命令行 / 编程

```bash
python -m strokelab.cli --root . --font Fonts/HarmonyOS_Sans_SC.ttf --chars 永汉国 --out out --svg
```

```python
from strokelab import DataHub, FontEntry, runPipeline
hub = DataHub(".")
font = FontEntry("Fonts/HarmonyOS_Sans_SC.ttf"); font.buildLibraryB(hub)
result = runPipeline(hub, font, "永")   # strokes[].path = 独立闭合矢量路径 D′
```

## 目录

- `strokelab/` —— 算法包：geometry / classify / datahub / fonthub / pipeline / boolean / server / cli
- `viewer/charStrokeLab.html` —— 可视化前端（纯渲染，算法调本地服务 API）
- `legacy/prepareData.py` —— 早期离线预处理版（存档）
- `方案评估与说明.md` —— 方案评估、算法坑记录、指标

## API（server）

- `GET /api/fonts` — 字体清单
- `GET /api/library?font=<file>` — A/B 库
- `GET /api/decompose?font=<file>&char=<字>` — 完整拆解结果（笔画路径、指标、结构、校验）
