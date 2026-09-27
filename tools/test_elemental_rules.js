"use strict";
const assert = require("assert");
require("../viewer/js/elemental/elementRules.js");
require("../viewer/js/elemental/reactionRules.js");

const E = globalThis.ElementalRules;
const RX = globalThis.ElementalReactions;
const leaf = char => ({ char });
const deco = (radical, children, matches=[]) => ({
  kai:{ radical, structure:{ op:"⿰", children:children.map(leaf) }, matches },
  strokes:matches.map(()=>({ failed:false }))
});
const ids = (ch,d) => E.detect(ch,d).map(x=>x.id);
const resolve = (a,b) => RX.resolve(new Set([a]),new Set([b]));

assert.deepStrictEqual(ids("江",deco("氵",["氵","工"])),["wet"]);
assert.deepStrictEqual(ids("林",deco("木",["木","木"])),["growth"]);
assert(ids("雷",deco("雨",["雨","田"])).includes("shock"));
assert(!ids("雨",deco("雨",["雨"])).includes("shock"));
assert.strictEqual(resolve("fire","growth").effect,"burn");
assert.strictEqual(resolve("crystal","diffuse").effect,"none");
assert.strictEqual(resolve("freeze","growth").effect,"none");
assert.strictEqual(resolve("wet","growth").derived,"nuclei");
assert.strictEqual(resolve("freeze","shock").effect,"superconduct");
assert.strictEqual(resolve("fire","crystal").effect,"particles");
console.log("elemental rules: 10 assertions passed");
