/* reactionRules.js — 属性组合→反应。规则按优先级匹配，可外部 register。 */
"use strict";
(function (root) {
  const rules = [];
  function register(rule) { rules.push(rule); rules.sort((a,b) => (b.priority||0)-(a.priority||0)); }
  function hasPair(a, b, x, y) { return (a.has(x)&&b.has(y)) || (a.has(y)&&b.has(x)); }

  register({ id:"crystalDiffuse", priority:200, when:(a,b)=>hasPair(a,b,"crystal","diffuse"),
    effect:"none", label:"结晶 × 扩散", detail:"稳定共存，不发生反应" });
  register({ id:"iceGrowth", priority:190, when:(a,b)=>hasPair(a,b,"freeze","growth"),
    effect:"none", label:"冻结 × 生长", detail:"当前规则下不反应" });
  register({ id:"fireGrowth", priority:150, when:(a,b)=>hasPair(a,b,"fire","growth"),
    effect:"burn", label:"燃烧 × 生长", detail:"植物纤维燃烧并逐渐焦化" });
  register({ id:"fireWet", priority:150, when:(a,b)=>hasPair(a,b,"fire","wet"),
    effect:"steam", label:"燃烧 × 潮湿", detail:"水分受热蒸发" });
  register({ id:"fireFreeze", priority:150, when:(a,b)=>hasPair(a,b,"fire","freeze"),
    effect:"melt", label:"燃烧 × 冻结", detail:"冰层融化并滴落" });
  register({ id:"fireShock", priority:160, when:(a,b)=>hasPair(a,b,"fire","shock"),
    effect:"explosion", label:"燃烧 × 触电", detail:"高能点火产生爆炸" });
  register({ id:"wetGrowth", priority:150, when:(a,b)=>hasPair(a,b,"wet","growth"),
    effect:"nuclei", derived:"nuclei", label:"潮湿 × 生长", detail:"生成大量活性晶核" });
  register({ id:"wetFreeze", priority:150, when:(a,b)=>hasPair(a,b,"wet","freeze"),
    effect:"hardFreeze", label:"潮湿 × 冻结", detail:"水分凝结成冰" });
  register({ id:"wetShock", priority:150, when:(a,b)=>hasPair(a,b,"wet","shock"),
    effect:"electrolysis", label:"潮湿 × 触电", detail:"电解与连续放电" });
  register({ id:"freezeShock", priority:150, when:(a,b)=>hasPair(a,b,"freeze","shock"),
    effect:"superconduct", label:"冻结 × 触电", detail:"局部进入高速超导态" });
  register({ id:"crystalAny", priority:80, when:(a,b)=>
      (a.has("crystal") && [...b].some(x=>x!=="crystal"&&x!=="diffuse")) ||
      (b.has("crystal") && [...a].some(x=>x!=="crystal"&&x!=="diffuse")),
    effect:"particles", label:"属性 × 结晶", detail:"冲击晶格，析出矢量粒子" });
  register({ id:"diffuseAny", priority:70, when:(a,b)=>
      (a.has("diffuse") && [...b].some(x=>x!=="diffuse"&&x!=="crystal")) ||
      (b.has("diffuse") && [...a].some(x=>x!=="diffuse"&&x!=="crystal")),
    effect:"spread", label:"属性 × 扩散", detail:"属性随风扩展到两字范围" });

  function resolve(setA, setB) { return rules.find(r => r.when(setA, setB)) || null; }
  root.ElementalReactions = { rules, register, resolve };
})(globalThis);
