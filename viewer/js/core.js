/* core.js — 基础设施与拆解协议消费端（模块二重构：viewer JS 模块化）
   职责：api/拆解结果缓存/el/glyphGroup/colorOf/state + ①〜⑥面板渲染 +
   效果注册表 + 启动引导。加载顺序：core → fxUtils → fxSingle → fxMulti，
   html 末尾调用 boot()。各文件为经典脚本（非 module），顶层声明即全局。 */
"use strict";
const SVG_NS = "http://www.w3.org/2000/svg";
const PALETTE = ["#e6194b","#3cb44b","#4363d8","#f58231","#911eb4","#0f9b8e",
                 "#f032e6","#9a6324","#1f6f43","#800000","#2b6fb3","#808000",
                 "#c94f7c","#5a7d2a","#7a4fd0","#b8860b"];
const colorOf = i => PALETTE[i % PALETTE.length];
const fmt = v => Math.round(v * 10) / 10;

function el(name, attrs, parent) {
  const node = document.createElementNS(SVG_NS, name);
  for (const k in (attrs || {})) node.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(node);
  return node;
}
function glyphGroup(svg) {
  svg.innerHTML = "";
  return el("g", { transform: "scale(1,-1) translate(0,-900)" }, svg);
}
function polylineLen(m) {
  let L = 0;
  for (let i = 0; i < m.length - 1; i++)
    L += Math.hypot(m[i+1][0]-m[i][0], m[i+1][1]-m[i][1]);
  return L;
}
async function api(path) {
  const res = await fetch(path);
  const j = await res.json();
  if (j.error) throw new Error(j.error);
  return j;
}

/* ---------------- 效果注册表 ----------------
   效果单元统一签名 async effect(ctx)，ctx={svg,root,chars,decos,setInfo,
   anim,token,engine…} 由各场的 ctx 工厂构造。新效果=新函数+一行注册。 */
const FX_REGISTRY = new Map();       // 按钮id → { effect, makeCtx }
function registerFx(id, effect, makeCtx) {
  FX_REGISTRY.set(id, { effect, makeCtx });
}
function bindFxButtons() {
  for (const [id, entry] of FX_REGISTRY) {
    const btn = document.getElementById(id);
    if (btn) btn.onclick = () => entry.effect(entry.makeCtx());
  }
}

