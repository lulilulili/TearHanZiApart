/* fxSingle.js — ⑦ 动画实验场：单字效果库。
   fx 为场引擎（场景装载/逐帧驱动/复位/场景保护），每个效果一个
   async effect(ctx)，ctx 由 makeFxCtx() 构造并经注册表绑定按钮。
   依赖 core.js（el/glyphGroup/colorOf/fmt/state/registerFx）与 fxUtils.js。 */
"use strict";

/* 部首→特效规则。启动时 fetch /api/radicals（部首语义 registry，
   tools/build_radicals.py 产物）构建：radical+variants→color 映射，
   particle 字段留存备用；fetch 失败（file:// 直开/后端未更新）退回
   FALLBACK_RULES。规则序：种子粒子系在前且保持原表优先级
   （①水蓝 ②火红 ③鬼黑 ④艹绿 ⑤木棕），其余兜底色按部首字典序，
   故命中判定/染色行为与硬编码时代一致。 */
const FALLBACK_RULES = [
  { comps: ["氵", "水", "氺"], name: "水", color: "#1e6fd9", particle: "water" },
  { comps: ["灬", "火"], name: "火", color: "#d92b2b", particle: "fire" },
  { comps: ["鬼"], name: "鬼", color: "#1b1b1f", particle: "smoke" },
  { comps: ["艹"], name: "艹", color: "#2e9e44", particle: "leaf" },
  { comps: ["木"], name: "木", color: "#8b5a2b", particle: "wood" },
];
const PARTICLE_ORDER = { water: 0, fire: 1, smoke: 2, leaf: 3, wood: 4 };
let RADICAL_RULES = FALLBACK_RULES;

function BuildRadicalRules(registry) {
  const byGroup = new Map();   // 同源位形组去重：组内取成员数最多者命名
  for (const e of registry.radicals || []) {
    if (!e.effect || !e.effect.color) continue;
    const comps = (e.variants && e.variants.length) ? e.variants : [e.radical];
    const key = comps.join("");
    const cur = byGroup.get(key);
    if (!cur || (e.count || 0) > cur.count)
      byGroup.set(key, { comps, name: e.radical, color: e.effect.color,
                         particle: e.effect.particle || null, count: e.count || 0 });
  }
  const rules = [...byGroup.values()];
  rules.sort((a, b) => {
    const pa = a.particle in PARTICLE_ORDER ? PARTICLE_ORDER[a.particle] : 9;
    const pb = b.particle in PARTICLE_ORDER ? PARTICLE_ORDER[b.particle] : 9;
    return (pa - pb) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  });
  return rules;
}

fetch("/api/radicals").then(r => r.ok ? r.json() : Promise.reject(r.status))
  .then(reg => {
    const rules = BuildRadicalRules(reg);
    if (rules.length) RADICAL_RULES = rules;
  })
  .catch(() => {});   // 失败保留 FALLBACK_RULES

/* ---- ⑦ 场引擎：场景装载 + 逐帧驱动 + 复位（含场景保护：会整场重建
   #svgFx 的效果置 sceneDirty，复位/旧效果前自动还原原始笔画场景） ---- */
