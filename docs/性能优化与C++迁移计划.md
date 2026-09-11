# 性能优化与 C++ 渐进迁移计划

## 目标与边界

目标是在不改变当前拆分算法、校验规则、几何阈值和输出语义的前提下，降低单字拆分耗时、提高批量校验吞吐，并为以后迁移 C++ 保留清晰的接口和可复现的等价性证据。

本计划不以“重写后必然十倍加速”为前提。当前 Shapely 调用的布尔、裁剪和 Voronoi 已由 GEOS 的 C/C++ 实现，整体收益取决于 Python 循环、对象创建和数据转换在端到端耗时中的比例。所有阶段都必须先测量，再决定是否继续下沉。

## 当前基线

- Python 管线：`strokelab/pipeline.py`、`geometry.py`、`boolean.py`。
- 现有原生加速：NumPy 批量距离/点内判断、Shapely/GEOS 布尔运算、B 库和骨架磁盘缓存。
- 代表性单字耗时（含当前默认重试逻辑）：`永`约 1.65 秒、`国`约 2.51 秒、复杂字 `爨`约 11.97 秒；这些数字只作当前环境基线，迁移前需重新测量。
- 已知热点：`outlineCenterline`、`recenterMedian`、`corridorOffset`、`clampStrokes`、`_loopPolys`，以及归属阶段的批量评分和重复路径解析。
- 硬性不变量：不新增 `ERROR`/`UNION`，并集覆盖/溢出阈值不变，原通过字不能新增硬性告警。

## 阶段 0：冻结环境和 golden 基准

先建立迁移前唯一可信的结果集，任何优化或语言迁移都必须与它比较。

1. 固定 Python、fontTools、NumPy、Shapely/GEOS 版本，记录操作系统、字体文件 SHA-256、MakeMeAHanzi 数据版本和算法源码提交号。
2. 选取三层字集：
   - 基础字：`永、汉、国、爱、十、口、木`；
   - 复杂字：`爨、贗、鼻、轟、鶴、龜`；
   - 固定回归集：当前 bench 难例和 100 个 SC 通过字、100 个 SC 失败字。
3. 保存逐笔路径、median、面积、retain、shapeSim、unionCheck、失败代码和 timings。浮点比较默认容差 `1e-9`；若序列化或 GEOS 版本造成微小差异，必须单独记录容差和原因。
4. 运行现有测试、`compileall`、bench `--check`，并生成一份不可覆盖的 golden 清单。

交付物：`golden/` 或等价外部目录、基线 JSON、环境指纹、基线耗时表和复现命令。

## 阶段 1：Python 内低风险优化

这一阶段只减少重复计算，不改变公式、迭代次数、候选排序和几何运算顺序。

### 1.1 单字内缓存

- 在一次 `runPipeline` 生命周期内缓存 `parseContours`、`flattenSegs`、轮廓 bbox/edge arrays 和 `shapeDescriptor`。
- 缓存楷体笔画描述子和已放置模板描述子；路径被就地替换时按版本号或新路径键失效。
- 在 `rescueStarved`、`clampStrokes`、`resolveKaiDisjointOverlaps`、`enforceConnectivity` 之间复用只读区域对象，避免重复 `_loopPolys`。
- 不跨请求共享可变 Shapely 区域，除非明确复制或保证不可变。

### 1.2 批量计算

- 对同一折线的一组样本使用现有 `nearestBatch`，仅在样本数达到批量收益阈值时启用。
- 合并 `corridorOffset` 的边数组，减少 NumPy 临时数组分配。
- 对 `outlineCenterline` 的 bbox 和 prepared geometry 做预筛；必须确认 Polygon/MultiPolygon 行为与现有实现一致。
- 删除不可达死代码可以做，但不把采样步长、迭代次数或阈值作为性能开关。

### 1.3 服务端、缓存和校验吞吐

- `/api/decompose` 对相同字体+汉字使用 key 级 Future/锁，避免并发重复拆分。
- 字体建库使用字体级锁和 double-check；不同字体不要互相阻塞。
- 缓存 `/api/library` 和结果的序列化 bytes，按字体文件/算法签名失效；保留 `Content-Length` 和现有 JSON 结构。
- B 库骨架 dirty 标记批量写入，使用临时文件加原子替换。
- 批量校验使用大缓冲写出；`reverify.py` 保留 `jobs=1` 串行 fallback，Windows 上先验证句柄和内存占用再调大并发。

### 1.4 阶段 1 验收

- 逐字段 golden 比较通过；路径、笔顺、候选选择和失败代码不变。
- 现有单元测试、bench `--check`、100+100 回归通过。
- 至少测量冷启动、热缓存、单字和 8 路并发；报告 p50/p95、内存和缓存命中率。
- 只有在结果等价后才保留优化；任何差异先回滚对应小步。

## 阶段 2：定义语言无关核心接口

在写 C++ 前先把 Python 数据结构边界固定下来，避免把当前内部 dict 结构直接复制到 C++。

建议核心接口按批量任务设计：

