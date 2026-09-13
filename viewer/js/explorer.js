/* explorer.js — 字形语义关系图（semanticExplorer.html 专用，独立自足）
   职责：偏旁注册表/检索/字典三个 API 的消费端 + 双中心网状图（SVG）渲染 +
   字详情面板。不依赖 core.js（其耦合实验台页面元素）；自带 el/dom/api 小工具。
   后端契约（strokelab.server，另行实现，本文件按契约编码并容错降级）：
     GET /api/radicals                       → { radicals: [{radical,variants,count,
         representative,positions,semanticHints,effect:{color,particle},memberSample}] }
     GET /api/search?radical=氵&structure=⿰ → { chars, count, total }
     GET /api/dict?ch=清                     → 字典完整条目 + { hasGlyph, strokeCount }
   任一端点未就绪时：页顶横幅提示"接口未就绪"，并回退到静态 mock 数据自验渲染路径。 */
"use strict";
const SVG_NS = "http://www.w3.org/2000/svg";
const ACCENT = "#4c8dff";
const GOLD = "#d9a441";
const EDGE_COLOR = "#3a4048";
const MAX_NODES = 60;          // 网状图最多展示的字节点数，超出显示"共N字，展示前60"
const NODE_R = 17;             // 字节点半径
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));   // 黄金角 ≈137.508°

const IDS_NAME = {
  "⿰": "左右", "⿱": "上下", "⿲": "左中右", "⿳": "上中下", "⿴": "全包围",
  "⿵": "上三包围", "⿶": "下三包围", "⿷": "左三包围", "⿸": "左上包围",
  "⿹": "右上包围", "⿺": "左下包围", "⿻": "镶嵌",
};
const IDS_ARITY = { "⿲": 3, "⿳": 3 };   // 其余算子均为二元
const STRUCTURE_VALUES = ["", "独体", "⿰", "⿱", "⿲", "⿳", "⿴", "⿵", "⿶", "⿷",
                          "⿸", "⿹", "⿺", "⿻"];
const ETYMOLOGY_CN = { pictographic: "象形", ideographic: "会意", pictophonetic: "形声" };
const POSITION_CN = { left: "左", right: "右", top: "上", bottom: "下", surround: "包围",
                      enclosure: "包围", overall: "整体", inner: "内", corner: "角" };

/* ---------------- 静态 mock（接口未就绪时的降级数据，仅供渲染路径自验） ------- */
const MOCK_RADICALS = [
  { radical: "氵", variants: ["氵", "水", "氺"], count: 24, representative: "江",
    positions: ["left"], semanticHints: ["水", "液体"],
    effect: { color: "#4fc3f7", particle: "waterDrop" },
    memberSample: "江河湖海清波泪洗汗油池沙浪流涛滴溪潮湿润泽泊沟渐" },
  { radical: "木", variants: ["木"], count: 12, representative: "林",
    positions: ["left", "bottom"], semanticHints: ["树木", "植物"],
    effect: { color: "#81c784", particle: "leaf" }, memberSample: "林森村树材松柏桂枝柳桥梦" },
  { radical: "口", variants: ["口"], count: 10, representative: "吃",
    positions: ["left", "surround"], semanticHints: ["嘴", "言语"],
    effect: { color: "#ffb74d", particle: "ring" }, memberSample: "吃喝叫吹唱含吐司右名" },
  { radical: "忄", variants: ["忄", "心"], count: 8, representative: "情",
    positions: ["left", "bottom"], semanticHints: ["心理", "情感"],
    effect: { color: "#f06292", particle: "heart" }, memberSample: "情怕忙快恨悟怀愉" },
  { radical: "火", variants: ["火", "灬"], count: 6, representative: "烧",
    positions: ["left", "bottom"], semanticHints: ["火", "热"],
    effect: { color: "#ff8a65", particle: "spark" }, memberSample: "烧灯烟炉烂热" },
];
const MOCK_DICT = {
  "清": { character: "清", definition: "clear, pure, clean; peaceful", pinyin: ["qīng"],
          decomposition: "⿰氵青",
          etymology: { type: "pictophonetic", semantic: "氵", phonetic: "青", hint: "water" },
          radical: "氵", hasGlyph: true, strokeCount: 11,
          matches: [[0], [0], [0], [1], [1], [1], [1], [1], [1], [1], [1]] },
  "江": { character: "江", definition: "large river; Yangtze", pinyin: ["jiāng"],
          decomposition: "⿰氵工",
          etymology: { type: "pictophonetic", semantic: "氵", phonetic: "工", hint: "water" },
          radical: "氵", hasGlyph: true, strokeCount: 6,
          matches: [[0], [0], [0], [1], [1], [1]] },
  "林": { character: "林", definition: "forest, grove; forestry", pinyin: ["lín"],
          decomposition: "⿰木木",
          etymology: { type: "ideographic", hint: "Two trees 木" },
          radical: "木", hasGlyph: true, strokeCount: 8,
          matches: [[0], [0], [0], [0], [1], [1], [1], [1]] },
};