const fx = {
  items: [], raf: 0, mode: null,
  sceneDirty: false, _last: null, faceG: null, faceIdx: 0,
  load(strokes) {
    this._last = strokes; this.sceneDirty = false; this.faceG = null;
    this.stop();
    const svg = document.getElementById("svgFx");
    svg.innerHTML = "";
    const root = glyphGroup(svg);
    this.items = strokes.map((s, i) => {
      const g = el("g", {}, root);
      const p = el("path", { d: s.path, fill: s.color, "fill-rule": "nonzero" }, g);
      let bb;
      try { bb = p.getBBox(); } catch (e) { bb = { x: 400, y: 400, width: 200, height: 200 }; }
      return { g, p, color: s.color,
               cx: bb.x + bb.width / 2, cy: bb.y + bb.height / 2,
               bx: bb.x, by: bb.y, bw: bb.width, bh: bb.height,
               seed: Math.sin(i * 999.7) * 0.5 + 0.5 };
    });
    this.setInfo("");
  },
  stop() { cancelAnimationFrame(this.raf); this.raf = 0; this.mode = null; },
  setInfo(html) { document.getElementById("fxInfo").innerHTML = html; },
  resetPose() {
    if (this.sceneDirty && this._last) {   // 场景被整场重建过 → 重装原始场景
      this.load(this._last);
      return;
    }
    for (const it of this.items) {
      it.g.removeAttribute("transform");
      it.g.setAttribute("opacity", 1);
      it.p.setAttribute("fill", it.color);
      it.p.removeAttribute("fill-opacity");
    }
  },
  reset() { this.stop(); this.resetPose(); this.setInfo(""); },
  // 通用逐帧驱动：per(it,i,t01) 返回是否仍在动
  run(durMs, stagMs, per, done) {
    this.stop();
    const t0 = performance.now();
    const step = now => {
      let alive = false;
      this.items.forEach((it, i) => {
        const t = Math.min(1, Math.max(0, (now - t0 - i * stagMs) / durMs));
        per(it, i, t);
        if (t < 1) alive = true;
      });
      if (alive) this.raf = requestAnimationFrame(step);
      else { this.raf = 0; if (done) done(); }
    };
    this.raf = requestAnimationFrame(step);
  },
};

/* ---- ⑦ ctx 工厂：每次按钮点击构造一份（decos[0] 即当前拆解结果） ---- */
function makeFxCtx() {
  const svg = document.getElementById("svgFx");
  return {
    svg,
    root: () => glyphGroup(svg),
    chars: [state.ch],
    decos: [state.result],
    setInfo: html => fx.setInfo(html),
    anim: (dur, frame) => new Promise(res => {   // 简单帧驱动（⑦ 无 token 竞争）
      const t0 = performance.now();
      const step = now => {
        const t = Math.min(1, (now - t0) / dur);
        frame(t);
        if (t < 1) fx.raf = requestAnimationFrame(step); else res(true);
      };
      fx.raf = requestAnimationFrame(step);
    }),
    token: 0,
    engine: fx,
  };
}

/* 1a 飞入组字：四面八方 + 自旋 → 归位（缓出） */
async function assembleEffect(ctx) {
  const eng = ctx.engine;
  eng.reset(); eng.mode = "assemble";
  const easeOut = t => 1 - Math.pow(1 - t, 3);
  eng.items.forEach((it, i) => {
    const ang = i * 2.399963 + it.seed * 6.28;   // 黄金角错开方向
    it.fx = { dx: Math.cos(ang) * 1500, dy: Math.sin(ang) * 1500,
              rot: (it.seed - 0.5) * 540 };
  });
  eng.run(700, 110, (it, i, t) => {
    const k = 1 - easeOut(t);
    it.g.setAttribute("opacity", t === 0 ? 0 : 1);
    it.g.setAttribute("transform",
      `translate(${it.fx.dx * k} ${it.fx.dy * k}) ` +
      `rotate(${it.fx.rot * k} ${it.cx} ${it.cy})`);
  });
}

/* 1b 爆炸飞散：归位状态 → 加速飞出 + 自旋 + 渐隐 */
async function explodeEffect(ctx) {
  const eng = ctx.engine;
  eng.stop(); eng.mode = "explode"; eng.resetPose();
  const easeIn = t => t * t * t;
  eng.items.forEach((it, i) => {
    const ang = Math.atan2(it.cy - 450, it.cx - 512) + (it.seed - 0.5) * 0.9;
    it.fx = { dx: Math.cos(ang) * 1600, dy: Math.sin(ang) * 1600,
              rot: (it.seed - 0.5) * 720 };
  });
  eng.run(800, 40, (it, i, t) => {
    const k = easeIn(t);
    it.g.setAttribute("opacity", Math.max(0, 1 - k * 1.15));
    it.g.setAttribute("transform",
      `translate(${it.fx.dx * k} ${it.fx.dy * k}) ` +
      `rotate(${it.fx.rot * k} ${it.cx} ${it.cy})`);
  });
}