/* ---------------- 笔顺动画器 ---------------- */
class StrokeAnimator {
  constructor(svg, statusEl) {
    this.svg = svg; this.statusEl = statusEl;
    this.items = []; this.playing = false; this.raf = 0; this.speed = 1;
    this.cursor = 0; this.progress = 0; this.tracks = [];
  }
  load(items, idPrefix) {
    cancelAnimationFrame(this.raf); this.playing = false;
    this.items = items; this.cursor = 0; this.progress = 0;
    const g = glyphGroup(this.svg);
    const defs = el("defs", {}, this.svg);
    this.tracks = items.map((it, i) => {
      const cid = idPrefix + "_clip" + i;
      const cp = el("clipPath", { id: cid }, defs);
      el("path", { d: it.clipPath, "clip-rule": "nonzero" }, cp);
      const base = el("path", { d: it.clipPath, fill: "#e8e8e8",
                                "fill-rule": "nonzero" }, g);
      const mp = it.median.map((p, j) => (j ? "L" : "M") + fmt(p[0]) + " " + fmt(p[1])).join(" ");
      const track = el("path", {
        d: mp, fill: "none", stroke: it.color,
        "stroke-width": Math.max(90, it.width * 2.2),
        "stroke-linecap": "round", "stroke-linejoin": "round",
        "clip-path": "url(#" + cid + ")",
      }, g);
      const len = polylineLen(it.median) + Math.max(90, it.width * 2.2);
      track.setAttribute("stroke-dasharray", len + " " + len);
      track.setAttribute("stroke-dashoffset", len);
      return { track, len, base, color: it.color };
    });
    this.updateStatus();
  }
  updateStatus() {
    if (this.statusEl) this.statusEl.textContent = this.items.length
      ? `笔画 ${Math.min(this.cursor + (this.progress > 0 ? 1 : 0), this.items.length)} / ${this.items.length}` : "";
  }
  play() {
    if (!this.items.length) return;
    if (this.cursor >= this.items.length) this.reset();
    this.playing = true;
    let last = performance.now();
    const step = now => {
      if (!this.playing) return;
      const dt = (now - last) / 1000; last = now;
      const tr = this.tracks[this.cursor];
      if (!tr) { this.playing = false; return; }
      const dur = Math.max(0.35, tr.len / (900 * this.speed));
      this.progress += dt / dur;
      if (this.progress >= 1) {
        tr.track.setAttribute("stroke-dashoffset", 0);
        tr.base.setAttribute("fill", tr.color);
        this.cursor++; this.progress = 0;
        this.updateStatus();
        if (this.cursor >= this.items.length) { this.playing = false; return; }
      } else {
        tr.track.setAttribute("stroke-dashoffset", tr.len * (1 - this.progress));
      }
      this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }
  pause() { this.playing = false; cancelAnimationFrame(this.raf); }
  reset() {
    this.pause(); this.cursor = 0; this.progress = 0;
    this.tracks.forEach(t => {
      t.track.setAttribute("stroke-dashoffset", t.len);
      t.base.setAttribute("fill", "#e8e8e8");
    });
    this.updateStatus();
  }
}

/* ---------------- 状态 ---------------- */
const state = { fontKey: null, ch: "永", libTab: "A", structLevel: 1, stepTab: 0,
                soloGroup: null, result: null, library: null };
const libraryCache = new Map();
const kaiAnimator = new StrokeAnimator(document.getElementById("svgKai"),
                                       document.getElementById("kaiStatus"));
const resAnimator = new StrokeAnimator(document.getElementById("svgResult"),
                                       document.getElementById("resStatus"));

function setOverlay(msg) {
  const ov = document.getElementById("overlay");
  if (msg === null) { ov.classList.add("hidden"); return; }
  ov.classList.remove("hidden");
  document.getElementById("ovMsg").textContent = msg;
}

async function refresh() {
  document.querySelectorAll("#charChips .chip").forEach(c =>
    c.classList.toggle("active", c.textContent === state.ch));
  state.soloGroup = null;
  setOverlay("拆解中…");
  try {
    const t0 = performance.now();
    let libMs = 0, libSrv = null;
    if (!libraryCache.has(state.fontKey)) {
      setOverlay("构建 " + state.fontKey + " 标准笔画库…");
      const tL = performance.now();
      libraryCache.set(state.fontKey,
        await api("/api/library?font=" + encodeURIComponent(state.fontKey)));
      libMs = performance.now() - tL;
      libSrv = (libraryCache.get(state.fontKey).serverTimings || {})["库准备"];
    }
    state.library = libraryCache.get(state.fontKey);
    const tD = performance.now();
    state.result = await api("/api/decompose?font=" + encodeURIComponent(state.fontKey)
                             + "&char=" + encodeURIComponent(state.ch));
    const decMs = performance.now() - tD;
    const st = state.result.serverTimings;
    const stages = (state.result.timings || [])
      .map(([n, ms]) => `${n} ${ms}`).join(" · ");
    // 全程 = 库拉取(服务端准备+传输解析) + 拆解请求(服务端拆解+传输解析)
    const parts = [];
    if (libMs > 0) parts.push(`库拉取 ${libMs.toFixed(0)}` +
      (libSrv != null ? `(服务端 ${libSrv})` : ""));
    parts.push(`拆解请求 ${decMs.toFixed(0)}` +
      (st ? `(服务端 ${st["库准备"]}+${st["拆解"]})` : "(服务端缓存)"));
    document.getElementById("topStatus").textContent =
      `耗时 ${(performance.now() - t0).toFixed(0)} ms ＝ ` + parts.join(" + ") +
      (stages ? ` ｜ ${stages}` : "");
    setOverlay(null);
    renderAll();
  } catch (e) {
    setOverlay(null);
    document.getElementById("topStatus").textContent = "错误: " + e.message;
  }
}

/* ---------------- 理想形态开关（设计裁定"路3"：双输出解耦） ----------------
   拆解协议 strokes[k].idealPath = B库模板按 骨架↔终态median 相似拟合到
   终态位置的完整笔形（允许交叠）；path 仍是恒等拼片。⑦/⑧ 面板各有独立
   checkbox（默认关），开启时效果单元用 idealPath 渲染/采样——绘制本就
   叠画，交叠无碍；拼装/校验类面板（①〜⑥）永远用恒等拼片，不受开关影响。 */
function idealOn(panel) {
  const cb = document.getElementById(panel === 2 ? "fx2Ideal" : "fxIdeal");
  return !!(cb && cb.checked);
}
function drawPathOf(s, panel) {
  return (idealOn(panel) && s.idealPath) ? s.idealPath : s.path;
}

function renderAll() {
  renderRealPaths();
  loadFxScene();
  renderKaiPanel();
  renderLibrary();
  renderStructure();
  renderSteps();
  renderResult();
}

/* ⑦ 场景装载（refresh 与理想形态开关切换共用同一映射） */
function loadFxScene() {
  fx.load(state.result.strokes.filter(s => !s.failed).map(s => ({
    path: drawPathOf(s, 1), color: colorOf(s.index),
  })));
}

/* ---------------- ① 真实路径 ---------------- */
function renderRealPaths() {
  const svg = document.getElementById("svgReal");
  const legend = document.getElementById("legendReal");
  const r = state.result;
  legend.innerHTML = "";
  const g = glyphGroup(svg);
  r.contours.forEach((c, i) => {
    el("path", { d: c.path, fill: colorOf(i), "fill-opacity": 0.13,
                 stroke: colorOf(i), "stroke-width": 5 }, g);
  });
  r.contours.forEach((c, i) => {
    const item = document.createElement("span");
    item.innerHTML = `<span class="sw" style="background:${colorOf(i)}"></span>` +
      `子路径${i + 1} · ${c.isHole ? "孔洞" : "外轮廓"} · 组${c.group + 1} · ${c.segCount}段` +
      ` · ${c.ccw ? "逆" : "顺"}时针`;
    legend.appendChild(item);
  });
}

/* ---------------- ② 楷体笔画 ---------------- */
function renderKaiPanel() {
  const legend = document.getElementById("legendKai");
  legend.innerHTML = "";
  const kai = state.result.kai;
  kaiAnimator.load(kai.strokes.map((s, i) => ({
    clipPath: s, median: kai.medians[i], width: 110, color: colorOf(i),
  })), "kai");
  kai.strokeTypes.forEach((t, i) => {
    const item = document.createElement("span");
    item.innerHTML = `<span class="sw" style="background:${colorOf(i)}"></span>第${i + 1}笔 ${t}`;
    legend.appendChild(item);
  });
}

/* ---------------- ③ 标准笔画库 ---------------- */
function miniGlyphSvg(parent, paths) {
  const svg = el("svg", { viewBox: "0 0 1024 1024" });
  const g = el("g", { transform: "scale(1,-1) translate(0,-900)" }, svg);
  for (const d of paths) el("path", { d, fill: "#222", "fill-rule": "nonzero" }, g);
  parent.appendChild(svg);
}
function skeletonName(name) {
  const hook = name.endsWith("钩");
  const core = [...(hook ? name.slice(0, -1) : name)];
  if (!core.length) return name;
  return core[0] + "折".repeat(core.length - 1) + (hook ? "钩" : "");
}
function renderLibrary() {
  document.getElementById("tabLibA").classList.toggle("active", state.libTab === "A");
  document.getElementById("tabLibB").classList.toggle("active", state.libTab === "B");
  const box = document.getElementById("libCards");
  const note = document.getElementById("libNote");
  box.innerHTML = "";
  const onlyUsed = document.getElementById("libFilter").checked;
  const usedTypes = new Set(state.result.kai.strokeTypes);
  const usedSk = new Set([...usedTypes].map(skeletonName));
  const relevant = t => !onlyUsed || usedTypes.has(t) || usedSk.has(skeletonName(t));
  let shown = 0;
  if (state.libTab === "A") {
    const libA = state.library.A;
    for (const type in libA) {
      if (!relevant(type)) continue;
      for (const entry of libA[type]) {
        const card = document.createElement("div");
        card.className = "card" + (entry === libA[type][0] ? " primary" : "");
        miniGlyphSvg(card, [entry.path]);
        card.innerHTML += `<div class="t">${type}</div><div>${entry.source}</div>` +
          `<span class="badge">${entry.kind === "wholeChar" ? "单笔画整字"
            : entry.tier === 2 ? "规则表·骨架匹配" : "规则表"}</span>`;
        box.appendChild(card); shown++;
      }
    }
    note.textContent = `A 库（文鼎楷体，按规则表建库）共 ${Object.keys(libA).length} 类；显示 ${shown} 条。`;
  } else {
    const all = state.library.B;
    const primaryTypes = new Set();
    for (const entry of all) {
      if (!relevant(entry.type)) continue;
      const isPrimary = !primaryTypes.has(entry.type);
      primaryTypes.add(entry.type);
      const card = document.createElement("div");
      card.className = "card" + (isPrimary ? " primary" : "");
      miniGlyphSvg(card, entry.contours);
      card.innerHTML += `<div class="t">${entry.type}</div><div>${entry.source}</div>` +
        `<span class="badge">${entry.kind === "unicode" ? "U+31C0 区标准笔画"
          : entry.tier === 2 ? "规则表·骨架匹配" : "规则表拆取"}${
          entry.shapeSim !== undefined ? " · 形似" + entry.shapeSim + "%" : ""}${
          isPrimary ? " · 首选" : ""}</span>`;
      box.appendChild(card); shown++;
    }
    note.textContent = `B 库（${state.fontKey}）覆盖 ${state.library.Btypes.length} 类 / ` +
      `${all.length} 条来源；显示 ${shown} 条。`;
  }
}

/* ---------------- ④ 结构与偏旁 ---------------- */
function componentAtPath(node, path) {
  let cur = node;
  for (const idx of path) {
    if (!cur.children || !cur.children[idx]) return cur;
    cur = cur.children[idx];
  }
  return cur;
}
function labelOfNode(n) {
  if (!n) return "？";
  if (n.char) return n.char;
  if (n.op) return n.op + (n.children || []).map(labelOfNode).join("");
  return "？";
}
function renderStructure() {
  const kai = state.result.kai;
  const tabs = document.getElementById("levelTabs");
  const svg = document.getElementById("svgStruct");
  const legend = document.getElementById("legendStruct");
  const info = document.getElementById("structInfo");
  tabs.innerHTML = ""; legend.innerHTML = ""; info.innerHTML = "";
  const maxLevel = Math.max(1, ...(kai.matches || []).filter(Boolean).map(p => p.length));
  if (state.structLevel > maxLevel) state.structLevel = 1;
  for (let L = 1; L <= maxLevel; L++) {
    const b = document.createElement("button");
    b.textContent = "第" + L + "层";
    b.className = L === state.structLevel ? "active" : "";
    b.onclick = () => { state.structLevel = L; renderStructure(); };
    tabs.appendChild(b);
  }
  const groups = new Map();
  (kai.matches || []).forEach((p, i) => {
    const key = p ? JSON.stringify(p.slice(0, state.structLevel)) : "null";
    if (!groups.has(key)) groups.set(key, { path: p ? p.slice(0, state.structLevel) : null, strokes: [] });
    groups.get(key).strokes.push(i);
  });
  const g = glyphGroup(svg);
  let gi = 0;
  for (const [, grp] of groups) {
    grp.color = colorOf(gi++);
    for (const si of grp.strokes)
      el("path", { d: kai.strokes[si], fill: grp.color, "fill-opacity": 0.9 }, g);
  }
  for (const [, grp] of groups) {
    const comp = grp.path ? componentAtPath(kai.structure, grp.path) : null;
    const item = document.createElement("span");
    item.innerHTML = `<span class="sw" style="background:${grp.color}"></span>` +
      `${comp ? labelOfNode(comp) : "未标注"} · 笔画 ${grp.strokes.map(i => i + 1).join(",")}`;
    legend.appendChild(item);
  }
  const treeHtml = n => {
    if (!n) return "？";
    let s = n.char ? `<b>${n.char}</b>` : (n.op || "");
    if (n.children)
      s += "<ul>" + n.children.map(c => "<li>" + treeHtml(c) + "</li>").join("") + "</ul>";
    return s;
  };
  info.innerHTML =
    `<div>IDS 分解：<b>${kai.decomposition || "（无）"}</b>　部首：<b>${kai.radical || "？"}</b></div>` +
    `<div>结构树：${treeHtml(kai.structure)}</div>` +
    `<div class="muted">hanzi_chaizi（简）：${(kai.chaiziJt || []).map(a => a.join(" ")).join(" ／ ") || "无"}` +
    `　（繁）：${(kai.chaiziFt || []).map(a => a.join(" ")).join(" ／ ") || "无"}</div>`;
}

/* ---------------- ⑤ 流程步骤 ---------------- */
const STEP_DEFS = [
  { key: "s0", name: "S0 孤立识别", caption: "孤立路径识别（分组分治入口）：按外轮廓+孔洞划分连通组，每笔按初始设计位置指定唯一所属组（公理：正常字体中同一笔画不会断成两个孤立组）。仅含一笔的组（实色+✓）整组直出——该笔与该组路径精确恒等、保留率100%、零切割；多笔共存的组（半透明）进入组内竞争拆解，跨组互不干扰。" },
  { key: "s1", name: "S1 对齐/D", caption: "对齐与 D 构建：目标字形轮廓（灰）与每笔的 D 骨架（彩色虚线）。D 骨架 = B库同类型笔画（目标字体自己的形态，A↔B 类型映射）按楷体结构 C 给出的该笔包围盒定位；B库缺该类型时退回楷体中轴线。部件比例悬殊导致组内名义布局整体错位时触发**组局部重锚定**（红虚框=错位的名义联合框 → 实线框=组墨框，按楷体相对布局重映射，磷·石口）。" },
  { key: "s2", name: "S2 模板", caption: "D 的形态模板：B库同类型笔画（半透明彩色）放置到各笔位置 —— 形态先验，用于归属评分与校验；真正的拆解在 S3–S5 用真实轮廓完成。无填充的笔表示 B库缺该类型（退回楷体中轴线）。" },
  { key: "s3", name: "S3 归属", caption: "边界归属（组内竞争）：轮廓按曲率密度采样，逐点按“宽度归一化距离 + 切向一致性”归属到本组各笔的 D 骨架，含主人判定整体归属、孔洞径向对应、标签平滑。" },
  { key: "s4", name: "S4 切割", caption: "矢量切割：标签跳变处沿轮廓参数二分（22 次迭代），De Casteljau 把原始贝塞尔段精确切开 —— 保留区段与原路径逐点恒等。黑圈为切割点。" },
  { key: "s5", name: "S5 重构 D′", caption: "划分式重构：环路追踪（按轮廓顺序串联弧段）+ 直割线弦桥接 + 环形笔画跨轮廓并环，交叠区双重归属；shapely 布尔收口保证所有笔画并集与原字形恒等（不多不少）。" },
  { key: "s6", name: "S6 校验", caption: "一致性校验：彩色为 D′ 各笔填充（半透明，交叠区=混色=双重归属），黑色细线为原始轮廓。并集覆盖率/溢出率见⑥面板 Re-Union 校验（布尔收口后应为 100%/0%）。" },
];
function renderSteps() {
  const tabs = document.getElementById("stepTabs");
  tabs.innerHTML = "";
  STEP_DEFS.forEach((s, i) => {
    const b = document.createElement("button");
    b.textContent = s.name;
    b.className = i === state.stepTab ? "active" : "";
    b.onclick = () => { state.stepTab = i; renderSteps(); };
    tabs.appendChild(b);
  });
  const svg = document.getElementById("svgStep");
  const cap = document.getElementById("stepCaption");
  const r = state.result;
  const solo = state.soloGroup;
  /* 组过滤：solo 模式只画选中组的轮廓/笔画/样本/切割点 */
  const showC = c => solo == null || c.group === solo;
  const showS = k => solo == null || (r.strokes[k] && r.strokes[k].group === solo);
  const toggleSolo = gid => {
    state.soloGroup = state.soloGroup === gid ? null : gid;
    renderSteps();
  };
  cap.textContent = STEP_DEFS[state.stepTab].caption +
    (solo == null ? "　※ 点击任一组（图形或图例）可只看该组的拆分。"
                  : `　※ 当前只看组${solo + 1}，再次点击或点“返回全部”退出。`);
  const g = glyphGroup(svg);
  const drawOutline = op => r.contours.forEach(c => { if (showC(c))
    el("path", { d: c.path, fill: "#777", "fill-opacity": op, "fill-rule": "nonzero" }, g); });
  const drawMedians = () => r.strokes.forEach((s, k) => { if (showS(k))
    el("path", { d: s.median.map((p, j) => (j ? "L" : "M") + p[0] + " " + p[1]).join(" "),
                 fill: "none", stroke: colorOf(k), "stroke-width": 9,
                 "stroke-dasharray": "22 14", "stroke-linecap": "round" }, g); });
  /* 图例首项：组切换器（所有步骤通用）*/
  const legendEl = document.getElementById("stepLegend");
  legendEl.innerHTML = "";
  const groupsAll = r.groups || [];
  if (groupsAll.length > 1) {
    const bar = document.createElement("span");
    bar.style.cssText = "display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap";
    const mk = (label, gid, active) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.className = active ? "active" : "";
      b.style.cssText = "padding:2px 10px;font-size:12px;cursor:pointer" +
        (gid != null ? `;border-color:${colorOf(gid + 8)}` : "");
      b.onclick = () => { state.soloGroup = gid; renderSteps(); };
      bar.appendChild(b);
    };
    mk("全部组", null, solo == null);
    groupsAll.forEach(gi => mk(`组${gi.id + 1}` +
      (gi.isolated ? "✓" : `(${gi.strokes.length}笔)`), gi.id, solo === gi.id));
    legendEl.appendChild(bar);
  }
  const key = STEP_DEFS[state.stepTab].key;
  if (key === "s0") {
    /* S0：孤立路径识别——按连通组着色；孤立组实色+✓，竞争组半透明；
       点击组图形/标签切换 solo */
    r.contours.forEach(c => {
      if (!showC(c)) return;
      const gInfo = groupsAll[c.group] || { isolated: false };
      const p = el("path", { d: c.path, fill: colorOf(c.group + 8),
                   "fill-opacity": gInfo.isolated ? 0.85 : 0.28,
                   "fill-rule": "nonzero", cursor: "pointer",
                   stroke: colorOf(c.group + 8), "stroke-width": 4 }, g);
      p.style.cursor = "pointer";
      p.addEventListener("click", () => toggleSolo(c.group));
    });
    const lg0 = el("g", {}, svg);
    groupsAll.forEach(gInfo => {
      if (solo != null && gInfo.id !== solo) return;
      const polys = r.contours.filter(c => c.group === gInfo.id && !c.isHole);
      if (!polys.length) return;
      let bx0 = 1e18, by0 = 1e18, bx1 = -1e18, by1 = -1e18;
      polys.forEach(c => {
        const b = pathBBox(c.path);
        if (!b) return;
        bx0 = Math.min(bx0, b.x0); by0 = Math.min(by0, b.y0);
        bx1 = Math.max(bx1, b.x1); by1 = Math.max(by1, b.y1);
      });
      const names = gInfo.strokes.map(k => (k + 1) + r.strokes[k].type).join(" ");
      const label = gInfo.isolated ? "✓ 组" + (gInfo.id + 1) + " 直出 " + names
                                   : "组" + (gInfo.id + 1) + " 竞争 " + names;
      const t = el("text", {
        x: (bx0 + bx1) / 2, y: 900 - by1 - 12, fill: colorOf(gInfo.id + 8),
        "font-size": 36, "font-weight": 700, "text-anchor": "middle",
        stroke: "#fff", "stroke-width": 6, "paint-order": "stroke",
        "font-family": "'Microsoft YaHei', sans-serif",
      }, lg0);
      t.textContent = label;
      t.style.cursor = "pointer";
      t.addEventListener("click", () => toggleSolo(gInfo.id));
    });
    groupsAll.forEach(gInfo => {
      if (solo != null && gInfo.id !== solo) return;
      const item = document.createElement("span");
      item.style.cursor = "pointer";
      item.innerHTML = `<span class="sw" style="background:${colorOf(gInfo.id + 8)}"></span>` +
        `组${gInfo.id + 1}${gInfo.isolated ? "（孤立→整组直出）" : "（组内竞争拆解）"}：` +
        gInfo.strokes.map(k => `第${k + 1}笔${r.strokes[k].type}`).join("、");
      item.addEventListener("click", () => toggleSolo(gInfo.id));
      legendEl.appendChild(item);
    });
    return;
  }
  if (key === "s1") {
    drawOutline(0.25); drawMedians();
    /* 部件槽位层：按 matches 一级槽位画包络虚框 + 图例；G7 槽位互换
       事件高亮（种子字体系：槽位错位是换家的硬证据） */
    const slotOf = r.slotOf || [];
    const slotMembers = new Map();
    r.strokes.forEach((s, k) => {
      const sl = slotOf[k];
      if (sl == null || !showS(k)) return;
      if (!slotMembers.has(sl)) slotMembers.set(sl, []);
      slotMembers.get(sl).push(k);
    });
    const slotName = sl => {
      const ch2 = ((r.kai || {}).structure || {}).children || [];
      return (ch2[sl] && ch2[sl].char) ? ch2[sl].char : ("槽" + (sl + 1));
    };
    for (const [sl, ks] of slotMembers) {
      let x0 = 1e18, y0 = 1e18, x1 = -1e18, y1 = -1e18;
      ks.forEach(k => r.strokes[k].median.forEach(p => {
        x0 = Math.min(x0, p[0]); y0 = Math.min(y0, p[1]);
        x1 = Math.max(x1, p[0]); y1 = Math.max(y1, p[1]);
      }));
      if (x1 < x0) continue;
      el("path", { d: `M ${x0-14} ${y0-14} L ${x1+14} ${y0-14} L ${x1+14} ${y1+14} L ${x0-14} ${y1+14} Z`,
                   fill: "none", stroke: colorOf(sl + 16), "stroke-width": 2.5,
                   "stroke-dasharray": "6 8", "stroke-opacity": 0.8 }, g);
      const item = document.createElement("span");
      item.innerHTML = `<span class="sw" style="background:${colorOf(sl + 16)}"></span>` +
        `槽位「${slotName(sl)}」：笔 ${ks.map(k => k + 1).join("、")}`;
      legendEl.appendChild(item);
    }
    (r.slotSwaps || []).forEach(([a, b]) => {
      const item = document.createElement("span");
      item.innerHTML = `<span class="sw" style="background:#e0a030"></span>` +
        `G7 槽位互换：笔${a + 1}(${r.strokes[a].type}) ⇄ 笔${b + 1}(${r.strokes[b].type})`;
      legendEl.appendChild(item);
    });
    /* 组局部重锚定可视化：红虚线=楷体名义联合框（全局仿射的绝对定位，
       失配处挤压/悬空），实线=重锚定后的组墨框；箭头连角指示重映射 */
    (r.groupRemap || []).forEach(rm => {
      if (solo != null && rm.group !== solo) return;
      const [fa, fb, fc, fd] = rm.from, [ta, tb2, tc2, td] = rm.to;
      el("path", { d: `M ${fa} ${fb} L ${fc} ${fb} L ${fc} ${fd} L ${fa} ${fd} Z`,
                   fill: "none", stroke: "#e05555", "stroke-width": 4,
                   "stroke-dasharray": "14 10" }, g);
      el("path", { d: `M ${ta} ${tb2} L ${tc2} ${tb2} L ${tc2} ${td} L ${ta} ${td} Z`,
                   fill: "none", stroke: colorOf(rm.group + 8),
                   "stroke-width": 5 }, g);
      el("path", { d: `M ${fa} ${fd} L ${ta} ${td}`, fill: "none",
                   stroke: "#e05555", "stroke-width": 3,
                   "stroke-dasharray": "4 6" }, g);
      el("path", { d: `M ${fc} ${fb} L ${tc2} ${tb2}`, fill: "none",
                   stroke: "#e05555", "stroke-width": 3,
                   "stroke-dasharray": "4 6" }, g);
      const item = document.createElement("span");
      item.innerHTML = `<span class="sw" style="background:#e05555"></span>` +
        `组${rm.group + 1} 局部重锚定：名义框覆盖不足（x ${Math.round(rm.cov[0] * 100)}% / ` +
        `y ${Math.round(rm.cov[1] * 100)}% &lt;70%），按楷体相对布局映射到组墨框 ` +
        `（笔 ${rm.strokes.map(k => k + 1).join("、")}）`;
      legendEl.appendChild(item);
    });
  } else if (key === "s2") {
    drawOutline(0.12);
    r.strokes.forEach((s, k) => {
      if (showS(k) && s.templatePath)
        el("path", { d: s.templatePath, fill: colorOf(k), "fill-opacity": 0.45,
                     "fill-rule": "nonzero" }, g);
    });
    drawMedians();
  } else if (key === "s3") {
    drawOutline(0.10); drawMedians();
    r.samples.forEach((arr, ci) => {
      if (r.contours[ci] && !showC(r.contours[ci])) return;
      arr.forEach(sm =>
        el("circle", { cx: sm[0], cy: sm[1], r: 5.5, fill: colorOf(sm[2]) }, g));
    });
  } else if (key === "s4") {
    r.strokes.forEach((s, k) => { if (showS(k)) el("path", {
      d: s.path, fill: "none", stroke: colorOf(k), "stroke-width": 7 }, g); });
    /* solo 模式：切割点按落在本组轮廓包围盒内过滤（切割点无组字段）*/
    let cutBoxes = null;
    if (solo != null) {
      cutBoxes = r.contours.filter(showC).map(c => pathBBox(c.path)).filter(Boolean);
    }
    r.cutPoints.forEach(cp => {
      if (cutBoxes && !cutBoxes.some(b =>
        cp[0] >= b.x0 - 6 && cp[0] <= b.x1 + 6 &&
        cp[1] >= b.y0 - 6 && cp[1] <= b.y1 + 6)) return;
      el("circle", {
        cx: cp[0], cy: cp[1], r: 13, fill: "none", stroke: "#111", "stroke-width": 5 }, g);
    });
  } else if (key === "s5") {
    r.strokes.forEach((s, k) => { if (showS(k)) el("path", {
      d: s.path, fill: colorOf(k), "fill-opacity": 0.85, "fill-rule": "nonzero",
      stroke: "#fff", "stroke-width": 3 }, g); });
  } else {
    r.strokes.forEach((s, k) => { if (showS(k)) el("path", {
      d: s.path, fill: colorOf(k), "fill-opacity": 0.5, "fill-rule": "nonzero" }, g); });
    r.contours.forEach(c => { if (showC(c)) el("path", {
      d: c.path, fill: "none", stroke: "#111", "stroke-width": 4 }, g); });
  }
  /* 每条线/每笔的类型标注：画布内文字（白描边保证可读）+ 图例 */
  const lg = el("g", {}, svg);
  r.strokes.forEach((s, k) => {
    if (!showS(k)) return;
    const m = s.median;
    const p = m[Math.floor(m.length / 2)];
    const t = el("text", {
      x: p[0], y: 900 - p[1], fill: colorOf(k),
      "font-size": 40, "font-weight": 700, "text-anchor": "middle",
      stroke: "#fff", "stroke-width": 6, "paint-order": "stroke",
      "font-family": "'Microsoft YaHei', sans-serif",
    }, lg);
    t.textContent = (k + 1) + " " + s.type;
  });
  r.strokes.forEach((s, k) => {
    if (!showS(k)) return;
    const item = document.createElement("span");
    item.innerHTML = `<span class="sw" style="background:${colorOf(k)}"></span>` +
      `第${k + 1}笔 ${s.type} ← ${s.template}`;
    legendEl.appendChild(item);
  });
}

