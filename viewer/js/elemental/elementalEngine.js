/* elementalEngine.js — 语段编排：加载拆解、传播颜色、解析反应、挂载插件。 */
"use strict";
(function (root) {
  const R = root.ElementalRules, RX = root.ElementalReactions, FX = root.ElementalFxPlugins;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const state = { run:0, font:"", tokens:[], paused:false, cache:new Map() };
  const $ = id => document.getElementById(id);
  const isHan = ch => /\p{Script=Han}/u.test(ch);

  async function api(path) {
    const res = await fetch(path);
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || ("HTTP "+res.status));
    return data;
  }
  function svgEl(name, attrs, parent) {
    const node=document.createElementNS(SVG_NS,name);
    Object.entries(attrs||{}).forEach(([k,v])=>node.setAttribute(k,v));
    if(parent) parent.appendChild(node); return node;
  }
  function hex(color) {
    const s=color.replace("#",""); return [0,2,4].map(i=>parseInt(s.slice(i,i+2),16));
  }
  function blend(entries) {
    const base=[210,221,229], baseW=.18;
    let rgb=base.map(v=>v*baseW), total=baseW;
    entries.forEach(e=>{ const c=hex(e.color); const w=Math.max(.05,e.intensity); total+=w;
      rgb=rgb.map((v,i)=>v+c[i]*w); });
    return "#"+rgb.map(v=>Math.round(v/total).toString(16).padStart(2,"0")).join("");
  }
  function putInfluence(token, id, intensity, color, source) {
    const old=token.influence.get(id);
    if (!old || intensity>old.intensity) token.influence.set(id,{ id,intensity,color,source });
  }
  function singleton(id) { return new Set([id]); }
  function addReaction(list, seen, rule, indices) {
    const key=rule.id+":"+[...new Set(indices)].sort((a,b)=>a-b).join(",");
    if(seen.has(key)) return; seen.add(key);
    list.push({ rule, indices:[...new Set(indices)] });
  }

  function plan(tokens) {
    tokens.forEach(t=>t.influence=new Map());
    tokens.forEach((t,i)=>t.base.forEach(a=>{
      putInfluence(t,a.id,1,a.color,i);
      for(const d of [-1,1]) if(tokens[i+d]) putInfluence(tokens[i+d],a.id,.32,a.color,i);
      if(a.id==="diffuse") for(const d of [-2,2]) if(tokens[i+d]) putInfluence(tokens[i+d],a.id,.17,a.color,i);
    }));
    /* 风把相邻核心字的属性带到以风字为中心的两字范围；结晶按约定不扩散。 */
    tokens.forEach((wind,i)=>{
      if(!wind.base.some(a=>a.id==="diffuse")) return;
      const carried=[];
      for(let j=Math.max(0,i-1);j<=Math.min(tokens.length-1,i+1);j++)
        tokens[j].base.forEach(a=>{ if(!["diffuse","crystal"].includes(a.id)) carried.push(a); });
      carried.forEach(a=>{
        for(let j=Math.max(0,i-2);j<=Math.min(tokens.length-1,i+2);j++)
          putInfluence(tokens[j],a.id,.5-.09*Math.abs(j-i),a.color,i);
      });
    });

    const reactions=[], seen=new Set(), nuclei=[];
    tokens.forEach((t,i)=>{
      for(let a=0;a<t.base.length;a++) for(let b=a+1;b<t.base.length;b++) {
        const rule=RX.resolve(singleton(t.base[a].id),singleton(t.base[b].id));
        if(rule){ addReaction(reactions,seen,rule,[i]); if(rule.derived==="nuclei") nuclei.push([i]); }
      }
      if(i+1<tokens.length) for(const a of t.base) for(const b of tokens[i+1].base) {
        const rule=RX.resolve(singleton(a.id),singleton(b.id));
        if(rule){ addReaction(reactions,seen,rule,[i,i+1]); if(rule.derived==="nuclei") nuclei.push([i,i+1]); }
      }
    });
    /* 水草生成的晶核若紧邻电属性，进行第二阶段爆炸。 */
    nuclei.forEach(pair=>{
      const shock=tokens.findIndex((t,i)=>pair.some(p=>Math.abs(i-p)<=1)&&t.base.some(a=>a.id==="shock"));
      if(shock>=0) addReaction(reactions,seen,{ id:"nucleiShock",effect:"nucleiExplosion",
        label:"晶核 × 触电",detail:"水草晶核受电后连锁爆炸",priority:220 },pair.concat(shock));
    });
    return reactions;
  }

  function makeCard(token,index) {
    const card=document.createElement("article"); card.className="glyph-card";
    card.style.setProperty("--i",index); token.card=card;
    const layer=document.createElement("div"); layer.className="fx-layer";
    if(!isHan(token.ch)) {
      card.classList.add("is-punctuation");
      const fallback=document.createElement("div"); fallback.className="fallback-glyph";
      fallback.textContent=token.ch; card.append(fallback,layer); return card;
    }
    if(token.deco && (token.deco.strokes||[]).some(s=>!s.failed&&s.path)) {
      const svg=svgEl("svg",{viewBox:"0 0 1024 1024",class:"glyph-svg","aria-label":token.ch},card);
      const g=svgEl("g",{transform:"scale(1,-1) translate(0,-900)"},svg);
      const meta=R.strokeAttributes(token.deco,token.base);
      (token.deco.strokes||[]).forEach((s,k)=>{
        if(s.failed||!s.path) return;
        const p=svgEl("path",{d:s.path,class:"glyph-path","fill-rule":"nonzero"},g);
        if(meta[k]&&meta[k].attributes.length) p.classList.add("is-component");
      });
    } else {
      const fallback=document.createElement("div"); fallback.className="fallback-glyph";
      fallback.textContent=token.ch; card.appendChild(fallback);
    }
    card.appendChild(layer);
    const meta=document.createElement("div"); meta.className="char-meta";
    token.base.forEach(a=>{ const b=document.createElement("span"); b.className="attr-badge";
      b.style.setProperty("--c",a.color); b.textContent=R.attributes[a.id].name; meta.appendChild(b); });
    card.appendChild(meta); return card;
  }

  function applyVisuals(tokens,reactions) {
    tokens.forEach(t=>{
      const influences=[...t.influence.values()];
      t.card.style.setProperty("--fill",influences.length?blend(influences):"#dce6ed");
      influences.forEach(v=>{ const plugin=FX.attribute.get(v.id); if(plugin) plugin.mount(t.card,v.intensity,t); });
    });
    reactions.forEach(item=>{
      const plugin=FX.reaction.get(item.rule.effect);
      if(plugin) plugin.mount(item.indices.map(i=>tokens[i].card),item);
    });
  }
  function renderLog(tokens,reactions) {
    const box=$("reactionLog"); box.innerHTML="";
    if(!reactions.length){ box.innerHTML='<div class="muted">本语段未触发已注册反应；属性颜色与邻字影响仍然生效。</div>'; return; }
    reactions.forEach(item=>{
      const e=document.createElement("div"); e.className="reaction-item"+(item.rule.effect==="none"?" none":"");
      const first=item.indices[0], attrs=tokens[first].base;
      e.style.setProperty("--c",attrs[0]?attrs[0].color:"#768594");
      const chars=item.indices.map(i=>tokens[i].ch).join(" · ");
      const b=document.createElement("b"); b.textContent=item.rule.label+"　"+chars;
      const s=document.createElement("small"); s.textContent=item.rule.detail;
      e.append(b,s); box.appendChild(e);
    });
  }

  async function fetchDeco(font,ch) {
    const key=font+"|"+ch; if(state.cache.has(key)) return state.cache.get(key);
    const promise=api("/api/decompose?font="+encodeURIComponent(font)+"&char="+encodeURIComponent(ch));
    state.cache.set(key,promise); promise.catch(()=>state.cache.delete(key)); return promise;
  }
  async function mapLimit(items,limit,fn) {
    let next=0; const out=new Array(items.length);
    async function worker(){ while(next<items.length){ const i=next++; out[i]=await fn(items[i],i); } }
    await Promise.all(Array.from({length:Math.min(limit,items.length)},worker)); return out;
  }
  async function build() {
    const text=Array.from($("phraseInput").value.trim()).slice(0,64);
    if(!text.length){ $("loadStatus").textContent="请输入至少一个字符"; return; }
    const run=++state.run, font=$("fontSelect").value; state.font=font;
    $("buildBtn").disabled=true; $("loadStatus").textContent="准备反应场…";
    const unique=[...new Set(text.filter(isHan))], decos=new Map(); let completed=0;
    await mapLimit(unique,2,async ch=>{
      try { decos.set(ch,await fetchDeco(font,ch)); }
      catch(err){ decos.set(ch,null); }
      completed++; if(run===state.run) $("loadStatus").textContent=`拆解 ${completed}/${unique.length} 个不同汉字`;
    });
    if(run!==state.run) return;
    const tokens=text.map(ch=>({ ch,deco:decos.get(ch)||null,base:[] }));
    tokens.forEach(t=>{ if(isHan(t.ch)) t.base=R.detect(t.ch,t.deco); });
    const reactions=plan(tokens), stage=$("phraseStage"); stage.innerHTML=""; stage.classList.remove("empty","paused");
    tokens.forEach((t,i)=>stage.appendChild(makeCard(t,i)));
    applyVisuals(tokens,reactions); renderLog(tokens,reactions); state.tokens=tokens;
    stage.classList.add("is-playing");
    $("loadStatus").textContent=`${text.length} 字符 · ${unique.length} 个矢量字 · ${reactions.length} 条反应`;
    $("buildBtn").disabled=false; $("replayBtn").disabled=false; $("pauseBtn").disabled=false;
    $("pauseBtn").textContent="暂停"; state.paused=false;
  }
  function replay() {
    const stage=$("phraseStage"); stage.classList.remove("paused"); stage.classList.add("restarting");
    void stage.offsetWidth; stage.classList.remove("restarting"); stage.classList.add("is-playing");
    state.paused=false; $("pauseBtn").textContent="暂停";
  }
  function pause() {
    state.paused=!state.paused; $("phraseStage").classList.toggle("paused",state.paused);
    $("pauseBtn").textContent=state.paused?"继续":"暂停";
  }
  function renderLegend() {
    const box=$("attributeLegend");
    Object.entries(R.attributes).forEach(([id,a])=>{
      const e=document.createElement("div"); e.className="legend-item";
      e.innerHTML=`<span class="legend-swatch" style="--c:${a.color}"></span><span><b>${a.name}</b><small>${a.components.join("/")||"雷·电·霆"}</small></span>`;
      box.appendChild(e);
    });
    $("pluginStats").innerHTML=`<span>${Object.keys(R.attributes).length} 个属性</span><span>${RX.rules.length} 条反应</span><span>${FX.reaction.size} 个视觉插件</span>`;
  }
  async function boot() {
    renderLegend();
    try {
      const data=await api("/api/fonts"), select=$("fontSelect"); select.innerHTML="";
      (data.fonts||[]).forEach(f=>{ const o=document.createElement("option");o.value=f;o.textContent=f;select.appendChild(o); });
      const preferred=[...select.options].find(o=>o.value==="HarmonyOS_Sans_SC.ttf"); if(preferred) select.value=preferred.value;
      $("loadStatus").textContent="已连接本地服务；生成场景时才会请求逐字拆解";
    } catch(err) { $("fontSelect").innerHTML='<option value="HarmonyOS_Sans_SC.ttf">HarmonyOS_Sans_SC.ttf</option>'; $("loadStatus").textContent="本地服务未连接"; }
    $("buildBtn").onclick=build; $("replayBtn").onclick=replay; $("pauseBtn").onclick=pause;
    $("phraseInput").addEventListener("keydown",e=>{ if((e.ctrlKey||e.metaKey)&&e.key==="Enter") build(); });
  }
  root.ElementalApp={ boot, plan, blend };
  boot();
})(globalThis);