/* 2 轰然倒塌：自上而下逐笔倾斜→坠落→触地小回弹（数据坐标 y 向上） */
async function collapseEffect(ctx) {
  const eng = ctx.engine;
  eng.reset(); eng.mode = "collapse";
  const order = [...eng.items].sort((a, b) => (b.by + b.bh) - (a.by + a.bh));
  order.forEach((it, rank) => { it.fx = {
    rank,
    pivotX: it.seed > 0.5 ? it.bx : it.bx + it.bw,   // 绕左/右下角倒
    pivotY: it.by,
    tilt: (it.seed > 0.5 ? 1 : -1) * (10 + it.seed * 8),
    rot: (it.seed > 0.5 ? 1 : -1) * (75 + it.seed * 30),
    drift: (it.seed - 0.5) * 140,
  }; });
  const TILT = 0.22;   // 前 22% 倾斜，其后自由落体
  eng.run(1150, 130, (it, i, t) => {
    const f = it.fx;
    // 目标：底边落到 y≈-60 的“地面”（数据坐标向上为正）
    const groundDy = -(it.by + 60 + f.rank * 6);
    if (t < TILT) {
      const k = t / TILT;
      it.g.setAttribute("transform",
        `rotate(${f.tilt * k * k} ${f.pivotX} ${f.pivotY})`);
    } else {
      let k = (t - TILT) / (1 - TILT);
      let fall = k * k;                       // 重力加速
      if (k > 0.86) fall = 1 - (1 - k) / 0.14 * 0.055 * Math.sin((k - 0.86) / 0.14 * Math.PI); // 触地回弹
      it.g.setAttribute("transform",
        `translate(${f.drift * k} ${groundDy * fall}) ` +
        `rotate(${(f.tilt + (f.rot - f.tilt) * k) } ${f.pivotX} ${f.pivotY})`);
    }
  });
}

/* 3 灯光闪烁：每笔不同相位的彩色霓虹循环 */
async function lightsEffect(ctx) {
  const eng = ctx.engine;
  eng.reset(); eng.mode = "lights";
  const t0 = performance.now();
  const step = now => {
    const t = (now - t0) / 1000;
    eng.items.forEach((it, i) => {
      const hue = (t * 140 + i * 47) % 360;
      const pulse = 0.7 + 0.3 * Math.sin(t * 7 + i * 1.7);
      it.p.setAttribute("fill", `hsl(${hue} 92% 56%)`);
      it.p.setAttribute("fill-opacity", pulse.toFixed(3));
    });
    eng.raf = requestAnimationFrame(step);
  };
  eng.raf = requestAnimationFrame(step);
  ctx.setInfo("霓虹模式：每笔独立相位循环变色（再点其他按钮停止）");
}

/* 4 部首识别色：结构分解(≤2级) + 字本身 匹配规则表 */
async function radicalColorEffect(ctx) {
  const eng = ctx.engine;
  eng.reset(); eng.mode = "radical";
  const comps = ((ctx.decos[0].kai || {}).components) || [];
  const present = new Map();   // char -> level (0=字本身)
  present.set(ctx.chars[0], 0);
  comps.forEach(c => { if (!present.has(c.char)) present.set(c.char, c.level); });
  let hit = null;
  const hitList = [];
  for (const rule of RADICAL_RULES) {
    const found = rule.comps.filter(c => present.has(c));
    if (found.length) {
      hitList.push(`${rule.name}系(${found.map(c =>
        c + (present.get(c) ? `·${present.get(c)}级` : "·整字")).join(" ")})`);
      if (!hit) hit = { rule, found };
    }
  }
  if (hit) {
    for (const it of eng.items) it.p.setAttribute("fill", hit.rule.color);
    ctx.setInfo(`命中 <b style="color:${hit.rule.color}">${hit.rule.name}系</b>` +
      `（${hit.found.join("、")}）→ 全字染色。全部命中：${hitList.join("；")}` +
      `<br>结构分解：${comps.map(c => `${c.char}<sub>${c.level}</sub>`).join(" ")}`);
  } else {
    ctx.setInfo(`无规则命中。结构分解：` +
      (comps.length ? comps.map(c => `${c.char}<sub>${c.level}</sub>`).join(" ") : "（无数据）"));
  }
}