/* ---------------- ⑥ 结果 ---------------- */
function renderResult() {
  const info = document.getElementById("resultInfo");
  const r = state.result;
  resAnimator.load(r.strokes.filter(s => !s.failed).map((s) => ({
    clipPath: s.path, median: s.median, width: s.width, color: colorOf(s.index),
  })), "res");
  const rows = r.strokes.map(s =>
    `<tr><td><span class="sw" style="background:${colorOf(s.index)}"></span>${s.index + 1}</td>` +
    `<td>${s.type}</td><td>${Math.round(s.width)}</td><td>${s.loops}</td>` +
    `<td>${s.bridges}</td><td>${Math.round(s.retainRatio * 100)}%</td>` +
    `<td>${s.shapeSim < 35 ? `<span class="warn">${s.shapeSim}%</span>` : s.shapeSim + "%"}</td>` +
    `<td>${s.clamped ? "布尔裁剪" : "贝塞尔精确"}</td>` +
    `<td>${s.failed ? '<span class="warn">失败</span>' : '<span class="ok">✓</span>'}</td></tr>`).join("");
  const uc = r.unionCheck || { cover: 0, excess: 0 };
  const ucOk = uc.cover >= 99.5 && Math.abs(uc.excess) <= 0.5;
  info.innerHTML = `<table class="info"><tr><th>笔顺</th><th>类型</th><th>笔宽</th>` +
    `<th>环</th><th>桥</th><th>原轮廓保留</th><th>形状匹配</th><th>路径来源</th><th>状态</th></tr>${rows}</table>` +
    `<div class="statusLine"><b class="${ucOk ? "ok" : "warn"}">Re-Union 校验：原形覆盖 ${uc.cover}%、` +
    `溢出 ${uc.excess}%</b>（要求 100%/0%：所有笔画并集与原始路径恒等）。` +
    `“贝塞尔精确”= 该笔全部保留原字形精确切片 + 内部直弦；“布尔裁剪”= 经 shapely 收口修正。</div>`;
}