1. `flattenContours(segs, tolerance) -> polylines`
2. `nearestBatch(points, polyline) -> distances, segmentIndices`
3. `shapeDescriptor(paths) -> descriptor`
4. `corridorSupport/offset(...) -> support data`
5. `outlineCenterline(region, params) -> median`
6. `recenterMedian(sections, region, params) -> median`
7. `assignSamples(samples, medians, widths, params) -> labels`

接口使用连续 `float64` 数组、显式长度和只读输入；一次跨语言调用处理整字或整批点，禁止逐点 pybind11 调用。路径、轮廓、孔洞、绕向、group 和失败状态必须有明确表示。

## 阶段 3：C++ 扩展试点（Python 编排保留）

先迁移最适合独立验证的纯计算热点，Python 继续负责 FontTools、DataHub、流程编排、HTTP 和 verify。

### 3.1 推荐顺序

1. 贝塞尔展平和边数组构造。
2. 批量最近点/距离和归属评分。
3. `outlineCenterline` 的点集、图构造和路径搜索。
4. `recenterMedian`/`corridorOffset` 的批量数值部分。
5. 最后再评估是否迁移切割和重构。

### 3.2 技术选择

- Python 绑定优先 `pybind11` 或 `nanobind`，按整段批量接口暴露。
- 几何布尔优先继续使用与 Shapely 匹配的同版本 GEOS；如需直接调用，优先稳定 C API，避免依赖不稳定的 C++ ABI。
- 字体解析初期继续使用 fontTools；只有剖析证明解析占主要耗时时，再评估 FreeType `FT_Outline_Decompose`。
- Eigen/xtensor 仅在能减少复制且不改变 float64 结果时使用；不要为替换 NumPy 而替换。
- JSON 使用现有结构；C++ 可选 nlohmann/json，但必须保持字段、排序和空值语义。

### 3.3 扩展验收

- 同一 Python 进程中对比 Python 函数和 C++ 函数的逐数组输出。
- 浮点、并列最小值、轮廓绕向、孔洞和异常输入分别测试。
- 运行 golden 字集和 bench；统计单函数加速、端到端加速、内存和跨语言拷贝量。
- 若端到端收益小于维护成本，保留 Python 实现并停止继续下沉。

## 阶段 4：按收益逐步迁移管线

只有阶段 3 证明扩展稳定且收益明确时才进行。

1. 将归属评分、几何重构等热点组合成整字批量核心，减少 Python/C++ 往返。
2. 保留 Python 版本作为 reference backend；增加 `backend=python/cpp` 选择，不改变默认行为。
3. 对 SC、TC、印刷体和手写体分别做结果对比，重点检查连笔、孔洞、重叠和退化路径。
4. 布尔收口最后迁移；固定 GEOS 版本、float64、操作次序和取整方式，避免几何差异被误认为性能收益。
5. HTTP 服务最后迁移或保持 Python 服务，仅替换核心库；不要把服务重写与几何重写绑定在同一阶段。

## 阶段 5：并发与部署

- C++ 核心必须无全局可变状态，字体库、骨架和结果缓存按实例或显式锁管理。
- 线程池按 CPU 和 GEOS 线程安全边界压测；不要把 Python ThreadingHTTPServer 的并发经验直接照搬。
- 对同一字体的建库和骨架写入做单写者保护；结果缓存使用 key 级去重。
- 提供命令行批处理、Python 绑定和独立服务三种入口中的最小必要集合。
- 固定发布包中的字体、GEOS、编译器和运行库版本，保存性能与正确性报告。

## 不应提前做的工作

- 不先整体重写 C++，不先替换 GEOS 为 Clipper/CGAL。
- 不为了速度降低 flatten 精度、采样数量、迭代次数或校验阈值。
- 不把 NumPy 已经承担的批量工作机械翻译成逐元素 C++。
- 不上传缓存、日志和未冻结的全量 JSONL 作为代码成果。
- 不用单个复杂字的加速替代端到端字集回归。

## 每阶段统一报告格式

每次优化或迁移都记录：源码提交号、环境指纹、字体/数据哈希、测试字集、冷/热缓存耗时、p50/p95、峰值内存、缓存命中率、逐字段差异、失败代码变化和回滚提交号。性能提升只有在正确性报告无新增回退时才计入正式结果。

## 公司环境执行顺序

```powershell
# 0. 同步并确认分支
git pull --ff-only origin claude
git status --short --branch

# 1. 基础验证
.venv\Scripts\python.exe -m unittest discover -s .local -p 'test_*.py'
.venv\Scripts\python.exe -m compileall -q strokelab

# 2. 先做剖析和基准，保存到项目外或明确的 results/current 目录
.venv\Scripts\python.exe -X utf8 tools/profile_pipeline.py

# 3. SC 串行回归（避免 Windows 多进程句柄限制）
.venv\Scripts\python.exe -X utf8 reverify.py sc_fail_chars.txt _reverify_fail_current.jsonl 0 1646 1
.venv\Scripts\python.exe -X utf8 reverify.py sc_pass_sample.txt _reverify_pass_current.jsonl 0 800 1

# 4. 每个小优化后重复基础验证和 golden diff，再进入下一阶段
```

`tools/profile_pipeline.py` 当前仓库可能尚未提供；若不存在，应先使用 `cProfile` 或现有 timings 生成等价报告，不要把临时剖析脚本当作算法改动提交。