/* ① 义音分层：义旁粒子沿 median 流动 + 声旁描边脉冲呼吸 */
async function etymEffect(ctx) {
  const eng = ctx.engine;
  eng.reset(); eng.mode = "etym"; eng.sceneDirty = true;
  const r = ctx.decos[0], kai = r.kai || {};
  const ety = kai.etymology;
  if (!ety || (ety.type && ety.type !== "pictophonetic") || !ety.phonetic) {
    ctx.setInfo(ety
      ? `「${ctx.chars[0]}」非形声字（${ety.type || "?"}${ety.hint ? " · " + ety.hint : ""}）`
      : "后端未提供字源数据（etymology 缺失：非形声字、旧缓存或后端未更新）");
    return;
  }
  let semSlot = fxU.slotOfComp(r, ety.semantic);
  let phoSlot = fxU.slotOfComp(r, ety.phonetic);
  const nCh = (((kai.structure) || {}).children || []).length;
  if (semSlot < 0 && phoSlot >= 0 && nCh === 2) semSlot = 1 - phoSlot;
  if (phoSlot < 0 && semSlot >= 0 && nCh === 2) phoSlot = 1 - semSlot;
  let semColor = "#1e6fd9";
  for (const rule of RADICAL_RULES)
    if (rule.comps.some(c => fxU.sameComp(c, ety.semantic))) {
      semColor = rule.color; break;
    }
  const phoColor = "#e08a2e";
  const root = ctx.root();
  const defs = el("defs", {}, ctx.svg);
  const flt = el("filter", { id: "fxEtymGlow",
    x: "-60%", y: "-60%", width: "220%", height: "220%" }, defs);
  el("feGaussianBlur", { stdDeviation: 14, result: "b" }, flt);
  const mrg = el("feMerge", {}, flt);
  el("feMergeNode", { in: "b" }, mrg);
  el("feMergeNode", { in: "SourceGraphic" }, mrg);
  const particles = [], pulses = [];
  r.strokes.forEach((s, k) => {
    if (s.failed || !s.path) return;
    const sl = (r.slotOf || [])[k];
    const isSem = semSlot >= 0 && sl === semSlot;
    const isPho = phoSlot >= 0 && sl === phoSlot;
    const p = el("path", { d: s.path, "fill-rule": "nonzero",
      fill: isSem ? semColor : isPho ? phoColor : "#b9bec6" }, root);
    if (isPho) {
      p.setAttribute("stroke", phoColor);
      p.setAttribute("filter", "url(#fxEtymGlow)");
      pulses.push(p);
    }
    if (isSem && s.median && s.median.length > 1) {
      const cum = fxU.medianCum(s.median);
      const L = cum[cum.length - 1];
      const nPar = L > 420 ? 3 : 2;
      for (let j = 0; j < nPar; j++) {
        const c = el("circle", { r: 15, fill: "#eaf4ff",
          stroke: semColor, "stroke-width": 5 }, root);
        particles.push({ c, m: s.median, cum, off: (L / nPar) * j,
                         speed: 200 + 90 * ((k * 7 + j * 13) % 5) / 4 });
      }
    }
  });
  const t0 = performance.now();
  const step = now => {
    const t = (now - t0) / 1000;
    for (const pr of particles) {
      const q = fxU.medianPointAt(pr.m, pr.cum, pr.off + t * pr.speed);
      pr.c.setAttribute("cx", fmt(q[0])); pr.c.setAttribute("cy", fmt(q[1]));
    }
    const b = 0.5 + 0.5 * Math.sin(t * 4.2);
    for (const p of pulses) {
      p.setAttribute("stroke-width", fmt(7 + 9 * b));
      p.setAttribute("stroke-opacity", fmt(0.35 + 0.45 * b));
    }
    eng.raf = requestAnimationFrame(step);
  };
  eng.raf = requestAnimationFrame(step);
  ctx.setInfo(`形声字：义旁 <b style="color:${semColor}">${ety.semantic || "?"}</b>（粒子流动）` +
    ` ＋ 声旁 <b style="color:${phoColor}">${ety.phonetic}</b>（描边脉冲）` +
    `${(kai.pinyin || [])[0] ? " · " + kai.pinyin[0] : ""}` +
    `${ety.hint ? " · " + ety.hint : ""}` +
    (semSlot < 0 && phoSlot < 0 ? "　<span class='warn'>槽位未定位，仅按缺省着色</span>" : ""));
}

