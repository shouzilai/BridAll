"use strict";
/* 平台体检页（ / D5）——**只读报告视图**。
 *
 * 三条硬规矩（`test_webconsole.py` 与浏览器 E2E 各钉一部分）：
 *
 *  1. **不自行推导结果**：结果徽标与每条检查的状态都直接来自服务端那一份报告
 *     （`Report.to_json()`，与终端跑 `doctor.py` 是同一个模型）。这里**没有**
 *     "失败 0 条就算 PASS" 这种推导 —— 只把 `report.result` 原样显示。
 *  2. **只读**：只发 `GET`，一条写请求都没有（下载也只是 GET 一个附件）。
 *  3. **硬件探测必须由人点**：默认请求**不带** `hardware`；只有点「枚举串口（只读）」
 *     才发一次 `?hardware=1`，而且服务端那边只枚举端口名（不打开、不复位、不发送字节）。
 */

const SCOPE_ORDER = ["platform", "target", "hardware"];
const HARDWARE_QUERY = "?hardware=1";
const THEME_KEY = "pw.theme";
const THEME_ORDER = ["dark", "warm"];
/*: 结果与状态的**用户语言**（页面上不再出现 PASS / WARN 这类内部枚举）。
 * ⚠️ 这只是一张名字表：判定仍然只有服务端那一份（`report.result` / `row.status`）——
 *    认不出的值**原样显示**，不编一个好听的名字。 */
const RESULT_TEXT = { PASS: "环境正常", WARN: "需要注意", FAIL: "有问题", SKIP: "未检查" };
const STATUS_TEXT = { PASS: "通过", WARN: "需要注意", FAIL: "失败", SKIP: "跳过" };
function label(map, value) {
  const key = String(value || "").toUpperCase();
  return map[key] || String(value || "");
}
/*: 这一页当前是否把 PASS 项也摊开（默认不摊：屏幕上只突出要处理的那些）。 */
let showAllChecks = false;
/*: 最近一次渲染的报告（只有“展开全部检查项”需要它重算被收起了几项）。 */
let lastReport = null;
//: 下载附件的文件名（服务端 `?download=1` 给的就是这个名字）。
const DOWNLOAD_NAME = "doctor-report.json";
//: 反馈条自动消失的时间（毫秒）。
const FLASH_MS = 6000;
//: 筛选条的顺序（空串 = 全部）。计数全部来自服务端给的 `report.summary`。
const FILTER_ORDER = ["", "PASS", "WARN", "FAIL", "SKIP"];

let flashTimer = 0;
//: 当前筛选（空 = 全部）。重新体检后仍然保留 —— 看的就是同一个状态。
let activeFilter = "";

function el(id) {
  return document.getElementById(id);
}

function loadTheme() {
  const raw = globalThis.localStorage ? globalThis.localStorage.getItem(THEME_KEY) : null;
  return THEME_ORDER.includes(raw) ? raw : "dark";
}

function applyTheme(themeId) {
  const next = THEME_ORDER.includes(themeId) ? themeId : "dark";
  document.documentElement.dataset.theme = next;
  document.documentElement.style.colorScheme = next === "warm" ? "light" : "dark";
  try {
    globalThis.localStorage.setItem(THEME_KEY, next);
  } catch (error) {
    /* 保存失败时仍按当前选择渲染 */
  }
  paintThemeButtons(next);
  return next;
}

/* 两个主题入口的按下态（用户口径 2026-10-12）：月亮 = 深色调试、太阳 = 暖色工作台。
 * ⚠️ 图标是内联 SVG，含义在 `aria-label`/`title` 上 —— 不下任何字符画。 */
function paintThemeButtons(themeId) {
  const current = THEME_ORDER.includes(themeId) ? themeId : "dark";
  for (const button of document.querySelectorAll(".theme-btn")) {
    button.setAttribute("aria-pressed", String(button.dataset.themeId === current));
  }
  return current;
}

/* 动作反馈：体检/下载完之后在页头下方亮一条（`data-kind` 区分 成功/提示/失败）。
 * 它只转述事实（毫秒数、服务端给的 result 与计数），不参与任何判定。 */
function flash(text, kind) {
  const box = el("doctorFlash");
  if (!box) return;
  box.dataset.kind = kind || "info";
  box.textContent = text;
  box.hidden = false;
  if (flashTimer) clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { box.hidden = true; }, FLASH_MS);
}