/* ---------------- 页面状态 ---------------- */
const state = {
  radicals: [],              // 注册表条目，按 count 降序
  selectedRadical: null,     // 当前选中的注册表条目
  selectedStructure: "",     // "" = 不限
  lastSearch: null,          // { radical, structure, chars, total } 检索时定格的快照
  dictCache: new Map(),      // 字 → 字典条目（成功结果缓存，避免 hover 重复请求）
  hoverChar: null,           // 当前悬停字（异步拼音回填时校验用）
  notices: new Set(),        // 降级提示去重集合
};

/* ---------------- 小工具（自足，不依赖 core.js） ---------------- */
async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error("HTTP " + res.status);
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  return data;
}
function el(name, attrs, parent) {          // SVG 元素工厂
  const node = document.createElementNS(SVG_NS, name);
  for (const k in (attrs || {})) node.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(node);
  return node;
}
function dom(tag, cls, parent, text) {      // HTML 元素工厂
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  if (parent) parent.appendChild(node);
  return node;
}
function joinVal(v, sep) {                  // 契约字段可能是数组或字符串，统一转文本
  return Array.isArray(v) ? v.join(sep) : (v == null ? "" : String(v));
}
function addNotice(text) {
  state.notices.add(text);
  const box = document.getElementById("notice");
  box.textContent = "⚠ 接口未就绪：" + [...state.notices].join(" ｜ ");
  box.classList.remove("hidden");
}

/* ---------------- 控件区：偏旁芯片流 + 结构芯片 ---------------- */
async function loadRadicals() {
  let list;
  try {
    const data = await api("/api/radicals");
    list = data.radicals || [];
  } catch (err) {
    addNotice("/api/radicals 不可用（" + err.message + "），偏旁清单为静态演示数据");
    list = MOCK_RADICALS;
  }
  state.radicals = [...list].sort((a, b) => (b.count || 0) - (a.count || 0));
  renderRadicalChips();
}
function radicalMatchesFilter(entry, filter) {
  if (!filter) return true;
  const hay = [entry.radical, joinVal(entry.variants, ""), joinVal(entry.semanticHints, ""),
               entry.representative || ""].join("");
  return hay.includes(filter);
}
function renderRadicalChips() {
  const box = document.getElementById("radicalChips");
  const filter = document.getElementById("radicalFilter").value.trim();
  box.innerHTML = "";
  const shown = state.radicals.filter(entry => radicalMatchesFilter(entry, filter));
  for (const entry of shown) {
    const chip = dom("span", "chip", box);
    if (state.selectedRadical && state.selectedRadical.radical === entry.radical)
      chip.classList.add("active");
    chip.appendChild(document.createTextNode(entry.radical));
    dom("span", "cnt", chip, String(entry.count != null ? entry.count : "?"));
    chip.onclick = () => selectRadical(entry);
  }
  if (!shown.length) dom("span", "muted", box, filter ? "无匹配偏旁" : "偏旁清单为空");
}
function selectRadical(entry) {
  state.selectedRadical = entry;
  renderRadicalChips();
  renderRadicalMeta(entry);
  runSearch();
}
function positionText(positions) {
  /* 契约写 positions 为列表；实际后端给 {位置名:占比} 字典（如 {"左":0.9}），
     两种形态都兼容：字典按占比降序展示"左90%/下10%"。 */
  if (!positions) return "";
  if (Array.isArray(positions)) return positions.map(p => POSITION_CN[p] || p).join("/");
  if (typeof positions === "object") {
    const items = Object.entries(positions).sort((a, b) => b[1] - a[1]);
    return items.map(kv => (POSITION_CN[kv[0]] || kv[0]) +
                           Math.round(kv[1] * 100) + "%").join("/");
  }
  return String(positions);
}
function renderRadicalMeta(entry) {
  const parts = ["偏旁 " + entry.radical];
  const variants = joinVal(entry.variants, "/");
  if (variants && variants !== entry.radical) parts.push("变体 " + variants);
  if (entry.representative) parts.push("代表字 " + entry.representative);
  const pos = positionText(entry.positions);
  if (pos) parts.push("常见位置 " + pos);
  const hintList = Array.isArray(entry.semanticHints) ? entry.semanticHints
                 : (entry.semanticHints ? [entry.semanticHints] : []);
  if (hintList.length) {          // 后端 hint 可能是整句英文，仅取前2条并截断，防元信息行爆行
    const brief = hintList.slice(0, 2).map(h => String(h).length > 28
        ? String(h).slice(0, 28) + "…" : String(h)).join(" / ");
    parts.push("语义 " + brief);
  }
  if (entry.effect && entry.effect.particle) parts.push("特效粒子 " + entry.effect.particle);
  document.getElementById("radicalMeta").textContent = parts.join(" · ");
}
function structureChipLabel(value) {
  if (value === "") return "不限";
  return IDS_NAME[value] ? value + " " + IDS_NAME[value] : value;
}
function renderStructureChips() {
  const box = document.getElementById("structChips");
  box.innerHTML = "";
  for (const value of STRUCTURE_VALUES) {
    const chip = dom("span", "chip", box, structureChipLabel(value));
    if (value === state.selectedStructure) chip.classList.add("active");
    chip.onclick = () => selectStructure(value);
  }
}
function selectStructure(value) {
  state.selectedStructure = value;
  renderStructureChips();
  if (state.selectedRadical) runSearch();
}