/* ② 笔顺书写（卡拉OK）：median 折线 + clipPath 轮廓 + dashoffset 逐笔书写 */
async function writeEffect(ctx) {
  const eng = ctx.engine;
  eng.reset(); eng.mode = "write"; eng.sceneDirty = true;
  const r = ctx.decos[0];
  const root = ctx.root();
  const defs = el("defs", {}, ctx.svg);
  const py = ((r.kai || {}).pinyin || [])[0] || "";
  const title = el("text", { x: 512, y: 80, "text-anchor": "middle",
    "font-size": 64, fill: "#333",
    "font-family": "'Segoe UI','Microsoft YaHei',sans-serif" }, ctx.svg);
  title.textContent = py || "（无拼音数据）";
  const seq = [];
  r.strokes.forEach((s, k) => {
    if (s.failed || !s.path) return;
    const base = el("path", { d: s.path, fill: "#ececec", stroke: "#d5d5d5",
      "stroke-width": 2, "fill-rule": "nonzero" }, root);
    if (!s.median || s.median.length < 2) {
      seq.push({ base, color: colorOf(s.index), instant: true });
      return;
    }
    const cid = "fxw_clip" + k;
    const cp = el("clipPath", { id: cid }, defs);
    el("path", { d: s.path, "clip-rule": "nonzero" }, cp);
    const mp = s.median.map((p, j) =>
      (j ? "L" : "M") + fmt(p[0]) + " " + fmt(p[1])).join(" ");
    const wpx = Math.max(60, (s.width || 110) * 1.3);
    const track = el("path", { d: mp, fill: "none", stroke: colorOf(s.index),
      "stroke-width": wpx, "stroke-linecap": "round",
      "stroke-linejoin": "round", "clip-path": `url(#${cid})` }, root);
    const len = polylineLen(s.median) + wpx;
    track.setAttribute("stroke-dasharray", len + " " + len);
    track.setAttribute("stroke-dashoffset", len);
    seq.push({ base, track, len, color: colorOf(s.index), type: s.type });
  });
  const n = seq.length;
  let idx = 0, prog = 0, last = performance.now();
  const hl = (it, on) => {
    it.base.setAttribute("stroke", on ? "#4c8dff" : "#d5d5d5");
    it.base.setAttribute("stroke-width", on ? 10 : 2);
  };
  const step = now => {
    const dt = (now - last) / 1000; last = now;
    const it = seq[idx];
    if (!it) { eng.raf = 0; ctx.setInfo(`书写完成：${n} 笔${py ? " · " + py : ""}`); return; }
    if (it.instant) {
      it.base.setAttribute("fill", it.color);
      idx++; eng.raf = requestAnimationFrame(step); return;
    }
    if (prog === 0) {
      hl(it, true);
      ctx.setInfo(`笔顺书写 ${idx + 1} / ${n} · 当前 ${it.type || ""}${py ? " · " + py : ""}`);
    }
    const dur = Math.max(0.32, it.len / 1000);
    prog += dt / dur;
    if (prog >= 1) {
      it.track.setAttribute("stroke-dashoffset", 0);
      it.base.setAttribute("fill", it.color);
      hl(it, false);
      idx++; prog = 0;
    } else it.track.setAttribute("stroke-dashoffset", fmt(it.len * (1 - prog)));
    eng.raf = requestAnimationFrame(step);
  };
  eng.raf = requestAnimationFrame(step);
}