function appendFact(list, term, value) {
  if (!value) return;
  const dt = document.createElement("dt");
  dt.textContent = term;
  const dd = document.createElement("dd");
  dd.textContent = String(value);
  list.append(dt, dd);
}

function checkRow(row) {
  const item = document.createElement("article");
  item.className = "dh-check";
  item.dataset.status = String(row.status || "").toLowerCase();
  // ⚠️ `check-id` 留在属性上（浏览器验收用它找行）；屏幕上**不画**这个机器码。
  item.dataset.checkId = String(row.id || "");

  const head = document.createElement("div");
  head.className = "dh-check-head";
  const status = document.createElement("span");
  status.className = "dh-status";
  status.textContent = label(STATUS_TEXT, row.status);
  const title = document.createElement("span");
  title.className = "dh-check-title";
  title.textContent = String(row.title || row.id || "");
  head.append(status, title);

  // 屏幕上只摆“哪里有问题 + 怎么处理”（用户口径 2026-10-12：失败原因与可执行的修复方法必须留着）。
  const trouble = item.dataset.status === "fail" || item.dataset.status === "warn";
  const facts = document.createElement("dl");
  facts.className = "dh-facts";
  if (trouble) {
    appendFact(facts, "原因", row.reason);
    appendFact(facts, "怎么处理", row.remediation);
  }

  /* 实测值 / 期望值 / 检查项编号这类**技术细节**进「详情」（默认收起）—— 不在主界面铺开；
   * 下载的 JSON 里也是同一份（本来就是服务端报告）。 */
  const button = document.createElement("button");
  button.type = "button";
  button.className = "dh-detail-btn";
  button.setAttribute("aria-expanded", "false");
  const detail = document.createElement("div");
  detail.className = "dh-check-detail";
  detail.hidden = true;
  const detailFacts = document.createElement("dl");
  detailFacts.className = "dh-facts";
  appendFact(detailFacts, "实测", row.actual);
  appendFact(detailFacts, "期望", row.expected);
  appendFact(detailFacts, "检查项", row.id);
  if (!trouble) {
    appendFact(detailFacts, "原因", row.reason);
    appendFact(detailFacts, "怎么处理", row.remediation);
  }
  detail.appendChild(detailFacts);
  const paint = (open) => {
    button.textContent = open ? "收起详情" : "详情";
    button.setAttribute("aria-expanded", open ? "true" : "false");
    detail.hidden = !open;
  };
  paint(false);
  button.addEventListener("click", () => paint(detail.hidden));

  item.append(head, facts, button, detail);
  return item;
}

function group(labelText, scope, rows) {
  const wrap = document.createElement("section");
  wrap.className = "dh-group";
  wrap.dataset.scope = scope;
  const head = document.createElement("h2");
  // 标题只用服务端给的那份人话名字（2026-10-12 起不再把 platform / target / hardware 这类
  // 内部键名摆在屏幕上）。
  head.textContent = labelText;
  head.append(Object.assign(document.createElement("span"), {
    className: "dh-group-count",
    textContent: ` ${rows.length} 条`,
  }));
  wrap.appendChild(head);

  // 二级分类：按报告给的 `group_label` 归堆（首次出现的顺序 = 报告里的顺序）。
  // 平台层是 Python/模块/目录/端口/装配/串口/工具链；目标层是"这条属于哪个目标/哪份固件"。
  // 某一层只有一类时不画二级标题（否则"硬件在线探测"会自己套自己）。
  const buckets = new Map();
  for (const row of rows) {
    const name = String(row.group_label || row.group || "其他");
    if (!buckets.has(name)) buckets.set(name, []);
    buckets.get(name).push(row);
  }
  if (buckets.size <= 1) {
    for (const row of rows) wrap.appendChild(checkRow(row));
    return wrap;
  }
  for (const [name, bucket] of buckets) {
    const box = document.createElement("div");
    box.className = "dh-subgroup";
    box.dataset.groupLabel = name;
    const sub = document.createElement("h3");
    sub.className = "dh-subgroup-head";
    sub.textContent = name;
    sub.append(Object.assign(document.createElement("span"), {
      className: "dh-subgroup-count",
      textContent: ` ${bucket.length} 条`,
    }));
    box.appendChild(sub);
    for (const row of bucket) box.appendChild(checkRow(row));
    wrap.appendChild(box);
  }
  return wrap;
}