/* ---------------- 检索 ---------------- */
async function runSearch() {
  const entry = state.selectedRadical;
  const statusEl = document.getElementById("searchStatus");
  if (!entry) { statusEl.textContent = "请先选择偏旁"; return; }
  const structure = state.selectedStructure;
  statusEl.textContent = "检索中…";
  let chars, total;
  try {
    let url = "/api/search?radical=" + encodeURIComponent(entry.radical);
    if (structure) url += "&structure=" + encodeURIComponent(structure);
    const data = await api(url);
    chars = data.chars || "";
    total = data.total != null ? data.total : Array.from(chars).length;
  } catch (err) {
    addNotice("/api/search 不可用（" + err.message + "），检索结果为该偏旁的静态样本");
    chars = joinVal(entry.memberSample, "");
    total = Array.from(chars).length;
  }
  state.lastSearch = { radical: entry, structure, chars, total };
  statusEl.textContent = "命中 " + total + " 字";
  renderGraph(state.lastSearch);
}

/* ---------------- 双中心网状图 ----------------
   布点算法：黄金角螺旋（向日葵种子盘）。第 i 个字节点取
     theta = i × 137.508°，t = sqrt((i+1)/n)（sqrt 使面密度近似均匀），
     椭圆缩放 (rx,ry) 拉成横向云。确定性、无迭代，不需力导引。
   两中心（偏旁圆 / 结构方）落在云内部左右两侧，被螺旋点位撞上时把该点
   沿"中心→点"方向推到安全距离外；两中心相距远（530 单位）而安全距离仅
   ~77，推离一个中心不会撞进另一个，故单轮修正即可。 */