/* ③ 洞内表情：holes[i] bbox 内矢量脸，眨眼 + 瞳孔游移；多洞循环切换 */
async function faceEffect(ctx) {
  const eng = ctx.engine;
  const r = ctx.decos[0];
  const holes = r.holes || [];
  if (!holes.length) {
    eng.stop(); eng.mode = null;
    ctx.setInfo(`「${ctx.chars[0]}」无围合空腔（holeCount=${r.holeCount || 0}）。试试 吞 / 回 / 国 / 目。`);
    return;
  }
  if (eng.mode === "face" && eng.faceG) {
    eng.faceIdx = (eng.faceIdx + 1) % holes.length;
    eng.faceG.remove(); eng.faceG = null;
    cancelAnimationFrame(eng.raf); eng.raf = 0;
  } else {
    eng.reset(); eng.mode = "face"; eng.faceIdx = 0;
  }
  eng.sceneDirty = true;
  const hb = holes[eng.faceIdx];
  const x0 = hb[0], y0 = hb[1], x1 = hb[2], y1 = hb[3];
  const g = el("g", {}, ctx.svg);      // 屏幕坐标（不翻转），叠在字形之上
  eng.faceG = g;
  const cx = (x0 + x1) / 2, cy = 900 - (y0 + y1) / 2;   // 数据 y → 屏幕 y
  const w = (x1 - x0) * 0.6, h = (y1 - y0) * 0.6;
  const eyeRx = Math.max(8, w * 0.16);
  const eyeRy = Math.max(8, Math.min(h * 0.22, eyeRx * 1.4));
  const ey = cy - h * 0.16;
  const lw = Math.max(3, w * 0.025);
  const eyes = [];
  for (const ex of [cx - w * 0.22, cx + w * 0.22]) {
    const eg = el("g", {}, g);
    el("ellipse", { cx: ex, cy: ey, rx: eyeRx, ry: eyeRy,
      fill: "#fff", stroke: "#222", "stroke-width": lw }, eg);
    const pu = el("circle", { cx: ex, cy: ey,
      r: Math.max(3, eyeRx * 0.42), fill: "#222" }, eg);
    eyes.push({ eg, pu, ex, ey });
  }
  const mw = w * 0.3, my = cy + h * 0.22;
  el("path", { d: `M ${fmt(cx - mw)} ${fmt(my)} Q ${fmt(cx)} ${fmt(my + Math.max(8, h * 0.18))} ${fmt(cx + mw)} ${fmt(my)}`,
    fill: "none", stroke: "#222", "stroke-width": Math.max(4, w * 0.03),
    "stroke-linecap": "round" }, g);
  const t0 = performance.now();
  const step = now => {
    const t = (now - t0) / 1000;
    const cyc = t % 3.2;
    let sy = 1;
    if (cyc > 2.9)                    // 眨眼：0.3s 内合上再睁开
      sy = Math.max(0.08, Math.abs(Math.cos((cyc - 2.9) / 0.3 * Math.PI)));
    const px = Math.cos(t * 0.9) * eyeRx * 0.35;
    const pyy = Math.sin(t * 1.3) * eyeRy * 0.3;
    for (const e2 of eyes) {
      e2.eg.setAttribute("transform",
        `translate(${fmt(e2.ex)} ${fmt(e2.ey)}) scale(1 ${fmt(sy)}) translate(${fmt(-e2.ex)} ${fmt(-e2.ey)})`);
      e2.pu.setAttribute("cx", fmt(e2.ex + px));
      e2.pu.setAttribute("cy", fmt(e2.ey + pyy));
    }
    eng.raf = requestAnimationFrame(step);
  };
  eng.raf = requestAnimationFrame(step);
  ctx.setInfo(`洞内表情：空腔 ${eng.faceIdx + 1}/${holes.length}` +
    (holes.length > 1 ? "（再点切换下一个洞）" : "") +
    ` · bbox [${x0},${y0}]–[${x1},${y1}]`);
}

