/* effectPlugins.js — 动画插件。mount 只消费卡片与强度，不参与语义判断。 */
"use strict";
(function (root) {
  const attribute = new Map(), reaction = new Map();
  const registerAttribute = (id, plugin) => attribute.set(id, plugin);
  const registerReaction = (id, plugin) => reaction.set(id, plugin);
  function layer(card) { return card.querySelector(".fx-layer"); }
  function particles(card, glyph, color, count, speed) {
    const box = layer(card); if (!box) return;
    for (let i=0;i<count;i++) {
      const p=document.createElement("span"); p.className="particle"; p.textContent=glyph;
      p.style.setProperty("--x",(12+Math.random()*76)+"%");
      p.style.setProperty("--delay",(-Math.random()*2)+"s");
      p.style.setProperty("--dur",((speed||1.8)+Math.random())+"s");
      p.style.setProperty("--pc",color); box.appendChild(p);
    }
  }
  const cls = id => ({ mount(card,intensity){ if(intensity>.12) card.classList.add("attr-"+id); } });
  registerAttribute("fire", { mount(card,n){ card.classList.add("attr-fire"); particles(card,"▲","#ff6338",Math.ceil(5*n),1.25); } });
  registerAttribute("wet", { mount(card,n){ card.classList.add("attr-wet"); particles(card,"●","#72d9ff",Math.ceil(4*n),2.1); } });
  registerAttribute("growth", { mount(card,n){ card.classList.add("attr-growth"); particles(card,"◆","#a8e782",Math.ceil(3*n),2.7); } });
  registerAttribute("crystal", { mount(card,n){ card.classList.add("attr-crystal"); particles(card,"✦","#f4d986",Math.ceil(4*n),2.2); } });
  registerAttribute("diffuse", cls("diffuse"));
  registerAttribute("freeze", { mount(card,n){ card.classList.add("attr-freeze"); particles(card,"✣","#d9f8ff",Math.ceil(3*n),2.4); } });
  registerAttribute("shock", { mount(card,n){ card.classList.add("attr-shock"); particles(card,"ϟ","#b88cff",Math.ceil(4*n),1.15); } });

  const reactionClass = name => ({ mount(cards){ cards.forEach(c=>c.classList.add("rx-"+name)); } });
  registerReaction("none", { mount(){} });
  registerReaction("burn", reactionClass("burn"));
  registerReaction("steam", reactionClass("steam"));
  registerReaction("melt", reactionClass("melt"));
  registerReaction("explosion", { mount(cards){ cards.forEach(c=>{c.classList.add("rx-explosion");particles(c,"✦","#fff0a5",7,.8);}); } });
  registerReaction("nuclei", { mount(cards){ cards.forEach(c=>{c.classList.add("rx-nuclei");particles(c,"•","#c8fbff",8,2.3);}); } });
  registerReaction("hardFreeze", reactionClass("hard-freeze"));
  registerReaction("electrolysis", { mount(cards){ cards.forEach(c=>{c.classList.add("rx-electrolysis");particles(c,"ϟ","#bf9aff",5,.9);}); } });
  registerReaction("superconduct", reactionClass("superconduct"));
  registerReaction("particles", { mount(cards){ cards.forEach(c=>{c.classList.add("rx-particles");particles(c,"◆","#f4d986",9,1.0);}); } });
  registerReaction("spread", { mount(cards){ cards.forEach(c=>c.classList.add("attr-diffuse")); } });
  registerReaction("nucleiExplosion", reaction.get("explosion"));

  root.ElementalFxPlugins = { attribute, reaction, registerAttribute, registerReaction, particles };
})(globalThis);