function render(report) {
  lastReport = report;
  // 结果**原样来自报告**（这里不做任何推导）：`dataset.result` 就是服务端那个值，
  // 屏幕上那句是它的用户语言名字（认不出的值原样显示）。
  const badge = el("doctorResult");
  badge.dataset.result = String(report.result || "");
  badge.textContent = label(RESULT_TEXT, report.result || "（报告没有结果字段）");

  const summary = report.summary || {};
  const total = Array.isArray(report.checks) ? report.checks.length : 0;
  el("doctorCounts").textContent =
    `检查 ${total} 项：通过 ${summary.pass || 0} · 需要注意 ${summary.warn || 0} · `
    + `失败 ${summary.fail || 0} · 跳过 ${summary.skip || 0}`;
  // 检查时间用**本地时间**（报告里那份是 UTC，原样挂在悬停里）。
  const when = el("doctorWhen");
  const local = localTimeText(report.generated_utc);
  when.textContent = local ? `检查时间 ${local}` : "";
  when.title = report.generated_utc ? `报告时间（UTC）${report.generated_utc}` : "";

  const labels = new Map((report.scopes || []).map((row) => [row.scope, row.label]));
  const checks = Array.isArray(report.checks) ? report.checks : [];
  const host = el("doctorSections");
  host.replaceChildren();
  for (const scope of SCOPE_ORDER) {
    const rows = checks.filter((row) => row.scope === scope);
    if (rows.length) host.appendChild(group(labels.get(scope) || scope, scope, rows));
  }

  // "下一步"只是把报告里已经写好的修复建议挑出来排一下 —— 不改变任何状态判断。
  const actions = checks.filter(
    (row) => (row.status === "FAIL" || row.status === "WARN") && row.remediation,
  );
  el("doctorActions").hidden = actions.length === 0;
  el("doctorActionList").replaceChildren(...actions.map((row) => {
    const item = document.createElement("li");
    item.dataset.status = String(row.status || "");
    // 屏幕上是用户语言（`[WARN]` 这类内部枚举不摆在页面上）。
    item.textContent = `[${label(STATUS_TEXT, row.status)}] ${row.remediation}`;
    return item;
  }));

  renderFilters(report);
  renderShowAll(report);
  applyFilter();
}