/* ④ 音调入场：kai.pinyin[0] 声调 → 四种入场缓动 */
async function toneEffect(ctx) {
  const eng = ctx.engine;
  eng.reset(); eng.mode = "tone";
  if (!eng.items.length) return;
  const py = ((ctx.decos[0].kai || {}).pinyin || [])[0] || "";
  const tn = fxU.toneOf(py);
  const names = { 1: "一声·平：匀速平移入场", 2: "二声·扬：自下弹升(easeOutBack)",
                  3: "三声·拐：先降后升折线入场", 4: "四声·降：自上急坠+落地微震" };
  ctx.setInfo(`${py || "（无拼音，按一声处理）"} → ${names[tn]}`);
  eng.items.forEach(it => it.g.setAttribute("opacity", 0));
  eng.run(tn === 4 ? 1000 : 900, 55, (it, i, t) => {
    it.g.setAttribute("opacity", t <= 0 ? 0 : 1);
    let dx = 0, dy = 0, rot = 0;      // 数据坐标：+y 向上
    if (tn === 1) {
      dx = -750 * (1 - t);
    } else if (tn === 2) {
      dy = -650 * (1 - fxU.easeOutBack(t));
    } else if (tn === 3) {
      dx = -560 * (1 - t);
      if (t < 0.5) dy = 420 - 590 * fxU.easeInCubic(t / 0.5);
      else dy = -170 * (1 - fxU.easeOutCubic((t - 0.5) / 0.5));
    } else {
      if (t < 0.72) dy = 820 * (1 - fxU.easeInCubic(t / 0.72));
      else {
        const u = (t - 0.72) / 0.28;
        dy = Math.sin(u * Math.PI * 3) * 26 * (1 - u);
        rot = Math.sin(u * Math.PI * 3) * 1.6 * (1 - u);
      }
    }
    it.g.setAttribute("transform", `translate(${fmt(dx)} ${fmt(dy)})` +
      (rot ? ` rotate(${fmt(rot)} ${fmt(it.cx)} ${fmt(it.cy)})` : ""));
  }, () => ctx.setInfo(`${py || ""} 第 ${tn} 声 · 入场完成`));
}

/* 复位：恢复原始笔画场景 */
async function fxResetEffect(ctx) { ctx.engine.reset(); }

/* ---- ⑦ 注册（新效果 = 新函数 + 一行注册） ---- */
registerFx("fxAssemble", assembleEffect, makeFxCtx);
registerFx("fxExplode", explodeEffect, makeFxCtx);
registerFx("fxCollapse", collapseEffect, makeFxCtx);
registerFx("fxLights", lightsEffect, makeFxCtx);
registerFx("fxRadical", radicalColorEffect, makeFxCtx);
registerFx("fxEtym", etymEffect, makeFxCtx);
registerFx("fxWrite", writeEffect, makeFxCtx);
registerFx("fxFace", faceEffect, makeFxCtx);
registerFx("fxTone", toneEffect, makeFxCtx);
registerFx("fxReset", fxResetEffect, makeFxCtx);