function graphGeometry(hasStructure) {
  if (hasStructure)
    return { cx: 500, cy: 300, rx: 430, ry: 252,
             centers: [{ x: 235, y: 300, r: 48, kind: "radical" },
                       { x: 765, y: 300, r: 48, kind: "structure" }] };
  return { cx: 500, cy: 300, rx: 445, ry: 252,
           centers: [{ x: 500, y: 300, r: 48, kind: "radical" }] };
}
function layoutCharNodes(count, geo) {
  const pts = [];
  for (let i = 0; i < count; i++) {
    const t = Math.sqrt((i + 1) / count);
    const factor = 0.16 + 0.84 * t;          // 内圈留 16% 空隙，避免挤在正中一点
    const theta = i * GOLDEN_ANGLE;
    let x = geo.cx + Math.cos(theta) * geo.rx * factor;
    let y = geo.cy + Math.sin(theta) * geo.ry * factor;
    for (const c of geo.centers) {
      const d = Math.hypot(x - c.x, y - c.y);
      const safe = c.r + NODE_R + 12;
      if (d < 0.01) { y = c.y - safe; }
      else if (d < safe) { x = c.x + (x - c.x) / d * safe; y = c.y + (y - c.y) / d * safe; }
    }
    pts.push({ x, y });
  }
  return pts;
}
function drawGraphPlaceholder(svg, text) {
  svg.innerHTML = "";
  const t = el("text", { x: 500, y: 300, "text-anchor": "middle",
                         "dominant-baseline": "central", "font-size": 18, fill: "#5a616b" }, svg);
  t.textContent = text;
}
function renderGraph(search) {
  const svg = document.getElementById("svgGraph");
  svg.innerHTML = "";
  const list = Array.from(search.chars || "").slice(0, MAX_NODES);
  const info = document.getElementById("graphInfo");
  info.textContent = !list.length ? "无命中" :
      (search.total > list.length ? "共" + search.total + "字，展示前" + list.length
                                  : "共" + search.total + "字");
  if (!list.length) { drawGraphPlaceholder(svg, "该偏旁×结构组合下无命中字"); return; }
  const geo = graphGeometry(search.structure !== "");
  const edgeLayer = el("g", {}, svg);
  const centerLayer = el("g", {}, svg);
  const nodeLayer = el("g", {}, svg);
  const pts = layoutCharNodes(list.length, geo);
  for (let i = 0; i < list.length; i++)
    drawCharNode({ edgeLayer, nodeLayer, geo, ch: list[i], pt: pts[i] });
  drawCenterNodes(centerLayer, geo, search);
}
function drawCenterNodes(layer, geo, search) {
  for (const c of geo.centers) {
    if (c.kind === "radical") drawRadicalCenter(layer, c, search.radical);
    else drawStructureCenter(layer, c, search.structure);
  }
}
function drawRadicalCenter(layer, c, entry) {
  const color = (entry.effect && entry.effect.color) ? entry.effect.color : ACCENT;
  el("circle", { cx: c.x, cy: c.y, r: c.r, fill: "#1d2026",
                 stroke: color, "stroke-width": 3 }, layer);
  const big = el("text", { x: c.x, y: c.y, "text-anchor": "middle",
                           "dominant-baseline": "central", "font-size": 40,
                           fill: "#e8eaed" }, layer);
  big.textContent = entry.radical;
  const sub = el("text", { x: c.x, y: c.y + c.r + 18, "text-anchor": "middle",
                           "font-size": 13, fill: "#9aa0a8" }, layer);
  sub.textContent = "偏旁" + (entry.count != null ? " · " + entry.count + "字" : "");
}
function drawStructureCenter(layer, c, structure) {
  el("rect", { x: c.x - c.r, y: c.y - c.r, width: c.r * 2, height: c.r * 2, rx: 12,
               fill: "#1d2026", stroke: GOLD, "stroke-width": 3 }, layer);
  const big = el("text", { x: c.x, y: c.y, "text-anchor": "middle",
                           "dominant-baseline": "central",
                           "font-size": structure === "独体" ? 28 : 40,
                           fill: "#e8eaed" }, layer);
  big.textContent = structure;
  const sub = el("text", { x: c.x, y: c.y + c.r + 18, "text-anchor": "middle",
                           "font-size": 13, fill: "#9aa0a8" }, layer);
  sub.textContent = "结构 · " + (IDS_NAME[structure] || structure);
}
function drawCharNode(ctx) {
  const { edgeLayer, nodeLayer, geo, ch, pt } = ctx;
  const edges = geo.centers.map(c => el("line", {
      x1: pt.x, y1: pt.y, x2: c.x, y2: c.y,
      stroke: EDGE_COLOR, "stroke-width": 1, opacity: 0.55 }, edgeLayer));
  const g = el("g", { class: "charNode" }, nodeLayer);
  const circle = el("circle", { cx: pt.x, cy: pt.y, r: NODE_R, fill: "#22262d",
                                stroke: "#4a5058", "stroke-width": 1.2 }, g);
  const label = el("text", { x: pt.x, y: pt.y, "text-anchor": "middle",
                             "dominant-baseline": "central", "font-size": 17,
                             fill: "#e8eaed" }, g);
  label.textContent = ch;
  const rec = { ch, circle, edges };
  g.addEventListener("mouseenter", ev => onNodeEnter(ev, rec));
  g.addEventListener("mouseleave", () => onNodeLeave(rec));
  g.addEventListener("click", () => showCharDetail(ch));
}
function onNodeEnter(ev, rec) {
  rec.circle.setAttribute("stroke", ACCENT);
  rec.circle.setAttribute("stroke-width", 2.5);
  for (const e of rec.edges) { e.setAttribute("stroke", ACCENT); e.setAttribute("opacity", 0.95); }
  state.hoverChar = rec.ch;
  showTooltip(rec.ch + " · 拼音查询中…", ev.clientX, ev.clientY);
  fetchDict(rec.ch).then(entry => updateHoverTooltip(rec.ch, entry))
                   .catch(() => updateHoverTooltip(rec.ch, null));
}
function onNodeLeave(rec) {
  rec.circle.setAttribute("stroke", "#4a5058");
  rec.circle.setAttribute("stroke-width", 1.2);
  for (const e of rec.edges) { e.setAttribute("stroke", EDGE_COLOR); e.setAttribute("opacity", 0.55); }
  state.hoverChar = null;
  hideTooltip();
}
function updateHoverTooltip(ch, entry) {
  if (state.hoverChar !== ch) return;    // 悬停已切换，丢弃过期回填
  const pin = entry ? joinVal(entry.pinyin, " / ") : "";
  setTooltipText(ch + (pin ? " · " + pin : " · （无拼音数据）"));
}
function showTooltip(text, clientX, clientY) {
  const tip = document.getElementById("tooltip");
  const host = document.getElementById("graphBody").getBoundingClientRect();
  tip.textContent = text;
  tip.classList.remove("hidden");
  tip.style.left = (clientX - host.left + 14) + "px";
  tip.style.top = (clientY - host.top - 10) + "px";
}
function setTooltipText(text) { document.getElementById("tooltip").textContent = text; }
function hideTooltip() { document.getElementById("tooltip").classList.add("hidden"); }