/** 检查时间：报告给的是 UTC，屏幕上写本地时间（认不出来的时间不编一个）。 */
function localTimeText(raw) {
  if (!raw) return "";
  const when = new Date(String(raw));
  if (Number.isNaN(when.getTime())) return String(raw);
  const pad = (value) => String(value).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())} `
    + `${pad(when.getHours())}:${pad(when.getMinutes())}:${pad(when.getSeconds())}`;
}

/* PASS 项默认收起（用户口径 2026-10-12：屏幕上只突出 WARN / FAIL 与对应处理办法）。
 * 按钮同时给出“收起了多少项”，展开就是全部检查项。 */
function renderShowAll(report) {
  const button = el("doctorShowAll");
  if (!button) return;
  const checks = Array.isArray(report.checks) ? report.checks : [];
  const folded = checks.filter((row) => String(row.status || "").toUpperCase() === "PASS").length;
  button.hidden = folded === 0;
  button.textContent = showAllChecks ? `只看需要处理的（${folded} 项通过）` : `展开全部检查项（${folded} 项通过）`;
  button.setAttribute("aria-pressed", showAllChecks ? "true" : "false");
  button.title = showAllChecks
    ? "收起来，只看需要注意与失败的那些"
    : "把通过的那些检查项也摆出来（它们本来也在报告里）";
}

/* 筛选条：每条检查的状态已经写在 DOM 的 `data-status` 上，这里只负责“显示/隐藏”，
 * **不重新判定任何检查**；计数也全部转述服务端的 `summary`。 */
function renderFilters(report) {
  const summary = report.summary || {};
  const checks = Array.isArray(report.checks) ? report.checks : [];
  el("doctorFilters").replaceChildren(...FILTER_ORDER.map((status) => {
    const count = status
      ? Number(summary[status.toLowerCase()] || 0)
      : checks.length;
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "dh-filter";
    chip.dataset.filter = status;
    chip.dataset.status = status.toLowerCase();
    chip.append(Object.assign(document.createElement("span"), {
      className: "dh-filter-name",
      textContent: status ? label(STATUS_TEXT, status) : "全部",
    }), Object.assign(document.createElement("span"), {
      className: "dh-filter-count",
      textContent: String(count),
    }));
    // 本次报告里一条都没有的状态：不许点进一个空页面（计数已经写在按钮上了）。
    if (status && count === 0) {
      chip.disabled = true;
      chip.title = `本次报告里没有「${label(STATUS_TEXT, status)}」的检查项`;
    }
    chip.addEventListener("click", () => setFilter(status));
    return chip;
  }));
  markActiveFilter();
}

function setFilter(status) {
  // 点同一个状态再点一次 = 回到全部（不用去找第二个按钮）。
  activeFilter = activeFilter === status ? "" : status;
  markActiveFilter();
  applyFilter();
}

function markActiveFilter() {
  for (const chip of document.querySelectorAll("#doctorFilters .dh-filter")) {
    chip.setAttribute("aria-pressed", String(chip.dataset.filter === activeFilter));
  }
}

function applyFilter() {
  const want = activeFilter.toLowerCase();
  for (const group of document.querySelectorAll("#doctorSections .dh-group")) {
    for (const row of group.querySelectorAll(".dh-check")) {
      // 两种“收起”合在一处判定（同一个 `hidden` 属性，没有第二个开关）：
      //   ① 筛选：选了某一个状态就只看它；
      //   ② 默认折叠：没筛的时候，通过的那一项不占屏幕（要看就点「展开全部检查项」）。
      const folded = !want && !showAllChecks && row.dataset.status === "pass";
      row.hidden = folded || (Boolean(want) && row.dataset.status !== want);
    }
    // 分类与整层跟着行走：全被筛掉就不画（不留下一个空壳标题）。
    for (const box of group.querySelectorAll(".dh-subgroup")) {
      box.hidden = !box.querySelector(".dh-check:not([hidden])");
    }
    const shown = group.querySelectorAll(".dh-check:not([hidden])").length;
    group.hidden = shown === 0;
    const label = group.querySelector(".dh-group-count");
    if (label) {
      const total = group.querySelectorAll(".dh-check").length;
      label.textContent = activeFilter ? ` ${total} 条 · 显示 ${shown}` : ` ${total} 条`;
    }
  }
}

async function load(hardware) {
  const url = hardware ? `/api/doctor${HARDWARE_QUERY}` : "/api/doctor";
  const error = el("doctorError");
  const button = el("doctorHardware");
  const reload = el("doctorReload");
  const started = performance.now();
  error.hidden = true;
  button.disabled = true;
  reload.disabled = true;
  reload.textContent = "体检中…";
  try {
    // 只读：GET，没有 body、没有方法覆盖。
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`服务端返回 ${response.status}`);
    const report = await response.json();
    render(report);
    const summary = report.summary || {};
    const total = Array.isArray(report.checks) ? report.checks.length : 0;
    flash(
      `体检完成（${Math.round(performance.now() - started)} ms）`
      + `· 结果 ${label(RESULT_TEXT, report.result)}`
      + `（检查 ${total} 项：通过 ${summary.pass || 0} · 需要注意 ${summary.warn || 0} · 失败 ${summary.fail || 0}）`,
      // 颜色只跟着服务端给的 result 走 —— 页面自己不判"这算好还是坏"。
      report.result === "FAIL" ? "err" : "ok",
    );
  } catch (problem) {
    const text = `读不到体检报告：${(problem && problem.message) || problem}`;
    error.hidden = false;
    error.textContent = text;
    flash(`体检失败：${text}`, "err");
  } finally {
    button.disabled = false;
    reload.disabled = false;
    reload.textContent = "重新体检";
  }
}

applyTheme(loadTheme());
for (const button of document.querySelectorAll(".theme-btn")) {
  button.addEventListener("click", () => applyTheme(button.dataset.themeId));
}
el("doctorReload").addEventListener("click", () => load(false));
el("doctorHardware").addEventListener("click", () => load(true));
if (el("doctorShowAll")) {
  el("doctorShowAll").addEventListener("click", () => {
    showAllChecks = !showAllChecks;
    if (lastReport) renderShowAll(lastReport);
    applyFilter();
  });
}

// 下载由浏览器自己发（`<a download>` 指服务端的附件）—— 这里只负责给一声反馈。
el("doctorDownload").addEventListener("click", () => {
  flash(`已开始下载 ${DOWNLOAD_NAME}；若没弹出，请检查浏览器的下载设置`, "info");
});

// 首屏：**不带** hardware —— 硬件探测必须由人点。
load(false);
