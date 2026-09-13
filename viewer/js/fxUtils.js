/* fxUtils.js — 统一单元操作库：采样（含子路径级）/对齐/插值/缓动/包围盒/
   适配/声调/部件变体表。所有效果只许经此复用，禁止各效果私有副本。
   依赖 core.js（el/SVG_NS/fmt/labelOfNode/componentAtPath）。 */
"use strict";

function pathBBox(d) {                 // 粗包围盒：扫 d 中数字对（控制点近似）
  const nums = d.match(/-?\d+\.?\d*/g);
  if (!nums || nums.length < 4) return null;
  let x0 = 1e18, y0 = 1e18, x1 = -1e18, y1 = -1e18;
  for (let i = 0; i + 1 < nums.length; i += 2) {
    const x = +nums[i], y = +nums[i + 1];
    x0 = Math.min(x0, x); y0 = Math.min(y0, y);
    x1 = Math.max(x1, x); y1 = Math.max(y1, y);
  }
  return { x0, y0, x1, y1 };
}

const fxU = (() => {
  const holder = document.createElementNS(SVG_NS, "svg");
  holder.setAttribute("width", "0"); holder.setAttribute("height", "0");
  holder.style.cssText = "position:absolute;left:-9999px;top:0;visibility:hidden";
  document.body.appendChild(holder);
  function samplePath(d, n) {          // 隐藏 path 重采样闭合轮廓到 n 点
    n = n || 96;
    const p = el("path", { d }, holder);
    let pts = [];
    try {
      const L = Math.max(1e-6, p.getTotalLength());
      for (let i = 0; i < n; i++) {
        const q = p.getPointAtLength(L * i / n);
        pts.push([q.x, q.y]);
      }
    } catch (e) { pts = null; }
    p.remove();
    return pts;
  }
  function alignClosed(ref, pts) {     // 闭合路径按最近点循环对齐（含反向）
    const n = pts.length;
    let best = pts, bestCost = Infinity;
    for (const v of [pts, pts.slice().reverse()]) {
      for (let off = 0; off < n; off++) {
        let c = 0;
        for (let i = 0; i < n; i += 6) {
          const q = v[(i + off) % n];
          c += Math.hypot(ref[i][0] - q[0], ref[i][1] - q[1]);
          if (c >= bestCost) break;
        }
        if (c < bestCost) {
          bestCost = c;
          best = v.slice(off).concat(v.slice(0, off));
        }
      }
    }
    return best;
  }
  function alignCycleOnly(ref, pts) {  // 仅循环偏移对齐（不反向，保绕向）
    const n = pts.length;
    let best = pts, bestCost = Infinity;
    for (let off = 0; off < n; off++) {
      let c = 0;
      for (let i = 0; i < n; i += 6) {
        const q = pts[(i + off) % n];
        c += Math.hypot(ref[i][0] - q[0], ref[i][1] - q[1]);
        if (c >= bestCost) break;
      }
      if (c < bestCost) {
        bestCost = c;
        best = pts.slice(off).concat(pts.slice(0, off));
      }
    }
    return best;
  }
  function ringMeta(ring) {            // 有向面积（shoelace）+质心；area 符号=绕向
    let a = 0, cx = 0, cy = 0;
    for (let i = 0; i < ring.length; i++) {
      const p = ring[i], q = ring[(i + 1) % ring.length];
      const w = p[0] * q[1] - q[0] * p[1];
      a += w; cx += (p[0] + q[0]) * w; cy += (p[1] + q[1]) * w;
    }
    a /= 2;
    if (Math.abs(a) < 1e-6) {          // 退化环（面积≈0）：质心退回均值
      cx = ring.reduce((s, p) => s + p[0], 0) / ring.length;
      cy = ring.reduce((s, p) => s + p[1], 0) / ring.length;
    } else { cx /= 6 * a; cy /= 6 * a; }
    return { ring, area: a, cx, cy };
  }
  function sampleSubpaths(d, nPer) {   // 按 "M" 切子路径分别采样成环
    // 后端笔画路径均为绝对命令；子路径内部若有相对命令（l/c 等）自成一体，
    // 唯独相对 moveto "m" 依赖上一子路径终点、无法独立切分——遇到则整串退化
    // 为单环（与旧 samplePath 行为一致，不会更差）。
    nPer = nPer || 64;
    const parts = d.indexOf("m") < 0 ? d.match(/M[^M]+/g) : null;
    if (!parts || parts.length < 2) {
      const ring = samplePath(d, nPer);
      return ring ? [ringMeta(ring)] : null;
    }
    const out = [];
    for (const pd of parts) {
      const ring = samplePath(pd, nPer);
      if (ring) out.push(ringMeta(ring));
    }
    return out.length ? out : null;
  }
  function degenerateSub(sub) {        // 自身质心的退化环（塌缩/生长的端点）
    const ring = Array.from({ length: sub.ring.length }, () => [sub.cx, sub.cy]);
    return { ring, area: 0, cx: sub.cx, cy: sub.cy };
  }
  function pairSubpaths(subsA, subsB) {
    // 配对规则：两侧按 |面积| 降序，首对首（主轮廓对主轮廓——带孔笔画
    // 最大环必是外轮廓），余下按质心最近贪心（孔对孔）；落单的子路径
    // 与"自身质心退化环"互插=塌缩/生长（如 口→一 的孔洞收缩消失）。
    const sa = subsA.slice().sort((x, y) => Math.abs(y.area) - Math.abs(x.area));
    const sb = subsB.slice().sort((x, y) => Math.abs(y.area) - Math.abs(x.area));
    const pairs = [], usedB = new Set();
    sa.forEach((s, i) => {
      if (i === 0 && sb.length) { pairs.push([s, sb[0]]); usedB.add(0); return; }
      let bj = -1, bd = Infinity;
      sb.forEach((t, j) => {
        if (usedB.has(j)) return;
        const dd = Math.hypot(t.cx - s.cx, t.cy - s.cy);
        if (dd < bd) { bd = dd; bj = j; }
      });
      if (bj >= 0) { usedB.add(bj); pairs.push([s, sb[bj]]); }
      else pairs.push([s, degenerateSub(s)]);
    });
    sb.forEach((t, j) => { if (!usedB.has(j)) pairs.push([degenerateSub(t), t]); });
    return pairs;
  }
  function fitSubs(subs, f) {          // 子路径组整体过仿射 {s,tx,ty}
    return subs.map(s => ({
      ring: s.ring.map(p => [f.s * p[0] + f.tx, f.s * p[1] + f.ty]),
      area: s.area * f.s * f.s, cx: f.s * s.cx + f.tx, cy: f.s * s.cy + f.ty }));
  }
  function buildRingPairs(subsA, subsB) {
    // 子路径级插值项：每对独立对齐。绕向保持：dst 绕向强制与 src 一致
    // （孔洞在中间态仍是孔——nonzero 下孔洞靠与外环反向成立，若对齐时
    // 自由反转会把孔"填实"；故先按 area 符号反转 dst，再只做循环偏移）。
    return pairSubpaths(subsA, subsB).map(([s, t]) => {
      let pts = t.ring;
      if (s.area * t.area < 0) pts = pts.slice().reverse();
      return { src: s.ring, dst: alignCycleOnly(s.ring, pts) };
    });
  }
  const lerpPts = (a, b, t) =>
    a.map((p, i) => [p[0] + (b[i][0] - p[0]) * t, p[1] + (b[i][1] - p[1]) * t]);
  const ptsPath = pts =>
    pts.map((p, i) => (i ? "L" : "M") + fmt(p[0]) + " " + fmt(p[1])).join(" ") + " Z";
  const lerpRingPairsPath = (pairs, t) =>  // 多子路径拼成同一 path 的 d
    pairs.map(pr => ptsPath(lerpPts(pr.src, pr.dst, t))).join(" ");
  const clamp01 = t => Math.min(1, Math.max(0, t));
  const easeOutCubic = t => 1 - Math.pow(1 - t, 3);
  const easeInCubic = t => t * t * t;
  const easeInOut = t => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  const easeOutBack = t => {
    const c = 1.70158;
    return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2);
  };
  function unionBox(a, b) {
    if (!a) return b; if (!b) return a;
    return { x0: Math.min(a.x0, b.x0), y0: Math.min(a.y0, b.y0),
             x1: Math.max(a.x1, b.x1), y1: Math.max(a.y1, b.y1) };
  }
  function strokesBox(deco, ks) {
    let bb = null;
    for (const k of ks) {
      const s = deco.strokes[k];
      if (!s || s.failed || !s.path) continue;
      bb = unionBox(bb, pathBBox(s.path));
    }
    return bb;
  }
  function fitBox(src, dst, shrink) {  // src 盒 → dst 盒的等比适配 {tx,ty,s}
    const sw = Math.max(1, src.x1 - src.x0), sh = Math.max(1, src.y1 - src.y0);
    const s = Math.min((dst.x1 - dst.x0) / sw, (dst.y1 - dst.y0) / sh) * (shrink || 1);
    return { s,
      tx: (dst.x0 + dst.x1) / 2 - s * (src.x0 + src.x1) / 2,
      ty: (dst.y0 + dst.y1) / 2 - s * (src.y0 + src.y1) / 2 };
  }
  const tf = p => `translate(${fmt(p.tx)} ${fmt(p.ty)}) scale(${p.s})`;
  function toneOf(py) {                // 带调元音 → 1..4 声
    if (!py) return 1;
    const T = { "ā":1,"ē":1,"ī":1,"ō":1,"ū":1,"ǖ":1,
                "á":2,"é":2,"í":2,"ó":2,"ú":2,"ǘ":2,"ń":2,
                "ǎ":3,"ě":3,"ǐ":3,"ǒ":3,"ǔ":3,"ǚ":3,"ň":3,
                "à":4,"è":4,"ì":4,"ò":4,"ù":4,"ǜ":4,"ǹ":4 };
    for (const c of String(py).normalize("NFC")) if (T[c]) return T[c];
    const m = String(py).match(/[1-4]/);
    return m ? +m[0] : 1;
  }
  const COMP_VARIANTS = [["氵","水","氺"],["亻","人"],["忄","心"],["扌","手"],
    ["讠","言"],["钅","金"],["饣","食"],["纟","糸","糹"],["艹","艸"],["辶","辵"],
    ["阝","邑","阜"],["犭","犬"],["礻","示"],["衤","衣"],["月","肉"],["王","玉"],
    ["灬","火"],["刂","刀"],["罒","网"],["宀","㝉"]];
  function sameComp(a, b) {
    if (!a || !b) return false;
    if (a === b) return true;
    return COMP_VARIANTS.some(g => g.indexOf(a) >= 0 && g.indexOf(b) >= 0);
  }
  const childChar = node => node ? (node.char || labelOfNode(node)) : null;
  function slotOfComp(deco, comp) {    // 一级槽位里找部件（含变体等价）
    const ch = (((deco.kai || {}).structure) || {}).children || [];
    for (let i = 0; i < ch.length; i++)
      if (sameComp(childChar(ch[i]), comp)) return i;
    return -1;
  }
  function slotStrokes(deco, slot) {
    const out = [];
    (deco.slotOf || []).forEach((sl, k) => {
      const s = deco.strokes[k];
      if (sl === slot && s && !s.failed && s.path) out.push(k);
    });
    return out;
  }
  function liveStrokes(deco) {
    return deco.strokes.map((s, k) => ({ s, k }))
                       .filter(o => !o.s.failed && o.s.path);
  }
  function leafGroups(deco) {          // 按 matches 全路径分组 → 叶级部件组
    const m = (deco.kai || {}).matches || [];
    const map = new Map();
    deco.strokes.forEach((s, k) => {
      if (s.failed || !s.path) return;
      const path = m[k] || null;
      const key = path ? JSON.stringify(path) : "root";
      if (!map.has(key)) map.set(key, { path, ks: [] });
      map.get(key).ks.push(k);
    });
    const out = [];
    for (const [, g] of map) {
      const node = g.path ? componentAtPath((deco.kai || {}).structure, g.path)
                          : (deco.kai || {}).structure;
      out.push({ char: childChar(node) || deco.ch, ks: g.ks,
                 box: strokesBox(deco, g.ks) });
    }
    return out.filter(g => g.box);
  }
  function medianCum(m) {
    const cum = [0];
    for (let i = 1; i < m.length; i++)
      cum.push(cum[i - 1] + Math.hypot(m[i][0] - m[i-1][0], m[i][1] - m[i-1][1]));
    return cum;
  }
  function medianPointAt(m, cum, d) {  // 沿折线取点（循环）
    const L = cum[cum.length - 1] || 1;
    d = ((d % L) + L) % L;
    for (let i = 1; i < cum.length; i++) {
      if (d <= cum[i]) {
        const t = (d - cum[i-1]) / Math.max(1e-6, cum[i] - cum[i-1]);
        return [m[i-1][0] + (m[i][0] - m[i-1][0]) * t,
                m[i-1][1] + (m[i][1] - m[i-1][1]) * t];
      }
    }
    return m[m.length - 1].slice();
  }
  return { samplePath, alignClosed, sampleSubpaths, pairSubpaths, fitSubs,
           buildRingPairs, lerpRingPairsPath, lerpPts, ptsPath, clamp01,
           easeOutCubic, easeInCubic, easeInOut, easeOutBack,
           unionBox, strokesBox, fitBox, tf, toneOf, sameComp, childChar,
           slotOfComp, slotStrokes, liveStrokes, leafGroups,
           medianCum, medianPointAt };
})();