/* ---- 拆解结果前端缓存 + 串行请求队列（本地拆一字 1~3s，禁止并发轰服务） ---- */
const decoCache = new Map();           // font|ch -> Promise(result)
let decoChain = Promise.resolve();
function fetchDeco(ch) {
  const key = state.fontKey + "|" + ch;
  if (decoCache.has(key)) return decoCache.get(key);
  const p = decoChain.catch(() => {}).then(() =>
    api("/api/decompose?font=" + encodeURIComponent(state.fontKey) +
        "&char=" + encodeURIComponent(ch)));
  decoChain = p.catch(() => {});
  decoCache.set(key, p);
  p.catch(() => decoCache.delete(key));
  return p;
}
function strokeSubpaths(deco, k, N) {  // 子路径采样缓存（挂在结果对象上）
  deco._subs = deco._subs || {};
  // 理想形态开关（⑧场）：开启且该笔有 idealPath 时采样理想笔形；
  // 缓存键带态别后缀，两态互不污染（同一 deco 会被反复切换取样）
  const ideal = idealOn(2) ? deco.strokes[k].idealPath : "";
  const key = k + "_" + N + (ideal ? "_i" : "");
  if (!(key in deco._subs))
    deco._subs[key] = fxU.sampleSubpaths(ideal || deco.strokes[k].path, N);
  return deco._subs[key];
}