/* ---------------- 字典查询与详情面板 ---------------- */
async function fetchDict(ch) {
  if (state.dictCache.has(ch)) return state.dictCache.get(ch);
  let entry;
  try {
    entry = await api("/api/dict?ch=" + encodeURIComponent(ch));
  } catch (err) {
    if (!MOCK_DICT[ch]) throw err;
    addNotice("/api/dict 不可用，字详情为静态演示数据");
    entry = MOCK_DICT[ch];
  }
  state.dictCache.set(ch, entry);
  return entry;
}
async function showCharDetail(ch) {
  const box = document.getElementById("detailBody");
  box.innerHTML = "";
  dom("div", "muted", box, "查询「" + ch + "」中…");
  try {
    const entry = await fetchDict(ch);
    box.innerHTML = "";
    renderDictEntry(entry, ch, box);
  } catch (err) {
    box.innerHTML = "";
    dom("div", "muted", box,
        "「" + ch + "」查询失败：" + err.message + "（接口未就绪或字典无该字条目）");
    appendLabLink(box, ch);
  }
}
function strokeCountText(entry) {
  const n = entry.strokeCount != null ? entry.strokeCount
          : (entry.matches ? entry.matches.length : null);
  return n != null ? n + " 笔" : "笔数未知";
}
function detailTextRow(box, label, text) {
  const row = dom("div", "detailRow", box);
  dom("span", "lbl", row, label);
  dom("span", "", row, text || "—");
  return row;
}
function appendLabLink(box, ch) {
  const row = dom("div", "detailRow", box);
  const a = dom("a", "labLink", row, "在拆解实验台打开「" + ch + "」 →");
  a.href = "charStrokeLab.html?ch=" + encodeURIComponent(ch);
}
function renderDictEntry(entry, ch, box) {
  const head = dom("div", "detailRow", box);
  dom("span", "bigChar", head, entry.character || ch);
  const pin = joinVal(entry.pinyin, " / ");
  dom("span", "", head, (pin ? pin + " · " : "") + strokeCountText(entry) +
      (entry.radical ? " · 部首 " + entry.radical : "") +
      (entry.hasGlyph === false ? " · 目标字库缺字形" : ""));
  detailTextRow(box, "释义", entry.definition || "（无释义数据）");
  const treeRow = dom("div", "detailRow", box);
  dom("span", "lbl", treeRow, "结构分解");
  const tree = dom("div", "treeBox", treeRow);
  const root = parseIds(entry.decomposition);
  if (root) renderIdsNode(root, dom("ul", "", tree));
  else dom("span", "muted", tree, "（独体或无分解数据）");
  detailTextRow(box, "字源", etymologyText(entry.etymology));
  detailTextRow(box, "笔画 → 部件（matches）", matchesSummaryText(entry, root));
  appendLabLink(box, entry.character || ch);
}
function etymologyText(ety) {
  if (!ety) return "（无字源数据）";
  const parts = [ETYMOLOGY_CN[ety.type] || ety.type || "未知类型"];
  if (ety.semantic) parts.push("形旁 " + ety.semantic);
  if (ety.phonetic) parts.push("声旁 " + ety.phonetic);
  if (ety.hint) parts.push("提示：" + ety.hint);
  return parts.join(" · ");
}

