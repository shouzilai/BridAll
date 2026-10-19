/* 阅读偏好的**纯解码**与布局规范化。
 *
 * 这里只做一件事：把本机存下来的那段 JSON（脏数据、旧版本、手改过的都算）解成
 * 一份**形状确定**的偏好对象。读 storage 是调用方的事（那是副作用），本模块不碰
 * `localStorage`、不碰 DOM、不设定时器。
 *
 * 三条规矩（都是踩过的坑）：
 * 1. **重建式**：没在这里显式读出来的键就等于被丢掉 —— 新加一个偏好忘了加键，
 *    表现是“刷新后自己回去”，而不是报错（ 的同一类坑）；
 * 2. **脏数据一律兜回默认值**，绝不抛给调用方；
 * 3. **哪个窗口收起了筛选行**只认 `foldedSet`（用户自己表过态）——
 *    老版本那份 `folded` 不生效，否则新默认永远只在没存过偏好的浏览器上看得见。
 *
 * 依赖：`sources.js` 必须先装载（窗口 id 是这些偏好的键）。
 *
 * 装载顺序：sources → format → log-filter → log-source → terminal → locate → 本文件 → app
 */
(function (global) {
  "use strict";

  const sources = global.PWModelSources;
  if (!sources) {
    throw new Error("PWModelSources 必须先于 PWModelPreferences 装载");
  }
  const { SOURCE_IDS } = sources;

  /* ---------------------------------------------------------------- 常量 */
  const FONT_MIN = 9;
  const FONT_MAX = 22;

  /* 分栏宽度的上下限（比例）与最大栏数：初始化读偏好、以及 normalizeSplit 都会用到，
   * 所以必须声明在 `app` 之前（踩过 的 TDZ 坑）。 */
  const SPLIT_MIN = 0.15;
  const SPLIT_MAX = 0.85;
  const SPLIT_MIN_N = 1;
  /* 最大栏数 = 窗口数（2026-10-10 起是 5：测试阶段也是一个窗口）。 */
  const SPLIT_MAX_N = 5;
  const SPLIT_STEP = 0.02;

  /* 命令输入行（W6-1）：它长在解码窗最底端，上沿可拖。
   * 拖到最上（≤ CMD_H_MIN）就收成一个 ▲；点 ▲ 回到 CMD_H_DEFAULT。 */
  const CMD_H_MIN = 26;
  const CMD_H_DEFAULT = 52;
  const CMD_H_MAX = 240;

  /* 指标趋势的**两个窗格**（用户 2026-10-06 提）：每窗一个指标。 */
  const METRIC_PANE_COUNT = 2;
  const METRIC_PANE_DEFAULTS = [
    // 左：堆。先看空闲量（曲线），`heap_total` 是占用读数的分母。
    ["metric.sys.heap", "metric.sys.heap_total", "metric.sys.min_heap"],
    // 右：默认看历史最低空闲堆（"是不是一直在往下掉"）。梯子里把**左窗格的首选排到最后**：
    // 数据稀疏时（比如只有 heap 与 heap_total 两条）两窗也尽量画不同的东西。
    ["metric.sys.min_heap", "metric.sys.tx_stack", "metric.sys.tasks",
     "metric.sys.heap_total", "metric.sys.heap"],
  ];

  /* 横轴的**时间窗**（用户 2026-10-07 指的"任务管理器那个效果"）：固定画最近一段，
   * 新的在右边、旧的从左边掉出去。
   * ⚠️ 把整段捕获压进一格时，一次 20 s 的占用/释放只剩下十几个像素 —— 形状就糊成"锯齿纹理"。 */
  const METRIC_WINDOW_CHOICES = [
    { key: "60", label: "60 秒", seconds: 60 },
    { key: "120", label: "2 分钟", seconds: 120 },
    { key: "300", label: "5 分钟", seconds: 300 },
    { key: "all", label: "全部", seconds: null },
  ];
  const METRIC_WINDOW_DEFAULT = "60";

  /* 纵轴的**刻度**。任务管理器一眼能读，是因为**轴有绝对含义**（0–100%）：
   * 那条贴着底部的小尖刺才能读成"很闲"；换成"按数据自动缩放"，同一段数据会被拉满整格。
   *   full = 0 … 满量程（堆类指标用固件报的**总量**；其余用整段捕获的最大值向上取整）
   *   auto = 按画出来的这一段缩放（看抖动形状方便，但轴每次重画都在变） */
  const METRIC_SCALE_CHOICES = [
    { key: "full", label: "0–满量程" },
    { key: "auto", label: "自动" },
  ];
  const METRIC_SCALE_DEFAULT = "full";

  /* ------------------------------------------------------------ 布局形态 */
  /* 分栏状态规范化：栏数 1～5、来源去重并补齐（脏数据也不能把工作区弄空）。 */
  function normalizeSplit(split) {
    const ids = [];
    const raw = split && Array.isArray(split.ids) ? split.ids : [];
    for (const id of raw) if (SOURCE_IDS.includes(id) && !ids.includes(id)) ids.push(id);
    for (const id of SOURCE_IDS) {
      if (ids.length >= SPLIT_MAX_N) break;
      if (!ids.includes(id)) ids.push(id);
    }
    const n = Math.max(SPLIT_MIN_N, Math.min(SPLIT_MAX_N, Math.round(Number(split && split.n) || 2)));
    // 栏位按**来源顺序**排（字典序就是屏幕上的左右顺序），所以 ids 只要是个集合。
    const picked = SOURCE_IDS.filter((id) => ids.slice(0, n).includes(id));
    // ⚠️ `on` 必须原样带回来：它是“现在是否处于分栏”，被 normalize 吃掉的话
    // 点“分栏”就会变成 mode=grid（踩过：开关点了没反应）。
    return { on: Boolean(split && split.on), n, ids: picked };
  }

  /* 窗口宽度权重：正数，脏数据当 1（等宽）。 */
  function normalizeWidths(widths) {
    const out = {};
    for (const id of SOURCE_IDS) {
      const value = Number(widths && widths[id]);
      out[id] = Number.isFinite(value) && value > 0 ? value : 1;
    }
    return out;
  }

  const equalWidths = () => normalizeWidths(null);

  /* 把指定窗口的宽度权重恢复成 1（= 均分剩余横向空间），其余窗口一点不动。 */
  function equalizedWidths(widths, ids) {
    const out = normalizeWidths(widths);
    for (const id of ids || []) if (SOURCE_IDS.includes(id)) out[id] = 1;
    return out;
  }

  /* 折叠状态：布尔映射（脏数据当未折叠）。 */
  function normalizeCollapsed(collapsed) {
    const out = {};
    for (const id of SOURCE_IDS) out[id] = Boolean(collapsed && collapsed[id] === true);
    return out;
  }

  /* ---------------------------------------------------------- 阅读偏好解码 */
  /* 命令输入行的高度：脏数据/手改的 localStorage 都要能兜住（读不出或越界就回默认值）。 */
  function clampCmdHeight(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return CMD_H_DEFAULT;
    return Math.min(CMD_H_MAX, Math.max(CMD_H_MIN, Math.round(number)));
  }

  /** 读回两个窗格的选择：只认非空字符串，缺项补 null（旧版本没有这个字段 ⇒ 走默认梯子）。 */
  function loadMetricPanes(saved) {
    const rows = Array.isArray(saved) ? saved : [];
    return Array.from({ length: METRIC_PANE_COUNT }, (_, index) =>
      typeof rows[index] === "string" && rows[index] ? rows[index] : null
    );
  }

  /** 读回两个窗格的时间窗 / 刻度选择（认不出的 key ⇒ 默认值；旧版本没这两个字段也一样）。 */
  function loadPaneChoice(saved, choices, fallback) {
    const rows = Array.isArray(saved) ? saved : [];
    return Array.from({ length: METRIC_PANE_COUNT }, (_, index) =>
      choices.some((row) => row.key === rows[index]) ? rows[index] : fallback
    );
  }

  function loadMetricWindows(saved) {
    return loadPaneChoice(saved, METRIC_WINDOW_CHOICES, METRIC_WINDOW_DEFAULT);
  }

  function loadMetricScales(saved) {
    return loadPaneChoice(saved, METRIC_SCALE_CHOICES, METRIC_SCALE_DEFAULT);
  }

  /** 本机存下来的那段 JSON → 偏好对象（形状固定；解不出来就是默认值）。 */

  global.PWModelPreferences = Object.freeze({
    FONT_MIN, FONT_MAX, SPLIT_MIN, SPLIT_MAX, SPLIT_MIN_N, SPLIT_MAX_N, SPLIT_STEP, CMD_H_MIN,
    CMD_H_DEFAULT, CMD_H_MAX, METRIC_PANE_COUNT, METRIC_PANE_DEFAULTS, METRIC_WINDOW_CHOICES,
    METRIC_WINDOW_DEFAULT, METRIC_SCALE_CHOICES, METRIC_SCALE_DEFAULT, normalizeSplit,
    normalizeWidths, equalWidths, equalizedWidths, normalizeCollapsed, clampCmdHeight,
    loadMetricPanes, loadMetricWindows, loadMetricScales
  });
})(globalThis);
