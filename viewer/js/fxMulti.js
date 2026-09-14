/* fxMulti.js — ⑧ 多字特效场：跨字交换/变形/景深效果库。
   fx2 为场引擎（token 竞争控制/串行拆解拉取/帧驱动/场景根），每个效果一个
   async effect(ctx)，ctx 由 makeFx2Ctx() 构造（构造即 bump token——一次点击
   一个令牌，旧动画自动作废）并经注册表绑定按钮。
   依赖 core.js（el/colorOf/fmt/state/fetchDeco/strokeSubpaths/registerFx）
   与 fxUtils.js（fxU/pathBBox）。 */
"use strict";

/* ---- ⑧ 场共用小工具 ---- */
function centerPlace(scale) {
  scale = scale || 0.8;
  return { s: scale, tx: 512 - 512 * scale, ty: 400 - 450 * scale };
}
function rowPlace(i, n, scale) {
  const w = 1024 * scale, gap = 20;
  const total = n * w + (n - 1) * gap;
  return { s: scale, tx: 512 - total / 2 + i * (w + gap), ty: 400 - 450 * scale };
}
function mkStrokeGroup(deco, ks, parent) {
  const g = el("g", {}, parent);
  for (const k of ks) {
    const s = deco.strokes[k];
    if (s && !s.failed && s.path)
      el("path", { d: s.path, "fill-rule": "nonzero", fill: colorOf(k) }, g);
  }
  return g;
}
function mkFlyRec(g, box, sign) {      // 沿远离字心方向飞出/由此飞入
  const ang = Math.atan2((box.y0 + box.y1) / 2 - 450,
                         (box.x0 + box.x1) / 2 - 512) + sign * 0.3;
  return { g, dx: Math.cos(ang) * 1500, dy: Math.sin(ang) * 1500 };
}
function flyFrame(outs, ins, t) {
  const eo = fxU.easeInCubic(Math.min(1, t / 0.7));
  for (const o of outs) {
    o.g.setAttribute("transform", `translate(${fmt(o.dx * eo)} ${fmt(o.dy * eo)})`);
    o.g.setAttribute("opacity", fmt(Math.max(0, 1 - eo * 1.25)));
  }
  const ei = 1 - fxU.easeOutCubic(fxU.clamp01((t - 0.2) / 0.8));
  for (const o of ins) {
    o.g.setAttribute("transform", `translate(${fmt(o.dx * ei)} ${fmt(o.dy * ei)})`);
    o.g.setAttribute("opacity", fmt(Math.min(1, (1 - ei) * 1.4)));
  }
}
function smFrame(it, t) {              // 偏旁变形帧：子路径级环插值，重写 d
  const e = fxU.easeInOut(fxU.clamp01(t));
  it.p.setAttribute("d", fxU.lerpRingPairsPath(it.pairs, e));
}
function swapFrame(ctx, dir, t) {      // dir=1 交换 / 0 还原；弧线互飞
  const e = fxU.easeInOut(t), lift = Math.sin(Math.PI * t);
  for (const mv of ctx.movers) {
    const from = dir ? mv.home : mv.away, to = dir ? mv.away : mv.home;
    mv.g.setAttribute("transform", fxU.tf({
      s: from.s + (to.s - from.s) * e,
      tx: from.tx + (to.tx - from.tx) * e,
      ty: from.ty + (to.ty - from.ty) * e + mv.lift * 160 * lift }));
  }
}
function greedyMatch(ga, gb) {         // 叶级部件贪心配对（同字符→就近）
  const used = new Set(), pairs = [], outs = [], ins = [];
  for (const s of ga) {
    let bj = -1, bd = Infinity;
    gb.forEach((t2, j) => {
      if (used.has(j) || t2.char !== s.char) return;
      const d = Math.hypot((t2.box.x0 + t2.box.x1) - (s.box.x0 + s.box.x1),
                           (t2.box.y0 + t2.box.y1) - (s.box.y0 + s.box.y1));
      if (d < bd) { bd = d; bj = j; }
    });
    if (bj >= 0) { used.add(bj); pairs.push({ src: s, dst: gb[bj] }); }
    else outs.push(s);
  }
  gb.forEach((t2, j) => { if (!used.has(j)) ins.push(t2); });
  return { pairs, outs, ins };
}
/* 双向并集比例配对：每个目标笔配一个比例位源笔（保证 t=1 完整），未被
   用到的源笔补配比例位目标（保证 t=0/变回完整；目标重复无害——同色重叠）。
   教训：此前只按目标建项，日→氵 的 4 源对 3 目标会漏掉日的第2笔，起始帧
   闪缺、变回后蓝日缺笔。swapMorph 与 anyMorph 共用。 */
function unionPairIdx(nS, nD) {
  const pairs = [], usedSrc = new Set();
  for (let j = 0; j < nD; j++) {
    const si = nD === 1 ? 0
      : Math.min(nS - 1, Math.round(j * (nS - 1) / Math.max(1, nD - 1)));
    pairs.push([si, j]); usedSrc.add(si);
  }
  for (let i = 0; i < nS; i++) {
    if (usedSrc.has(i)) continue;
    const dj = nS === 1 ? 0
      : Math.min(nD - 1, Math.round(i * (nD - 1) / Math.max(1, nS - 1)));
    pairs.push([i, dj]);
  }
  return pairs;
}

