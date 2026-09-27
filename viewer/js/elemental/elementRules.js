/* elementRules.js — 偏旁语义→属性。只描述“是什么”，不包含动画实现。 */
"use strict";
(function (root) {
  const attributes = {
    fire:    { name:"燃烧·高温", color:"#c94132", components:["火","灬"], keywords:["fire","flame","heat"] },
    wet:     { name:"潮湿", color:"#5ac8fa", components:["水","氵","氺"], keywords:["water","liquid","river"] },
    growth:  { name:"缠绕·生长", color:"#82d66f", components:["艹","艸","木","山"], keywords:["plant","grass","tree","wood","mountain"] },
    crystal: { name:"结晶", color:"#d9bd72", components:["金","钅","石","土","王","玉"], keywords:["metal","stone","mineral","jade","earth"] },
    diffuse: { name:"扩散", color:"#55d6c2", components:["风","風"], keywords:["wind"] },
    freeze:  { name:"冻结", color:"#bcefff", components:["冫","⺀"], keywords:["ice"] },
    shock:   { name:"触电", color:"#9b6cff", components:[], strongChars:["雷","电","電","霆"], keywords:[] },
  };

  /* 同一属性内仍保留细分色：草浅绿、木深褐。属性相同，视觉基色可不同。 */
  const componentColors = {
    "艹":"#a6df79", "艸":"#a6df79", "木":"#54301f", "山":"#6f7f54",
    "火":"#c94132", "灬":"#c94132", "水":"#5ac8fa", "氵":"#5ac8fa", "氺":"#5ac8fa",
  };

  function walk(node, path, out) {
    if (!node) return out;
    if (node.char) out.push({ char:node.char, path:path.slice() });
    (node.children || []).forEach((child, i) => walk(child, path.concat(i), out));
    return out;
  }
  function nodeAt(node, path) {
    let cur = node;
    for (const i of (path || [])) {
      if (!cur || !cur.children || !cur.children[i]) return null;
      cur = cur.children[i];
    }
    return cur;
  }
  function hintText(kai) {
    const ety = kai.etymology || {};
    return [ety.hint, ety.semantic, kai.radical].filter(Boolean).join(" ").toLowerCase();
  }
  function matchingAttribute(component) {
    for (const [id, def] of Object.entries(attributes))
      if (def.components.includes(component)) return id;
    return null;
  }
  function detect(ch, deco) {
    const kai = (deco || {}).kai || {};
    const leaves = walk(kai.structure, [], []);
    const comps = new Set(leaves.map(x => x.char));
    if (kai.radical) comps.add(kai.radical);
    const hint = hintText(kai);
    const found = [];
    for (const [id, def] of Object.entries(attributes)) {
      const strong = (def.strongChars || []).includes(ch);
      const component = def.components.find(c => comps.has(c));
      /* semantic hint 只作字典缺部件时的兜底，避免“雨”因 hint 提到 thunderbolt
         自动变触电。shock 没有关键词兜底，严格采用强关联字表。 */
      const keyword = !component && def.keywords.some(k => hint.includes(k));
      if (strong || component || keyword)
        found.push({ id, component:component || null, strong, color:componentColors[component] || def.color });
    }
    return found;
  }
  function strokeAttributes(deco, detected) {
    const kai = (deco || {}).kai || {}, matches = kai.matches || [];
    return ((deco || {}).strokes || []).map((stroke, index) => {
      const node = nodeAt(kai.structure, matches[index]);
      const component = node && node.char;
      const attr = matchingAttribute(component);
      const ids = [];
      if (attr && detected.some(d => d.id === attr)) ids.push(attr);
      for (const d of detected) if (d.strong && !ids.includes(d.id)) ids.push(d.id);
      return { index, component, attributes:ids, failed:!!stroke.failed };
    });
  }
  root.ElementalRules = { attributes, componentColors, detect, strokeAttributes, nodeAt };
})(globalThis);
