/* loadingStages.js — loading overlay stage model and cancellable presenter. */
"use strict";

const LOADING_STAGE_PLANS = Object.freeze({
  connect: [
    { title: "连接本地服务", detail: "读取可用字体列表与示例字", duration: 1400 },
    { title: "确认服务状态", detail: "等待字体与拆解接口就绪", duration: 1800 },
  ],
  font: [
    { title: "读取目标字体", detail: "解析字体轮廓、字符映射和字面坐标", duration: 1500 },
    { title: "构建标准笔画库", detail: "按笔画类型查找目标字体的标准取字来源", duration: 1900 },
    { title: "提取目标字体骨架", detail: "从标准笔画轮廓建立目标字体自己的中轴线", duration: 1900 },
    { title: "整理笔画映射", detail: "建立文鼎楷体笔画名称与目标字体标准笔画的对应", duration: 1900 },
    { title: "完成字体准备", detail: "整理标准笔画库，随后开始当前字符拆解", duration: 2400 },
  ],
  decompose: [
    { title: "解析字符轮廓", detail: "识别外环、孔洞和互不相交的连通组", duration: 1500 },
    { title: "建立笔画分布", detail: "用楷体相对结构摆放目标字体的标准笔画骨架", duration: 1900 },
    { title: "指派笔画分组", detail: "结合墨距离、位置、方向和部件关系匹配连通组", duration: 1900 },
    { title: "处理竞争组", detail: "在组内拟合中轴线并进行纯矢量归属与切割", duration: 2100 },
    { title: "执行布尔收口", detail: "补齐残余墨形并检查笔画并集是否等于原字符", duration: 2600 },
  ],
});

function stageAt(plan, elapsedMs) {
  if (!plan || !plan.length) return { index: 0, stage: null };
  let remaining = Math.max(0, elapsedMs);
  for (let index = 0; index < plan.length - 1; index++) {
    const duration = Math.max(1, plan[index].duration || 1);
    if (remaining < duration) return { index, stage: plan[index] };
    remaining -= duration;
  }
  return { index: plan.length - 1, stage: plan[plan.length - 1] };
}

class LoadingStageController {
  constructor(root, options = {}) {
    this.root = root;
    this.title = root.querySelector("#ovMsg");
    this.detail = root.querySelector("#ovDetail");
    this.progress = root.querySelector("#ovProgress");
    this.meta = root.querySelector("#ovMeta");
    this.now = options.now || (() => performance.now());
    this.setTimer = options.setTimer || ((fn, ms) => setInterval(fn, ms));
    this.clearTimer = options.clearTimer || (id => clearInterval(id));
    this.serial = 0;
    this.timer = null;
    this.plan = [];
    this.context = {};
    this.startedAt = 0;
  }

  start(planName, context = {}) {
    const token = ++this.serial;
    if (this.timer !== null) this.clearTimer(this.timer);
    this.root.classList.remove("hidden");
    this.root.setAttribute("aria-busy", "true");
    this.switchPlan(token, planName, context);
    this.timer = this.setTimer(() => this.render(token), 200);
    return token;
  }

  switchPlan(token, planName, context = {}) {
    if (token !== this.serial) return false;
    this.plan = LOADING_STAGE_PLANS[planName] || [];
    this.context = context;
    this.startedAt = this.now();
    this.render(token);
    return true;
  }

  render(token) {
    if (token !== this.serial) return;
    const elapsed = this.now() - this.startedAt;
    const current = stageAt(this.plan, elapsed);
    if (!current.stage) return;
    this.title.textContent = current.stage.title;
    this.detail.textContent = current.stage.detail;
    this.progress.style.width = `${((current.index + 1) / this.plan.length) * 100}%`;
    const subject = this.context.char ? `“${this.context.char}” · `
      : (this.context.font ? `${this.context.font} · ` : "");
    this.meta.textContent = `${subject}阶段 ${current.index + 1}/${this.plan.length} · ` +
      `${Math.max(0, elapsed / 1000).toFixed(1)} 秒`;
  }

  stop(token = this.serial) {
    if (token !== this.serial) return false;
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
    this.root.classList.add("hidden");
    this.root.setAttribute("aria-busy", "false");
    return true;
  }

  showMessage(message, detail = "") {
    ++this.serial;
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
    this.root.classList.remove("hidden");
    this.root.setAttribute("aria-busy", "true");
    this.title.textContent = message;
    this.detail.textContent = detail;
    this.progress.style.width = "100%";
    this.meta.textContent = "";
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { LOADING_STAGE_PLANS, stageAt, LoadingStageController };
}