/* ---- ⑧ 场引擎 ---- */
const fx2 = {
  svg: null, raf: 0, token: 0, mode: null,
  swapCtx: null, smCtx: null, anyCtx: null, morphCtx: null, gatherCtx: null,
  init() { this.svg = document.getElementById("svgFx2"); },
  setInfo(html) { document.getElementById("fx2Info").innerHTML = html; },
  bump() { this.token++; cancelAnimationFrame(this.raf); this.raf = 0; return this.token; },
  alive(tk) { return tk === this.token; },
  stopAll() { this.bump(); this.mode = null; },
  chars(defStr, nMin, nMax) {
    const v = [...document.getElementById("fx2Input").value.replace(/\s+/g, "")];
    const arr = v.length >= nMin && v.length > 0 ? v : [...defStr];
    return arr.slice(0, nMax);
  },
  sleep(ms, tk) {
    return new Promise(res => setTimeout(() => res(this.alive(tk)), ms));
  },
  anim(dur, tk, frame) {               // 帧驱动，token 失效即中止(false)
    return new Promise(res => {
      const t0 = performance.now();
      const step = now => {
        if (!this.alive(tk)) return res(false);
        const t = Math.min(1, (now - t0) / dur);
        frame(t);
        if (t < 1) this.raf = requestAnimationFrame(step);
        else res(true);
      };
      this.raf = requestAnimationFrame(step);
    });
  },
  async fetchAll(chs, tk, label) {     // 逐字串行拆解 + 加载态；失败置 null
    const out = [];
    for (let i = 0; i < chs.length; i++) {
      if (!this.alive(tk)) return null;
      this.setInfo(`${label || "加载"}：拆解「${chs[i]}」 (${i + 1}/${chs.length}) …`);
      try { out.push(await fetchDeco(chs[i])); }
      catch (e) { out.push(null); }
    }
    return this.alive(tk) ? out : null;
  },
  root() { return glyphGroup(this.svg); },
  renderSingle(deco, place) {
    const root = this.root();
    const cg = el("g", { transform: fxU.tf(place) }, root);
    deco.strokes.forEach((s, k) => {
      if (!s.failed && s.path)
        el("path", { d: s.path, "fill-rule": "nonzero", fill: colorOf(k) }, cg);
    });
  },
  async morphChar(a, b, place, tk, pho) {   // 声旁槽内逐笔插值；其余飞出/飞入
    const slA = pho ? fxU.slotOfComp(a, pho) : -1;
    const slB = pho ? fxU.slotOfComp(b, pho) : -1;
    const liveA = fxU.liveStrokes(a), liveB = fxU.liveStrokes(b);
    let pa = [], pb = [];
    if (slA >= 0 && slB >= 0) {
      pa = liveA.filter(o => (a.slotOf || [])[o.k] === slA);
      pb = liveB.filter(o => (b.slotOf || [])[o.k] === slB);
    }
    const nPair = Math.min(pa.length, pb.length);
    const pairSetA = new Set(pa.slice(0, nPair).map(o => o.k));
    const pairSetB = new Set(pb.slice(0, nPair).map(o => o.k));
    const root = this.root();
    const cg = el("g", { transform: fxU.tf(place) }, root);
    const N = 64;
    const morphs = [];
    for (let i = 0; i < nPair; i++) {
      const subsA = strokeSubpaths(a, pa[i].k, N);
      const subsB = strokeSubpaths(b, pb[i].k, N);
      if (!subsA || !subsB) continue;
      const pairs = fxU.buildRingPairs(subsA, subsB);
      const node = el("path", { "fill-rule": "nonzero",
        fill: colorOf(pb[i].k), d: fxU.lerpRingPairsPath(pairs, 0) }, cg);
      morphs.push({ node, pairs });
    }
    const outs = liveA.filter(o => !pairSetA.has(o.k)).map(o =>
      mkFlyRec(mkStrokeGroup(a, [o.k], cg),
               pathBBox(a.strokes[o.k].path) || { x0: 400, y0: 400, x1: 600, y1: 600 }, 1));
    const ins = liveB.filter(o => !pairSetB.has(o.k)).map(o => {
      const rec = mkFlyRec(mkStrokeGroup(b, [o.k], cg),
        pathBBox(b.strokes[o.k].path) || { x0: 400, y0: 400, x1: 600, y1: 600 }, -1);
      rec.g.setAttribute("opacity", 0);
      rec.g.setAttribute("transform", `translate(${fmt(rec.dx)} ${fmt(rec.dy)})`);
      return rec;
    });
    const ok = await this.anim(950, tk, t => {
      const e = fxU.easeInOut(t);
      for (const m of morphs)
        m.node.setAttribute("d", fxU.lerpRingPairsPath(m.pairs, e));
      flyFrame(outs, ins, t);
    });
    if (ok) this.renderSingle(b, place);
    return ok;
  },
  async growStep(a, b, place, tk) {
    const { pairs, outs: srcOuts, ins: dstIns } =
      greedyMatch(fxU.leafGroups(a), fxU.leafGroups(b));
    const root = this.root();
    const cg = el("g", { transform: fxU.tf(place) }, root);
    const mv = pairs.map(m => ({ g: mkStrokeGroup(a, m.src.ks, cg),
                                 fit: fxU.fitBox(m.src.box, m.dst.box) }));
    const outs = srcOuts.map(s => mkFlyRec(mkStrokeGroup(a, s.ks, cg), s.box, 1));
    const ins = dstIns.map(s => {
      const rec = mkFlyRec(mkStrokeGroup(b, s.ks, cg), s.box, -1);
      rec.g.setAttribute("opacity", 0);
      rec.g.setAttribute("transform", `translate(${fmt(rec.dx)} ${fmt(rec.dy)})`);
      return rec;
    });
    const ok = await this.anim(950, tk, t => {
      const e = fxU.easeInOut(t);
      for (const m of mv) {
        const s = 1 + (m.fit.s - 1) * e;
        m.g.setAttribute("transform",
          `translate(${fmt(m.fit.tx * e)} ${fmt(m.fit.ty * e)}) scale(${fmt(s)})`);
      }
      flyFrame(outs, ins, t);
    });
    if (ok) this.renderSingle(b, place);
    return ok;
  },
  async structTransition(A, B, tk) {
    const place = centerPlace(0.78);
    const pairName = A.ch + "→" + B.ch;
    const ga = fxU.leafGroups(A), gb = fxU.leafGroups(B);
    let { pairs, outs: po, ins: pi } = greedyMatch(ga, gb);
    if (po.length && pi.length) {      // 字符标注不一致时按笔画数兜底配对
      for (const s of po) {
        let bj = -1, bd = Infinity;
        pi.forEach((t2, j) => {
          const d = Math.abs(t2.ks.length - s.ks.length);
          if (d < bd) { bd = d; bj = j; }
        });
        if (bj >= 0) { pairs.push({ src: s, dst: pi[bj] }); pi.splice(bj, 1); }
      }
    }
    if (!pairs.length) { this.setInfo("未能建立部件配对"); return; }
    /* 第一段：部件整体仿射对位 */
    this.setInfo(`跨结构转化 ${pairName}：第一段 部件整体仿射对位…`);
    const root = this.root();
    const cg = el("g", { transform: fxU.tf(place) }, root);
    const items = pairs.map(p => ({ g: mkStrokeGroup(A, p.src.ks, cg),
                                    fit: fxU.fitBox(p.src.box, p.dst.box) }));
    if (!await this.anim(850, tk, t => {
      const e = fxU.easeInOut(t);
      for (const it of items) {
        const s = 1 + (it.fit.s - 1) * e;
        it.g.setAttribute("transform",
          `translate(${fmt(it.fit.tx * e)} ${fmt(it.fit.ty * e)}) scale(${fmt(s)})`);
      }
    })) return;
    /* 第二段：残差逐笔插值（A 笔先施加一段仿射，再子路径级插到 B 对应笔） */
    this.setInfo(`跨结构转化 ${pairName}：第二段 残差逐笔插值…`);
    const root2 = this.root();
    const cg2 = el("g", { transform: fxU.tf(place) }, root2);
    const N = 64, morphs = [];
    for (const pr of pairs) {
      const f = fxU.fitBox(pr.src.box, pr.dst.box);
      const n = Math.min(pr.src.ks.length, pr.dst.ks.length);
      for (let i = 0; i < n; i++) {
        const subsA = strokeSubpaths(A, pr.src.ks[i], N);
        const subsB = strokeSubpaths(B, pr.dst.ks[i], N);
        if (!subsA || !subsB) continue;
        const ringPairs = fxU.buildRingPairs(fxU.fitSubs(subsA, f), subsB);
        morphs.push({ pairs: ringPairs, node: el("path", {
          "fill-rule": "nonzero", fill: colorOf(pr.dst.ks[i]),
          d: fxU.lerpRingPairsPath(ringPairs, 0) }, cg2) });
      }
      for (let i = n; i < pr.dst.ks.length; i++) {   // 笔画数不等的兜底淡入
        const sb = B.strokes[pr.dst.ks[i]];
        if (sb && !sb.failed && sb.path)
          morphs.push({ fadeIn: true, node: el("path", { d: sb.path,
            "fill-rule": "nonzero", fill: colorOf(pr.dst.ks[i]), opacity: 0 }, cg2) });
      }
    }
    if (!await this.anim(900, tk, t => {
      const e = fxU.easeInOut(t);
      for (const m of morphs) {
        if (m.fadeIn) { m.node.setAttribute("opacity", fmt(e)); continue; }
        m.node.setAttribute("d", fxU.lerpRingPairsPath(m.pairs, e));
      }
    })) return;
    this.renderSingle(B, place);
    this.morphCtx = { A: B, B: A };
    this.setInfo(`跨结构转化完成：${pairName}（再按一次反向 ${B.ch}→${A.ch}）`);
  },
  setGather(P) {
    const ctx = this.gatherCtx;
    if (!ctx) return;
    for (const r of ctx.recs) {
      const t = fxU.clamp01((P - r.st0) / r.w);
      const e = fxU.easeOutCubic(t);
      const k = r.z + (1 - r.z) * e;    // 深度收敛到 1
      const dx = r.ix * (1 - e), dy = r.iy * (1 - e);
      r.g.setAttribute("transform",
        `translate(${fmt(dx + r.cx * (1 - k))} ${fmt(dy + r.cy * (1 - k))}) scale(${fmt(k)})`);
      r.g.setAttribute("opacity", fmt(0.22 + 0.78 * e));
      let f = "";
      if (t < 0.6) f = ctx.tiers[r.tier][0];
      else if (t < 0.88) f = ctx.tiers[Math.min(2, r.tier + 1)][0];
      if (f) r.g.setAttribute("filter", `url(#${f})`);
      else r.g.removeAttribute("filter");
    }
  },
  gatherPlay() {
    if (!this.gatherCtx) return;
    const tk = ++this.token;
    cancelAnimationFrame(this.raf);
    const slider = document.getElementById("fx2Scrub");
    const t0 = performance.now(), dur = 3200;
    const step = now => {
      if (!this.alive(tk)) return;
      const P = Math.min(1, (now - t0) / dur);
      slider.value = Math.round(P * 100);
      this.setGather(P);
      if (P < 1) this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  },
};

/* ---- ⑧ ctx 工厂：构造即 bump token（一次点击一个令牌，旧动画作废） ---- */
function makeFx2Ctx() {
  const tk = fx2.bump();
  return {
    svg: fx2.svg,
    root: () => fx2.root(),
    chars: (defStr, nMin, nMax) => fx2.chars(defStr, nMin, nMax),
    decos: (chs, label) => fx2.fetchAll(chs, tk, label),
    setInfo: html => fx2.setInfo(html),
    anim: (dur, frame) => fx2.anim(dur, tk, frame),
    sleep: ms => fx2.sleep(ms, tk),
    token: tk,
    engine: fx2,
  };
}

/* ⑤ 偏旁交换：双字并排，差异槽位笔画组弧线互飞 + 仿射适配，可来回切换 */
async function fx2SwapEffect(ctx) {
  const eng = ctx.engine;
  const pair = ctx.chars("清晴", 2, 2);
  if (pair.length < 2) { ctx.setInfo("请输入两个字（如 清晴）"); return; }
  if (eng.mode === "swap" && eng.swapCtx && eng.swapCtx.key === pair.join("")) {
    const c = eng.swapCtx, dir = c.swapped ? 0 : 1;
    if (await ctx.anim(900, t => swapFrame(c, dir, t))) {
      c.swapped = !c.swapped;
      ctx.setInfo(`偏旁交换：${c.label} — ${c.swapped ? "已交换（再按一次换回）" : "已还原"}`);
    }
    return;
  }
  eng.mode = "swap"; eng.swapCtx = null;
  const decos = await ctx.decos(pair, "偏旁交换");
  if (!decos) return;
  if (!decos[0] || !decos[1]) {
    ctx.setInfo("拆解失败：" + pair.filter((c, i) => !decos[i]).join("、"));
    return;
  }
  const A = decos[0], B = decos[1];
  const chA = (((A.kai || {}).structure) || {}).children || [];
  const chB = (((B.kai || {}).structure) || {}).children || [];
  let slot = -1;
  for (let i = 0; i < Math.min(chA.length, chB.length); i++) {
    const a = fxU.childChar(chA[i]), b = fxU.childChar(chB[i]);
    if (a && b && a !== b) { slot = i; break; }
  }
  if (slot < 0) {
    ctx.setInfo(`未找到差异槽位（${pair.join("/")}：` +
      `${chA.map(fxU.childChar).join("+") || "独体"} vs ` +
      `${chB.map(fxU.childChar).join("+") || "独体"}）`);
    return;
  }
  const ksA = fxU.slotStrokes(A, slot), ksB = fxU.slotStrokes(B, slot);
  if (!ksA.length || !ksB.length) {
    ctx.setInfo("差异槽位没有对应笔画（slotOf 缺失或全部失败笔）"); return;
  }
  const root = ctx.root();
  const pA = rowPlace(0, 2, 0.45), pB = rowPlace(1, 2, 0.45);
  const mkSide = (deco, place, ks, tint) => {
    const stat = el("g", { transform: fxU.tf(place) }, root);
    deco.strokes.forEach((s, k) => {
      if (s.failed || !s.path || ks.indexOf(k) >= 0) return;
      el("path", { d: s.path, "fill-rule": "nonzero", fill: "#565b63" }, stat);
    });
    const mov = el("g", { transform: fxU.tf(place) }, root);
    for (const k of ks) {
      const s = deco.strokes[k];
      el("path", { d: s.path, "fill-rule": "nonzero", fill: tint }, mov);
    }
    return mov;
  };
  const movA = mkSide(A, pA, ksA, "#e6194b");
  const movB = mkSide(B, pB, ksB, "#2b6fb3");
  const boxA = fxU.strokesBox(A, ksA), boxB = fxU.strokesBox(B, ksB);
  if (!boxA || !boxB) { ctx.setInfo("槽位包围盒计算失败"); return; }
  const outBox = (box, pl) => ({
    x0: pl.tx + pl.s * box.x0, y0: pl.ty + pl.s * box.y0,
    x1: pl.tx + pl.s * box.x1, y1: pl.ty + pl.s * box.y1 });
  // 同型结构（根 IDS 算子一致，如 乒/乓 都是⿱丘X、清/晴 都是⿰X青）：
  // 来件保留原生 em 坐标直接落到对方面板——fitBox 按对方旧件包围盒
  // 重定位会张冠李戴（乓的丶在右下，乒的丿在左下，互换后点落错边，
  // 用户目检发现）；两字 1024 框对齐时原生坐标即真身位置。
  // 异型结构才退回 fitBox 适配。
  const sameOp = (((A.kai || {}).structure) || {}).op &&
    (((A.kai || {}).structure) || {}).op === (((B.kai || {}).structure) || {}).op;
  eng.swapCtx = {
    key: pair.join(""), swapped: false,
    label: `「${fxU.childChar(chA[slot])}」⇄「${fxU.childChar(chB[slot])}」（槽位${slot + 1}）`,
    movers: [
      { g: movA, home: pA,
        away: sameOp ? pB : fxU.fitBox(boxA, outBox(boxB, pB)), lift: 1 },
      { g: movB, home: pB,
        away: sameOp ? pA : fxU.fitBox(boxB, outBox(boxA, pA)), lift: -1 },
    ],
  };
  ctx.setInfo(`偏旁交换 ${pair.join(" / ")}：${eng.swapCtx.label} 交换中…`);
  if (await ctx.anim(950, t => swapFrame(eng.swapCtx, 1, t))) {
    eng.swapCtx.swapped = true;
    ctx.setInfo(`偏旁交换：${eng.swapCtx.label} — 已交换（再按一次换回）`);
  }
}

/* ⑤b 偏旁变形：差异槽位不位移，原地逐笔插值变成对方部首（形状经
   fitBox 适配本字槽位盒；笔画数不等按序比例配对——源笔可被复用/
   拆分，保证两侧终态都是完整目标部首；再按一次反向变回）。 */
async function fx2SwapMorphEffect(ctx) {
  const eng = ctx.engine;
  const pair = ctx.chars("清晴", 2, 2);
  if (pair.length < 2) { ctx.setInfo("请输入两个字（如 清晴）"); return; }
  if (eng.mode === "swapM" && eng.smCtx && eng.smCtx.key === pair.join("")) {
    const c = eng.smCtx, toB = !c.morphed;
    if (await ctx.anim(1100, t =>
        c.items.forEach(it => smFrame(it, toB ? t : 1 - t)))) {
      c.morphed = toB;
      ctx.setInfo(`偏旁变形：${c.label} — ${c.morphed ? "已变形（再按一次变回）" : "已还原"}`);
    }
    return;
  }
  eng.mode = "swapM"; eng.smCtx = null;
  const decos = await ctx.decos(pair, "偏旁变形");
  if (!decos) return;
  if (!decos[0] || !decos[1]) {
    ctx.setInfo("拆解失败：" + pair.filter((c, i) => !decos[i]).join("、"));
    return;
  }
  const A = decos[0], B = decos[1];
  const chA = (((A.kai || {}).structure) || {}).children || [];
  const chB = (((B.kai || {}).structure) || {}).children || [];
  let slot = -1;
  for (let i = 0; i < Math.min(chA.length, chB.length); i++) {
    const a = fxU.childChar(chA[i]), b = fxU.childChar(chB[i]);
    if (a && b && a !== b) { slot = i; break; }
  }
  if (slot < 0) { ctx.setInfo("未找到差异槽位"); return; }
  const ksA = fxU.slotStrokes(A, slot), ksB = fxU.slotStrokes(B, slot);
  if (!ksA.length || !ksB.length) { ctx.setInfo("差异槽位无笔画"); return; }
  const boxA = fxU.strokesBox(A, ksA), boxB = fxU.strokesBox(B, ksB);
  if (!boxA || !boxB) { ctx.setInfo("槽位包围盒计算失败"); return; }
  const root = ctx.root();
  const pA = rowPlace(0, 2, 0.45), pB = rowPlace(1, 2, 0.45);
  const N = 64;                        // 每子路径采样点数（子路径级配对）
  // 一侧的变形项：本侧源笔组 srcKs(本地坐标) → 对方笔组 dstKs
  // 经 fit(对方槽盒→本侧槽盒) 适配后的目标子路径组;按并集配对建项
  const mkItems = (deco, place, srcKs, other, dstKs, fit, tint) => {
    const stat = el("g", { transform: fxU.tf(place) }, root);
    deco.strokes.forEach((s, k) => {
      if (s.failed || !s.path || srcKs.indexOf(k) >= 0) return;
      el("path", { d: s.path, "fill-rule": "nonzero", fill: "#565b63" }, stat);
    });
    const mg = el("g", { transform: fxU.tf(place) }, root);
    const srcSubsList = srcKs.map(k => strokeSubpaths(deco, k, N)).filter(Boolean);
    const items = [];
    const dstSubsCache = {};
    for (const [si, j] of unionPairIdx(srcSubsList.length, dstKs.length)) {
      const srcSubs = srcSubsList[si];
      if (!srcSubs) continue;
      if (!(j in dstSubsCache)) {
        const raw = strokeSubpaths(other, dstKs[j], N);
        dstSubsCache[j] = raw ? fxU.fitSubs(raw, fit) : null;
      }
      if (!dstSubsCache[j]) continue;
      const pEl = el("path", { "fill-rule": "nonzero", fill: tint }, mg);
      items.push({ p: pEl, pairs: fxU.buildRingPairs(srcSubs, dstSubsCache[j]),
                   tint });
    }
    return items;
  };
  // 同型结构保留原生坐标(理由同 swap:fitBox 会把 乒丿⇢乓丶 的终态
  // 錨到旧件盒,点落错边);异型才做槽位盒适配
  const _sameOp = (((A.kai || {}).structure) || {}).op &&
    (((A.kai || {}).structure) || {}).op === (((B.kai || {}).structure) || {}).op;
  const ID_FIT = { s: 1, tx: 0, ty: 0 };
  const fitBtoA = _sameOp ? ID_FIT : fxU.fitBox(boxB, boxA);
  const fitAtoB = _sameOp ? ID_FIT : fxU.fitBox(boxA, boxB);
  const itemsA = mkItems(A, pA, ksA, B, ksB, fitBtoA, "#e6194b");
  const itemsB = mkItems(B, pB, ksB, A, ksA, fitAtoB, "#2b6fb3");
  if (!itemsA.length || !itemsB.length) { ctx.setInfo("重采样失败"); return; }
  eng.smCtx = {
    key: pair.join(""), morphed: false,
    label: `「${fxU.childChar(chA[slot])}」⇢「${fxU.childChar(chB[slot])}」（原地插值）`,
    items: itemsA.concat(itemsB),
  };
  eng.smCtx.items.forEach(it => smFrame(it, 0));
  ctx.setInfo(`偏旁变形 ${pair.join(" / ")}：${eng.smCtx.label} 变形中…`);
  if (await ctx.anim(1100, t =>
      eng.smCtx.items.forEach(it => smFrame(it, t)))) {
    eng.smCtx.morphed = true;
    ctx.setInfo(`偏旁变形：${eng.smCtx.label} — 已变形（再按一次变回）`);
  }
}

/* ⑤c 任意字变形：不要求任何结构/部件关系。B 整字包围盒 fitBox 进 A 盒，
   两侧全部笔画按笔序做双向并集比例配对（同 swapMorph 的 pairs 逻辑），
   每对笔画走子路径级插值；单字居中渲染，A 原地渐变成 B，再按一次变回。 */
async function fx2AnyMorphEffect(ctx) {
  const eng = ctx.engine;
  const pair = ctx.chars("汉字", 2, 2);
  if (pair.length < 2) { ctx.setInfo("请输入两个字（如 汉字）"); return; }
  if (eng.mode === "any" && eng.anyCtx && eng.anyCtx.key === pair.join("")) {
    const c = eng.anyCtx, toB = !c.morphed;
    if (await ctx.anim(1200, t =>
        c.items.forEach(it => smFrame(it, toB ? t : 1 - t)))) {
      c.morphed = toB;
      ctx.setInfo(`任意变形：${c.label} — ${c.morphed ? "已变形（再按一次变回）" : "已还原"}`);
    }
    return;
  }
  eng.mode = "any"; eng.anyCtx = null;
  const decos = await ctx.decos(pair, "任意变形");
  if (!decos) return;
  if (!decos[0] || !decos[1]) {
    ctx.setInfo("拆解失败：" + pair.filter((c, i) => !decos[i]).join("、"));
    return;
  }
  const A = decos[0], B = decos[1];
  const liveA = fxU.liveStrokes(A), liveB = fxU.liveStrokes(B);
  if (!liveA.length || !liveB.length) { ctx.setInfo("无可用笔画"); return; }
  const boxA = fxU.strokesBox(A, liveA.map(o => o.k));
  const boxB = fxU.strokesBox(B, liveB.map(o => o.k));
  if (!boxA || !boxB) { ctx.setInfo("整字包围盒计算失败"); return; }
  const fit = fxU.fitBox(boxB, boxA);      // B 整字盒 → A 盒
  const place = centerPlace(0.78);
  const root = ctx.root();
  const cg = el("g", { transform: fxU.tf(place) }, root);
  const N = 64;
  const dstSubsCache = {}, items = [];
  for (const [si, j] of unionPairIdx(liveA.length, liveB.length)) {
    const srcSubs = strokeSubpaths(A, liveA[si].k, N);
    if (!srcSubs) continue;
    if (!(j in dstSubsCache)) {
      const raw = strokeSubpaths(B, liveB[j].k, N);
      dstSubsCache[j] = raw ? fxU.fitSubs(raw, fit) : null;
    }
    if (!dstSubsCache[j]) continue;
    const pEl = el("path", { "fill-rule": "nonzero",
                             fill: colorOf(liveA[si].k) }, cg);
    items.push({ p: pEl, pairs: fxU.buildRingPairs(srcSubs, dstSubsCache[j]) });
  }
  if (!items.length) { ctx.setInfo("重采样失败"); return; }
  eng.anyCtx = { key: pair.join(""), morphed: false,
    label: `「${pair[0]}」⇢「${pair[1]}」（整字子路径级插值）`, items };
  items.forEach(it => smFrame(it, 0));
  ctx.setInfo(`任意变形 ${pair.join(" / ")}：${eng.anyCtx.label} 变形中…`);
  if (await ctx.anim(1200, t => items.forEach(it => smFrame(it, t)))) {
    eng.anyCtx.morphed = true;
    ctx.setInfo(`任意变形：${eng.anyCtx.label} — 已变形（再按一次变回）`);
  }
}

/* ⑥ 家族变形接龙：声旁家族逐字变形，声旁逐笔插值、义旁飞出/飞入 */
async function fx2FamilyEffect(ctx) {
  const eng = ctx.engine;
  eng.mode = "family";
  const ch0 = ctx.chars("清", 1, 1)[0];
  const first = await ctx.decos([ch0], "家族接龙");
  if (!first) return;
  const d0 = first[0];
  if (!d0) { ctx.setInfo(`「${ch0}」拆解失败`); return; }
  const pho = ((d0.kai || {}).etymology || {}).phonetic;
  if (!pho) {
    ctx.setInfo(`「${ch0}」无声旁数据（etymology 缺失/非形声字，接龙需要后端字源支持）`);
    return;
  }
  let famStr = "";
  try {
    const fam = await api("/api/family?comp=" + encodeURIComponent(pho) + "&kind=phonetic");
    famStr = fam.chars || "";
  } catch (e) { ctx.setInfo("family 接口不可用：" + e.message); return; }
  if (!eng.alive(ctx.token)) return;
  let chs = [...famStr].filter(c => c !== ch0);
  chs = [ch0].concat(chs).slice(0, 5);
  if (chs.length < 2) {
    ctx.setInfo(`声旁「${pho}」家族只有 ${chs.join("")}，无法接龙`); return;
  }
  const decos = (await ctx.decos(chs, "家族接龙") || []).filter(Boolean);
  if (!eng.alive(ctx.token)) return;
  if (decos.length < 2) { ctx.setInfo("家族字拆解成功数不足 2"); return; }
  const place = centerPlace(0.8);
  const cap = d => `<b style="font-size:16px">${d.ch}</b> ` +
    `${(((d.kai || {}).pinyin) || [])[0] || ""} — ${(d.kai || {}).definition || "（无释义）"}` +
    `　· 声旁「${pho}」家族：${decos.map(x => x.ch).join(" ")}`;
  let cur = 0;
  eng.renderSingle(decos[0], place);
  ctx.setInfo(cap(decos[0]));
  while (eng.alive(ctx.token)) {
    if (!await ctx.sleep(1500)) return;
    const a = decos[cur], b = decos[(cur + 1) % decos.length];
    if (!await eng.morphChar(a, b, place, ctx.token, pho)) return;
    cur = (cur + 1) % decos.length;
    ctx.setInfo(cap(decos[cur]));
  }
}

/* ⑦ 生长链：部件仿射滑移到新槽位、新增部件飞入，链尾反向收缩，循环 */
async function fx2GrowEffect(ctx) {
  const eng = ctx.engine;
  eng.mode = "grow";
  const chain = [...document.getElementById("fx2Chain").value];
  const decos = await ctx.decos(chain, "生长链");
  if (!decos) return;
  if (decos.some(d => !d)) {
    ctx.setInfo("链中有字拆解失败：" + chain.filter((c, i) => !decos[i]).join("、"));
    return;
  }
  const place = centerPlace(0.78);
  const cap = d => `<b style="font-size:16px">${d.ch}</b> ` +
    `${(((d.kai || {}).pinyin) || [])[0] || ""} — ${(d.kai || {}).definition || "（无释义）"}`;
  let i = 0, dir = 1;
  eng.renderSingle(decos[0], place);
  ctx.setInfo(cap(decos[0]) + " · 生长链 " + chain.join("→"));
  while (eng.alive(ctx.token)) {
    if (!await ctx.sleep(1400)) return;
    const j = i + dir;
    if (!await eng.growStep(decos[i], decos[j], place, ctx.token)) return;
    i = j;
    ctx.setInfo(cap(decos[i]) +
      (dir > 0 ? " · 生长 " : " · 收缩 ") + chain.join(dir > 0 ? "→" : "←"));
    if (i === decos.length - 1) dir = -1;
    else if (i === 0) dir = 1;
  }
}

/* ⑧ 跨结构转化：一段=部件整体仿射对位，二段=残差逐笔插值；再按反向 */
async function fx2MorphStructEffect(ctx) {
  const eng = ctx.engine;
  if (eng.mode === "xstruct" && eng.morphCtx) {
    const mc = eng.morphCtx; eng.morphCtx = null;
    await eng.structTransition(mc.A, mc.B, ctx.token);
    return;
  }
  eng.mode = "xstruct";
  const inp = ctx.chars("", 0, 2);
  const cand = inp.length >= 2 ? [[inp[0], inp[1]]]
                               : [["峰", "峯"], ["群", "羣"], ["略", "畧"]];
  let A = null, B = null;
  for (const [c1, c2] of cand) {
    const ds = await ctx.decos([c1, c2], "跨结构转化");
    if (!ds) return;
    if (ds[0] && ds[1]) { A = ds[0]; B = ds[1]; break; }
    ctx.setInfo(`「${c1}/${c2}」字库缺字或拆解失败，尝试下一组…`);
  }
  if (!A) {
    ctx.setInfo("候选字对（峰峯 / 群羣 / 略畧）都不可用；请手动输入两个部件相同、结构不同的字");
    eng.mode = null;
    return;
  }
  await eng.structTransition(A, B, ctx.token);
}

/* ⑨ 汇聚景深：笔画自四边外带深度散布 → 错峰汇聚，3 档共享模糊 filter */
async function fx2GatherEffect(ctx, auto) {
  const eng = ctx.engine;
  eng.mode = "gather"; eng.gatherCtx = null;
  const chs = ctx.chars("泽草所生", 1, 6);
  const decos = await ctx.decos(chs, "汇聚景深");
  if (!decos) return;
  const oks = decos.map((d, i) => ({ d, ch: chs[i] })).filter(o => o.d);
  if (!oks.length) { ctx.setInfo("全部拆解失败"); return; }
  const n = oks.length;
  const s = Math.min(0.23, (984 - (n - 1) * 24) / (n * 1024));
  const root = ctx.root();
  const defs = el("defs", {}, eng.svg);
  const tiers = [["fx2BlurFar", 60], ["fx2BlurMid", 26], ["fx2BlurNear", 9]];
  for (const [id, sd] of tiers) {
    const f = el("filter", { id,
      x: "-80%", y: "-80%", width: "260%", height: "260%" }, defs);
    el("feGaussianBlur", { stdDeviation: sd }, f);
  }
  const recs = [];
  oks.forEach((o, i) => {
    const place = { s, ty: 400 - 450 * s,
      tx: 512 - (n * 1024 * s + (n - 1) * 24) / 2 + i * (1024 * s + 24) };
    const cg = el("g", { transform: fxU.tf(place) }, root);
    o.d.strokes.forEach((st, k) => {
      if (st.failed || !st.path) return;
      const g = el("g", {}, cg);
      el("path", { d: st.path, "fill-rule": "nonzero", fill: colorOf(k) }, g);
      const bb = pathBBox(st.path) || { x0: 400, y0: 400, x1: 600, y1: 600 };
      const side = Math.floor(Math.random() * 4);
      let ox, oy;                     // 视口四边外（外层数据坐标）
      if (side === 0) { ox = -260 - Math.random() * 260; oy = -150 + Math.random() * 1150; }
      else if (side === 1) { ox = 1284 + Math.random() * 260; oy = -150 + Math.random() * 1150; }
      else if (side === 2) { ox = -150 + Math.random() * 1320; oy = 1060 + Math.random() * 260; }
      else { ox = -150 + Math.random() * 1320; oy = -300 - Math.random() * 260; }
      const cx = (bb.x0 + bb.x1) / 2, cy = (bb.y0 + bb.y1) / 2;
      const z = 0.4 + Math.random() * 1.8;   // 随机深度 scale 0.4~2.2
      recs.push({ g, cx, cy, z,
        ix: (ox - place.tx) / place.s - cx, iy: (oy - place.ty) / place.s - cy,
        tier: (z < 0.7 || z > 1.9) ? 0 : (z < 0.9 || z > 1.5) ? 1 : 2,
        st0: Math.random() * 0.55, w: 0.45 });
    });
  });
  eng.gatherCtx = { recs, tiers };
  eng.setGather(0);
  document.getElementById("fx2Scrub").value = 0;
  ctx.setInfo(`汇聚景深：${oks.map(o => o.ch).join("")} · ${recs.length} 笔 · ` +
    "拖动滑杆 scrub（模拟折叠屏展开）或点自动播放");
  if (auto) eng.gatherPlay();
}
async function fx2PlayEffect(ctx) {    // ▶ 自动播放：已有场景续播，否则重建
  const eng = ctx.engine;
  if (eng.mode === "gather" && eng.gatherCtx) eng.gatherPlay();
  else await fx2GatherEffect(ctx, true);
}

/* ---- ⑧ 注册 + 非效果类控件绑定 ---- */
fx2.init();
registerFx("fx2Swap", fx2SwapEffect, makeFx2Ctx);
registerFx("fx2SwapM", fx2SwapMorphEffect, makeFx2Ctx);
registerFx("fx2Any", fx2AnyMorphEffect, makeFx2Ctx);
registerFx("fx2Family", fx2FamilyEffect, makeFx2Ctx);
registerFx("fx2Grow", fx2GrowEffect, makeFx2Ctx);
registerFx("fx2Morph", fx2MorphStructEffect, makeFx2Ctx);
registerFx("fx2Gather", ctx => fx2GatherEffect(ctx, true), makeFx2Ctx);
registerFx("fx2Play", fx2PlayEffect, makeFx2Ctx);
document.getElementById("fx2Stop").onclick = () => { fx2.stopAll(); fx2.setInfo("已停止"); };
document.getElementById("fx2Scrub").addEventListener("input", e => {
  if (fx2.mode === "gather" && fx2.gatherCtx) {
    ++fx2.token; cancelAnimationFrame(fx2.raf);   // 手动 scrub 时停自动播放
    fx2.setGather((+e.target.value) / 100);
  }
});