/* ---------------- 启动 ---------------- */
async function boot() {
  try {
    setOverlay("连接本地服务…");
    // 语义关系图页"在拆解实验台打开"经 ?ch=X 直达指定字
    const urlCh = new URLSearchParams(location.search).get("ch");
    if (urlCh && urlCh.length) state.ch = [...urlCh][0];
    const meta = await api("/api/fonts");
    const fs = document.getElementById("fontSelect");
    meta.fonts.forEach(f => {
      const o = document.createElement("option");
      o.value = f; o.textContent = f;
      fs.appendChild(o);
    });
    if (!meta.fonts.length) throw new Error("Fonts 目录为空");
    state.fontKey = meta.fonts[0];
    fs.value = state.fontKey;
    fs.onchange = () => { state.fontKey = fs.value; refresh(); };
    const chips = document.getElementById("charChips");
    for (const ch of meta.chips) {
      const s = document.createElement("span");
      s.className = "chip"; s.textContent = ch;
      s.onclick = () => { state.ch = ch; refresh(); };
      chips.appendChild(s);
    }
    const inp = document.getElementById("charInput");
    const commit = () => {
      const v = [...inp.value.trim()];
      if (v.length) { state.ch = v[0]; inp.value = ""; refresh(); }
    };
    inp.onkeydown = e => { if (e.key === "Enter") commit(); };
    document.getElementById("tabLibA").onclick = () => { state.libTab = "A"; renderLibrary(); };
    document.getElementById("tabLibB").onclick = () => { state.libTab = "B"; renderLibrary(); };
    document.getElementById("libFilter").onchange = renderLibrary;
    document.getElementById("kaiPlay").onclick = () =>
      kaiAnimator.playing ? kaiAnimator.pause() : kaiAnimator.play();
    document.getElementById("kaiReset").onclick = () => kaiAnimator.reset();
    document.getElementById("kaiSpeed").onchange = e => kaiAnimator.speed = +e.target.value;
    document.getElementById("resPlay").onclick = () =>
      resAnimator.playing ? resAnimator.pause() : resAnimator.play();
    document.getElementById("resReset").onclick = () => resAnimator.reset();
    document.getElementById("resSpeed").onchange = e => resAnimator.speed = +e.target.value;
    // 理想形态开关：⑦切换即重装场景（立即可见）；⑧的效果在点击时读
    // 开关状态并把态别织进场景 key（fxMulti），此处无需重建
    document.getElementById("fxIdeal").onchange = () => {
      if (state.result) loadFxScene();
    };
    bindFxButtons();                   // ⑦/⑧ 效果按钮统一走注册表
    await refresh();
  } catch (e) {
    setOverlay("启动失败：" + e.message + "（请用 StrokeLab.bat 启动本地服务）");
  }
}