/* ---------------- IDS 分解串解析（递归下降） ----------------
   makemeahanzi 的 decomposition 是 IDS 前缀表达式，如 "⿰氵青"、"⿱⿰丬夕舛"；
   算子 ⿲/⿳ 取 3 个子式，其余取 2 个；"？"表示未知部件。
   用 Array.from 按码点切分，兼容 BMP 外部件字。 */
function parseIds(decomposition) {
  const cps = Array.from(decomposition || "");
  if (!cps.length || (cps.length === 1 && cps[0] === "？")) return null;
  return parseIdsAt(cps, 0).node;
}
function parseIdsAt(cps, pos) {
  const head = cps[pos];
  if (head === undefined) return { node: null, next: pos };
  if (IDS_NAME[head]) {
    const arity = IDS_ARITY[head] || 2;
    const children = [];
    let cur = pos + 1;
    for (let i = 0; i < arity; i++) {
      const sub = parseIdsAt(cps, cur);
      if (!sub.node) break;
      children.push(sub.node);
      cur = sub.next;
    }
    return { node: { op: head, children }, next: cur };
  }
  return { node: { ch: head }, next: pos + 1 };
}
function renderIdsNode(node, parentUl) {
  const li = dom("li", "", parentUl);
  if (node.op) {
    dom("span", "opName", li, node.op + " " + (IDS_NAME[node.op] || "结构") + "结构");
    const ul = dom("ul", "", li);
    for (const child of node.children) renderIdsNode(child, ul);
  } else if (node.ch === "？") {
    dom("span", "muted", li, "？（未知部件）");
  } else {
    const s = dom("span", "comp", li, node.ch);
    s.title = "查询「" + node.ch + "」";
    s.onclick = () => showCharDetail(node.ch);
  }
}
function idsToString(node) {
  if (!node) return "？";
  if (!node.op) return node.ch;
  return node.op + node.children.map(idsToString).join("");
}
function componentAtPath(root, path) {
  let cur = root;
  for (const idx of path) {
    if (!cur || !cur.children || !cur.children[idx]) break;
    cur = cur.children[idx];
  }
  return idsToString(cur);
}
function matchesSummaryText(entry, root) {
  /* matches[i] 是第 i+1 笔在分解树中的槽位路径（如 [1,0]）；null=未标注。
     连续同槽位的笔画合并成"第a-b笔→部件"区段，保持摘要紧凑。 */
  if (!entry.matches || !entry.matches.length) return "（无笔画对位数据）";
  const labels = entry.matches.map(p => (p ? componentAtPath(root, p) : "未标注"));
  const runs = [];
  for (let i = 0; i < labels.length; i++) {
    const last = runs[runs.length - 1];
    if (last && last.label === labels[i]) last.end = i + 1;
    else runs.push({ label: labels[i], start: i + 1, end: i + 1 });
  }
  return runs.map(r => (r.start === r.end ? "第" + r.start + "笔" :
                        "第" + r.start + "-" + r.end + "笔") + "→" + r.label).join(" · ");
}

/* ---------------- 启动引导 ---------------- */
function queryDetailInput() {
  const raw = document.getElementById("detailInput").value.trim();
  if (raw) showCharDetail(Array.from(raw)[0]);
}
function bootExplorer() {
  renderStructureChips();
  document.getElementById("searchBtn").onclick = () => runSearch();
  document.getElementById("radicalFilter").oninput = () => renderRadicalChips();
  document.getElementById("detailBtn").onclick = () => queryDetailInput();
  document.getElementById("detailInput").onkeydown =
      ev => { if (ev.key === "Enter") queryDetailInput(); };
  drawGraphPlaceholder(document.getElementById("svgGraph"), "从上方选择偏旁开始探索");
  loadRadicals();
  const ch = new URLSearchParams(location.search).get("ch");
  if (ch) showCharDetail(Array.from(ch)[0]);
}
