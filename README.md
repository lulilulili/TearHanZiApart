# TearHanZiApart · 汉字矢量笔画拆解实验台

基于 [MakeMeAHanzi](https://github.com/skishore/makemeahanzi)（文鼎楷体基准）对任意矢量中文字体做**纯矢量**笔画拆解——全程无像素/SDF 判定。

## 流程（对应页面 S1–S6）

1. **建库**：A库 = 文鼎楷体标准笔画；B库 = 目标字体标准笔画（来源：U+31C0–31E5 笔画区字形，或按"32类笔画×独立/少相交代表字"规则表从孤立连通组拆取）。
2. **A↔B 映射 → D**：按笔画类型把 A库中轴线映射进 B库笔画轮廓并精调，得到**目标字体自己的笔画形态骨架**；按楷体结构 C（MakeMeAHanzi 笔顺/类型/位置）定位，构成该字的笔画结构 D。
3. **D ↔ 实际路径匹配**：目标字形轮廓按曲率采样，逐点按"宽度归一化距离+切向一致性"归属到 D 各笔骨架；迭代精调（笔宽估计、直笔伸缩、折笔分段平移）。
4. **矢量切割**：标签跳变处沿轮廓参数二分，De Casteljau 精确切段——保留区段与原字形路径逐点恒等。
5. **闭合重构（划分式）**：环路追踪（按轮廓参数顺序串联弧段）+ 直割线弦桥接 + 环形笔画跨轮廓并环——目标：**所有笔画并集与原字形恒等（不多不少）**，交叠区双重归属。页面常驻 Re-Union 校验（覆盖率/溢出率）。

## 使用

1. 在仓库根目录放置三个数据目录（体积/许可原因不入库）：
   - `Fonts/` —— 任意 ttf/otf 中文字体若干；
   - `makemeahanzi-master/` —— [MakeMeAHanzi](https://github.com/skishore/makemeahanzi)（需 graphics.txt、dictionary.txt）；
   - `hanzi_chaizi-master/` —— [hanzi_chaizi](https://github.com/howl-anderson/hanzi_chaizi)（需 raw_data/chaizi-jt.txt、chaizi-ft.txt）。
2. 双击 `StrokeLab.bat`（需 Python 3，仅用于起本地静态服务）。浏览器自动打开实验台。
3. 任意汉字即输即拆（约覆盖 9500 字）；支持拖入 ttf/otf 作为目标字体。

## 文件

- `StrokeLab/charStrokeLab.html` —— 实验台（六面板：真实子路径 / 楷体笔顺动画 / A·B库含来源 / 多层级结构 / S1–S6 流程可视化 / D′ 按楷体笔顺播放），全部逻辑在浏览器内运行。
- `StrokeLab/opentype.min.js` —— 字体解析（本地副本）。
- `StrokeLab/prepareData.py` —— 遗留的离线预处理版（算法与页面 JS 一致，可批量导出）。
- `StrokeLab/方案评估与说明.md` —— 方案评估、算法坑记录、当前指标与待改进项。

## 已知差距（诚实记录）

"并集恒等"当前平均覆盖约 97%、溢出约 6%，严格达标约 1/3——残余问题：艺术字体环形并环方向性、连笔字体标签碎片、凹形处弦出界。收口方案为矢量布尔裁剪+残差回填，见方案文档。
