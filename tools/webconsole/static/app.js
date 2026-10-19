/* 嵌入式实验与诊断工作台 — 前端只读展示 + 受控意图，不直接接触串口。 */
"use strict";

/* 纯模型住在 `static/js/model/*.js`，由页面按固定顺序先装载（见 `index.html`），这里只
 * 取它们的命名空间 —— 组合根不再保留这些函数的第二份实现。模型不读 DOM、不发请求、
 * 不读写浏览器存储、不设定时器，只接收值、返回值。 */
const {
  BEIJING_OFFSET_MS, escapeRegExp, LEVEL_MODULE_RE, parseLine, isErrorLine, TIME_RE, lineTime,
  toEpochMs, beijingParts, beijingClock, beijingStamp,
  formatDuration, byteUnits, compactDuration, shortTime, shortSha, escapeHtml,
} = globalThis.PWModelFormat;
/* 五个窗口的清单是唯一出处：窗口顺序、偏好键、证据文件名都从这里来。 */
const {
  SOURCES, SOURCE_IDS, SOURCE_BY_ID, SOURCE_LABEL,
} = globalThis.PWModelSources;
/* 日志筛选的纯模型：查询/排除的编译与增量记账、重复折叠、可见项推进。 */
const {
  MAX_QUERY, MAX_MARKS_PER_LINE, compileQuery, matchRanges, compileExclude, excludeActive,
  lineExcluded, hiddenReason, excludedCount, linePasses, visibleItem, viewItems, viewText,
  totalHits, locateHit,
} = globalThis.PWModelLogFilter;
/* 行模型：文本 → 展示行、追加/裁剪、行号基准、视图推进（不碰 DOM）。 */
const {
  MAX_LINES, stripAnsi, toLines, isWatched, appendLineText, appendLineTime, appendMany,
  appendText, setSourceLines, setSourceText, flushPlan, markRendered, trimSource, acceptItem,
  advanceView, resetView, streamLineFrame, streamGapIssue, applyStreamFrame, paneFrameIssue,
} = globalThis.PWModelLogSource;
/* 证据定位与区间窗口的纯模型（行号换算、区间合并、窗口标注、拼接计划）。 */
const {
  FALLBACK_TAIL_BYTES, byteText, mergeByteRanges, tailByteRange, previousByteRange,
  windowContract, parseContentRangeTotal, windowNote, prependPlan, olderMergePlan,
  lineBaseFromHeader, lineBaseIndex, evidenceLineIndex, seqRowIndex, locatePlan,
} = globalThis.PWModelLocate;

/* 事件 → 窗口的唯一映射（不认得的 kind 不进任何窗口，计数并说明原因）。 */
const {
  EVENT_WINDOW_BY_KIND, eventToSource, stageEventMode,
} = globalThis.PWModelEventRoute;
/* 阅读位置与布局的纯计算（行高取整、按网格底边修正、滚轮边界）。 */
const {
  READING_MIN_BODY, READING_BOTTOM_GAP, sanitizeHeadHeight, layoutIsSane,
  readingPlan, paneChromeGap, wheelEdge,
} = globalThis.PWModelLayout;

const $ = (id) => document.getElementById(id);
const STATE_POLL_MS = 2000;

const STEP_ORDER = ["precheck", "build", "upload", "observe", "decode", "assert"];
const MANDATORY = [
  ["manifest.json", "manifest"],
  ["raw.bin", "raw"],
  ["decoded.log", "decoded"],
  ["report.json", "report"],
  ["app_events.jsonl", "events"],
  ["metrics.jsonl", "metrics"],
];
const TEST_EVIDENCE = [
  ["test_results.jsonl", "tests"],
  ["test_input.bin", "input"],
  ["test_readback.bin", "readback"],
];

/* ------------------------------------------------------- 统一日志工作台 */
/* 四份**流式**证据（工具输出 / 原始串口 / 解码日志 / 结构化事件）共处一个工作区，
 * 但保留**四个同时可见、相互独立的窗口**（宽屏一行、中等 2×2、窄屏纵向）。
 *
 * 为什么不合成一条时间线：这四份证据的时间基准根本不同 —— 工具输出是主机时钟，
 * raw/decoded 是设备 uptime，事件带的是 decoded.log 的字节偏移。按到达顺序混排会
 * 造出一条并不存在的时间线，而“哪一份来源说了什么”正是证据的意义。
 *
 * 为什么不做成标签切换：需要对照时，被藏起来的那个来源等于不存在；
 * 而且“当前标签”这个概念会让暂停/未读/跟随变成跨窗口连坐的状态。
 * 每个窗口始终同时可见、各自持有一份状态：滚动位置、是否暂停、未读计数、是否跟尾。
 *
 * ⚠️ 用户口径（2026-10-10）：**测试阶段也是一个窗口**（第 5 格，紧挨“工具输出”），
 *    走同一套窗口操作（折叠筛选行/全屏/收起成标签/拖宽/分栏/独立滚动/筛选/复制）。
 *    但它不是**行流**：内容是阶段卡，所以行级开关（级别/模块/行号/时间戳/折叠重复/
 *    换行/排除/只看错误）与“向前加载/下载”在它身上不适用 —— 那几个控件**根本不出现**
 *    （摆一个点了没反应的按钮，比没有这个按钮更糟）。
 *
 * 三条硬约束：
 *  - 追加只往容器**末尾**加新节点（DocumentFragment + appendChild），绝不重写
 *    已有文本节点 —— 整段文本自增赋值会替换文本节点，浏览器里的选区当场消失；
 *  - 暂停只冻结那一个窗口：SSE、串口采集与证据落盘继续跑，模型一行都不少；
 *  - 每个窗口高度固定（约 50 行），日志再多也只在自己内部滚动，不把整个页面撑长。 */
/* 窗口清单（`SOURCES`）本身住在 `static/js/model/sources.js`：它是窗口顺序、偏好键与
 * 证据文件名的唯一出处，这里只解构使用。 */

/* 阅读偏好存在浏览器本地。
 * ⚠️ 这几个常量必须定义在 `app` 之前：`app.prefs = loadPrefs()` 在模块求值时就跑，
 * 那时在 TDZ 里的 `const` 会直接抛 ReferenceError —— 而 loadPrefs 里的 try/catch
 * 会把它当成“读不到偏好”，于是一整类偏好（字号/字体/哪些窗口收起了筛选行）
 * 刷新后永远回到默认值，而且完全不报错（）。 */
const PREFS_KEY = "pw.logPrefs";
const FONT_MIN = 9;
const FONT_MAX = 22;
/* 分栏宽度的上下限（比例）与最大栏数：初始化读偏好、以及 normalizeSplit 都会用到，
 * 所以必须声明在 `app` 之前（踩过  的 TDZ 坑）。 */
const SPLIT_MIN = 0.15;
const SPLIT_MAX = 0.85;
const SPLIT_MIN_N = 1;
/* 最大栏数 = 窗口数（2026-10-10 起是 5：测试阶段也是一个窗口）。
 * 加窗口就要一起改这里：不然“分成 5 栏”在界面上会静默少一栏。 */
const SPLIT_MAX_N = 5;
const SPLIT_STEP = 0.02;
/* 窗口最小可用宽度：拖到比它还窄就“收成标签”（而不是挤成一条缝）。
   它远大于标签条本身（--rail-w: 26px）—— 收起是为了省地方。 */
const PANE_MIN_W = 160;

/* 命令输入行（W6-1）：它长在解码窗最底端，上沿可拖。
   拖到最上（≤ CMD_H_MIN）就收成一个 ▲；点 ▲ 回到 CMD_H_DEFAULT。 */
const CMD_H_MIN = 26;
const CMD_H_DEFAULT = 52;
const CMD_H_MAX = 240;
/* 拖动分隔条要先动过这么多像素才算“在拖”：否则单击一下就会把窗口挪半个条的宽度，
   而且双击的第一下先挪动第二下就换地方了 —— 双击很难触发（用户报过）。 */
const DRAG_SLOP = 3;
/* 分栏菜单鼠标移开后多久才收起：鼠标从按钮挪进菜单要跨过 6 px 的缝，
   太快收起会“还没来得及点就没了”（用户报过）。 */
const SPLIT_MENU_CLOSE_MS = 450;
/* 点一下标签要等这么久才算“单击”：这段窗口里凑齐第二下就是双击。
   之前是单击立即展开 —— 结果第一下就把标签展开成窗口，
   第二下落在别处，`dblclick` 再也不触发（用户报过“双击标签没反应”）。 */
const RAIL_CLICK_MS = 260;
/* 一行布局的断点（和 CSS 里的 @media (max-width: 1044px) 是同一个数）。 */
const ROW_MIN_W = 1045;
const isRowLayout = () => !globalThis.window || !globalThis.window.innerWidth
  || globalThis.window.innerWidth >= ROW_MIN_W;

/* --------------------------------------------------------------- 运行模式 */
/* 三种模式共用一个调试台：自动验证（build→upload→observe→assert）、实时观察（连上设备
 * 长时间看）、离线诊断（只看已有文件）。切换模式**不触发任何动作**：不编译、不烧录、
 * 不复位、不操作 DTR/RTS、不清理设备数据（需求 §1）。"谁拿着串口"由服务端的串口所有权
 * 状态机保证，页面只负责把它画出来，并在按钮上说清现在能不能点、为什么不能点。
 *
 * ⚠️ 模式与本机偏好一样要记住：刷新后如果悄悄回到“自动验证”，用户会以为实时观察已经
 * 断了去点启动 —— 服务端会正确地拒绝他，但这是一场可以避免的困惑。
 * ⚠️ 这几个常量必须声明在 `app` 之前（ 的 TDZ 坑）。 */
const MODES = [
  { id: "auto", label: "自动验证" },
  { id: "live", label: "实时观察" },
  { id: "offline", label: "离线诊断" },
];
const MODE_IDS = MODES.map((entry) => entry.id);
const MODE_KEY = "pw.mode";
/* 命令历史存在本机（按端口隔离）。常量必须在 `app` 之前声明（ 的 TDZ 坑）。 */
const CMD_HISTORY_KEY = "pw.cmdHistory";
const CMD_HISTORY_MAX = 50;

function loadCmdHistory() {
  try {
    const raw = localStorage.getItem(CMD_HISTORY_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (error) {
    return {};
  }
}

function loadMode() {
  try {
    const stored = localStorage.getItem(MODE_KEY);
    if (MODE_IDS.includes(stored)) return stored;
  } catch (error) {
    /* 本机读不到就用默认模式，不影响使用 */
  }
  return "auto";
}

/* 页面状态。`log` 在这里初始化（不能写在文件开头：它依赖上面的 SOURCE_IDS）。 */
/* 实时降载（L5）的**阈值**：`createShedState()` 在下面的 `app` 字面量里就被调用了，
 * 所以这几个数必须比它先初始化（`const` 不是 `function`：写在后面会掉进 TDZ）。 */
const SHED_FLUSH_MIN_MS = 16;      // 一帧
const SHED_FLUSH_MAX_MS = 250;     // 再慢也不许把界面拖成"卡死"
const SHED_BATCH_BUDGET_MS = 16;   // 需求 §6：主线程单批 p95 < 16 ms
const SHED_QUEUE_MAX = 4000;       // 队列上限（到了丢最旧的，并记账）
const SHED_SAMPLE_CAP = 64;        // 留最近多少批的耗时（用来算 p95）

/* 指标趋势的**两个窗格**（用户 2026-10-06 提）：每窗一个指标。
 * 每个窗格的高度 = 单个图表卡片的两倍（见 style.css 的 `--chart-card-h`）。
 * ⚠️ 这两个常量被 `loadPrefs()` 在**模块求值时**用到 ⇒ 必须声明在 `app` 之前（ 的 TDZ 坑）。 */
const METRIC_PANE_COUNT = 2;
const METRIC_PANE_DEFAULTS = [
  // 左：堆。先看空闲量（曲线），`heap_total` 是占用读数的分母。
  ["metric.sys.heap", "metric.sys.heap_total", "metric.sys.min_heap"],
  // 右：默认看历史最低空闲堆（"是不是一直在往下走"）。梯子里把**左窗格的首选排到最后**：
  // 数据稀疏时（比如只有 heap 与 heap_total 两条）两窗也尽量画不同的东西。
  ["metric.sys.min_heap", "metric.sys.tx_stack", "metric.sys.tasks",
   "metric.sys.heap_total", "metric.sys.heap"],
];

/** 读回两个窗格的选择：只认非空字符串，缺项补 null（旧版本没有这个字段 ⇒ 走默认梯子）。 */
function loadMetricPanes(saved) {
  const rows = Array.isArray(saved) ? saved : [];
  return Array.from({ length: METRIC_PANE_COUNT }, (_, index) =>
    typeof rows[index] === "string" && rows[index] ? rows[index] : null
  );
}

/* 横轴的**时间窗**（用户 2026-10-07 指的"任务管理器那个效果"）：任务管理器固定画最近 60 s，
 * 新的在右边、旧的从左边掉出去。
 * ⚠️ 把整段捕获压进一格时，一次 20 s 的占用/释放只剩下十几个像素 —— 形状就糊成"锯齿纹理"了。 */
const METRIC_WINDOW_CHOICES = [
  { key: "60", label: "60 秒", seconds: 60 },
  { key: "120", label: "2 分钟", seconds: 120 },
  { key: "300", label: "5 分钟", seconds: 300 },
  { key: "all", label: "全部", seconds: null },
];
const METRIC_WINDOW_DEFAULT = "60";

/* 纵轴的**刻度**。任务管理器一眼能读，是因为**轴有绝对含义**（0–100%）：
 * 那条贴着底部的小尖刺才能读成"很闲"；换成"按数据自动缩放"，同一段数据会被拉满整格，
 * 大小就失去了参照。
 *   full = 0 … 满量程（堆类指标用固件报的**总量**；其余用整段捕获的最大值向上取整）
 *   auto = 按画出来的这一段缩放（看抖动形状方便，但轴每次重画都在变） */
const METRIC_SCALE_CHOICES = [
  { key: "full", label: "0–满量程" },
  { key: "auto", label: "自动" },
];
const METRIC_SCALE_DEFAULT = "full";
/* 这几个指标的"满量程"就是堆总量（它们的占用率才说得清）。 */
const HEAP_SCALE_KEYS = ["metric.sys.heap", "metric.sys.min_heap"];

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

/* 开箱那一份阅读偏好：`app` 建好之前就要用（三份 store 在 `app` 的字面量求值期间就建好了）。
 * ⚠️ 那时读 `app.prefs` 会撞 TDZ（ 的老坑）⇒ 先取一份快照，`app.prefs` 指向同一个对象。 */
const BOOT_PREFS = loadPrefs();

const app = {
  lastSeq: 0,
  source: null,
  running: false,
  runId: null,
  steps: [],
  /*  / D5：设备嗅探。
   * `sniff` = 服务端 `/api/state` 里那一块（只读回显上一次结果）；
   * `selectedCard` = 用户在卡片里点的那一个（`{scan_seq, port}`）。
   * ⚠️ 选中的是**这一次嗅探里的序号 + 口**，不是"哪块板"：序号只是本次的显示编号
   *    （`scan_seq_persistent=false`），服务端每次 run 之前会拿它重新复核一遍。 */
  sniff: null,
  selectedCard: null,
  sniffBusy: false,
  /*  / D8：嗅探按钮那三态（嗅探 / 正在嗅探 / 嗅探完成）里的“完成”要能自己退回去
   * —— 退回去的计时器住在这里（“完成”只是一次提示，不是常驻状态）。 */
  sniffDoneTimer: 0,
  /* 这一屏自己有没有真的点过“设备嗅探”。⚠️ 卡片上的结果可能来自**上一次会话**
   * （服务端把上一次结果只读回显在 `/api/state` 里）—— 不说清就会让人以为
   * “刚才它自己又去问了一遍”（而那是隐式动作，本页绝不做）。 */
  sniffedHere: false,
  /*  T3：阶段卡片。`stage_id` → 最新状态，**顺序 = 首次出现顺序**。
   * 按 id 折叠而不是按到达顺序堆：同一步会推多次（pending → running → 终态），
   * 卡片说的是“这一步现在怎么样”，不是“推过几条事件”。
   *
   * ⚠️ 用户口径（2026-10-10）：阶段卡和四窗一样**按模式各存一份**（原来是一份全局的，
   *    切屏就清掉）—— 现在切到别的屏再切回来，那几张卡还在，
   *    别的窗口就是这个手感（ 的同一条理由）。每份 = `{ rows, seen, note }`。 */
  stages: { auto: null, live: null, offline: null },
  testStepTimer: null,
  /* 断言行（模型）：回放与实时都先收进这里再整屏重画，于是“把两个结论分开念”
   * 这条规则在两条路径上是**同一份实现**（不各写一遍、也不会两处走调）。 */
  assertionRows: [],
  /* 两个指标窗格各显示哪个 key（用户选过的优先；null = 走 `METRIC_PANE_DEFAULTS` 的梯子）。 */
  metricPanes: [],
  /* 两窗各自的时间窗与纵轴刻度（见 METRIC_WINDOW_CHOICES / METRIC_SCALE_CHOICES）。 */
  metricWindows: loadMetricWindows(null),
  metricScales: loadMetricScales(null),
  metrics: null,
  /* 指标和四窗一样按模式分账：历史 run、实时会话不能共用一张图。
   * `metrics` 只是当前贴在 DOM 上的那份；后台到达的数据写进对应模式，切回时再画。 */
  metricsViews: { auto: null, live: null, offline: null },
  /* 这份指标图属于**哪一次会话**（）：服务端只推新增的点，页面把它们并进
   * 已有的那一份 —— 换了会话就不并（新会话从零开始画），否则两次会话的点会画成一条线。 */
  metricsSession: { auto: null, live: null, offline: null },
  /* 跳过了多少条"上一次会话的积压行"（）：说实话用的，不是装饰。
   * `unattributed` 是"会话开始**之前**的控制行"（那时还没有会话号）：它们同样不该进实时窗口，
   * 但**不能**说成"上一次会话" —— 正在跑着会话时那句话是假的（真机验收当场看到过）。 */
  replaySkip: { lines: 0, unattributed: 0, session: "", reported: false },
  /* 还没拿到 `/api/state` 之前先存着的实时行（）。
   * 为什么必须存：SSE 的重放**常常比状态先到**，那一刻 `currentLiveSessionId()` 还是 null ——
   * 直接判"不是我的"就会把**正在跑的那次会话**的行也丢掉（真机验收：刷新页面后少了 586 行，
   * 还弹了一句"上一次会话（<就是这次会话>）"）。所以先存着，等状态到了按同一道闸再判一遍。 */
  replayHold: { waiting: true, events: [] },
  /* 未分类事件（一条事件不属于任何窗口）：计数 + 每条原因只报一次。
   * 它们不进任何窗口的正文 —— 静默丢弃与“本来就没有”长得一样，所以要记账。 */
  unclassified: { count: 0, reasons: new Map() },
  /* 渲染账本：`事件号 → 画进哪一窗`。同一事件号进了两个窗口就是串窗，能当场抓出来。 */
  renderLedger: new Map(),
  /* 这次页面加载时服务端的重放**被截断过**没有（）：实时那一屏的行是
   * 从 SSE 接上来的，截断就意味着窗口的第一行**不是**会话证据的第 1 行 ——
   * 而阶段坐标是按文件行号记的，这时不能按它定位（宁可明说不定位）。 */
  replayTruncated: false,
  observeSeen: false,   // 状态到过没有（没到过 ⇒ 上面的"存着"才生效）
  lastPane: "tool",   // Alt+L / Ctrl+F 作用于最近操作过的那个窗口
  foldStep: 48,       // 收起一行筛选行省下的高度（applyPrefs 时从 CSS 读）
  /* 布局形态（Slice C）：focus = 单窗全屏；split = 双源分栏。两者都是临时阅读形态，不落盘。 */
  layout: { mode: "grid", focus: "", split: normalizeSplit({ n: 2 }) },
  prefs: BOOT_PREFS,
  /* 本次看/跑的那份证据的目录（页脚那一行要用它说“证据在哪”）：两个装载路径各写一个，
   * 页脚按**当前这一屏**取（自动验证屏取 run 目录、离线屏取会话目录）。
   * 取不到就是空串 —— 页脚就不说“证据在哪”，不编一个路径。 */
  runDir: "",
  sessionDir: "",
  /* 「详情」那一行（身份与字典的依据）的展开状态与原文：
   * `open` = 用户点了展开（**默认收起**：屏幕上只留结论 chip，P1）；
   * `say` = 当前这一份 `{text, title}`（由 `identityDetailSay()` 算）；
   * `for` = 它属于哪个口 —— 换设备就收起（不沿用上一台的说明）。 */
  identDetailOpen: false,
  identDetailSay: null,
  identDetailFor: "",
  /* 「更多设置」（§11.1：低频选项默认收起）此刻是展开还是收起。 */
  moreOpen: false,
  mode: loadMode(),   // auto | live | offline（存在本机）
  /* 状态句的档位：简洁 / 标准 / 详细。用户口径 2026-10-12 起页面上**没有**档位控件，
   * 屏幕上固定用「简洁」那一档（`loadSayLevel()` 只说这一件事；`boot()` 也会写一次）。
   * 常量声明在 `app` 之前是  的坑。 */
  sayLevel: "brief",
  webCatalog: null,   // 装配层公布的目标 / 固件 / 套件；页面不写死具体设备
  /* ：`/api/state.platform.assemblies[]` —— 每条装配**自己声明**的能力表（九项）。
   * ⚠️ 不走 `web_catalog.firmwares[]`：那份实测**没有** `capabilities` 字段（全为 null），
   *    要回答“选中的这份固件支不支持解码”，只有装配自述说得准。 */
  platformAssemblies: [],
  devices: [],        // 服务端的设备登记（每块板的口在这里；用来判断"这次 run 会用哪个口"）
  observe: null,      // 服务端的实时观察会话快照（/api/state 的 observe 字段）
  /* 三个模式**各一份**四窗内容（）：store + 来源 + 可搜产物。
   * 以前只有一份 ⇒ 谁后写谁赢（打开一个离线包会把正在看的 run 顶掉，
   * 而且实时观察的行会**混进**正在回看的会话里 —— 两者时间基准都不一样）。 */
  views: createLogViews(),
  /* 谁正在收数据（SSE 的行事件按它路由）：一次 run ⇒ "auto"，一次实时观察会话 ⇒ "live"。
   * 一台设备一条串口：两者不会同时采集（互相 409），所以一个字段就够。 */
  receivingMode: "auto",
  sessions: [],       // 本机 + 导入的会话清单（只读目录，不碰串口）
  /* 模式化历史列表（RUNTIME_MODES §12）的状态：三类数据 + 各自的加载/失败状态。
   * 状态分开存是为了**诚实降级** —— 接口失败要说“读不到”，不能拿上一次的行冒充这一次。 */
  runs: [],
  runsState: "loading",
  runsError: "",
  sessionsState: "loading",
  sessionsError: "",
  sessionsDir: "",    // 本机会话目录的绝对路径（服务端给的，用于页脚明说证据在哪）
  importsDir: "",     // 导入包的目录（另一个根：别人给的包不冒充本机采集）
  // 正在导入的诊断包（P4）：任务号先给、边传边报、可取消。
  importTask: null,
  importRequest: null,
  cmdHistory: loadCmdHistory(),  // 主机侧命令历史：{ 端口: [命令, …] }
  cmdCursor: null,    // 正在用 ↑/↓ 翻到哪一条（null = 还没翻）
  cmdEndingDevice: "",  // 已经套用过默认行结束符的那个设备（换设备才重新套用）
  // 窗口内直敲（W6-4）：哪个窗口拿着键盘（"" / "raw" / "decoded"）。
  capture: "",
  // 页面加载时服务给的构建戳（）：轮询发现它变了就提示"页面是旧的"。
  build: null,
  // 终端直敲：按键队列 + “同时只有一个请求在飞”。
  terminal: { queue: [], inFlight: false, batches: 0, keys: 0, bytes: 0, dropped: 0 },
  // 详情里的产物元数据（L4 全文搜索的证据下拉框就列这些，不另编一份清单）。
  artifactsMeta: [],
  // 全文搜索：当前任务的 id 与证据名；timer 是轮询句柄（切对象时要清掉）。
  fullSearch: null,
  fullSearchTimer: null,
  fullSearchMisses: 0,
  // 实时大流量的 UI 降载（L5）：待写 DOM 的事件队列 + 自适应间隔 + 记账。
  queue: [],
  shed: createShedState(),
  flushTimer: null,
};

/* 用户动过哪几个窗格（“阶段窗默认收起”那条规则唯一的例外依据）。
 * ⚠️ 必须**在模块求值时就**从本机偏好装载：`renderObserve` 可能在任何一次状态轮询里
 *    问它，而初始化排在后面 —— 晚装载就会出现一个“窗口期”：刚刷新时，用户收起来的那一窗
 *    被当成“我们默认收的”又拨开（实测踩到：双击收起 → 刷新 → 它自己又展开了）。
 * ⚠️ 只记“谁动过”，不记“动成什么样”—— 布局状态仍然只有 `app.layout.*` 一份。 */
const layoutTouched = new Set();
for (const id of ((app.prefs.workspace && app.prefs.workspace.touched) || [])) {
  if (SOURCE_IDS.includes(String(id))) layoutTouched.add(String(id));
}

/* ------------------------------------------------- 三个模式各一份四窗内容（）
 * 自动验证 / 实时观察 / 离线诊断 **各自**持有一份四窗内容；切换模式 = 换一屏。
 *
 * ⚠️ 关键做法：三个模式各有一份 `sources` 表，但四窗只有**一个** store 对象
 *（`attachedLog`），切模式只换它挂的 `sources`。为什么不是三个 store 对象：
 * 四窗的拖拽/折叠/搜索/下载那一大堆接线在启动时就把 store **捕获**了（一次），
 * 而那些处理函数里每次都是 `store.sources[id]` 取值 —— 换表之后它们自动跟着
 * 当前模式走，一行都不用改；换对象的话每一处闭包都得改成“调用时再取”。
 *
 * ⇒ 因此只一条规矩：**不要在一开始把 `source` 对象单独存起来**（存 id，用时再取）。
 *
 * ⚠️ DOM 只有一份（四个窗口的节点、按钮、输入框），三个模式**共用**这些引用
 *（`shareDomRefs()` 把它们分给每一份）；所以只有“贴在窗口上的那一份”允许碰 DOM，
 * 而正在收数据的那一份如果不在眼前，就只进模型（切回去再画）——
 * “一边把实时观察存下来、一边分析离线包”靠的就是这一条。 */
function createLogView(mode) {
  const sources = createLogStore().sources;
  return {
    mode,
    sources,
    /* 后台写入用的外壳：模型函数只认 `store.sources[id]`，给它套一层就能用同一批函数，
     * 而它与 `attachedLog` **不同一个对象** ⇒ 自动不落 DOM。 */
    shell: { sources },
    viewing: null,       // 这一屏在看的 run（只有自动验证会用到）
    review: null,        // 这一屏在回看的会话（只有离线诊断会用到）
    artifactsMeta: [],   // 这一屏可搜的产物（全文件搜索的下拉框）
    windowContract: null,
    /* ：这一次 run **自己**声明的能力表（`manifest.capabilities`，九项）。
     * 需求 §2 要“历史 run 按其 manifest 中的能力回放” ⇒ 这一屏的能力只能来自**这一次 run**，
     * 不能借用实时观察那次会话的（拿别的对象的能力判这一屏，就会把 A 的原因贴在 B 的脸上）。 */
    capabilities: null,
  };
}

function createLogViews() {
  const out = {};
  for (const id of MODE_IDS) out[id] = createLogView(id);
  return out;
}

function viewFor(mode) {
  return app.views[mode] || app.views.auto;
}

/** 某一屏的阶段卡（按需创建）。传 `view` 而不是模式名：写法与 `attachedViewOf()` 一致
 *（用**那一份 view 对象**认人，不看 `app.mode` —— 换屏那一瞬间两者会不一致）。
 * 返回 `{ rows: Map, seen: bool, note: string }`。 */
function stageBag(view = attachedView()) {
  const key = view && MODE_IDS.includes(view.mode) ? view.mode : "auto";
  if (!app.stages[key]) {
    app.stages[key] = { rows: new Map(), seen: false, note: "", conclusion: "", batchId: "" };
  }
  return app.stages[key];
}

/* 贴在四个窗口上的那一份。
 * ⚠️ 按 `attachedLog.sources` 的**身份**判断，**不看 `app.mode`** —— `setMode()` 会先把
 * `app.mode` 改掉、再重画四个窗口，那一瞬间两者不一致（踩过一次：换模式时
 * “刚离开的那一份”算成了新那一份，滚动位置存到了错的地方）。 */
function attachedViewOf(store) {
  const sources = store && store.sources;
  for (const id of MODE_IDS) {
    if (app.views[id].sources === sources) return app.views[id];
  }
  return app.views.auto;
}

function attachedView() {
  return attachedViewOf(attachedLog);
}

/* 四窗的 store：**永远是同一个对象**，只换 `sources` 表（见上面那段注释）。
 * ⚠️ 一开始就要挂上当前模式的表：`boot()` 第一件事就是 `buildWorkspace(app.log)`。 */
const attachedLog = { sources: viewFor(app.mode).sources };

/* 往某一份**模型**写：它正好贴在窗口上就给那个 store（于是同时落 DOM），
 * 否则给它的外壳（只进模型，切回去才画）。 */
function storeFor(view) {
  return view === attachedView() ? attachedLog : view.shell;
}

/* 正在收数据的那一份（SSE 的行事件按它路由）。 */
function receivingView() {
  return viewFor(app.receivingMode || "auto");
}

function receivingStore() {
  return storeFor(receivingView());
}

/* ------------------------------------------- 五窗来源隔离（契约层）
 * 一条事件的正文**只**属于一个窗口：
 *   · `raw` → 只进「原始串口」；`decoded` → 只进「解码日志」；
 *   · `tool`（主机/平台/编排流程）→ 只进「工具输出」；
 *   · `app_events` → 只进「结构化事件」；`test_step(_refs)` → 只进「测试阶段」；
 * 认不得的 kind ⇒ **不进任何窗**（计数 + 记原因），绝不兜底到工具输出。
 *
 * ⚠️ “哪一**屏**”（自动验证 / 实时观察 / 离线诊断）是另一件事，由 `laneStore()` 回答。
 * ⚠️ `control` = 事件是真的、但它的正文不落任何窗（状态/消息/阶段卡由别的入口画）。
 */

/* 一条事件属于哪一窗（纯函数）：`{ source, reason }`。
 * `source === null` ⇒ 未分类（不进任何窗）；`reason` 是给人看的原因。 */

/* 未分类事件：**计数 + 记原因**，每条原因只说一次（同一段积压会连着来几千条）。
 * 它不进任何窗口的正文 —— 工具输出里出现一句“未分类事件”仍然是“多出来的一行”，
 * 所以这句话走消息行（那是状态，不是日志正文）。 */
function noteUnclassified(event, reason) {
  const book = app.unclassified;
  const key = String(reason || "未分类");
  book.count += 1;
  const seen = (book.reasons.get(key) || 0) + 1;
  book.reasons.set(key, seen);
  if (seen > 1) return;
  showMessage(
    `未分类事件：${key}（已计数 ${book.count} 条；它们不进任何窗口）`,
    "warn", "observe",
  );
}

/* 这一条事件把正文画进了哪一窗（机器检查用）：同一事件号**不许**进两个窗口。 */
function noteRenderedEvent(event, source) {
  const seq = event && event.seq;
  if (seq === null || seq === undefined || !source) return;
  const key = `${seq}`;
  const previous = app.renderLedger.get(key);
  if (previous && previous !== source) {
    app.renderLedger.set(key, `${previous}+${source}`);
    return;
  }
  app.renderLedger.set(key, source);
}

/* 五窗来源隔离的机器检查（纯函数，宿主回归与浏览器验收跑同一份实现）：
 *   ① 设备 RX 正文出现在「工具输出」；② 解码正文出现在「工具输出」；
 *   ③ 工具正文出现在「原始串口」；④ 同一事件号被两个窗口画过；
 *   ⑤ 未分类事件被兜底画进某个窗（结构上已不可能，这里仍按账核对）。
 * 返回一串人话问题（空数组 = 干净）。
 *
 * ⚠️ 只比**正文**：空行与短于 `DEVICE_MIN_CHARS` 的字串不参与（一个空行或单个换行
 *    说明不了任何事，比了只会假红）。 */
const DEVICE_MIN_CHARS = 3;

function windowSourceViolations(store, ledger = app.renderLedger) {
  const problems = [];
  const lines = (id) => ((store && store.sources[id] && store.sources[id].lines) || []);
  const body = (text) => String(text ?? "").trim();
  const long = (text) => text.length >= DEVICE_MIN_CHARS;
  const device = new Set(
    [...lines("raw"), ...lines("decoded")].map(body).filter(long),
  );
  for (const text of lines("tool")) {
    const clean = body(text);
    if (!long(clean)) continue;
    if (device.has(clean)) problems.push(`工具输出里有设备串口正文：${clean.slice(0, 80)}`);
  }
  const host = new Set(lines("tool").map(body).filter(long));
  for (const text of lines("raw")) {
    const clean = body(text);
    if (host.has(clean)) problems.push(`原始串口里有工具正文：${clean.slice(0, 80)}`);
  }
  for (const [seq, source] of ledger || []) {
    if (String(source).includes("+")) problems.push(`事件 ${seq} 同时画进了 ${source}`);
  }
  return problems;
}

/* 一条行事件该进哪一份（）：**事件自己带来源**（服务端给的 `origin`）。
 * 以前只看 `app.receivingMode` —— 那个变量只记得"谁最后一个开始收"：
 * 开着实时观察（COM<n>）时另一个人跑了 run，`run_started` 把它改成 `auto`，
 * 于是**实时那一路的行**落进了自动验证的窗口（用户报的"两个不通的串口一起刷屏"）。
 * `origin` 缺省（旧录音、老服务）时仍旧退回老行为，不然那些录下的东西会突然没窗口可进。
 *
 * ⚠️ （第二刀）：路标只说"这是哪条路"，不说"这是哪一次会话"。总线是**进程级**的，
 * 新页面连上来时服务端会重放一段（每条都带 `replay: true`）—— 上一次会话的尾巴于是带着
 * `origin="live"` 落进实时那一屏：头部写着"未连接"，窗口里却一行行冒数据（用户报的就是它）。
 * 所以实时这一路再加一道闸：**重放的**行只有属于"正在跑的那次会话"才进窗口（刷新页面
 * 之后补看本次会话已到的内容，这条必须留）；其余一律 `null` = 不进任何一份 ——
 * 要看去「会话回看」/ 离线诊断（证据文件是完整的）。
 * ⚠️ 自动验证那一路**不设**这道闸：那一屏本来就在显示"最近一次 run 的输出"，重放出来的
 * 与它当时见过的内容一致（`/api/state.last_run` 也是这么说的），两边不矛盾。 */
function laneStore(event) {
  const origin = String((event && (event.origin || event.lane)) || "");
  if (origin === "live") {
    const mine = replayedLineBelongsToCurrentSession(event);
    if (app.replayHold.waiting) {
      /* 还没拿到 `/api/state`（重放**常常比状态先到**）：不下结论。
       *   · 行类（raw/decoded/app_events/tool）先存着 —— 连"不是重放"的实时行也一起存，
       *     顺序不能乱（早的先落）；
       *   · 控制类（observe_state / info）直接丢：状态**以 `/api/state` 为准**，
       *     重放里的"已连接"是上一次的事（真机验收：这一条曾经被判成"上一次会话"，
       *     而那个会话号就是**正在跑的那一次** ⇒ 弹了一句假话）。 */
      if (SHED_QUEUED_KINDS.has(event.kind)) {
        holdReplayLine(event);
        return null;
      }
      return event.replay ? null : storeFor(viewFor("live"));
    }
    if (event.replay && !mine) {
      noteSkippedReplay(event);
      return null;
    }
    return storeFor(viewFor("live"));
  }
  if (origin === "run" || origin === "auto") return storeFor(viewFor("auto"));
  return receivingStore();
}

/* 一页最多存多少条（正常只有几百毫秒的窗口；上限只是防"状态一直不来"）。 */
const REPLAY_HOLD_MAX = 4000;

function holdReplayLine(event) {
  const held = app.replayHold;
  if (!held.waiting) return false;
  held.events.push(event);
  if (held.events.length > REPLAY_HOLD_MAX) {
    held.events.splice(0, held.events.length - REPLAY_HOLD_MAX);
  }
  return true;
}

/* 状态到了（知道自己在哪次会话）：把存着的那批按**同一道闸**再走一遍。
 * 顺序不能乱：存着的那批里排在前面的是更早的行，所以按到达顺序回放。 */
function flushHeldReplayLines() {
  const held = app.replayHold;
  if (!held.waiting) return 0;
  held.waiting = false;
  const events = held.events;
  held.events = [];
  const mine = currentLiveSessionId();
  let kept = 0;
  for (const event of events) {
    if (!event.replay || (mine && event.session_id === mine)) {
      applyEvent(event, null);
      kept += 1;
    } else {
      noteSkippedReplay(event);
    }
  }
  return kept;
}

/* 正在跑的那次实时会话的会话号（服务端快照里 `observe.session.session_id`）。 */
function currentLiveSessionId() {
  const session = app.observe && app.observe.session;
  return (session && session.session_id) || null;
}

/* 重放下来的一条实时行，属不属于**正在跑的那次会话**？ */
function replayedLineBelongsToCurrentSession(event) {
  const mine = currentLiveSessionId();
  return Boolean(mine) && event.session_id === mine;
}

/* 跳过的旧会话积压：**说得出**（咽下去就等于把"你看到的不是全部"藏起来）。
 * ⚠️ kinds：带会话号 **且不是** 正在跑的那次 ⇒ 真的是"上一次会话"（要说一次）；
 * 不带会话号 ⇒ 是那次会话**开始之前**的控制行（连接/身份那几句）：同样不进窗口，
 * 但那不是"上一次会话"，正在跑着会话时这么说就是假话（真机验收当场看到过）。
 * 一条页面生命周期内只说一次：同一段积压会连着来几千条，逐条报等于刷屏。 */
function noteSkippedReplay(event) {
  const its = event.session_id || "";
  app.replaySkip.lines += 1;
  // ⚠️ 状态还没到 ⇒ **不下"上一次会话"这个结论**（那条消息在真机上都假过一次）。
  if (!app.observeSeen) return;
  if (!its) {
    app.replaySkip.unattributed += 1;   // 会话开始**之前**的控制行：不是"上一次会话"
    return;
  }
  if (its === currentLiveSessionId()) return;   // 保险：这道闸本来就不该走到这里
  /* ⚠️ 用户口径（2026-10-12）：**没有正在跑的会话**时一个字都不说。
   *    刷新页面时服务端会把上一次会话的尾巴重放一遍，那时实时那一屏本来就不该收它们
   *    （头部写着「未连接」，也没有什么可对照的）—— 原来的实现会因此弹一条常驻红字，
   *    每次刷新都出现，对使用没有任何影响。**正在跑着的那一次**才需要这句提醒。 */
  if (!currentLiveSessionId()) return;
  app.replaySkip.session = its;
  if (app.replaySkip.reported) return;
  app.replaySkip.reported = true;
  showMessage(
    `上一次会话（${its}）的积压正在被跳过：实时那一屏只显示`
    + "\u201c正在跑的那次会话\u201d的行。要看这次录像请打开会话回看 / 离线诊断"
    + "（证据文件是完整的）。",
    "warn",
    "observe"
  );
}

/* 这一份此刻是不是贴在窗口上 —— **只有它允许碰 DOM**。 */
function isAttached(store) {
  return store === attachedLog;
}

/* 把模式记住的那一屏接到四个窗口上：换 `sources` 表 + 重画四窗 + 控件与工具栏同步。
 * ⚠️ DOM 只有一份，所以**滚动位置也得自己按模式记**：离开前把四个窗口的位置写在
 * 那一份上，回来时放回去（跟尾的窗口仍然贴底 —— `renderView` 已经处理）。 */
function attachView(mode) {
  const leaving = attachedView();
  for (const id of SOURCE_IDS) {
    const el = leaving.sources[id] && leaving.sources[id].el;
    if (el) leaving.sources[id].savedScrollTop = el.scrollTop;
  }
  const view = viewFor(mode);
  attachedLog.sources = view.sources;
  for (const id of SOURCE_IDS) {
    renderView(attachedLog, id);
    const source = view.sources[id];
    const el = source.el;
    if (el && source.savedScrollTop !== undefined && !source.followTail) {
      el.scrollTop = source.savedScrollTop;
    }
  }
  updateLogIndicators(attachedLog);
  updateButtons();
  renderFullSearchChoices();
  // 降载的记账只在**实时观察那一屏**显示 ⇒ 换屏就得重算一次（不然它会在别的屏上留着）。
  updateShedIndicator();
  return view;
}

/* `app.log` / `app.viewing` / `app.review` / `app.artifactsMeta` / `app.windowContract`
 * 都是**当前模式那一份**的转口：老代码一行不用改，语义却自动按模式分开。 */
Object.defineProperty(app, "log", { get: () => attachedLog });
Object.defineProperty(app, "viewing", {
  get: () => attachedView().viewing,
  set: (value) => { attachedView().viewing = value; },
});
Object.defineProperty(app, "review", {
  get: () => attachedView().review,
  set: (value) => { attachedView().review = value; },
});
Object.defineProperty(app, "artifactsMeta", {
  get: () => attachedView().artifactsMeta,
  set: (value) => { attachedView().artifactsMeta = value; },
});
Object.defineProperty(app, "windowContract", {
  get: () => attachedView().windowContract,
  set: (value) => { attachedView().windowContract = value; },
});

/* 四窗的 DOM 引用表（`buildWorkspace()` 把它们绑到 store 上）。
 * ⚠️ 这里就是“一份 DOM、三份模型”的接口面：**少写一个键的后果是“某一模式的控件没跟上来”**，
 * 所以新增控件时把它加进来，并且 `test_webconsole` 有一条判据核对这张表与绑定点一致。 */
const DOM_REF_KEYS = [
  "el", "pane", "pauseBtn", "tailBtn", "olderBtn", "pill", "pillCount", "stat", "notice",
  "link", "searchInput", "regexBtn", "prevBtn", "nextBtn", "hitsLabel", "errorsBtn",
  "excludeBtn", "excludeCount", "excludeInput", "excludeRegexBtn", "excludeHint",
  "menuBtn", "menu", "menuOpen", "foldBtn", "fullBtn", "findRow",
  /* ⚠️ 下面三个是**装 DOM 元素的容器**（面板里的下拉框、级别按钮、那几个开关按钮）：
   * 一份 DOM、三份表 —— 不共享的话，别的模式下 `refreshModuleOptions()` 会因为
   * `moduleSelect` 是 null 直接返回 ⇒ 模块下拉框是空的、级别/开关按钮永远不亮。 */
  "moduleSelect", "levelButtons", "toggles",
];

/* 把一份绑好的 DOM 引用分给**所有**模式那一份（DOM 是一份，模型是三份）。 */
function shareDomRefs(store) {
  for (const view of Object.values(app.views)) {
    if (!view || view.sources === store.sources) continue;
    for (const id of SOURCE_IDS) {
      const from = store.sources[id];
      const to = view.sources[id];
      if (!from || !to) continue;
      for (const key of DOM_REF_KEYS) {
        if (key in from) to[key] = from[key];
      }
    }
  }
}

/* 从一份**给定的**实时观察快照里挑“这一次（或最近一次）的会话”（纯函数）。
 * ⚠️ 与 `evidenceSession()` 的区别：那个在没给快照时会**回退到全局** `app.observe`，
 * 而这个只认传进来那一份 —— 否则判据会被“此刻全局恰好是什么”影响。 */
function snapshotSession(snapshot) {
  if (!snapshot) return null;
  if (snapshot.session && snapshot.session.session_id) return snapshot.session;
  return snapshot.last && snapshot.last.session_id ? snapshot.last : null;
}
/* 导入任务的轮询节奏（P4）：有限次、不抢带宽 —— 一次导入通常几十秒，
 * 2 s 一跳足够看出阶段变化；拿不到就作罢（请求结束自有结论）。 */
const IMPORT_POLL_MS = 2000;
const IMPORT_POLL_MAX = 300;

/* 设备流是无限的，视图必须有上限；但截断必须**记账**，不然“看到的就是全部”
 * 会变成一句没人能核实的话。 */

/* --------------------------------------- 历史证据的区间窗口（ / L3） */
/* 一份证据可能几百 MB，页面**不能**把整份装进来。所以历史装载改成"窗口"：
 * 默认只取尾部一段，往前翻一页取相邻的更早一段，并且永远说清"现在看的是哪一段"。
 *
 * 三条不变量（宿主回归直接核对下面的纯函数）：
 *
 * 1. **尾部窗口大小不在这里写死**：服务端详情里带 `evidence_contract.tail_window_bytes`
 *    （core 的 `large_evidence` 是唯一出处）。只有对端太老、没带这个字段时才退回
 *    `FALLBACK_TAIL_BYTES` —— 而"退回"这件事本身要看得见（`windowContract().from`），
 *    不然"页面用的到底是谁的数"就没人说得清。
 * 2. **旧响应绝不写进新对象**：每次装载递增 `win.generation`，回来时对不上就丢掉；
 *    换来源时显式 `abort()` 在飞的请求（需求 §5.3）。
 * 3. **边界行只标记、不假装完整**：字节起点落在行中间时，顶上那一行标成不完整，
 *    绝不把它当成一条完整的证据行（需求 PERF-HIST-05）。
 */

/* 字节数 → 人话。证据用 1024 进制（写成 MiB 而不是 MB，免得和十进制混）。 */

/* 区间合并：**重叠或相接**都合并（连着就是同一段字节）。
 * ⚠️ 与 core 的 `large_evidence.merge_ranges()` 同一条规则：中间缺至少一个字节的两段
 * 绝不合并 —— 合并会把"这里没加载"说成"已加载"，而页面正是拿它告诉用户看的是哪一段。 */

/* 尾部窗口：最后 `bytes` 字节（文件更小就整份）。0 字节 ⇒ null。 */

/* 向前翻一页：已加载区间里**最靠前那一段之前**的一段。到头（已经在 0 字节）⇒ null。 */

/* 服务端给的契约：尾部窗口大小 + 它的**出处**。 */

/* 从 `Content-Range` 里取文件总大小：正常形式是 `bytes a-b/total`；
 * 区间不可能满足时服务端发的是「星号 + 斜杠 + total」那一种 —— 两种都要认。
 * ⚠️ 这条注释里**不能**直接写出那两个字面字符：注释会在那里提前结束（踩过一次）。 */

/* 区间窗口的**诚实标注**（纯函数，宿主回归直接核对文字）：
 * 总大小 / 已加载字节 / 省略方向 / 顶行是否不完整。
 * 一句也不许把"只加载了尾部一段"说成"完整日志"（需求 §3.3）。 */

/* 把"更早的一段"接到模型前面（纯函数：不碰 DOM，也不改传进来的数组）。
 *
 * 返回 `{ head, headPartial, stitchWith }`：
 * - `head`：要插到模型最前面的行；
 * - `headPartial`：这段的第一行是不是半行（字节起点落在行中间）；
 * - `stitchWith`：这段的尾巴要与模型第几行拼成一条完整行（-1 = 不用拼）。
 *
 * ⚠️ 拼接是**必须**的：相邻两段之间没有重叠，所以更早那一段的最后一个片段正好是
 * 模型第一行的开头 —— 不拼就是把同一行显示成两条，而且两条都不完整。 */

/* 决定"更早的一段"到底怎么接上去（纯函数，宿主回归直接驱动）。
 *
 * ⚠️ 这里有一条**真机上摔出来的**规矩：只有模型**还保留着尾部窗口的第一行**
 * （`modelBaseIndex === 0`，也就是说没被行数上限裁过）时，拼接才有意义。
 * 模型有上限（`MAX_LINES`），前置了一大段之后最先被裁掉的恰好就是那行 ——
 * 那时候再把尾部片段拼到"现在的第一行"上，就是给一行**凭空加一个它没有的开头**
 * （真机上看到的是 `[I][I][demo] line=…`）。拼不成时那个片段只能如实留着。
 */


/* 文本 → 展示行。末尾的换行是行终止符，不是“一行空内容”。 */

function createLogStore() {
  const store = { sources: {} };
  for (const id of SOURCE_IDS) {
    const source = createSourceState(id);
    /* 筛选行的开合是**阅读偏好**，而三个模式**各有一份 store** ⇒ 必须在这里（唯一的工厂）装。
     * ⚠️ 从前只在 `buildWorkspace()` 里装 ⇒ 只有启动时那一屏吃到偏好，切到另两屏是展开的
     *（）。开箱默认改成“收起”之后，那个不一致就看得见了：同一个页面里两屏不一样。
     * ⚠️ 用 `BOOT_PREFS`（不是 `app.prefs`）：三份 store 是在 `app` 那个对象字面量**求值期间**
     * 建好的，那时 `app` 还在 TDZ 里（ 的老坑）。 */
    source.view.folded = BOOT_PREFS.folded[id] === true;
    store.sources[id] = source;
  }
  return store;
}

/* 每个窗口一份完整状态：**没有**“当前源”这种全局概念，
 * 所以暂停/未读/跟随都不可能跨窗口连坐。 */
function createSourceState(id) {
  return {
    id,
    lines: [],       // 模型：这个来源收到的全部展示行（有上限）
    baseIndex: 0,    // 已经被裁掉的行数：源行号 = baseIndex + 数组下标（裁剪后也不变）
    /* 派生视图（工具输出）的**行身份**：与 `lines` 逐行对齐的事件号（`io_events.jsonl` 的
     * `seq`），没有对应事件的行是 null。为什么单独一列而不是把行号当身份：这一窗的内容是
     * 画出来的，行号与文件行号不同源（）。不是派生视图的窗（例如 run 的 tool.log、
     * 原始串口）这一列是 null —— 它们按真行号定位，不需要也不该有事件号。 */
    seqs: SOURCE_BY_ID[id] && SOURCE_BY_ID[id].derived ? [] : null,
    /* 这次装载的内容是不是**派生视图**（与上面的静态声明分开：同一个窗口在三屏里
     * 装的内容不一样 —— 回看/实时是派生视图，run 的 tool.log 不是）。 */
    derived: SOURCE_BY_ID[id] ? SOURCE_BY_ID[id].derived === true : false,
    /* 行号基准可与不可信、以及“这一窗根本不用行号定位”两件事要分开说（）。
     * `derived` 的窗：`paneFrameIssue` 不再拿行号说事，而按行号给的坐标会被如实拒掉。 */
    /* 这个基准**可信到什么程度**（）：行号列、`data-src` 与阶段定位都建立在它上面。
     *   `exact`   = 真基准：证据装载带回了 `X-Line-Base`，或流式行事件带回了 `line_no`（已核对过）；
     *   `assumed` = 假定 `lines[0]` 就是文件第 1 行 —— 行事件没带行号时的老行为；
     *   `broken`  = 流里出现过缺口 ⇒ 这一窗的行号从此不再指向文件行（结论不再改）。 */
    baseTrust: "assumed",
    baseIssue: "",   // `broken` 时那句**人话原因**（屏幕上的拒绝理由抄它）
    /* 这一窗的行是不是**从 SSE 接上来的**（）：证据装载/区间装载的窗不是 ——
     * `assumed` 那条降级规则只适用于流式窗（否则"这一页曾经重放被截断"会连累
     * 离线回看那一屏：它明明是按区间/整份装载的，行号基准随响应头回来）。 */
    streamed: false,
    /* L3：历史证据的**区间窗口**。非 null 时，`lines` 里装的不是整份证据，
     * 而是 `window.ranges` 那几段字节 —— 所以统计行必须说清"看的是哪一段"。 */
    window: null,
    partialHead: false,   // L3：模型第 1 行的开头在没加载的区间里（不完整行，不假装完整）
    /* 每行的**主机时刻**：键是源行号（= baseIndex + 数组下标），值是 UTC 毫秒/ISO 串。
     * 为什么不塞进 `lines` 里：`lines` 是**纯文本**（复制、搜索、折叠、宿主回归都按字符串
     * 对待它），往里塞对象会让那一整套逻辑悄悄换语义。
     * 为什么用 Map 而不是第二个数组：行会被裁剪（`trimSource`），两个数组要一直对齐 ——
     * 漏一处就会出现"时间戳串行"，而且看不出来。Map 里**没有键 = 不知道时刻**，
     * 那种行就不显示时间（不猜、不借用上一行的时间）。 */
    times: new Map(),
    rendered: 0,     // 已经处理过的行数（暂停时它会落在模型后面）
    pending: 0,      // “这个窗口里你还没看到的新行”
    dropped: 0,      // 因上限丢弃的最旧行数
    paused: false,   // 只冻结这一个窗口
    followTail: true,
    seen: new Set(), // 事件去重（SSE 重连会重发整批）
    view: createViewState(),
    el: null,
    pane: null,
    pauseBtn: null,
    tailBtn: null,
    pill: null,
    pillCount: null,
    stat: null,
    link: null,
    searchInput: null,
    regexBtn: null,
    prevBtn: null,
    nextBtn: null,
    hitsLabel: null,
    errorsBtn: null,
    // 文本排除（切片 6）：搜索行上的「排除」按钮（带已隐藏行数）+ 选项面板里的条件输入
    excludeBtn: null,
    excludeCount: null,
    excludeInput: null,
    excludeRegexBtn: null,
    excludeHint: null,
    menuBtn: null,
    menu: null,
    menuOpen: false,
    moduleSelect: null,
    levelButtons: [],
    toggles: {},
  };
}

/* “这个窗口正看着自己的尾巴” —— 只有这种情况，新行才不需要提示、才跟着滚。 */

/* 往模型里追加。`lines` 里元素可以是字符串，也可以是 `{ text, ts }`（一条一行自己的时刻）。
 * `ts` 是整个批次的时刻（一次串口读到的 N 行共享**到达那一刻**）——
 * 这不是偷懒：`io_events.jsonl` 里记的就是"这一次读到的这些字节"，同一批行本来就同时到达。
 * 两种都给时，行自己的 `ts` 优先。 */




/* 串口半行会为了低延迟分批到达：后续批次是上一条视觉行的续写，不是新行。
 * `lineNo`（可选）= 这一行在证据里的行号：接得上才接（见 `streamLineFrame`）。 */
function continueLine(store, id, text, ts, { lineNo = null } = {}) {
  const source = store.sources[id];
  if (!source || !source.lines.length) {
    // 半行成了这一窗的第一行（刷新正好落在半行中间）：它自己就把基准说清了。
    return feed(store, id, text, ts, { lineNo, continuation: true });
  }
  const frame = streamLineFrame(source, lineNo, source.lines.length, true);
  if (frame.issue) {
    /* ⚠️ 行号对不上 ⇒ 接上去就是给一行**凭空加一个它没有的尾巴**（屏幕上看起来像
     *    `[I][I]…`）。这一截照实**另起一行**，并把这一窗标成"行号不可信"。 */
    applyStreamFrame(source, frame);
    return feed(store, id, text, ts, { lineNo, continuation: false });
  }
  if (frame.base !== null) applyStreamFrame(source, frame);
  const last = source.lines.length - 1;
  source.lines[last] += String(text ?? "");
  const absolute = source.baseIndex + last;
  if (!source.times.has(absolute) && ts) source.times.set(absolute, ts);
  if (source.paused) {
    source.needsRebuild = true;
  } else {
    const scrollTop = source.el ? source.el.scrollTop : 0;
    renderView(store, id);
    if (source.el && !source.followTail) source.el.scrollTop = scrollTop;
  }
  updateLogIndicators(store);
  return 0;
}

/* --------------------------------------------- 派生视图：搜索、过滤、折叠 */
/* 这一层的铁律：**只读模型**。搜索、过滤、折叠、高亮都只是“怎么显示”，
 * 不会改写 source.lines，更不会碰 runs/ 里的证据文件；源行号也不随过滤变化。
 *
 * 每个窗口一份视图状态：没有“全局搜索”，一个窗口的查询不会影响另三个。 */

function createViewState() {
  return {
    query: "",          // 关键字或正则
    regex: false,       // 把 query 当正则
    error: "",         // 非空 = 查询不生效（并给出原因）
    /* 文本排除（切片 6，需求 §7）：只改**这一窗的显示**。
     * `excludeOff` 就是“一键停用”：停用后条件留着，方便反复对照。
     * 条件本身不落盘（与搜索词一样是临时的阅读状态）。 */
    exclude: "",
    excludeRegex: false,
    excludeOff: false,
    excache: null,      // 排除条件的编译缓存（搜索与排除各一个槽，互不顶掉）
    excludeStat: null,  // 已隐藏行数的缓存（增量追加只扫新来的行）
    level: "all",       // all | E | W | I | D
    module: "all",      // all | 具体模块名
    errorsOnly: false,
    lineNumbers: false,
    // 时间戳默认打开：现场联调第一件事就是"这行是什么时候来的"（北京时间、毫秒）。
    timestamps: true,
    dedupe: false,      // 连续重复行折叠成一行
    wrap: true,
    folded: false,      // 这一窗的搜索/筛选行收起（搜索条件仍然生效）。
                        // ⚠️ 这里是**单窗视图对象**的初值；开箱默认由 `loadPrefs()` 定（现为收起）。
    expanded: new Set(),// 被手动展开的折叠组（组首行号）
    hits: [],           // 可见行上的命中（按行顺序）
    hitCursor: -1,      // 当前命中（扁平的“第几处”）
    visible: 0,         // 当前视图里有多少行
    tail: null,         // 增量追加时的折叠组
    cache: { key: "", re: null, error: "" },
    notice: "",         // 一次性提示（重建清掉选区、复制结果等）
  };
}

/* 查询 → 正则。无效正则**当场**给出说明，而不是抛掉或悄悄当普通文本用：
 * 悄悄降级会让人以为“确实没有命中”。 */

/* 一行里的命中区间。每行设上限：一行里几千个命中只说明查询没意义，
 * 画出来只会把浏览器拖死（性能要求写在不卡死，而不是“能画出来”）。 */

/* 排除条件 → 正则。规则与搜索**完全一致**（无效正则当场说明，不悄悄降级）：
 * 悄悄把正则当普通文本用，会让人以为“确实没有命中”。
 * 单独一个缓存槽：搜索与排除各改各的，不能互相把对方的编译结果顶掉。 */

/* 排除是否在生效：填了条件、没有一键停用、而且条件本身是有效的。
 * ⚠️ 正则无效时**不生效**（既不是“全部隐藏”，也不是“偷偷当普通文本”）：
 * 那两种都会让人以为“确实没有匹配”，而真正的原因是一个写错的正则。 */

/* 这一行是不是被排除掉了（关键词或正则，规则与搜索一致）。 */

/* 这一行为什么看不见：null = 显示；"filter" = 级别/模块/错误过滤；"exclude" = 文本排除。
 * 两种“看不见”**必须分开**：被排除的行要计数并说清楚（需求 §7），被过滤掉的行不用。 */

/* 被排除掉的行数（显示用）。它是**算出来**的，不是一个只增不减的计数器：
 * 增量追加只扫新来的行，条件变了或线被裁剪过（baseIndex 变了）才重扫。
 * 一个累加计数器在裁剪之后会永远偏高 —— 而这个数是要给人看的。 */


/* 下一个可见项 —— 增量追加与整体重建**共用这一条规则**，所以两条路径不可能走散。
 * 返回 null = 这一行被过滤掉；merged = 并进了上一条折叠组（重复内容不再画）。
 *
 * 展开状态要**沿整组传递**：只把组首标成“摊开”不够，否则组的第 2 行会重新把
 * 第 3 行并进去 —— 看起来就像“只展开了第一行”。 */

/* 整份派生视图（可见项清单）。复制与宿主回归都拿它当“视图应该长什么样”的参照。 */

/* 复制/导出用的文本 = **看得见的那些行**：没有 UI 标记，也没有隐藏行。
 * 折叠组只留代表行（重复的那些行确实没显示出来）。 */


/* 扁平的第 n 处命中 → 哪一行、这一行的第几个。 */

/* 整体装载（历史 run、切换 run、清空）：视图被权威证据替换，
 * 所以这一路不再处于“冻结”状态 —— “暂停着但显示最新”是自相矛盾的。 */


function setPaused(store, id, paused) {
  const source = store.sources[id];
  if (source) source.paused = Boolean(paused);
}

function setFollowState(store, id, atBottom) {
  const source = store.sources[id];
  if (!source) return;
  source.followTail = Boolean(atBottom);
  // 暂停期间即使滚到底也不清零：视图停着，那些行仍然没被看过。
  if (source.followTail && !source.paused) source.pending = 0;
}

/* 恢复某一个窗口：只清**这一个窗口**的未读。
 * 其他三个窗口里的新行是它们自己的事 —— 它们没被看过，计数就该留着。
 * 如果这个窗口本来就没停在末尾（用户翻在上面读），连它也留着。 */
function resumeSource(store, id) {
  const source = store.sources[id];
  if (!source) return;
  source.paused = false;
  if (source.needsRebuild) {
    source.needsRebuild = false;
    renderView(store, id);
  }
  if (source.followTail) source.pending = 0;
}

/* 视图落后模型多少行（暂停期间才会 > 0）。 */

/* 视图追上模型：`rendered` 是“已画进 DOM 的行数”，只有 DOM 层会推进它。
 * 暂停时它原地不动 —— 差距就是待显示的行数。 */

/* 有界缓冲：丢掉最旧的行并记账。DOM 里装的永远是模型的**前缀**，
 * 所以“丢最旧的行”= 同时删掉容器最前面的同数节点。 */

/* 结构化事件 → 一行文本。seq 与 offset 都留着：前者是事件流里的身份，
 * 后者本身就是一个“能回到 decoded.log 那一字节”的定位器。 */
function eventLine(event) {
  const fields = Object.entries(event.fields || {})
    .map(([key, value]) => `${key}=${value}`)
    .join(" ");
  const body = event.raw ? String(event.raw) : (fields || "(无字段)");
  return `#${event.seq} ${event.kind} ${body} · offset ${event.offset}`;
}

function parseEventLog(text) {
  return String(text || "")
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch (error) {
        return null;
      }
    })
    .filter(Boolean);
}

/* 历史 run 的四来源装载：每份证据只进它自己那一路。 */
function buildRunSources(detail, rawText) {
  const stream = parseEventLog(detail && detail.event_stream);
  const gaps = (detail && detail.report && detail.report.evidence_gaps) || {};
  const streamed = (kind, fallback) => {
    const rows = [];
    for (const event of stream.filter((item) => item.kind === kind && item.line !== undefined)) {
      if (event.continuation && rows.length) rows[rows.length - 1].text += String(event.line);
      else rows.push({ text: String(event.line), ts: event.ts });
    }
    return rows.length ? rows : toLines(fallback);
  };
  const structured = [];
  const seen = new Set();
  for (const envelope of stream.filter((event) => event.kind === "app_events")) {
    for (const event of envelope.events || []) {
      const key = `${event.seq ?? "?"}:${event.offset ?? "?"}:${event.kind ?? "?"}`;
      if (seen.has(key)) continue;
      seen.add(key);
      structured.push({ text: eventLine(event), ts: event.ts ?? envelope.ts });
    }
  }
  const generic = stream
    .filter((event) => !["tool", "raw", "decoded", "app_events"].includes(event.kind))
    .map((event) => ({ text: JSON.stringify(event), ts: event.ts }));
  const withGap = (rows, key) => rows.length
    ? rows
    : (gaps[key] ? [{ text: `[无回放数据] ${gaps[key]}` }] : []);
  return {
    tool: withGap(streamed("tool", detail && detail.tool_log), "tool"),
    raw: withGap(streamed("raw", rawText), "raw"),
    decoded: withGap(streamed("decoded", detail && detail.decoded_log), "decoded"),
    events: structured.length
      ? structured
      : (generic.length ? generic : parseEventLog(detail && detail.app_events).map(eventLine)),
  };
}

/* --------------------------------------------------------- 工作台 DOM 层 */
/* 命中高亮用 mark 包住命中的片段：只改样式，不改文字，所以复制出来还是原始行。 */
function highlighted(text, ranges) {
  const fragment = document.createDocumentFragment();
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start > cursor) fragment.appendChild(document.createTextNode(text.slice(cursor, start)));
    const mark = document.createElement("mark");
    mark.className = "hit";
    mark.textContent = text.slice(start, end);
    fragment.appendChild(mark);
    cursor = end;
  }
  if (cursor < text.length) fragment.appendChild(document.createTextNode(text.slice(cursor)));
  return fragment;
}

/* 时间列的内容（纯函数，宿主回归直接核对它）：
 * 主机时刻优先（北京时间、毫秒），没有就退回设备自报的 uptime（`+12.345s`），
 * 两样都没有返回空串 —— 那一列就不画（不发明一个时间）。 */
function lineTimeLabel(source, absolute, text) {
  const hostStamp = source.times.get(absolute);
  if (hostStamp) return beijingClock(hostStamp);
  return lineTime(text);
}

function buildLineNode(source, item) {
  const node = document.createElement("div");
  node.className = "log-line";
  // data-src / data-ln 用的是**证据里的行号**（baseIndex + 数组下标）：
  // 行号列、命中定位、裁剪都靠它，过滤不会改变它，裁剪也不会让它跳回去。
  const absolute = source.baseIndex + item.index;
  node.dataset.src = String(absolute);
  node.dataset.ln = `${absolute + 1} `;
  /* 派生视图（工具输出）的行**身份**是它来自的那条 io 事件（）：阶段卡上的
   * `{ source: "tool", event_seq: N }` 就靠这一格找到行。没有对应事件的行（标题行、
   * 只在现场存在的主机说明）不带这一格 —— 不拿别的事件号顶上。 */
  const eventSeq = source.seqs ? source.seqs[item.index] : null;
  if (eventSeq !== null && eventSeq !== undefined) node.dataset.seq = String(eventSeq);
  else delete node.dataset.seq;
  // 时间列：主机时刻优先 —— 那是"这行什么时候到的"，和电脑上的表、和证据文件里的
  // ts / host_time 是同一个时刻；没有主机时刻的行退回设备 uptime，两种样子一眼能分开。
  const time = lineTimeLabel(source, absolute, item.text);
  if (time) node.dataset.time = `${time} `;
  if (item.repeats > 1) node.dataset.repeat = String(item.repeats);
  if (item.ranges.length) node.appendChild(highlighted(item.text, item.ranges));
  else node.textContent = item.text;
  return node;
}

/* 视图状态的记账：**两种渲染路径共用**（有 DOM 的与只推模型的宿主回归），
 * 所以“可见计数”“折叠组”“命中清单”不可能在两条路径里算出两个结果。 */

/* 只推模型、不建 DOM：宿主回归用它驱动与浏览器完全相同的判定。 */

/* 只追加：既有节点（连同其中的选区）一个都不碰。
 * 过滤与折叠也走这条路 —— 被过滤掉的行根本不生成节点。 */
function renderPending(store, id) {
  const source = store.sources[id];
  const host = source ? source.el : null;
  if (!host || source.rendered >= source.lines.length) return 0;
  const view = source.view;
  const fragment = document.createDocumentFragment();
  let appended = 0;
  for (let index = source.rendered; index < source.lines.length; index += 1) {
    const item = visibleItem(source, index, view.tail);
    if (!item) continue;
    if (item.merged) {
      acceptItem(view, item);
      if (view.tail.node) view.tail.node.dataset.repeat = String(view.tail.repeats);
      continue;
    }
    item.node = buildLineNode(source, item);
    fragment.appendChild(item.node);
    if (acceptItem(view, item, source.baseIndex + index)) appended += 1;
  }
  host.appendChild(fragment);
  markRendered(store, id, source.lines.length);
  return appended;
}

/* 视图回到“什么都没画”的状态（重建或整体装载的第一步）。 */

/* 裁剪后把视图状态也对齐：丢掉的命中不能再被“上一个/下一个”找到。
 * 可见计数按**真正删掉的节点数**减少 —— 被过滤掉的行本来就没节点，不能一条抵一个。 */
function pruneViewState(source, removedNodes = 0) {
  const view = source.view;
  view.visible = Math.max(0, view.visible - removedNodes);
  if (view.hits.length) view.hits = view.hits.filter((hit) => hit.index >= source.baseIndex);
  // 折叠组的代表行可能已经被删掉：那就当这一组结束了，下一次追加另起一行。
  const tailNode = view.tail && view.tail.node;
  if (tailNode && tailNode.isConnected === false) view.tail = null;
}

/* 整体重建派生视图（条件变了才走这条路）。它就是增量路径从 0 跑一遍，
 * 所以两条路径用的是同一条规则。重建会清掉选区 —— 调用方负责说出来。 */
function renderView(store, id) {
  // 不在眼前的那一份**只改模型、不碰 DOM**（切回去时才画）。
  if (!isAttached(store)) return 0;
  const source = store.sources[id];
  if (!source) return 0;
  // 【卡片窗】的正文不是行，行机制整个跳过它（内容由 `renderStepCards()` 自己画）：
  // 否则随便一次行重建都会把这一窗清成一片空白 —— 而“空白”看起来就像“测试没跑”。
  if (SOURCE_BY_ID[id] && SOURCE_BY_ID[id].cards) return 0;
  if (source.el) source.el.textContent = "";
  resetView(store, id);
  const appended = renderPending(store, id);
  if (source.el && source.followTail) source.el.scrollTop = source.el.scrollHeight;
  // 条件（排除/只看错误/级别/模块/折叠重复/搜索/行号/时间戳）变了就一起重画终端：
  // 两套画法共用同一份视图状态，不能一边变了另一边停在旧画面上。
  if (id === "raw" && terminalOn()) renderTerminalNow();
  return appended;
}

/* 整体装载（历史 run、切换 run、清空）时才允许清空重建。 */
function rebuildSource(store, id) {
  const source = store.sources[id];
  if (!source) return;
  trimSource(store, id);   // 历史 run 也守同一个行数上限，并如实记账
  // 换了一个 run，就不该把上一个 run 的模块名继续当过滤条件（那会把窗口清空，
  // 而“空窗口”看起来和“日志丢了”一样）；搜索词与文本排除也一并清掉。
  source.view.query = "";
  source.view.module = "all";
  source.view.error = "";
  source.view.cache = { key: "", re: null, error: "" };
  // 排除条件同样不跟着新来源走：一个针对上一次会话写的条件，用在另一份内容上
  // 很可能一下把窗口清空 —— 而“什么都不剩”与“本来就没内容”长得一模一样。
  source.view.exclude = "";
  source.view.excludeRegex = false;
  source.view.excludeOff = false;
  source.view.excludeStat = null;
  source.view.expanded.clear();
  /* 【卡片窗】没有行可重建，但“换来源就把条件清掉”这条规矩对它同样成立 ——
   * 而且清掉之后必须**立刻重新过滤一次**：卡片是被上一次筛选逐张打上 `hidden` 的，
   * 条件清了却不重滤，画面就停在“整窗空白”，而筛选框里已经空了
   *（用户没有任何条件可以清、也没有任何东西可以点 —— 真浏览器上就是这么撞出来的）。 */
  if (SOURCE_BY_ID[id] && SOURCE_BY_ID[id].cards) {
    source.view.hitCursor = 0;
    if (isAttached(store)) applyStageFilter(store);
    return;
  }
  renderView(store, id);
}

/* 裁剪后删掉容器里对应“已丢弃的源行号”的节点。
 * 按 data-src 删而不是数前 N 个：过滤/折叠之后，源行与节点的对应本来就不是 1:1。 */
function dropOldestNodes(source) {
  const host = source.el;
  if (!host) return 0;
  let removed = 0;
  while (host.firstChild && Number(host.firstChild.dataset.src) < source.baseIndex) {
    host.removeChild(host.firstChild);
    removed += 1;
  }
  return removed;
}

function settle(store, id, added) {
  const source = store.sources[id];
  // 正在收数据的那一份可能**不在眼前**（比如在看离线包时实时还在跑）：
  // 那就只进模型 —— 这就是“一边把实时观察存下来、一边分析离线包”。
  const visible = isAttached(store);
  // 暂停时这个窗口的视图不动，模型照收 —— 这正是“后台继续采集”。
  if (!source.paused && visible) renderPending(store, id);
  const dropped = trimSource(store, id);
  if (dropped && visible) {
    // 视图里已经没有的行，不能还留在“可见计数”和命中清单里。
    pruneViewState(source, dropOldestNodes(source));
  }
  // 只有“正看着自己尾巴”的窗口才跟着滚；翻上去读的窗口不该被抽走那一页。
  if (added && visible && isWatched(store, id) && source.el) {
    source.el.scrollTop = source.el.scrollHeight;
  }
  if (visible) updateLogIndicators(store);
  return added;
}

/* `ts` = 这条事件在**主机侧发生的那一刻**（服务端 `EventBus.publish` 给每条事件盖的章，
 * 毫秒精度的 UTC）。四窗的每行时间列就用它 —— 于是页面上的时间和证据文件
 * （run 的 `events.jsonl`、会话的 `io_events.jsonl`）里能查到的时刻是同一个。 */

/* ------------------------------------------- 流式行的**行号基准**（）
 *
 * 为什么要有这一段：实时那一屏的窗口内容是从 SSE **接**出来的，而阶段坐标是按
 * **证据文件的行号**记的（`start_line` / `anchor_line`，1 基）。刷新页面时服务端只重放
 * **有界的一段**（），于是"窗口第一行 = 文件第一行"就不再成立；订阅者跟不上时
 * 服务端还会**挤掉**事件（同样的道理，中间会少行）。那两种情况下左边的行号列与"定位"
 * 都会指着别的行 —— 而屏幕上看起来一切正常（那正是最坏的一种错）。
 *
 * 所以每一类证据行事件都带上 `line_no`（它在自己那份证据里的行号，1 基；
 * 与受控测试阶段坐标**同一份账**）。有了它就只剩两件事：**落基准**与**验连续性**：
 * 连续就对得上，对不上就是缺口 —— 后者说出来，而不是照旧往下数。 */

/* 一条带行号的流式行到了：这一窗的基准还信不信得过？（纯函数，宿主回归直接驱动它）
 *
 * `lineNo` = 这一行在证据文件里的行号（1 基）；`rowsBefore` = 收下它之前这一窗有几行；
 * `continuation` = 这条只是把最后一行**接完**（不新增行）。
 * 返回 `{ base, issue }`：
 *   · `base` 非 null ⇒ 用这个数当新的 `baseIndex`（这一窗从这一行开始）；
 *   · `issue` 非空 ⇒ 这一窗的行号**不可信**（含人话原因）；
 *   · 两者都没有 ⇒ 对得上，什么都不用改。
 * 没有行号（`lineNo` 不是正整数）时不改结论 —— 老服务/旧录音就是这一路。 */

/* 缺口那句话（纯函数）：说清"从哪里跳到了哪里"，不说"可能丢了几条"这种估算。 */

/* 把一条判定落到这一窗上：基准/可信度就位，缺口则记账（**只说一次原因**）。
 *
 * ⚠️ 升级到 `exact` 的条件是**刚刚钉上了一个由行号算出来的基准**（`frame.base` 非空）。
 *    "这一行没带行号"（老服务 / 旧录音那条路）与"续行落在末尾那一行"都**什么都没验证**：
 *    它们的不改结论 ≠ 核实过。真机上撞到过这条被架空 —— 老录音的窗被一路升级成 `exact`，
 *    于是"重放被截断时不拿这一窗去定位"这条护栏根本不触发（而且半行接完那条路也走不到了）。
 *    宁可不说错：没验证过的窗就停在"假定"上，截断时如实拒绝定位。 */

/* 这一窗现在能不能按**文件行号**定位（纯函数：给现场，回答"不能的话为什么"）。
 *
 * ① `broken`：流里出现过缺口 ⇒ 从缺口之后，这一窗的行号不再指向文件行；
 * ② `assumed` + 重放被截断：这一窗的行是从 SSE 接上来的，刷新时服务端只重放了**有界的一段**
 *    ⇒ "窗口第一行 = 文件第一行"这个前提已经不成立。
 * `exact` 的两条路（事件带行号 / 证据装载带 `X-Line-Base`）都不受这两条影响 ——
 * 自动验证与离线回放的窗口本来就是**按区间装载**的，行号基准随响应头回来。
 * ⚠️ 派生视图（工具输出）：上面两条都是拿**行号**说事，而它根本不用行号定位
 *    （它的行身份是事件号）⇒ 重放被截断不影响它按事件号定位（那条路在
 *    `locatePaneLines` 里另算）。但**按行号**给它的坐标会被如实拒掉 —— 宁可说清楚。
 * ⚠️ 四条规则与"宁可不定位、不瞎跳"是同一条（需求 §8）：宁可明说定位不了。 */

/* `defer`：只进模型，**先不落 DOM**（批量写的那条路用它 —— 一批的事件攒够了
 * 再一次性 renderPending，而不是每条事件都走一遍 DOM + 四个窗口的状态行）。
 * 返回值两种情况下都是"新增了几行"。
 * `lineNo`（可选）= 这一行在它那份证据里的**行号**（1 基，服务端给）：
 * 一窗的**第一行**用它落基准，后续每行用它验连续性（见 `streamLineFrame`）。
 * `seq`（可选）= 这一行来自的那条 io 事件的事件号：只有派生视图（工具输出）用得上，
 * 它那一行的身份就是这个事件号（）。
 * ⚠️ 参数名与事件字段都是 `event_seq`：`seq` 是**平台事件流自己**的序号（SSE 续传游标）。 */
function feed(
  store, id, text, ts,
  { defer = false, lineNo = null, continuation = false, event_seq = null } = {}
) {
  const source = store.sources[id];
  if (source && event_seq !== null && event_seq !== undefined) {
    // 这一窗的内容确实是画出来的（带着事件号到达）⇒ 按事件号定位就是它唯一可信的路。
    source.derived = true;
  }
  const rowsBefore = source ? source.lines.length : 0;
  const rows = toLines(text);
  const numbered = Number.isFinite(Number(lineNo)) && Number(lineNo) >= 1;
  /* ⚠️ **一行就是一行**：服务端给了行号（`lineNo`）= 这一行在证据文件里**真实存在** ⇒
   * 哪怕它洗完是空串（裸 CR / 空行 —— STM32 裸机复位时真打过一个 `\r`）也必须占一行。
   * 不然这一窗的行号与文件的行号从此差 1，连续性检查会把它记成“缺口” ⇒ 那一窗
   * **永久拒绍定位**（2026-10-13 真机撞到：raw 与 decoded 两窗同时被判 broken，
   * 阶段卡上四窗只动一个）。
   * 没给行号的流（老服务 / 旧录音）保持老行为：空行不占位。 */
  if (!rows.length && numbered) rows.push("");
  const added = appendMany(store, id, rows, ts, event_seq);
  if (added > 0 && source) {
    applyStreamFrame(source, streamLineFrame(source, lineNo, rowsBefore, continuation));
  }
  return defer ? added : settle(store, id, added);
}

/* SSE 每次送**累计**的事件数组，所以只追加没见过的那些：重连重发同一批时，
 * 重复出现的行会让人以为“事件真的发生了两次”。
 * `lineNo`（可选）= 这一批里**第一行**在证据文件里的行号（服务端给，见 `streamLineFrame`）。 */
function appendEvents(store, events, ts, { defer = false, lineNo = null } = {}) {
  const source = store.sources.events;
  const fresh = [];
  for (const event of events || []) {
    const key = `${event.seq ?? "?"}:${event.offset ?? "?"}:${event.kind ?? "?"}`;
    if (source.seen.has(key)) continue;
    source.seen.add(key);
    // 行自己带时刻就用它，否则用这一批到达的时刻（批量结构事件是同一秒到的）。
    fresh.push({ text: eventLine(event), ts: event.ts ?? ts });
  }
  const rowsBefore = source.lines.length;
  const added = appendMany(store, "events", fresh);
  // 一行都没新增（重连重发的那一批）时**不动**基准：没有新行就没有可验的边界。
  if (added > 0) applyStreamFrame(source, streamLineFrame(source, lineNo, rowsBefore, false));
  return defer ? added : settle(store, "events", added);
}

function resetSources(store) {
  for (const id of SOURCE_IDS) {
    setSourceLines(store, id, []);
    // L3：换来源时把区间窗口也清掉，并且**取消在飞的请求** ——
    // 否则上一份证据的字节会填进新的一次装载。
    cancelWindow(store, id);
    store.sources[id].window = null;
    store.sources[id].partialHead = false;
    rebuildSource(store, id);
  }
  updateLogIndicators(store);
}

/* 统计行文字：短版给窗口头（超出用省略号），长版放 title。
 * 纯函数：宿主回归直接拿它核对“哪些条件会出现在统计行里”。 */
function statParts(source) {
  const view = source.view;
  const total = source.lines.length;
  const short = [`${total} 行`];
  const long = [`${total} 行`];
  if (view.visible !== total) {
    short[0] = `${view.visible}/${total} 行`;
    long[0] = `${view.visible}/${total} 行`;
  }
  if (view.level !== "all") { short.push(`级别${view.level}`); long.push(`级别 ${view.level}`); }
  if (view.module !== "all") { short.push(`模${view.module}`); long.push(`模块 ${view.module}`); }
  if (view.errorsOnly) { short.push("错误"); long.push("只看错误"); }
  // 文本排除（需求 §7）：**隐藏了多少行必须看得见**，而且一键就能停用。
  if (view.exclude) {
    const what = `${view.excludeRegex ? "正则 " : ""}“${view.exclude}”`;
    const excludeError = compileExclude(view).error;
    if (excludeError) {
      short.push("排除失效");
      long.push(`排除未生效：${excludeError}`);
    } else if (!excludeActive(view)) {
      short.push("排除停用");
      long.push(`排除 ${what} 已停用（点「排除」按钮恢复）`);
    } else {
      const hidden = excludedCount(source);
      short.push(`隐藏${hidden}`);
      long.push(`已按 ${what} 隐藏 ${hidden} 行（下载仍是完整文件）`);
    }
  }
  if (view.dedupe) { short.push("折叠"); long.push("折叠重复"); }
  if (view.folded && view.query) {
    short.push(`搜“${view.query}”`);
    long.push(`搜索 “${view.query}”（筛选行已收起，条件仍然生效）`);
  } else if (view.folded && (view.level !== "all" || view.module !== "all" || view.errorsOnly)) {
    long.push("筛选行已收起（过滤条件仍然生效）");
  }
  if (view.error) { short.push("搜索失效"); long.push("搜索未生效（正则无效或查询过长）"); }
  if (source.pending) { short.push(`待${source.pending}`); long.push(`待显示 ${source.pending} 行`); }
  if (source.dropped) { short.push(`丢${source.dropped}`); long.push(`已丢最旧 ${source.dropped} 行（完整内容见下载）`); }
  // 行号基准不可信（）：这件事**不能只在点“定位”时才说** —— 左边那列数字
  // 与阶段坐标对不上，看一眼就知道；宁可先说清。
  if (source.baseTrust === "broken") {
    short.push("行号不可信");
    long.push(`${source.baseIssue || "这一窗的实时行有缺口"} ⇒ 这一窗的行号不再指向证据文件里`
      + `的行（阶段定位不在这一窗做；完整内容在证据文件里）`);
  }
  // 区间窗口（L3）：总大小 / 已加载字节 / 省略方向 —— 只说事实，不说"完整"。
  const note = source.window ? windowNote(source.window) : null;
  if (note) {
    short.push(note.whole ? note.short : `区间 ${note.short}`);
    long.push(note.long);
  }
  if (source.window && source.window.loading) { short.push("加载中…"); long.push("正在取这一段字节"); }
  return { short: short.join(" · "), long: long.join(" · ") };
}

/* ------------------------------------------------------------ 工作台控件 */
/* 每个窗口自己一套控件：未读药丸、暂停、跳尾、下载、统计。
 * 这里**不做任何跨窗口的读写** —— 一个窗口的状态变化不允许影响另外三个。 */
function updateLogIndicators(store) {
  // 不在眼前的那一份不许碰 DOM（它连 DOM 引用都未必拿得到 —— 一份 DOM、三份模型）。
  if (!isAttached(store)) return;
  // 收起一个窗口的筛选行 = 给**所有**窗口的日志区多留一截高度（它们等高，所以是一起变高）。
  const foldedCount = SOURCE_IDS.filter((id) => store.sources[id].view.folded).length;
  const gain = `${Math.round(foldedCount * app.foldStep)}px`;
  const root = document.documentElement;
  if (root.style.getPropertyValue("--pane-fold-gain") !== gain) {
    root.style.setProperty("--pane-fold-gain", gain);
  }
    // 工作区那一栏的“收起/展开筛选行”：四个都收起时才换成“展开”。
  const foldAllBtn = $("foldAll");
  if (foldAllBtn) {
    const allFolded = foldedCount === SOURCE_IDS.length;
    foldAllBtn.textContent = allFolded ? "展开筛选行" : "收起筛选行";
    foldAllBtn.setAttribute("aria-pressed", foldedCount ? "true" : "false");
    foldAllBtn.title = allFolded
      ? "把五个窗口的搜索/筛选行都展开（也可以逐窗点文件名旁的 ▸，或按 Alt+F）"
      : `把五个窗口的搜索/筛选行都收起：条件仍然生效，省下的高度分给五份日志（当前已收起 ${foldedCount} 个）。`
        + "也可以逐窗点文件名旁的 ▾，或按 Alt+F 只收当前窗口";
  }
  // 标签条：能点、能 Tab 到、按 Enter 展开；标题里说清楚单击/双击各做什么。
  // ⚠️ 用户口径（2026-10-17）：「分栏」按钮与它的菜单已删 —— 展开几个只靠双击工具条收/展、
  //    拖分隔条改宽度（拖到最窄收成标签）、双击标签全部展开。这里不再回写任何分栏徽标。
  for (const id of SOURCE_IDS) {
    const source = store.sources[id];
    if (!source || !source.pane) continue;
    const pane = source.pane;
    const rail = pane.dataset.collapsed === "1";
    pane.setAttribute("aria-expanded", rail ? "false" : "true");
    const titleEl = pane.querySelector(".pane-title");
    if (titleEl) {
      titleEl.title = `${SOURCE_BY_ID[id] ? SOURCE_BY_ID[id].hint : ""} · 双击这一行可把这一窗收成标签`;
    }
    if (rail) {
      pane.setAttribute("role", "button");
      pane.setAttribute("tabindex", "0");
      pane.setAttribute("aria-label", `${SOURCE_LABEL[id]}：已收成标签，按 Enter 展开`);
      pane.title = `${SOURCE_LABEL[id]} 已收成标签：单击展开，双击把五个窗口恢复成等宽`;
    } else {
      pane.removeAttribute("role");
      pane.removeAttribute("tabindex");
      pane.removeAttribute("aria-label");
      pane.title = "";
    }
  }
  document.querySelectorAll(".split-bar").forEach((bar) => {
    bar.setAttribute("aria-valuemin", String(Math.round(SPLIT_MIN * 100)));
    bar.setAttribute("aria-valuemax", String(Math.round(SPLIT_MAX * 100)));
  });
  for (const id of SOURCE_IDS) {
    const source = store.sources[id];
    // 这一窗是**卡片**（不是行流）：行级指示器（“N 行”/命中计数）由卡片自己数，
    // 这里只更新它与别的窗口共有的那几个（暂停/药丸/折叠/全屏）。
    const cardsPane = Boolean(SOURCE_BY_ID[id].cards);
    if (source.pane) source.pane.dataset.paused = source.paused ? "1" : "0";
    // “当前窗口”只是一层视觉标记（哪个窗口在看）：不影响任何行为，也不影响证据。
    if (source.pane) {
      const active = app.lastPane === id ? "1" : "0";
      if (source.pane.dataset.active !== active) source.pane.dataset.active = active;
      source.pane.setAttribute("aria-current", active === "1" ? "true" : "false");
    }

    if (source.pauseBtn) {
      source.pauseBtn.setAttribute("aria-pressed", source.paused ? "true" : "false");
      source.pauseBtn.textContent = source.paused ? "继续" : "暂停";
      source.pauseBtn.title = cardsPane
        ? (source.paused
            ? "这一窗已冻结：阶段事件照旧在收（一个都不丢）。点“继续”补上冻结期间到达的阶段"
            : "只冻结这一窗的画面：阶段事件照旧在收。冻结期间到达的阶段记在右下角的药丸上，恢复时补齐")
        : (source.paused
            ? "这个窗口已冻结：SSE、串口采集与证据落盘仍在继续。点击后补齐新内容并回到最新"
            : "只冻结这个窗口：采集与证据落盘继续。暂停期间到达的行一行都不会丢，恢复时按原顺序补齐");
    }

    // 【向前加载】（L3）：只有"窗口模式且前面还有没加载的字节"时才能点。
    // 已经到头就置灰并说清"已经到开头了" —— 而不是一个点了没反应的按钮。
    if (source.olderBtn) {
      const note = source.window ? windowNote(source.window) : null;
      const loading = Boolean(source.window && source.window.loading);
      source.olderBtn.hidden = !note;
      source.olderBtn.disabled = !note || loading || note.omittedBefore <= 0;
      source.olderBtn.textContent = loading ? "加载中…" : "向前加载";
      // 顶上那一行不完整时，在正文区上沿画一条虚线提示（行本身不伪造、不改）——
      // 光写进统计行还不够：用户一眼应该看得出"顶上这不是一整行"。
      if (source.el) source.el.dataset.partialHead = source.partialHead ? "1" : "0";
      source.olderBtn.title = note
        ? (note.omittedBefore > 0
            ? `${source.window.evidence} 共 ${byteText(source.window.totalBytes)}，`
              + `前面还有 ${byteText(note.omittedBefore)} 没加载；点一下取相邻的更早一段`
            : `${source.window.evidence} 已经加载到开头了`)
        : "";
    }

    // 【区间窗口的诚实标注】（L3/L4）：这一窗只装了**一段字节**时，它自己的搜索框
    // 只能搜这一段 —— 必须写明，否则"局部无命中"看起来就像"全文无命中"。
    if (source.searchInput && source.window) {
      source.searchInput.title =
        "这一窗只装了一段内容：这里的搜索只在已加载的这部分里找。"
        + "要搜整个文件，用工作区上方的「搜索整个文件」";
      if (source.searchInput.placeholder === "搜索…") source.searchInput.placeholder = "搜索已加载区间…";
    } else if (source.searchInput) {
      source.searchInput.title = "";
      if (source.searchInput.placeholder === "搜索已加载区间…") source.searchInput.placeholder = "搜索…";
    }

    if (source.pill) {
      if (source.pillCount) source.pillCount.textContent = String(source.pending);
      source.pill.hidden = source.pending === 0;
      source.pill.title = [
        `${SOURCE_BY_ID[id].label}：${source.pending} ${cardsPane ? "个阶段" : "行"}还没显示`,
        "点击补齐并跳到最新（只作用于这个窗口）",
      ].join("\n");
    }

    const view = source.view;
    if (source.el && !cardsPane) {
      // 行号/时间戳的前缀组合、折叠徽标、换行都靠容器属性切换（伪元素画出来，
      // 不进 textContent，所以复制内容里不会多出界面字符）。
      source.el.dataset.wrap = view.wrap ? "1" : "0";
      source.el.dataset.dedupe = view.dedupe ? "1" : "0";
      source.el.dataset.prefix = view.lineNumbers && view.timestamps ? "both"
        : view.lineNumbers ? "ln"
        : view.timestamps ? "time" : "none";
    }

    // 搜索/筛选行收起：省下的高度让日志区变高（四个窗口等高，所以是一起变高）。
    if (source.findRow) source.findRow.hidden = !!view.folded;
    if (source.foldBtn) {
      source.foldBtn.setAttribute("aria-expanded", view.folded ? "false" : "true");
      source.foldBtn.textContent = view.folded ? "▸" : "▾";
      source.foldBtn.title = view.folded
        ? "展开这一窗的搜索与筛选行（Alt+F；当前收起，搜索条件仍然生效）"
        : "收起这一窗的搜索与筛选行（Alt+F）：搜索条件仍然生效，省下的高度会分给日志区（各窗等高，所以是一起变高）";
    }
    // 单窗全屏：按钮文字就是“当前状态 + 怎么退出”。
    if (source.fullBtn) {
      const focused = app.layout.mode === "focus" && app.layout.focus === id;
      source.fullBtn.setAttribute("aria-pressed", focused ? "true" : "false");
      source.fullBtn.textContent = focused ? "退出全屏" : "全屏";
      source.fullBtn.title = focused
        ? "回到五个窗口（Esc 也可以）：其他窗口的阅读位置会被放回原处"
        : "只看这一个窗口（全屏）：Esc 退出，其他窗口的阅读位置会记住";
    }

    const compiled = compileQuery(view);
    const total = totalHits(source);
    if (source.searchInput) {
      // ⚠️ 用户正在这个框里打字时**不要**把模型的值回写进去：搜索是防抖 120 ms 的，
      //    而那 120 ms 里只要有一拍 `updateLogIndicators`（页面每 2 s 轮询一次、滚一下也会），
      //    就会把用户刚敲的字符抹回旧值，紧接着防抖回调读到空串 ⇒ 筛选永远不生效、
      //    输入框自己变空（真浏览器上撞到过：同一串关键词第一次被抹掉、第二次才行）。
      if (document.activeElement !== source.searchInput
        && source.searchInput.value !== view.query) {
        source.searchInput.value = view.query;
      }
      source.searchInput.dataset.error = compiled.error ? "1" : "";
    }
    if (source.regexBtn) source.regexBtn.setAttribute("aria-pressed", view.regex ? "true" : "false");
    if (source.errorsBtn) source.errorsBtn.setAttribute("aria-pressed", view.errorsOnly ? "true" : "false");
    // 文本排除（需求 §7）：按钮上的徽标就是**已隐藏行数**，按一下停用/恢复。
    const excludeError = view.exclude ? compileExclude(view).error : "";
    const excludeOn = excludeActive(view);
    const hidden = view.exclude && excludeOn ? excludedCount(source) : 0;
    if (source.excludeBtn) {
      source.excludeBtn.setAttribute("aria-pressed", excludeOn ? "true" : "false");
      source.excludeBtn.dataset.state = excludeError ? "error" : (view.exclude && !excludeOn ? "off" : "");
      source.excludeBtn.title = !view.exclude
        ? "文本排除：在「选项」里填要隐藏的行（也可以用 .* 当正则）。只改这一窗的显示：证据文件与下载始终是完整的"
        : excludeError
          ? `排除未生效：${excludeError}（点一下打开「选项」改条件）`
          : excludeOn
            ? `已隐藏 ${hidden} 行（条件：“${view.exclude}”）。点一下 = 停用（条件留着），再点一下 = 恢复`
            : `排除已停用（条件：“${view.exclude}”）。点一下 = 重新生效`;
    }
    if (source.excludeCount) {
      const label = !view.exclude ? "0" : (excludeError ? "!" : (excludeOn ? String(hidden) : "停用"));
      if (source.excludeCount.textContent !== label) source.excludeCount.textContent = label;
    }
    if (source.excludeInput) {
      if (source.excludeInput.value !== view.exclude) source.excludeInput.value = view.exclude;
      source.excludeInput.dataset.error = excludeError ? "1" : "";
    }
    if (source.excludeRegexBtn) {
      source.excludeRegexBtn.setAttribute("aria-pressed", view.excludeRegex ? "true" : "false");
    }
    if (source.excludeHint) {
      /* 用户口径（2026-10-11）：这一行屏幕上的本职是**说结果**（隐藏了多少 / 为什么没生效 /
       * 停用了）；“只影响这一窗的显示 / 复制只含可见行 / 下载始终是完整文件”属于
       * **教学语**（看一次就够）⇒ 退到 title，一个事实都没删。 */
      source.excludeHint.title =
        "排除条件只影响这一窗的显示：行还在证据文件里，复制本窗口只含可见行，下载始终是完整文件";
      source.excludeHint.textContent = !view.exclude
        ? "没有排除条件"
        : excludeError
          ? `不生效：${excludeError}`
          : excludeOn
            ? `已隐藏 ${hidden} 行`
            : `已停用（条件：“${view.exclude}”）`;
      source.excludeHint.dataset.kind = excludeError ? "fail" : (excludeOn ? "on" : "");
    }
    for (const [level, button] of source.levelButtons || []) {
      button.setAttribute("aria-pressed", view.level === level ? "true" : "false");
    }
    for (const [key, button] of Object.entries(source.toggles || {})) {
      button.setAttribute("aria-pressed", view[key] ? "true" : "false");
    }
    if (source.hitsLabel && !cardsPane) {
      // 原始串口开着终端视图时，这一格由终端自己写：终端把 `\r` 覆盖与折叠也算进正文，
      // 行视图那份计数与屏幕上看到的不是一回事（两边都写就成了“闪一下又变回去”）。
      const terminalOwns = Boolean(source.el && source.el.id === "lines-raw" && terminalOn());
      if (!terminalOwns) {
        // 计数栏宽度固定（5ch），否则“0/0”与“0/62”会把工具条折在不同的地方，
        // 四个窗口的日志区就不在同一条水平线上了。错误用 “!” 表示，详情写在 title 里。
        source.hitsLabel.textContent = compiled.error ? "!" : `${total ? view.hitCursor + 1 : 0}/${total}`;
        source.hitsLabel.dataset.state = compiled.error ? "error" : (view.query && !total ? "none" : "");
        source.hitsLabel.title = compiled.error
          ? `${compiled.error} —— 搜索暂不生效，视图仍是全部行`
          : view.query
            ? `命中 ${total} 处（在当前过滤条件下）`
            : "输入关键字后回车找下一个；.* 表示按正则匹配";
      }
    }
    if (source.menuBtn) source.menuBtn.setAttribute("aria-expanded", source.menuOpen ? "true" : "false");
    if (source.menu) source.menu.hidden = !source.menuOpen;

    if (source.stat && !cardsPane) {
      const parts = statParts(source);
      source.stat.textContent = parts.short;
      // 完整文字进 title：窗口头不换行，超出部分用省略号，但内容不丢。
      source.stat.title = parts.long;
    }
    // 卡片窗的两个数由卡片自己数（几个阶段 / 命中几个）。
    if (cardsPane) stageIndicators(source);

    if (source.notice) {
      source.notice.textContent = view.notice || "";
      source.notice.hidden = !view.notice;
    }

    if (source.link) {
      const meta = SOURCE_BY_ID[id];
      // 四窗的内容只有一个来源：正在看的 run / 正在回看的会话 / 实时观察（含刚结束的）。
      const session = app.viewing ? null : (app.review || evidenceSession());
      const target = session ? sessionEvidenceLink(meta.id, app.observe, session) : null;
      if (session) {
        if (target.file) {
          source.link.href = `/api/observe/sessions/${encodeURIComponent(session.session_id)}`
            + `/evidence/${target.file}?download=1`;
        } else {
          source.link.removeAttribute("href");
        }
        source.link.title = target.title;
      } else if (app.viewing) {
        source.link.href = `/api/runs/${encodeURIComponent(app.viewing)}/evidence/${meta.file}?download=1`;
        source.link.title = `下载本次运行的 ${meta.file}（原始证据：不受视图截断、暂停或去重影响）`;
      } else {
        source.link.removeAttribute("href");
        source.link.title = "先加载一个 run（或发起实时观察会话）才能下载证据";
      }
    }
  }

  // 诊断包入口（ 收口 / RUNTIME_MODES §9.4）：它只属于**离线诊断**那一屏，
  // 所以不再写工作台那一栏的全局按钮，改由 `renderBundleEntry()` 按离线屏的选中项写。
  renderBundleEntry();
}

/* ------------------------------------------------------- 搜索 / 过滤 / 复制 */
/* 模块候选项：把本窗口里真实出现过的模块名列出来（不猜、不预设）。 */
function moduleOptions(source) {
  const names = new Set();
  for (const line of source.lines) {
    const parsed = parseLine(line);
    if (parsed.module) names.add(parsed.module);
  }
  return Array.from(names).sort();
}

function refreshModuleOptions(source) {
  if (!source.moduleSelect) return;
  const options = moduleOptions(source);
  // 换了 run 或这个模块在新证据里不存在了，就回到“全部”：
  // 继续用一个不存在的条件把窗口清空，看起来和“日志丢了”一样。
  if (source.view.module !== "all" && !options.includes(source.view.module)) {
    source.view.module = "all";
  }
  source.moduleSelect.textContent = "";
  const add = (value, label, disabled) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    if (disabled) option.disabled = true;
    source.moduleSelect.appendChild(option);
  };
  add("all", "全部");
  for (const name of options) add(name, name);
  if (!options.length) add("", "（本窗口没有模块标记）", true);
  source.moduleSelect.value = source.view.module;
}

function hasSelectionIn(host) {
  if (!host) return false;
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount || selection.isCollapsed) return false;
  return host.contains(selection.anchorNode);
}

/* 「排除」按钮（搜索行上那一颗）：填过条件就是**一键停用/恢复**（条件留着，方便反复对照）；
 * 还没填过条件时先打开「选项」并把光标放进去 —— 一个没法填条件的按钮什么也做不了。 */
function toggleExclude(id) {
  const source = app.log.sources[id];
  if (!source) return;
  const view = source.view;
  if (!view.exclude) {
    closeMenus(app.log, id);
    source.menuOpen = true;
    updateLogIndicators(app.log);
    if (source.excludeInput) source.excludeInput.focus();
    paneNotice(source, "在「排除」里填要隐藏的文本（或用 .* 当正则）");
    return;
  }
  view.excludeOff = !view.excludeOff;
  refreshView(app.log, id);
  paneNotice(source, view.excludeOff
    ? "排除已停用：隐藏的行都回来了（条件留着，再点一下「排除」就重新生效）"
    : `排除已生效：窗口里隐藏了 ${excludedCount(source)} 行`);
}

/* 临时提示写在窗口左下角的浮动气泡里（几秒后自动消失），不弹窗、不挡日志。
 * 不写进统计行：统计行会把窗口头顶高，四个窗口的底边会跟着跳。 */
function paneNotice(source, text) {
  source.view.notice = text;
  updateLogIndicators(app.log);
  if (source.view.noticeTimer) clearTimeout(source.view.noticeTimer);
  source.view.noticeTimer = setTimeout(() => {
    source.view.notice = "";
    updateLogIndicators(app.log);
  }, 6000);
}

/* 条件变了：重建这个窗口的派生视图。重建会清掉选区（这是允许的），但要**说出来**。 */
function refreshView(store, id) {
  const source = store.sources[id];
  if (!source) return;
  const hadSelection = hasSelectionIn(source.el);
  source.view.error = compileQuery(source.view).error;
  renderView(store, id);
  if (hadSelection) paneNotice(source, "条件已变：视图已重建，选区已清除");
  else updateLogIndicators(store);
}

/* 把“当前命中”标出来并滚到视野中间（只在这个窗口内）。 */
function paintCurrentHit(source) {
  const view = source.view;
  if (!source.el) return;
  const staleLine = source.el.querySelector(".log-line[data-current='1']");
  if (staleLine) staleLine.removeAttribute("data-current");
  const staleMark = source.el.querySelector("mark.hit.current");
  if (staleMark) staleMark.classList.remove("current");
  const located = locateHit(view.hits, view.hitCursor);
  if (!located) return;
  const node = source.el.querySelector(`.log-line[data-src="${located.index}"]`);
  if (!node) return;                        // 命中的行已经被裁剪掉了
  node.dataset.current = "1";
  const marks = node.querySelectorAll("mark.hit");
  if (marks[located.slot]) marks[located.slot].classList.add("current");
  // 手算滚动位置，不用 scrollIntoView —— 后者会把整个页面也一起拖走。
  const target = node.offsetTop - source.el.clientHeight / 2 + node.offsetHeight / 2;
  source.el.scrollTop = Math.max(0, target);
  // 终端视图开着时它就是这一窗的正文：当前命中同样要滚过去、同样要标 current。
  if (source.el.id === "lines-raw" && terminalOn()) renderTerminalNow();
  source.followTail = false;                // 用户在看某一处命中，不是尾巴
}

/* 上一个 / 下一个命中（循环）。 */
function gotoHit(id, step) {
  const store = app.log;
  const source = store.sources[id];
  if (!source) return;
  const view = source.view;
  const total = totalHits(source);
  if (!total) {
    paneNotice(source, "这个窗口里没有命中");
    return;
  }
  view.hitCursor = ((view.hitCursor + step) % total + total) % total;
  paintCurrentHit(source);
  updateLogIndicators(store);
}

async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (error) { /* 权限被拒时落到下面的兜底 */ }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.left = "-9999px";
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (error) { ok = false; }
  area.remove();
  return ok;
}

/* 复制的是**看得见的那些行**：过滤后的可见行，不含行号/时间戳/×N 这些界面标记，
 * 也不含被过滤掉或折叠掉的行（它们确实没显示出来）。 */
async function copyView(id) {
  const source = app.log.sources[id];
  // 卡片窗：复制的是**当前筛出来的**那几张阶段卡的文字（与行流窗口同一个口径：
  // 可见的才复制，被收起来的不算）。
  if (SOURCE_BY_ID[id].cards) {
    const items = stageVisibleItems();
    if (!items.length) {
      paneNotice(source, "没有可复制的内容（当前筛选下没有阶段卡）");
      return;
    }
    const text = items
      .map((item) => (item.innerText || item.textContent || "").trim())
      .filter(Boolean)
      .join("\n\n");
    const ok = await copyText(text);
    paneNotice(source, ok ? `已复制 ${items.length} 个阶段（当前筛选）` : "复制失败：浏览器拒绝了剪贴板访问");
    return;
  }
  const lines = viewItems(source);
  if (!lines.length) {
    paneNotice(source, "没有可复制的内容（当前条件下没有可见行）");
    return;
  }
  const ok = await copyText(viewText(source));
  paneNotice(source, ok ? `已复制 ${lines.length} 行（当前视图）` : "复制失败：浏览器拒绝了剪贴板访问");
}

/* 全选本窗口：选的是 DOM 里真实存在的可见文本，隐藏/过滤的行本来就选不到。 */
function selectAllInPane(id) {
  const source = app.log.sources[id];
  if (!source || !source.el) return;
  const selection = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(source.el);
  selection.removeAllRanges();
  selection.addRange(range);
  paneNotice(source, SOURCE_BY_ID[id].cards
    ? `已选中本窗口 ${stageVisibleItems().length} 个阶段`
    : `已选中本窗口 ${source.view.visible} 行`);
}

function clearCriteria(id) {
  const source = app.log.sources[id];
  const view = source.view;
  // 卡片窗只有“筛选关键字”这一个条件（级别/模块/排除在它身上不存在）。
  if (SOURCE_BY_ID[id].cards) {
    view.query = "";
    view.hitCursor = 0;
    if (source.searchInput) source.searchInput.value = "";
    applyStageFilter(app.log);
    paneNotice(source, "筛选已清除：所有阶段卡都回来了");
    return;
  }
  view.query = "";
  view.regex = false;
  view.level = "all";
  view.module = "all";
  view.errorsOnly = false;
  view.exclude = "";
  view.excludeRegex = false;
  view.excludeOff = false;
  view.expanded.clear();
  if (source.moduleSelect) source.moduleSelect.value = "all";
  refreshView(app.log, id);
}

/* Alt+点某一行：只展开/收起那一组折叠（组首行号就写在节点的 data-src 上）。 */
function altClickExpand(store, id, event) {
  const source = store.sources[id];
  const view = source.view;
  if (!view.dedupe) return;
  const target = event.target instanceof Element ? event.target.closest(".log-line") : null;
  if (!target) return;
  const index = Number(target.dataset.src);
  if (!Number.isFinite(index)) return;
  if (view.expanded.has(index)) view.expanded.delete(index);
  else view.expanded.add(index);
  refreshView(store, id);
}

function focusPaneSearch(store, id) {
  const source = store.sources[id];
  if (!source || !source.searchInput) return false;
  if (source.view.folded) setFindFolded(store, id, false);   // 藏起来的搜索框没法打字
  source.searchInput.focus();
  source.searchInput.select();
  setLastPane(id);
  return true;
}

/* 收起/展开这一窗的搜索与筛选行。只改显示：**不重建视图**、不动滚动位置、不动选区，
 * 所以日志内容不会因为折叠而跳一下。收起的只是控件，搜索条件照旧生效。 */
function setFindFolded(store, id, folded, { persist = true } = {}) {
  const source = store.sources[id];
  if (!source) return;
  source.view.folded = !!folded;
  if (source.view.folded) source.menuOpen = false;   // 选项面板在那一行里，不能留个孤儿开着
  if (persist) {
    app.prefs.folded = { ...app.prefs.folded, [id]: source.view.folded };
    // 用户自己按过 ⇒ 从此“默认收起”那条不再插手（同 `layoutTouched` 的形状）。
    app.prefs.foldedSet = true;
    savePrefs();
  }
  updateLogIndicators(store);
}

function closeMenus(store, exceptId) {
  for (const id of SOURCE_IDS) {
    if (id === exceptId) continue;
    store.sources[id].menuOpen = false;
  }
}

/* “当前窗口”：记住最近碰过的那个（Alt+L / Ctrl+F 也用它），并刷新高亮。 */
function setLastPane(id) {
  if (app.lastPane === id) return;
  app.lastPane = id;
  if (app.log) updateLogIndicators(app.log);
}

const LEVEL_CHOICES = [
  ["all", "全部", "不做级别过滤"],
  ["E", "E", "只看 [E]（错误）"],
  ["W", "W", "只看 [W]（警告）"],
  ["I", "I", "只看 [I]（信息）"],
  ["D", "D", "只看 [D]（调试）"],
];

const VIEW_TOGGLES = [
  ["dedupe", "折叠重复", "连续重复的行折叠成一行并标 ×N；只影响显示，按住 Alt 点某行可展开那一组"],
  ["lineNumbers", "行号", "显示源行号（过滤后不重新编号，永远是证据里的行号）"],
  ["timestamps", "时间戳", "每行前面加一列时间（北京时间，毫秒）。证据里没有主机时刻的行不显示时间，"
    + "写成 `+12.345s` 那种样子的是设备自报的 uptime，两者一眼能分开"],
  ["wrap", "自动换行", "长行折行显示；关掉则改为横向滚动"],
];

function menuLabel(text) {
  const label = document.createElement("span");
  label.className = "menu-label";
  label.textContent = text;
  return label;
}

function menuButton(label, hint, ariaLabel) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tool-ico";
  button.textContent = label;
  button.title = hint;
  button.setAttribute("aria-label", ariaLabel || hint);
  return button;
}

/* 阅读选项面板：低频开关不占常驻位置，但每一项都有名字、提示与选中态。 */
function buildMenu(store, meta) {
  const source = store.sources[meta.id];
  const panel = source.menu;
  /* 面板里那些按钮也要按**当前那一份表**写状态（理由同 `buildWorkspace()` 里那段注释）。 */
  const src = () => store.sources[meta.id];
  if (!panel) return;
  panel.textContent = "";

  /* 【卡片窗】（测试阶段）：行级开关（级别 / 模块 / 行号 / 时间戳 / 折叠重复 / 换行 /
   * 排除 / 只看错误）在这一窗没有意义 —— 它没有“行”。这里只给它真有的那几件：
   * 复制这一窗、全选、清除筛选。摆一排点了没反应的开关比没有这些开关更糟。 */
  if (meta.cards) {
    source.levelButtons = [];
    source.toggles = {};
    const hintRow = document.createElement("div");
    hintRow.className = "menu-row menu-hint";
    hintRow.textContent = "这一窗是阶段卡，没有行：级别、模块、行号、时间戳、折叠重复、换行、排除"
      + "这些行级开关在这里用不上。";
    panel.appendChild(hintRow);
    const actionRow = document.createElement("div");
    actionRow.className = "menu-row";
    for (const [action, label, tip] of [
      ["copy", "复制本窗口", "复制当前筛出来的阶段卡文字（卡片上的按钮字不会进去）"],
      ["selectall", "全选本窗口", "选中这一窗里看得见的文字，然后可以 Ctrl+C"],
      ["clear", "清除筛选", "清空筛选关键字：所有阶段卡都回来"],
    ]) {
      const button = menuButton(label, tip, `${meta.label}：${tip}`);
      button.id = `${action}-${meta.id}`;
      button.addEventListener("click", () => {
        if (action === "copy") void copyView(meta.id);
        else if (action === "selectall") selectAllInPane(meta.id);
        else clearCriteria(meta.id);
      });
      actionRow.appendChild(button);
    }
    panel.appendChild(actionRow);
    return;
  }

  const levelRow = document.createElement("div");
  levelRow.className = "menu-row";
  levelRow.appendChild(menuLabel("级别"));
  source.levelButtons = [];
  for (const [value, label, hint] of LEVEL_CHOICES) {
    const button = menuButton(label, hint, `${meta.label}：${hint}`);
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => { src().view.level = value; refreshView(store, meta.id); });
    levelRow.appendChild(button);
    source.levelButtons.push([value, button]);
  }
  panel.appendChild(levelRow);

  const moduleRow = document.createElement("div");
  moduleRow.className = "menu-row";
  moduleRow.appendChild(menuLabel("模块"));
  const select = document.createElement("select");
  select.id = `module-${meta.id}`;
  select.setAttribute("aria-label", `${meta.label}：只看某个模块`);
  select.addEventListener("change", () => { src().view.module = select.value; refreshView(store, meta.id); });
  source.moduleSelect = select;
  moduleRow.appendChild(select);
  panel.appendChild(moduleRow);

  /* 文本排除（需求 §7）：只改变这一窗的派生视图。条件放在面板里（低频），
   * 而“隐藏了多少行 + 一键停用”放在搜索行那颗按钮上（高频、随时要看）。 */
  const excludeRow = document.createElement("div");
  excludeRow.className = "menu-row";
  excludeRow.appendChild(menuLabel("排除"));
  const excludeInput = document.createElement("input");
  excludeInput.type = "search";
  excludeInput.id = `excludeInput-${meta.id}`;
  excludeInput.className = "find-input excl-input";
  excludeInput.placeholder = "排除匹配的行…";
  excludeInput.autocomplete = "off";
  excludeInput.spellcheck = false;
  excludeInput.setAttribute("aria-label", `${meta.label}：排除匹配的行（只影响显示）`);
  let excludeTimer = 0;
  const applyExclude = () => {
    const raw = excludeInput.value.trim();
    if (raw === src().view.exclude) return;
    src().view.exclude = raw;
    // 填了新条件就是“要用它”：停用状态只在用户明确点过按钮之后才保留。
    src().view.excludeOff = false;
    refreshView(store, meta.id);
  };
  excludeInput.addEventListener("input", () => {
    clearTimeout(excludeTimer);
    excludeTimer = setTimeout(applyExclude, 120);   // 边打边排，但不是每键重建一次
  });
  excludeInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      clearTimeout(excludeTimer);
      applyExclude();
    } else if (event.key === "Escape") {
      excludeInput.value = "";
      clearTimeout(excludeTimer);
      applyExclude();
    }
  });
  source.excludeInput = excludeInput;
  excludeRow.appendChild(excludeInput);
  const excludeRegex = menuButton(
    ".*",
    "把排除条件当正则表达式（无效正则立刻提示，并且**不生效**）",
    `${meta.label}：排除条件按正则匹配`,
  );
  excludeRegex.id = `excludeRegex-${meta.id}`;
  excludeRegex.addEventListener("click", () => {
    src().view.excludeRegex = !src().view.excludeRegex;
    refreshView(store, meta.id);
  });
  source.excludeRegexBtn = excludeRegex;
  excludeRow.appendChild(excludeRegex);
  const excludeHint = document.createElement("span");
  excludeHint.className = "menu-hint";
  excludeHint.id = `excludeHint-${meta.id}`;
  source.excludeHint = excludeHint;
  excludeRow.appendChild(excludeHint);
  panel.appendChild(excludeRow);

  const toggleRow = document.createElement("div");
  toggleRow.className = "menu-row";
  source.toggles = {};
  for (const [key, label, hint] of VIEW_TOGGLES) {
    const button = menuButton(label, hint, `${meta.label}：${hint}`);
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => { src().view[key] = !src().view[key]; refreshView(store, meta.id); });
    toggleRow.appendChild(button);
    source.toggles[key] = button;
  }
  panel.appendChild(toggleRow);

  const actionRow = document.createElement("div");
  actionRow.className = "menu-row";
  for (const [action, label, hint, handler] of [
    ["copy", "复制本窗口", "复制当前视图：过滤后的可见行，不含行号/时间戳/×N 等界面标记", () => copyView(meta.id)],
    ["selectall", "全选本窗口", "选中当前窗口里可见的文本，然后可以 Ctrl+C", () => selectAllInPane(meta.id)],
    ["clear", "清除条件", "清空搜索、取消级别/模块/只看错误过滤，并去掉文本排除", () => clearCriteria(meta.id)],
  ]) {
    const button = menuButton(label, hint, `${meta.label}：${hint}`);
    button.id = `${action}-${meta.id}`;
    button.addEventListener("click", handler);
    actionRow.appendChild(button);
  }
  panel.appendChild(actionRow);
}

/* ---------------------------------------------------------------- 布局形态 */
/* 工作区有三种形态，共用**同一个 source model**（只是换 CSS 与显示/隐藏）：
 *   grid  五个窗口（默认，宽屏一行 / 2×2 + 1 / 单列；每窗宽度可拖、可折叠成标签）
 *   focus 单窗全屏（临时阅读形态，刷新后回到 grid）
 *   split 只展开选中的 1～4 个窗口，其余收成标签（临时阅读形态）
 * 隐藏某个窗口会丢掉它的滚动位置，所以切换前先记下来、回来时放回原处；
 * 折叠成标签只是把它变窄（内容仍在模型里），所以不会丢任何东西。 */

/* 分栏状态规范化：栏数 1～4、来源去重并补齐（脏数据也不能把工作区弄空）。 */
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

/* 拖动分隔条的第一步：先看指针到底动没动。
 * ⚠️ 分两件事很重要：
 *   1) **单击不许移动窗口** —— 指针位置本身就在条上（≈ 左窗口右边缘），没有阈值的话
 *      单击一下就会把分栏挪半个条的宽度；
 *   2) 不动就不算拖动 → 双击的第一下不会先挪一下（不然第二下可能已经落到别的元素上）；
 *      双击才能靠得住地触发。
 * 纯函数：宿主回归直接喂几个位移。 */
function paneDragPlan({ startX, clientX, startLeftWidth, slop = DRAG_SLOP }) {
  const dx = Number(clientX) - Number(startX);
  return { moved: Number.isFinite(dx) && Math.abs(dx) >= slop, leftPx: Number(startLeftWidth) + dx };
}

/* 把指定窗口的宽度权重恢复成 1（= 均分剩余横向空间），其余窗口一点不动。
 * 双击分隔条用这个：**只均分当前展开的窗口**，收起来的标签不参与。 */
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

/* 一行布局：每个窗口是“展开”还是“收成标签”。纯函数，宿主回归直接测。
 * rowMode = 宽到自己能排下四列（窄屏回到 2×2 / 单列，那时折叠与拖宽都不生效）。 */
function paneView(layout, rowMode) {
  const shown = {};
  const collapsed = {};
  for (const id of SOURCE_IDS) { shown[id] = true; collapsed[id] = false; }
  if (!layout) return { shown, collapsed, expanded: SOURCE_IDS.slice(), rails: [] };
  if (layout.mode === "focus" && SOURCE_IDS.includes(layout.focus)) {
    for (const id of SOURCE_IDS) shown[id] = id === layout.focus;
    return { shown, collapsed, expanded: [layout.focus], rails: [] };
  }
  if (layout.mode === "split") {
    const chosen = normalizeSplit(layout.split).ids;
    for (const id of SOURCE_IDS) {
      if (rowMode) collapsed[id] = !chosen.includes(id);   // 一行：没选中的收成标签（还能点）
      else shown[id] = chosen.includes(id);                // 窄屏：没选中的没法竖起来，直接不显示
    }
    return { shown, collapsed, expanded: chosen, rails: SOURCE_IDS.filter((id) => !chosen.includes(id)) };
  }
  // grid：四个都在，折叠是用户自己按的（但至少留一个展开）
  if (rowMode) {
    const map = normalizeCollapsed(layout.collapsed);
    for (const id of SOURCE_IDS) collapsed[id] = map[id];
    if (SOURCE_IDS.every((id) => collapsed[id])) collapsed[SOURCE_IDS[0]] = false;
  }
  return {
    shown,
    collapsed,
    expanded: SOURCE_IDS.filter((id) => !collapsed[id]),
    rails: SOURCE_IDS.filter((id) => collapsed[id]),
  };
}

/* 点一个标签（把它展开）：
 * - grid：直接展开（其他窗口不动）；
 * - split：还有空位就补上；否则替换“来源顺序上最近”的那一栏（栏数不变）。 */
function nextExpandedForRail({ mode, ids, clicked, n }) {
  const current = SOURCE_IDS.filter((id) => (ids || []).includes(id));
  if (!SOURCE_IDS.includes(clicked)) return current;
  if (mode !== "split") {
    return SOURCE_IDS.filter((id) => current.includes(id) || id === clicked);
  }
  const out = current.slice();
  const want = Math.max(SPLIT_MIN_N, Math.min(SPLIT_MAX_N, Math.round(Number(n) || out.length || 2)));
  if (out.includes(clicked)) return out;
  if (out.length < want) out.push(clicked);
  else {
    const target = SOURCE_IDS.indexOf(clicked);
    let victim = 0;
    let best = Infinity;
    out.forEach((id, index) => {
      const distance = Math.abs(SOURCE_IDS.indexOf(id) - target);
      if (distance < best) { best = distance; victim = index; }
    });
    out[victim] = clicked;
  }
  return SOURCE_IDS.filter((id) => out.includes(id));
}

/* 拖某一根分隔条：在**相邻两个窗口**之间重新分配宽度（别的窗口一点不动）。
 * 某一侧窄过 --pane-min-w（PANE_MIN_W）时不再挤它，而是判定“收成标签”。
 * 纯函数：宿主回归直接喂指针位置。 */
function dragPaneWidths({ widths, leftId, rightId, leftPx, pairPx, minPx = PANE_MIN_W }) {
  const out = normalizeWidths(widths);
  if (!SOURCE_IDS.includes(leftId) || !SOURCE_IDS.includes(rightId) || leftId === rightId) {
    return { widths: out, collapse: null };
  }
  if (!Number.isFinite(leftPx) || !(pairPx > 0)) return { widths: out, collapse: null };
  if (leftPx < minPx) return { widths: out, collapse: leftId };
  if (pairPx - leftPx < minPx) return { widths: out, collapse: rightId };
  const total = out[leftId] + out[rightId];
  const first = Math.max(total * 0.08, Math.min(total * 0.92, (leftPx / pairPx) * total));
  out[leftId] = first;
  out[rightId] = total - first;
  return { widths: out, collapse: null };
}

function visiblePanes(layout) {
  if (!layout) return SOURCE_IDS.slice();
  if (layout.mode === "focus" && SOURCE_IDS.includes(layout.focus)) return [layout.focus];
  if (layout.mode === "split") return normalizeSplit(layout.split).ids;
  // 兜底：任何说不清的形态都回到四个窗口 —— 绝不能让工作区变成空的。
  return SOURCE_IDS.slice();
}

function nextFocus(current, id) {
  return current === id ? "" : id;
}

/* 这个窗口里当前的选区（不在窗口里/没有选区就返回 null）。 */
function rangeIn(host) {
  const selection = window.getSelection();
  if (!host || !selection || !selection.rangeCount || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  if (!host.contains(range.startContainer) || !host.contains(range.endContainer)) return null;
  return range.cloneRange();
}

/* 把记住的选区放回去 —— 只在用户当前没有别的选区时（不然会抢走人家刚选的）。 */
function restoreRange(source) {
  const range = source.viewRange;
  source.viewRange = undefined;
  if (!range || !range.startContainer || !range.startContainer.isConnected) return;
  const selection = window.getSelection();
  if (!selection || (selection.rangeCount && !selection.isCollapsed)) return;
  try {
    selection.removeAllRanges();
    selection.addRange(range);
  } catch (error) {
    // 节点可能已经被缓冲裁剪掉了：那就当没记过，不要报错也不要乱选。
  }
}

/* 工作台那一栏是吸顶的，窗口头要排到它下面 —— 高度写进 CSS 变量（--ws-head-h）。
 * 它会随窗口宽度换行而变化，所以量一次不够：有 ResizeObserver 就一直跟着。 */
const WS_HEAD_MIN_SCROLL = 240;

/* 吸顶控件高度的**限幅**：工作台那栏不可能占掉大半屏。
 * ⚠️ 实测撞到过一次 **460px**（视口还没成形时量到的），而它一旦写进 CSS 变量就**不会自己变回来**：
 *    窗口头于是吸在屏幕中间、`阅读位` 把正文顶到 561px（上面空一大片）——
 *    用户报的“阅读位异常”就是这一路。所以两层保护：① 量出来的值先夹住；② 滚动时也重量一次
 *    （这个值的唯一用途就是滚动时的吸顶），错的值自己会好。 */

/* 视口还没成形时（典型：在**后台标签页**里打开页面，`innerWidth`/`innerHeight` 是 0）
 * 量出来的几何值全是垃圾：那时窗口头会被折成 400 多 px（真值 101）。
 * ⚠️ 这种值一旦写进 CSS 变量就没人再重算 —— 用户看到的是一排矮得只剩 240 px 的窗口。
 * 所以：布局不像话时**不写**，等它成形（尺寸变化/滚动）再重量。 */

/* 方便调用处：读的是当下的视口。 */
function viewportIsLaidOut() {
  return layoutIsSane(window.innerWidth, window.innerHeight);
}

/* 视口还没成形时，三个几何变量的量测全都作不得数（见 `layoutIsSane`）—— 但也不能就这么算了：
 * 安排几次重试，等布局真的成形再量（真的成了就停）。
 * ⚠️ 不能在 `app` 字面量里加字段（同名键会静默胜出），所以用模块级变量。 */
const GEOMETRY_RETRY_MS = 150;
const GEOMETRY_RETRY_MAX = 40;          // 约 6 秒：标签页一被显示出来，布局就成形了
let geometryRetryTimer = 0;
let geometryRetryTries = 0;

function scheduleGeometryRetry() {
  if (geometryRetryTimer) return;
  geometryRetryTries += 1;
  if (geometryRetryTries > GEOMETRY_RETRY_MAX) return;   // 放弃：CSS 里的兜底值还在，不会更糟
  geometryRetryTimer = setTimeout(() => {
    geometryRetryTimer = 0;
    if (syncGeometryVariables()) geometryRetryTries = 0;
  }, GEOMETRY_RETRY_MS);
}

/* 四个“吸顶 / 占掉视口高度”的量测只有这一个驱动处：
 * 工作台那一栏的高度（--ws-head-h）、窗口头的高度（--pane-head-h）、
 * 夹在它们与四窗之间那几行的高度（--pane-chrome-gap）、解码窗底栏的高度（--pane-foot-h）——
 * 它们共同决定正文能有多高。 */
function syncGeometryVariables() {
  if (!viewportIsLaidOut()) {
    scheduleGeometryRetry();
    return false;
  }
  syncWorkspaceHeadHeight();
  syncPaneHeadHeight();
  syncPaneViewportBudget();
  syncPaneFootHeight();
  return true;
}

function syncWorkspaceHeadHeight() {
  const head = document.querySelector("#logWorkspace .ws-head");
  if (!head || !viewportIsLaidOut()) return 0;
  const height = sanitizeHeadHeight(head.getBoundingClientRect().height, window.innerHeight);
  const root = document.documentElement;
  const current = parseFloat(root.style.getPropertyValue("--ws-head-h"));
  if (!Number.isFinite(current) || Math.abs(current - height) >= 1) {
    root.style.setProperty("--ws-head-h", `${height}px`);
  }
  return height;
}

/* 窗口头的高度也要量（--pane-head-h）：它是“吸顶控件之下看得见多少正文”的一部分。
 * CSS 里写的 100px 是保守估计（宁可少显示一行，也不能让正文被吸顶的窗口头盖住），
 * 实测值通常更矮 —— 用实测值能把多出来的高度还给日志区。
 * 只量**没被收成标签**的窗口：标签是竖排的，高度和横向窗口头不是一回事。 */
function syncPaneHeadHeight() {
  const root = document.documentElement;
  if (!viewportIsLaidOut()) return 0;      // 视口还没成形：这个数会是 400 多 px 的垃圾（见 layoutIsSane）
  let height = 0;
  for (const id of SOURCE_IDS) {
    const pane = $(`pane-${id}`);
    if (!pane || pane.dataset.shown === "0" || pane.dataset.collapsed === "1") continue;
    const head = pane.querySelector(".pane-head");
    if (!head) continue;
    const h = Math.round(head.getBoundingClientRect().height);
    if (h > height) height = h;
  }
  if (!height) return 0;     // 四个都收成标签了：保持原值，别瞎猜
  const current = parseFloat(root.style.getPropertyValue("--pane-head-h"));
  if (!Number.isFinite(current) || Math.abs(current - height) >= 1) {
    root.style.setProperty("--pane-head-h", `${height}px`);
  }
  return height;
}

/* “回到页面顶部”：滚过一屏才出现（浮在右下角，不挡日志）。 */
function updateToTop() {
  const button = $("toTop");
  if (!button) return;
  button.hidden = window.scrollY <= WS_HEAD_MIN_SCROLL;
}

function scrollToPageTop() {
  window.scrollTo({ top: 0, behavior: "smooth" });
  // 与“阅读位”同样的保险：有些环境会忽略 smooth，那就直接定位。
  setTimeout(() => {
    if (window.scrollY > 1) window.scrollTo(0, 0);
  }, 180);
}

function wireFloatingControls(store) {
  const button = $("toTop");
  if (button) button.addEventListener("click", () => scrollToPageTop());
  let scheduled = false;
  window.addEventListener("scroll", () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      // 吸顶高度的唯一用途就是滚动时的吸顶：滚动时重量一次，陈旧/离谱的值自己会好
      // （PW-UX-002：460px 那次就是它一直没人重算）。
      // ⚠️ 窗口头与“中间那几行”也一起重量：它们在**视口成形前**量到的是垃圾值
      //    （后台标签页打开 ⇒ innerWidth=0 ⇒ 窗口头 428px），而它们决定正文能有多高。
      syncGeometryVariables();
      updateToTop();
    });
  }, { passive: true });
  window.addEventListener("resize", () => {
    // 宽度变了，搜索栏那一行可能换行 ⇒ “夹在中间那几行”的高度也得重量（它决定四窗多高）。
    // 阅读位写下的那个高度是**当时那一屏**的量测结果 ⇒ 视口一变就作废（回到预算公式）。
    clearReadingHeight();
    syncGeometryVariables();
  });
  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(() => {
      // 搜索栏/结果列表（与“页面是旧的”提示条）自己变高变矮也在这里跟上：
      // 它们的高度直接决定四窗的正文上限（见 `--pane-chrome-gap`）。
      // ⚠️ 解码窗底栏（常驻说明 + 命令输入行）也归这里管：它一变高，正文就得把那几十像素
      //    还回来，否则整个网格行被撑高、四窗底边掉到屏幕外面（`--pane-foot-h` 的保鲜处）。
      syncGeometryVariables();
      syncPaneFootHeight();
    });
    const head = document.querySelector("#logWorkspace .ws-head");
    if (head) observer.observe(head);
    // 窗口头也会因为字号、折叠、标题换行而变高变矮：同样是量一次不够。
    for (const id of SOURCE_IDS) {
      const paneHead = document.querySelector(`#pane-${id} .pane-head`);
      if (paneHead) observer.observe(paneHead);
    }
    // “夹在工作台栏与四窗之间”的那几行同理：搜索栏会换行、结果列表会忽长忽短，
    // 它们直接决定四窗的正文上限（`--pane-chrome-gap`）。
    for (const id of PANE_GAP_NODES) {
      const gapNode = $(id);
      if (gapNode) observer.observe(gapNode);
    }
    // 解码窗的底栏（常驻说明 + 命令输入行）同属这一类：说明行是按状态出现的、命令行还能被拖高，
    // 量一次就不管 ⇒ 正文多吃几十像素 ⇒ 四窗底边掉到屏幕外面（`--pane-foot-h` 的唯一保鲜处）。
    const foot = $("paneFootDecoded");
    if (foot) observer.observe(foot);
  }
  syncGeometryVariables();
  // 全文搜索的结果列表与历史运行那一窗，用**同一套**滚轮边界处理（PW-UX-001 第 1/3 条）。
  edgeWheelTarget($("fullSearchResults"));
  edgeWheelTarget($("runsScroll"));
  updateToTop();
}

function applyLayout(store) {
  const grid = $("logGrid");
  if (!grid) return;
  const layout = app.layout;
  const view = paneView(layout, isRowLayout());
  // 收/展窗格、全屏都改变“当前窗格是谁、它顶边在哪” ⇒ 阅读位那次的高度作废。
  // ⚠️ 但**阅读位还开着**时不要在这里清：`alignReadingPosition()` 本来就会先清再重算，
  //    而它是在下一帧跑的 —— 中间这一帧把高度清掉，屏幕上就是“先缩回去、再伸回来”的抽动
  //    （实测：轮询每轮都会走一次 applyLayout，那一帧能看见底边从 716 掉到 700）。
  if (!(app && app.readingEngaged)) clearReadingHeight();

  /* ⚠️ 顺序很重要：**先把所有滚动位置读出来**，再改布局。
   * 改 `data-mode` / `data-shown` 会让浏览器重排，隐藏的窗口 scrollTop 会被归零
   * （先改后读就会把“读到一半的位置”记成 0 或某个被锚定过的值 —— 踩过：4000 → 1568）。 */
  const willHide = [];
  const willShow = [];
  for (const id of SOURCE_IDS) {
    const source = store.sources[id];
    if (!source.pane) continue;
    const shown = view.shown[id];
    const wasShown = source.pane.dataset.shown !== "0";
    if (!shown && source.el && wasShown) willHide.push(id);
    if (shown && !wasShown) willShow.push(id);
  }
  for (const id of willHide) {
    const source = store.sources[id];
    source.viewScroll = source.el.scrollTop;      // 读，不写
    source.viewFollowTail = source.followTail;
    source.viewRange = rangeIn(source.el);        // 隐藏也会清掉选区：一并记住
  }
  grid.dataset.mode = layout.mode;
  applyRowLayout(store);                          // 显示/折叠/宽度/分隔条都跟着状态走
  for (const id of willShow) {
    const source = store.sources[id];
    if (typeof source.viewScroll === "number") source.el.scrollTop = source.viewScroll;
    source.viewScroll = undefined;
    setFollowState(store, id,
      source.el.scrollTop + source.el.clientHeight >= source.el.scrollHeight - 8);
    restoreRange(source);
  }
  /* ⚠️ 用户实测（2026-10-17）：点了阅读位、窗口都对上了；接着把一个窗口收成标签，
   * 工作区立刻矮回去（旧高度按新布局作废是对的），但**那之后就没人再对了** ——
   * 看上去就是“一折叠，阅读位就失效”。所以布局一变就按新布局重新对一次。 */
  reapplyReadingPosition();
}

function setFocus(store, id) {
  app.layout.focus = nextFocus(app.layout.focus, id);
  app.layout.mode = app.layout.focus ? "focus" : "grid";
  applyLayout(store);
  updateLogIndicators(store);
}

/* 分栏（Slice C）：模式与栏数记在 app.layout.split；宽度/折叠是**阅读偏好**，落盘。 */
function setSplit(store, patch) {
  markLayoutTouched();                   // 改分栏 = 用户在调整套布局
  Object.assign(app.layout.split, patch || {});
  app.layout.split = normalizeSplit(app.layout.split);
  app.layout.mode = app.layout.split.on ? "split" : "grid";
  applyLayout(store);
  updateLogIndicators(store);
}

/* 一行的排布：展开的窗口按 flex-grow 分宽度（顺序 = 来源顺序），
 * 收成标签的排到右端（order=100+i）并固定 --rail-w；分隔条只夹在两个展开窗口之间。
 * 窄屏（≤1044 px）回到 2×2 / 单列，这时把 order / flex-grow 清掉，交给 CSS。 */
function applyRowLayout(store) {
  const grid = $("logGrid");
  if (!grid) return;
  const rowMode = isRowLayout();
  const view = paneView(app.layout, rowMode);
  const widths = normalizeWidths(app.layout.widths);
  app.layout.widths = widths;
  app.layout.collapsed = normalizeCollapsed(app.layout.collapsed);
  for (const id of SOURCE_IDS) {
    const source = store.sources[id];
    if (!source || !source.pane) continue;
    const pane = source.pane;
    const shown = view.shown[id];
    const rail = shown && view.collapsed[id];
    pane.dataset.shown = shown ? "1" : "0";
    pane.dataset.collapsed = rail ? "1" : "0";
    if (rowMode && shown) {
      if (rail) {
        pane.style.order = String(100 + SOURCE_IDS.indexOf(id));
        pane.style.flexGrow = "0";
      } else {
        pane.style.order = String(2 * Math.max(0, view.expanded.indexOf(id)));
        pane.style.flexGrow = String(widths[id]);
      }
    } else {
      pane.style.order = "";
      pane.style.flexGrow = "";
    }
  }
  const bars = Array.from(document.querySelectorAll(".split-bar"));
  for (const bar of bars) {
    const index = Number(bar.dataset.pair) || 0;
    const usable = rowMode && app.layout.mode !== "focus" && view.expanded.length > index + 1;
    bar.hidden = !usable;
    bar.style.order = String(2 * index + 1);
    if (usable) {
      const left = view.expanded[index];
      const right = view.expanded[index + 1];
      const total = widths[left] + widths[right] || 1;
      bar.setAttribute("aria-valuenow", String(Math.round((widths[left] / total) * 100)));
      bar.setAttribute("aria-valuetext", `${SOURCE_LABEL[left]} ${Math.round((widths[left] / total) * 100)}%`
        + ` / ${SOURCE_LABEL[right]} ${100 - Math.round((widths[left] / total) * 100)}%`);
    }
  }
}

/* 宽度与折叠是阅读偏好（存在本机），存的时候 split 也一起存（栏数与哪几个展开）。
 * ⚠️ `touched`（用户动过哪几个窗格）也要存：它是“阶段窗默认收起”那条规则唯一的例外依据。
 *    不存的话刷新之后一份新内存里就分不出“我们默认收的”与“用户自己收的”——
 *    而这两件事的处置相反（前者可以拨开，后者不该动）。 */
function saveWorkspacePrefs() {
  const split = normalizeSplit(app.layout.split);
  app.prefs.split = { n: split.n, ids: split.ids.slice() };
  app.prefs.workspace = {
    collapsed: normalizeCollapsed(app.layout.collapsed),
    widths: normalizeWidths(app.layout.widths),
    touched: [...layoutTouched],
  };
  savePrefs();
}

/* 分隔条：拖动只在**相邻两个展开窗口**之间重新分配宽度，别的窗口一点不动；
 * 某一侧拖到窄于 PANE_MIN_W 就把它**收成标签**（而不是挤成一条缝）；←/→ 微调走同一条规则。
 * ⚠️ 必须先超过 DRAG_SLOP 才算拖动：否则单击一下就会把窗口挪半个条的宽度
 * （而且双击的第一下先挪动，第二下就可能落到别的元素上，双击很难触发）。 */
function wireSplitBar(store) {
  for (const bar of Array.from(document.querySelectorAll(".split-bar"))) {
    const pair = Number(bar.dataset.pair) || 0;
    const pairIds = () => {
      const view = paneView(app.layout, isRowLayout());
      return [view.expanded[pair], view.expanded[pair + 1]];
    };
    let drag = null;
    const stop = (event) => {
      const moved = Boolean(drag && drag.moved);
      drag = null;
      app.draggingSplit = false;
      try { bar.releasePointerCapture(event.pointerId); } catch (error) { /* 已经释放了 */ }
      return moved;
    };
    const apply = (outcome) => {
      if (outcome.collapse) {
        collapsePane(store, outcome.collapse);
        return true;
      }
      app.layout.widths = outcome.widths;
      applyRowLayout(store);
      updateLogIndicators(store);          // 分隔条的无障碍值跟着走
      return false;
    };
    // 双击分隔条：当前展开的窗口均分横向空间（收起的标签不动）。
    bar.addEventListener("dblclick", (event) => {
      event.preventDefault();
      equalizePanes(store);
    });
    bar.addEventListener("pointerdown", (event) => {
      const [leftId, rightId] = pairIds();
      if (!leftId || !rightId) return;
      const left = store.sources[leftId].pane.getBoundingClientRect();
      drag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        leftWidth: left.width,
        moved: false,
      };
      app.draggingSplit = true;
      try { bar.setPointerCapture(event.pointerId); } catch (error) { /* 捕获不到也能拖 */ }
      event.preventDefault();
    });
    bar.addEventListener("pointermove", (event) => {
      if (!app.draggingSplit || !drag) return;
      const [leftId, rightId] = pairIds();
      if (!leftId || !rightId) return;
      const plan = paneDragPlan({ startX: drag.startX, clientX: event.clientX, startLeftWidth: drag.leftWidth });
      if (!plan.moved) return;             // 还没动够：当它只是想点一下
      drag.moved = true;
      const right = store.sources[rightId].pane.getBoundingClientRect();
      const outcome = dragPaneWidths({
        widths: app.layout.widths,
        leftId,
        rightId,
        leftPx: plan.leftPx,
        pairPx: drag.leftWidth + right.width + bar.getBoundingClientRect().width,
      });
      if (apply(outcome)) stop(event);     // 收起来了：本次拖动到此为止
    });
    for (const type of ["pointerup", "pointercancel"]) {
      bar.addEventListener(type, (event) => {
        if (!app.draggingSplit) return;
        if (stop(event)) saveWorkspacePrefs();   // 只是点了一下就不存偏好
      });
    }
    bar.addEventListener("keydown", (event) => {
      const [leftId, rightId] = pairIds();
      const delta = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
      if (!delta || !leftId || !rightId) return;
      event.preventDefault();
      const widths = normalizeWidths(app.layout.widths);
      const total = widths[leftId] + widths[rightId];
      const step = event.shiftKey ? SPLIT_STEP * 2.5 : SPLIT_STEP;
      // 用 1000 px 当作“两个窗口总宽”的替身：和拖动共用同一条判定（含“窄到最小就收起”）。
      const outcome = dragPaneWidths({
        widths,
        leftId,
        rightId,
        leftPx: ((widths[leftId] + delta * step * total) / total) * 1000,
        pairPx: 1000,
      });
      apply(outcome);
      saveWorkspacePrefs();
    });
  }
}

/* 布局是不是**用户自己调过**的（用户口径 2026-10-12）。
 *
 * 为什么需要它：实时观察那一屏有一条默认值 —— 阶段窗没有阶段时收成竖排标签
 *（平时它就是一个空窗格，白占一栏的宽）。这条默认值**不得覆盖用户的手动选择**：
 * 用户刚把它展开看，换个屏再回来又被收起来，那是软件在跟人抢控制权。
 * ⚠️ 集合本体声明在 `app` 旁边（模块求值时就按本机偏好装好了，见那里的说明）。
 */

function markLayoutTouched(id) {
  if (id) layoutTouched.add(id);
  else for (const name of SOURCE_IDS) layoutTouched.add(name);
  // 存起来：刷新之后还要靠它区分“我们默认收的”与“用户自己收的”。
  saveWorkspacePrefs();
}

function paneCollapsed(id) {
  const pane = $(`pane-${id}`);
  return !pane || pane.dataset.collapsed === "1";
}

/* 实时观察那一屏的阶段窗默认值（用户口径 2026-10-12）：**没有阶段就收成竖排标签**。
 * 收到首条 `test_step` 会自动展开（见 SSE 那条）；跑完之后只要还有阶段就不再收回
 *（这条只看“有没有阶段”）；用户自己动过这一窗就不再插手。
 * ⚠️ “有没有阶段”按这一屏**自己那一份**判（`stageBag`）：别的屏有卡不算。
 * ⚠️ 调用点只有一处（`renderObserve`）——它保证“状态已经拿到了”。抢在状态前面判，
 *    会把“刚刷新、这一批已经跑完”的那一窗收起来（卡还在里面）。 */
function applyStagePaneDefault(mode) {
  if (mode !== "live") return false;
  if (layoutTouched.has("stage")) return false;
  const view = app.views.live;
  const bag = stageBag(view);
  if (bag.seen || bag.rows.size) return false;      // 已经有阶段：保持展开
  if (paneCollapsed("stage")) return false;         // 已经收起来了
  collapsePane(storeFor(view), "stage");
  return true;
}

/* 标签条：单击展开（split 下是“换进这一栏”），双击把四个窗口恢复成等宽；
 * 键盘 Tab 也能聚焦到标签，Enter/Space 同单击。
 * ⚠️ 单击要**等一拍**再执行（见 RAIL_CLICK_MS）：否则第一下就把标签展开掉了，
 * 第二下落在别的地方，`dblclick` 永远不会触发。 */
function wirePaneRails(store) {
  let railTimer = 0;
  for (const id of SOURCE_IDS) {
    const source = store.sources[id];
    if (!source || !source.pane) continue;
    const pane = source.pane;
    const isRail = () => pane.dataset.collapsed === "1";
    pane.addEventListener("click", (event) => {
      if (!isRail()) return;
      event.stopPropagation();
      markLayoutTouched(id);                 // 用户自己展开的：默认值不再插手
      clearTimeout(railTimer);
      railTimer = setTimeout(() => { railTimer = 0; expandPane(store, id); }, RAIL_CLICK_MS);
    });
    pane.addEventListener("dblclick", (event) => {
      if (!isRail()) return;
      event.stopPropagation();
      markLayoutTouched();                   // 双击 = 全部展开（用户动的是整套布局）
      clearTimeout(railTimer);                 // 凑齐了双击：取消那一下“单击展开”
      railTimer = 0;
      expandAllPanes(store);
    });
    pane.addEventListener("keydown", (event) => {
      if (!isRail()) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      markLayoutTouched(id);
      clearTimeout(railTimer);
      railTimer = 0;
      expandPane(store, id);
    });
    // 展开状态下双击**工具条那一行** = 把这一窗收成标签（与双击标签展开对称）。
    // 只剩它一个展开时改成“全部展开”（与双击标签一致），否则工作区就空了。
    // ⚠️ 这条手势挂在栅格上（事件委托），不是挂在 .pane-row 上：宿主里装配时
    //    `pane.querySelector(".pane-row")` 可能拿不到对应元素，手势就静默失效了（踩过）。
  }
  const grid = $("logGrid");
  if (!grid) return;
  grid.addEventListener("dblclick", (event) => {
    const row = event.target.closest ? event.target.closest(".pane-row") : null;
    if (!row) return;
    // 工具栏里全是按钮：双击按钮不该把窗口收掉（那是用户想点它）。
    if (event.target.closest("button, a, input, select, label")) return;
    const pane = row.closest(".log-pane");
    if (!pane || pane.dataset.collapsed === "1") return;     // 标签有自己的处理
    const id = pane.id.replace(/^pane-/, "");
    if (!SOURCE_IDS.includes(id)) return;
    event.preventDefault();
    markLayoutTouched(id);                   // 用户自己收的：默认值不再插手
    if (paneView(app.layout, isRowLayout()).expanded.length <= 1) expandAllPanes(store);
    else collapsePane(store, id);
  });
}

/* 把一个窗口收成标签：grid 下记在 collapsed 里；split 下就是把它从“展开的一组”里拿掉
 * （栏数跟着减，至少留 1 栏）。 */
function collapsePane(store, id) {
  if (!SOURCE_IDS.includes(id)) return;
  if (app.layout.mode === "split") {
    const split = normalizeSplit(app.layout.split);
    const ids = split.ids.filter((value) => value !== id);
    if (!ids.length) return;                     // 最后一栏不允许收
    app.layout.split = normalizeSplit({ ...split, n: ids.length, ids });
  } else {
    const collapsed = normalizeCollapsed(app.layout.collapsed);
    if (SOURCE_IDS.every((other) => other === id || collapsed[other])) return;   // 同上：至少留一个
    collapsed[id] = true;
    app.layout.collapsed = collapsed;
  }
  applyLayout(store);
  saveWorkspacePrefs();
  updateLogIndicators(store);
}

/* 把标签展开：grid 直接展开；split 还有空位就补上，否则替换“来源顺序上最近”的那一栏。 */
function expandPane(store, id) {
  if (!SOURCE_IDS.includes(id)) return;
  if (app.layout.mode === "split") {
    const split = normalizeSplit(app.layout.split);
    const ids = nextExpandedForRail({ mode: "split", ids: split.ids, clicked: id, n: split.n });
    app.layout.split = normalizeSplit({ ...split, n: Math.max(ids.length, SPLIT_MIN_N), ids });
  } else {
    const collapsed = normalizeCollapsed(app.layout.collapsed);
    collapsed[id] = false;
    app.layout.collapsed = collapsed;
  }
  applyLayout(store);
  saveWorkspacePrefs();
  updateLogIndicators(store);
}

/* 双击分隔条：把**已经展开**的窗口宽度恢复成平均（收起来的标签不参与，也不会被展开）。 */
function equalizePanes(store) {
  markLayoutTouched();                   // 双击分隔条 = 用户把宽度恢复成均值
  const view = paneView(app.layout, isRowLayout());
  app.layout.widths = equalizedWidths(app.layout.widths, view.expanded);
  applyRowLayout(store);
  saveWorkspacePrefs();
  updateLogIndicators(store);
}

/* 双击标签：展开所有窗口，并把宽度恢复成**平均**（= 回到默认四窗）。 */
function expandAllPanes(store) {
  app.layout.collapsed = normalizeCollapsed(null);
  app.layout.widths = equalWidths();
  setSplit(store, { on: false });
  saveWorkspacePrefs();
}

/* ---------------------------------------------------------------- 阅读偏好 */
/* 命令输入行的高度：脏数据/手改的 localStorage 都要能兜住（读不出或越界就回默认值）。 */
function clampCmdHeight(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return CMD_H_DEFAULT;
  return Math.min(CMD_H_MAX, Math.max(CMD_H_MIN, Math.round(number)));
}

/* 字号与字体是**阅读偏好**（存在浏览器本地），改的是 CSS 变量：
 * 既不重建视图，也不碰证据。常量定义在文件靠前的位置（见 PREFS_KEY 那里的说明）。 */
function loadPrefs() {
  let saved = {};
  try {
    const raw = globalThis.localStorage ? globalThis.localStorage.getItem(PREFS_KEY) : null;
    saved = JSON.parse(raw || "{}") || {};
  } catch (error) { saved = {}; }
  const size = Number(saved.fontSize);
  /* 哪些窗口收起了搜索/筛选行也是**阅读偏好**（刷新后应该还是自己收好的样子）。
   * ⚠️ 用户口径 2026-10-12：**开箱默认收起**（工作台一启动就是收好的样子：条件仍然生效，
   *    但那一行控件不占高度，日志区更高）。
   *    判定“用户自己表过态”的是 `foldedSet`：老偏好里那个 `folded`（那时默认是展开）不再生效 ——
   *    否则新默认永远只在没存过偏好的浏览器上看得见（实测开发机那份就是五个 false）。 */
  const foldedTouched = saved.foldedSet === true;
  const folded = {};
  for (const id of SOURCE_IDS) {
    folded[id] = foldedTouched ? Boolean(saved.folded && saved.folded[id] === true) : true;
  }
  // 分栏偏好：只存“几栏 + 展开哪几个”，不存“是否正在分栏”（那是临时形态）。
  const savedSplit = saved.split || {};
  // 一行布局的偏好：每个窗口的宽度与是否收成标签（旧版只存过 a/b/ratio，读不出来就当默认）。
  const savedWork = saved.workspace || {};
  const savedObserve = saved.observe || {};
  return {
    fontSize: Number.isFinite(size) ? Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round(size))) : 12,
    fontFamily: typeof saved.fontFamily === "string" ? saved.fontFamily : "",
    folded,
    split: normalizeSplit({
      n: savedSplit.n,
      ids: Array.isArray(savedSplit.ids) ? savedSplit.ids : [savedSplit.a, savedSplit.b, savedSplit.c],
    }),
    workspace: {
      collapsed: normalizeCollapsed(savedWork.collapsed),
      widths: normalizeWidths(savedWork.widths),
      // 用户动过哪几个窗格（“阶段窗默认收起”唯一的例外依据）。
      // ⚠️ 这个函数是**重建式**的：没在这里显式读出来的键就等于被丢掉（ 同一类坑）——
      //    实测踩过：不读它，刷新后那一窗又被当成“我们默认收的”拨开了。
      touched: Array.isArray(savedWork.touched) ? savedWork.touched.map(String) : [],
    },
    // 命令输入行的高度/折叠也是阅读偏好：刷新后还是自己拉好的样子。
    observe: {
      cmdHeight: clampCmdHeight(savedObserve.cmdHeight),
      cmdCollapsed: savedObserve.cmdCollapsed === true,
    },
    // 两个指标窗格各选了哪个指标：刷新后还是自己选的那两个。
    metrics: loadMetricPanes(saved.metrics),
    // 时间窗与纵轴刻度也是阅读偏好（同一份数据，看整体还是看抖动）。
    metricWindows: loadMetricWindows(saved.metricWindows),
    metricScales: loadMetricScales(saved.metricScales),
  };
}

/* 路径最后一段（页脚那一行只写这一截；绝对路径进 title）。 */
function tailSegment(path) {
  return String(path || "").split(/[\\/]/).filter(Boolean).pop() || "";
}

function savePrefs() {
  try {
    if (globalThis.localStorage) {
      globalThis.localStorage.setItem(PREFS_KEY, JSON.stringify(app.prefs));
    }
  } catch (error) { /* 存不了就算了，不影响使用 */ }
}

function applyPrefs() {
  const root = document.documentElement;
  root.style.setProperty("--log-font-size", `${app.prefs.fontSize}px`);
  // 行高跟着字号走，否则字号一大行与行就叠在一起。
  root.style.setProperty("--log-line-h", `${Math.round(app.prefs.fontSize * 1.42)}px`);
  root.style.setProperty(
    "--log-font-family",
    app.prefs.fontFamily ? `"${app.prefs.fontFamily}", var(--mono)` : "var(--mono)"
  );
  // 收起一行筛选行能省多少高度由 CSS 说（--pane-fold-step），JS 只负责按收起的窗口数相乘。
  const step = parseFloat(getComputedStyle(root).getPropertyValue("--pane-fold-step"));
  app.foldStep = Number.isFinite(step) && step > 0 ? step : 48;
  const label = $("fontSize");
  if (label) label.textContent = `${app.prefs.fontSize} px`;
  const select = $("fontFamily");
  if (select && select.value !== app.prefs.fontFamily) select.value = app.prefs.fontFamily;
}

function bumpFont(delta) {
  app.prefs.fontSize = Math.min(FONT_MAX, Math.max(FONT_MIN, app.prefs.fontSize + delta));
  applyPrefs();
  savePrefs();
}

/* “补齐并跳到最新”，只作用于这一个窗口：暂停时也照做 —— 视图与模型对齐，
 * 但暂停状态不变，之后到达的行继续累计。 */
function jumpToLatest(id) {
  const store = app.log;
  const source = store.sources[id];
  if (!source) return;
  // 卡片窗的“跳尾”= 补齐暂停期间的阶段并滚到最新一张卡（行机制不碰这一窗）。
  if (SOURCE_BY_ID[id].cards) {
    stageJumpToLatest(store);
    return;
  }
  renderPending(store, id);
  source.followTail = true;
  source.pending = 0;
  if (source.el) source.el.scrollTop = source.el.scrollHeight;
  updateLogIndicators(store);
}

function togglePause(id) {
  const store = app.log;
  const source = store.sources[id];
  if (!source) return;
  if (SOURCE_BY_ID[id].cards) {
    toggleStagePause(store);
    return;
  }
  if (source.paused) {
    resumeSource(store, id);     // 只清这一个窗口的未读
    renderPending(store, id);    // 把冻结期间到达的行按原顺序补齐
    if (source.el && source.followTail) source.el.scrollTop = source.el.scrollHeight;
  } else {
    setPaused(store, id, true);
  }
  updateLogIndicators(store);
}

/* “阅读位”要滚到哪、窗口正文该多高（纯函数，宿主回归直接驱动）。
 *
 * 用户口径（2026-10-12）：
 *   ① **工作台顶部对齐浏览器可视区顶部**（顶部对不齐就如实说：页面已经到底了）；
 *   ② 动态改**当前窗格**的高度，让它的**底边落在可视区底部**；
 *   ③ 屏幕不够高（算出来的正文低于可读下限）时**诚实降级**：只做①，窗格高度不动。
 *
 * 入参全是量出来的数（文档坐标 / px），这里一个 DOM 也不碰：
 *   `wsTop`      工作台顶（文档坐标）
 *   `paneOffset` 当前窗格顶 − 工作台顶（文档坐标）
 *   `paneChrome` 窗口头 + 底栏 + 边框（占屏幕但不属于“正文”的那一截）
 *   `minBody`    正文的可读下限（再压下去就不是“读”了——退回降级模式）
 */

/* 正文的可读下限（与 CSS 那条 `max(240px, …)` 同一个数）与窗格底边留的那一点。 */
const READING_HEIGHT_VAR = "--pane-reading-h";

/* 把上次“阅读位”写下的高度撤掉（回到 CSS 的预算公式）。
 * 什么时候必须撤：视口变了、布局变了（收/展窗格、改分栏）、换屏了 —— 那几种情况下
 * 上一次那个 px 数已经不是“这一屏该有多高”，留着就是错误答案。 */
function clearReadingHeight() {
  document.documentElement.style.removeProperty(READING_HEIGHT_VAR);
}

/* `--pane-reading-h` 的**唯一**写入口（阅读位算出来的 px 只能从这里进去）。
 * ⚠️ 校正循环会写不止一次 ⇒ 必须只有一个 `setProperty` 处，不然“高度只有一个写入口”
 *    这条规矩就静默失效了。 */
function applyReadingHeight(px) {
  document.documentElement.style.setProperty(READING_HEIGHT_VAR, `${Math.round(px)}px`);
}

/* “当前监视窗格”：最近动过的那个（`app.lastPane`）优先，否则第一个看得见的窗口。
 * 收成标签的、这一屏不显示的都不算 —— 它们连正文都没有。 */
function readingPane() {
  const order = [app.lastPane, ...SOURCE_IDS];
  for (const id of order) {
    if (!id || !SOURCE_IDS.includes(id)) continue;
    const pane = $(`pane-${id}`);
    if (!pane || pane.dataset.shown === "0" || pane.dataset.collapsed === "1") continue;
    return pane;
  }
  return null;
}

/* 一个窗口除了正文之外占掉的屏幕高度：窗口头 + 底栏 + 2px 边框。
 * ⚠️ 逐窗量而不是拿全局的 `--pane-head-h`：那个是**最大**值（预算公式用），
 *    而这里要的是“这一个窗格”的实数 —— 差几像素，底边就贴不齐。 */
function paneChromeHeight(pane) {
  const head = pane.querySelector(".pane-head");
  const foot = pane.querySelector(".pane-foot");
  const headH = head ? Math.round(head.getBoundingClientRect().height) : 0;
  const footH = foot ? Math.round(foot.getBoundingClientRect().height) : 0;
  return headH + footH + 2;
}

/* 工作台那一栏与五窗之间**会占掉同一块屏幕**的那几行：设备嗅探的卡片（ / D5：
 * 一轮嗅探看几个口就有几张卡，高度是变的）、全文件搜索栏（含最多五行的结果列表）
 * 与“页面是旧的”提示条。它们不吸顶，但和五窗抢高度：
 * 不把它们算进去，五窗就永远比一屏高一点 —— 用户报的正是“用了搜索整个文件、底下出了
 * 五条结果之后点阅读位，五窗下面还是被切掉”；D5 加卡片时同一个坑又踩了一次
 *（浏览器实测：卡片 141 px ⇒ 五窗底边 866 / 视口 860，掉出去 6 px）。
 * ⚠️ 2026-10-10：阶段卡不再夹在中间了 —— 它成了工作台里的第 5 格（自己就是一个窗口、
 *    自己吸顶、自己在网格里），所以从这张表里**去掉**了 `stageCards`（它已不存在）。
 * 新增这类夹在中间的行要一起登记进来。 */
const PANE_GAP_NODES = ["sniffCards", "sniffProgress", "fullSearch", "staleBanner", "liveTestPanel"];

/* 夹在中间那几行一共多高（纯函数：给一组 `{hidden, height}`）。 */

function paneGapParts() {
  return PANE_GAP_NODES.map((id) => {
    const el = $(id);
    if (!el) return null;
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    const height = box.height + (parseFloat(style.marginTop) || 0) + (parseFloat(style.marginBottom) || 0);
    return {
      hidden: Boolean(el.hidden) || style.display === "none"
        || style.visibility === "hidden" || height < 1,
      height,
    };
  });
}

/* 把量出来的高度写进 CSS 变量（`--pane-chrome-gap`）：四个窗口的正文上限（`--pane-visible-h`）
 * 会跟着降 —— 于是“搜索栏 + 结果列表 + 四窗”仍然装得进一屏（这就是阅读位要的那件事）。
 * ⚠️ 不能只写进 JS：上限是 CSS 算的，两个地方各算一份就会出现两套真相。 */
function syncPaneViewportBudget() {
  const root = document.documentElement;
  if (!viewportIsLaidOut()) return 0;      // 同一个道理：视口没成形时量到的“中间那几行”也不算数
  const gap = paneChromeGap(paneGapParts());
  const current = parseFloat(root.style.getPropertyValue("--pane-chrome-gap"));
  if (!Number.isFinite(current) || Math.abs(current - gap) >= 1) {
    root.style.setProperty("--pane-chrome-gap", `${gap}px`);
  }
  return gap;
}

/* 解码窗底栏（常驻说明 + 命令输入行）占掉的高度，写进这一窗的 `--pane-foot-h`：
 * 正文的上下限都从 `--pane-body-*` 里减掉这个数（CSS），不减的话整个网格行会被撑高。
 * ⚠️ 它**一变就要重量**，而且只能有这一个写入口：说明行是按状态出现的
 *   （“解码窗口现在没有内容：…”那一条，实测 43 px），量一次就不管了的话，
 *   正文会把这几十像素当成自己的额度 —— 四窗底边就掉到屏幕外面
 *   （2026-10-06 实测：底栏 182 px、变量还写着 139 px，四窗底边 891 / 视口 860）。
 * 所以 `applyDock()`（命令行高度/折叠变了）与看着底栏的 ResizeObserver 都走这一个函数。 */
function syncPaneFootHeight() {
  const pane = $("pane-decoded");
  const foot = $("paneFootDecoded");
  if (!pane || !foot) return 0;
  const height = Math.round(foot.offsetHeight);
  const current = parseFloat(pane.style.getPropertyValue("--pane-foot-h"));
  if (!Number.isFinite(current) || Math.abs(current - height) >= 1) {
    pane.style.setProperty("--pane-foot-h", `${height}px`);
  }
  return height;
}

/* 对齐之后把顶部再确认一次的余量（px）：窗格高度写下去、页面高度跟着变，顶部可能被推走。 */
const READING_TOP_TOLERANCE = 2;

/* 这块工作区现在排成几行？（1 = 一行；≤1044 px 的 2×2+1 与 ≤640 px 的单列都 > 1） */
function paneRowCount() {
  const grid = $("logGrid");
  if (!grid) return 1;
  const tops = new Set();
  for (const pane of grid.querySelectorAll(".log-pane")) {
    if (getComputedStyle(pane).display === "none") continue;
    tops.add(Math.round(pane.getBoundingClientRect().top));
  }
  return Math.max(1, tops.size);
}

/* 布局一改（阅读位写了高度 / 分栏换了 / 视口变了），**本来就在尾部**的窗口要重新贴回尾部。
 * ⚠️ 用户实测（2026-10-17）：五窗 + 阅读位之后，每个窗格最底下那一行被切掉 9 px ——
 *    原因不是高度算错，而是高度一变 `scrollTop` 不会自己跟着贴底（实测 maxScroll 567、
 *    scrollTop 只到 558）：“最新几行在底”变成了“最后一行只剩半行”。
 * ⚠️ 只动 `followTail` 的窗格：用户自己往上滚着看的，不许被拽回底（那是他在读的东西）。 */
function settlePaneTails(store = app.log) {
  const log = store || app.log;
  if (!log) return 0;
  let pinned = 0;
  for (const id of SOURCE_IDS) {
    const source = log.sources[id];
    const el = source && source.el;
    if (!el || !source.followTail) continue;
    if (el.scrollHeight - el.clientHeight <= 0) continue;
    const max = el.scrollHeight - el.clientHeight;
    el.scrollTop = max;
    /* ⚠️ 贴到底还不够：最后一行可能**比剩下的空间还高**（窄窗格里一句日志折成好几行），
     * 那样它就只能露出半行。往回退到“整行可见”为止 —— 底部留几像素，胜过切掉半行。 */
    const kids = el.children;
    if (kids.length) {
      const inner = el.getBoundingClientRect().bottom
        - parseFloat(getComputedStyle(el).paddingBottom || "0");
      const over = Math.round(kids[kids.length - 1].getBoundingClientRect().bottom - inner);
      if (over > 0) el.scrollTop = Math.max(0, el.scrollTop - over);
    }
    pinned += 1;
  }
  return pinned;
}

function alignReadingPosition() {
  const ws = $("logWorkspace");
  const grid = $("logGrid");
  if (!ws || !grid) return;
  // 从零算：上一次的调整会改变页面高度，不清掉量到的就是上一版的产物。
  clearReadingHeight();
  // 先重算“工作台栏与四窗之间那几行占掉多少”——用户点这个按钮时，全文件搜索栏/结果列表
  // 可能刚变过（刚出结果、刚收起）。它决定四窗能有多高：不先算，量到的还是旧高度。
  syncGeometryVariables();
  const scroller = document.documentElement;
  const pageHeight = () => Math.max(scroller.scrollHeight, document.body ? document.body.scrollHeight : 0);
  const pane = readingPane();
  const wsTop = Math.round(ws.getBoundingClientRect().top + window.scrollY);
  const paneTop = pane ? Math.round(pane.getBoundingClientRect().top + window.scrollY) : wsTop;
  // 排成几行很要紧：多行时"把一个窗格拉到一屏高"会把整块工作区变成 2～3 屏（见 readingPlan）。
  const rows = paneRowCount();
  const plan = readingPlan({
    wsTop,
    paneOffset: paneTop - wsTop,
    paneChrome: pane ? paneChromeHeight(pane) : 0,
    innerHeight: window.innerHeight,
    maxScroll: Math.max(0, pageHeight() - window.innerHeight),
    rows,
  });
  /* ⚠️ 这里的滚动**不用 smooth**：紧接着要拿"网格底边此刻在视口哪里"去校正正文高度，
   * 平滑滚动会让校正量到动画途中的位置（写下去的高度就跟着错）。这个按钮要的是"到位"。 */
  if (Math.abs(window.scrollY - plan.target) >= 1) window.scrollTo(0, plan.target);
  // ③ 屏幕不够高 / 排成了多行：**不做高度调整**（正文压到 240 px 以下就不是“读”了），
  //    只对齐顶部，并把这件事写在提示里 —— 降级可以，悄悄降级不行。
  if (!plan.fit) {
    app.readingEngaged = false;     // 降级了：别在下次布局变动时自作主张再伸一次
    alignHint(plan.rows > 1
      ? `窗口排成 ${plan.rows} 行，一屏装不下：只把工作台顶部对到浏览器顶部（正文高度不动，往下滚看其余各行）`
      : "屏幕不够高：只把工作台顶部对到浏览器顶部（窗口高度保持原样，正文往下滚看）");
    return plan;
  }
  // ② 动态高度：写进唯一入口 `--pane-reading-h`（CSS 的 `--pane-body-max` 读它）。
  applyReadingHeight(plan.body);
  /* ⚠️ 写一次不一定正好落在视口底边：行高由**这一行里最高的那个窗格**决定（各窗的窗口头 /
   * 底栏不一样），网格还自带下内边距。量网格底边与目标的差，按差值修正（最多三次）。
   * 用户实测（2026-10-17）：5 栏时底边掉出视口 17 px，提示却写着"已对齐"。 */
  for (let pass = 0; pass < 3; pass += 1) {
    const over = Math.round(grid.getBoundingClientRect().bottom)
      - (window.innerHeight - READING_BOTTOM_GAP);
    if (Math.abs(over) <= 2) break;
    const next = Math.round(plan.body - over);
    if (next < READING_MIN_BODY) { plan.fit = false; break; }
    plan.body = next;
    applyReadingHeight(plan.body);
    const pinned = Math.min(plan.target, Math.max(0, pageHeight() - window.innerHeight));
    if (Math.abs(window.scrollY - pinned) >= 1) window.scrollTo(0, pinned);
  }
  // 高度一变，页面高度也跟着变：把顶部对齐**再压一次**，并如实说有没有压住。
  const settled = Math.min(plan.target, Math.max(0, pageHeight() - window.innerHeight));
  if (Math.abs(window.scrollY - settled) >= 1) window.scrollTo(0, settled);
  if (!plan.fit) {
    clearReadingHeight();     // 框到可读下限以下：老老实实退回"只对齐顶部"
    alignHint("屏幕不够高：只把工作台顶部对到浏览器顶部（窗口高度保持原样，正文往下滚看）");
    return plan;
  }
  /* ③ 布局变了就要把“跟着尾巴”的窗口重新贴回尾部：高度一变 `scrollTop` 不会自己跟上，
   * 差几像素就表现成**最底下那一行被切掉一半**（实测 9 px）。
   * ⚠️ 贴一次不够：改宽度/换分栏之后，换行与滚动条要到下一帧才定下来，所以下一帧**再贴一次**。
   * 只动 `followTail` 的窗格，用户自己往上滚的不许被拽回去。 */
  settlePaneTails();
  requestAnimationFrame(() => settlePaneTails());
  app.readingEngaged = true;      // 这一次真的对上了：以后布局一变就自动再对一次
  const kept = Math.abs(window.scrollY - plan.target) <= READING_TOP_TOLERANCE;
  const gap = Math.round(grid.getBoundingClientRect().bottom) - (window.innerHeight - READING_BOTTOM_GAP);
  alignHint(!kept
    ? "窗格高度已按可视区调好；页面已经到底，工作台顶部只差一点没贴上（再往下没有内容了）"
    : Math.abs(gap) <= 2
      ? `已对齐阅读位：工作台顶部在浏览器顶部，窗格底边在浏览器底部（正文 ${plan.body} px）`
      : `已对齐阅读位：工作台顶部在浏览器顶部（正文 ${plan.body} px；底边差 ${gap} px，来自网格自己的内边距）`);
  return plan;
}

/* 布局变了（收/展窗格、全屏、换屏）之后：如果上一次「阅读位」是**生效的**，就按新布局再对一次。
 * ⚠️ 用户实测（2026-10-17）：点了阅读位四个窗口都对上了；接着把某个窗口收成标签，工作区立刻矮回去 ——
 *    旧高度按新布局作废是对的，但**那之后就没人再对**，看上去就是“一折叠，阅读位就失效”。
 * 用 rAF 推开一帧：调用方（`applyLayout`）正在改 DOM，现在量到的是改到一半的布局。 */
function reapplyReadingPosition() {
  if (!app.readingEngaged) return false;
  requestAnimationFrame(() => {
    if (app.readingEngaged) alignReadingPosition();
  });
  return true;
}

/* 工作区那一栏里的临时提示：只改按钮的 title（不动布局，不会把四份日志顶下去）。 */
function alignHint(text) {
  const button = $("alignWorkspace");
  if (!button) return;
  button.title = text;
  if (app.alignHintTimer) clearTimeout(app.alignHintTimer);
  app.alignHintTimer = setTimeout(() => {
    button.title = "最佳阅读位置：把工作区底部对齐浏览器底部";
  }, 6000);
}

/* 四个窗口的搜索/筛选行一起收起或展开（逐个窗口的那个开关在文件名旁边）。 */
function setAllFindsFolded(store, folded) {
  for (const id of SOURCE_IDS) setFindFolded(store, id, folded, { persist: false });
  app.prefs.folded = Object.fromEntries(SOURCE_IDS.map((id) => [id, Boolean(folded)]));
  // 一起收起/展开是用户按的（`#foldAll`）⇒ 记下“他表过态了”，别再按默认值拨回来。
  app.prefs.foldedSet = true;
  savePrefs();
  savePrefs();
  updateLogIndicators(store);
}

/* ------------------------------------------------- 滚轮边界与选区范围 */
/* 滚轮落到窗口边界时不要把滚动交给主页面（否则看着日志页面会突然跑掉），
 * 换成一次轻轻的上下晃动，说明“已经到头了”。 */
let lastEdgeNudge = 0;
function edgeNudge(el, up) {
  const now = Date.now();
  if (now - lastEdgeNudge < 220) return;   // 触控板惯性会连发上百个事件
  lastEdgeNudge = now;
  const cls = up ? "edge-nudge-up" : "edge-nudge-down";
  el.classList.remove("edge-nudge-up", "edge-nudge-down");
  void el.offsetWidth;                     // 强制一次重排，让动画能重播
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), 200);
}

/* 滚轮在当前滚动位置上该不该被拦下？
 * 返回 1 = 已经到底（还在往下滚）、-1 = 已经到顶（还在往上滚）、null = 里面还能滚。
 * 故意做成纯函数：宿主回归能直接喂几个滚动位置把边界判定钉住。 */

/* 让一个滚动容器“到边界就别把滚动链给主页面”（反倒轻轻晃一下说明到头了）。
 *
 * ⚠️ **四处共用这一处**：四个日志窗口、全文搜索的结果列表、历史运行那一窗。
 *    各写一遍一定会漂（“窗口里的滚轮到顶会晃、结果列表里却把整页带跑”）。
 *    `el.dataset.edgeWheel` 是幂等标记：接两次也只挂一个监听。
 */
function edgeWheelTarget(el) {
  if (!el || el.dataset.edgeWheel === "1") return el;
  el.dataset.edgeWheel = "1";
  el.addEventListener("wheel", (event) => {
    if (event.ctrlKey) return;               // Ctrl+滚轮是浏览器缩放，不抢
    const edge = wheelEdge(el, event.deltaY);
    if (edge === null) return;                // 还能滚：交给浏览器原生滚动
    event.preventDefault();                   // 到头了：滚轮不再链给主页面
    edgeNudge(el, edge < 0);
  }, { passive: false });
  return el;
}

/* 鼠标拖动选择时只允许选本窗口里的文本，别把别的窗口和整页一起圈进来。
 * 不在 mousedown 中切 user-select：部分 Chromium 会因此取消原生选区。选区变化时
 * 直接把越界端点夹回当前窗口，既保留原生拖选，也不会圈进相邻窗口。 */
let selectingPane = null;
function beginPaneSelection(id) {
  selectingPane = id;
  document.body.classList.add("pane-selecting");
}
function endPaneSelection() {
  selectingPane = null;
  document.body.classList.remove("pane-selecting");
}

/* 把选区夹在 host 的内容范围内：伸出窗口的那一端收回到窗口边界。 */
function clampSelection(host) {
  const selection = window.getSelection();
  if (!host || !selection || !selection.rangeCount) return;
  const range = selection.getRangeAt(0);
  if (host.contains(range.startContainer) && host.contains(range.endContainer)) return;
  try {
    const bounds = document.createRange();
    bounds.selectNodeContents(host);
    // 每个端点：本来就在窗口里就照原样，否则收到窗口开头（在窗口之前）或末尾。
    const settle = (node, offset) => {
      if (host.contains(node)) return [node, offset];
      const cmp = bounds.comparePoint(node, offset);   // -1 在窗口之前 / 0 在边界上 / 1 之后
      return cmp <= 0
        ? [bounds.startContainer, bounds.startOffset]
        : [bounds.endContainer, bounds.endOffset];
    };
    const start = settle(range.startContainer, range.startOffset);
    const end = settle(range.endContainer, range.endOffset);
    const clamped = document.createRange();
    clamped.setStart(start[0], start[1]);
    clamped.setEnd(end[0], end[1]);
    selection.removeAllRanges();
    selection.addRange(clamped);
  } catch (error) {
    // 拿不到边界就不动它：宁可这一次没夹住，也不能把用户的选区弄坏。
  }
}

/* ------------------------------------------------------------ 高亮调试接口 */
/* “当前窗口”的观感全是 CSS 变量，这里给一个控制台接口实时调（只改观感，不改行为、不碰证据）。
 *
 *   PWLogWorkspace.tune.set({ brightness: 1.15, dim: 0.85, titleWeight: 800 })
 *   PWLogWorkspace.tune.show()     // 打印当前数值 + 一段可以直接贴回来的 CSS
 *   PWLogWorkspace.tune.reset()    // 回到 style.css 里的默认值
 *
 * 调到你满意之后把那行 CSS 发给我，我把它固化成默认值。 */
const TUNE_VARS = Object.freeze({
  brightness: "--pane-active-brightness",   // 当前窗口的日志亮度（1 = 不变）
  dim: "--pane-dim",                        // 其它窗口的暗度（1 = 不暗）
  outlineW: "--pane-active-outline-w",      // 当前窗口的描边粗细（不占布局）
  border: "--pane-active-border",           // 当前窗口的边框/描边颜色
  head: "--pane-active-head",               // 当前窗口工具条底色
  title: "--pane-active-title",             // 当前窗口标题颜色
  titleWeight: "--pane-active-title-weight",// 当前窗口标题字重（加粗）
  glow: "--pane-active-glow",               // 当前窗口外发光（box-shadow）
});
const TUNE_DEFAULTS = Object.freeze({
  brightness: "1.12",
  dim: "0.9",
  outlineW: "2px",
  border: "#3d6fa8",
  head: "#1b2735",
  title: "#f2f7ff",
  titleWeight: "700",
  glow: "0 0 18px -6px rgba(61, 111, 168, .85)",
});

/* 纯函数：把一批观感参数变成 CSS（宿主回归直接测它，不用真浏览器）。 */
function tuneCss(values) {
  const parts = [];
  for (const [key, value] of Object.entries(values || {})) {
    const name = TUNE_VARS[key];
    if (!name || value === undefined || value === null || value === "") continue;
    parts.push(`${name}: ${value};`);
  }
  return parts.length ? `:root { ${parts.join(" ")} }` : "";
}

function tuneApplied() {
  const root = getComputedStyle(document.documentElement);
  const applied = {};
  for (const [key, name] of Object.entries(TUNE_VARS)) {
    applied[key] = root.getPropertyValue(name).trim();
  }
  return applied;
}

function tuneSet(values) {
  const root = document.documentElement;
  for (const [key, value] of Object.entries(values || {})) {
    const name = TUNE_VARS[key];
    if (!name || value === undefined || value === null || value === "") continue;
    root.style.setProperty(name, String(value));
  }
  return tuneShow();
}

function tuneReset() {
  for (const name of Object.values(TUNE_VARS)) {
    document.documentElement.style.removeProperty(name);
  }
  return tuneShow();
}

/* 打印当前生效值 + 一段可以直接发回来固化的 CSS。 */
function tuneShow() {
  const applied = tuneApplied();
  const css = tuneCss(applied);
  if (typeof console !== "undefined" && console.log) {
    console.log("%c当前窗口高亮参数（发给我就能固化成默认值）", "font-weight:700");
    console.log(css);
    console.log("用法：PWLogWorkspace.tune.set({...}) 实时改；PWLogWorkspace.tune.reset() 回到默认");
  }
  return css;
}

/* ---------------------------------------------------------- 装配与快捷键 */

/* 装配四个同时可见的窗口：每个窗口按 id 约定绑定自己的控件，彼此不共享状态。 */
/* 四窗的接线：把 DOM 引用绑到 store 上（**只跑一次**），并把这一份分给三个模式。 */
function buildWorkspace(store) {
  if (!$("logGrid")) return;
  for (const meta of SOURCES) {
    const source = store.sources[meta.id];
    /* ⚠️ 一份 DOM、三份表：绑 DOM 引用用启动时那一份 `source` 没问题（DOM 是同一份），
     * 但处理函数里读/写**状态**（查询串 / 正则 / 只看错误 / 折叠 / 菜单开合）必须重新按
     * `store.sources[meta.id]` 取 —— `attachedLog` 换模式时只换 `sources` 表，而 `source`
     * 这个引用还停在启动时那一份 ⇒ 会写进**别的模式**的模型里，界面看着就是"点了没反应"
     * （用户实测 2026-10-06：实时观察 / 离线诊断里窗口的「选项」等按钮点不动）。 */
    const src = () => store.sources[meta.id];
    source.el = $(`lines-${meta.id}`);
    source.pane = $(`pane-${meta.id}`);
    source.pauseBtn = $(`pause-${meta.id}`);
    source.tailBtn = $(`tail-${meta.id}`);
    source.olderBtn = $(`older-${meta.id}`);   // L3：向前加载更早的一段（只有窗口模式才出现）
    source.pill = $(`pending-${meta.id}`);
    source.pillCount = $(`pendingCount-${meta.id}`);
    source.stat = $(`stat-${meta.id}`);
    source.notice = $(`notice-${meta.id}`);
    source.link = $(`download-${meta.id}`);
    source.searchInput = $(`search-${meta.id}`);
    source.regexBtn = $(`regex-${meta.id}`);
    source.prevBtn = $(`prev-${meta.id}`);
    source.nextBtn = $(`next-${meta.id}`);
    source.hitsLabel = $(`hits-${meta.id}`);
    source.errorsBtn = $(`errorsonly-${meta.id}`);
    source.excludeBtn = $(`exclude-${meta.id}`);
    source.excludeCount = $(`excludeCount-${meta.id}`);
    source.menuBtn = $(`menu-${meta.id}`);
    source.menu = $(`panel-${meta.id}`);
    source.foldBtn = $(`fold-${meta.id}`);
    source.fullBtn = $(`full-${meta.id}`);
    source.findRow = $(`find-${meta.id}`);
    /* ⚠️ 筛选行的开合**不在这里装**：它已经在 `createLogStore()` 里按阅读偏好装好了 ——
     * 三个模式各有一份 store，只装在启动那一屏就会“同一个页面里两屏不一样”（）。 */

    /* ⚠️ 窗口操作分两类：
     *  ① 【共有的】折叠筛选行 / 全屏 / 收起成标签 / 拖宽 / 分栏 / 独立滚动 / 复制全选
     *     —— 每个窗口都有，接线也在下面共用同一个分支；
     *  ② 【行流专用的】暂停=冻结行渲染、跳尾=滚到最新一行、向前加载、正则/排除/
     *     只看错误/在行里搜索 —— 测试阶段那一窗是**卡片**，没有“行”，所以既不接
     *     这些处理函数，HTML 里也不摆这些按钮（见 index.html 那段注释）。
     *     它自己那套（冻结重画/跳到最新一张/筛选阶段卡）在 `wireStagePane` 里。 */
    const cards = Boolean(meta.cards);
    if (cards) {
      wireStagePane(store, meta);
    } else {
      if (source.pauseBtn) source.pauseBtn.addEventListener("click", () => togglePause(meta.id));
      if (source.tailBtn) source.tailBtn.addEventListener("click", () => jumpToLatest(meta.id));
      if (source.olderBtn) source.olderBtn.addEventListener("click", () => void loadOlderWindow(meta.id));
    }
    // ↑ 共有的两个：全屏与折叠筛选行（折叠只是收起控件，卡片窗一样适用）。
    if (source.fullBtn) {
      source.fullBtn.addEventListener("click", () => setFocus(store, meta.id));
    }
    if (source.foldBtn) {
      source.foldBtn.addEventListener("click", () => setFindFolded(store, meta.id, !src().view.folded));
    }
    if (!cards) {
      if (source.pill) source.pill.addEventListener("click", () => jumpToLatest(meta.id));
      if (source.regexBtn) {
        source.regexBtn.addEventListener("click", () => {
          src().view.regex = !src().view.regex;
          refreshView(store, meta.id);
        });
      }
      if (source.prevBtn) source.prevBtn.addEventListener("click", () => gotoHit(meta.id, -1));
      if (source.nextBtn) source.nextBtn.addEventListener("click", () => gotoHit(meta.id, 1));
      if (source.errorsBtn) {
        source.errorsBtn.addEventListener("click", () => {
          src().view.errorsOnly = !src().view.errorsOnly;
          refreshView(store, meta.id);
        });
      }
      if (source.excludeBtn) {
        source.excludeBtn.addEventListener("click", () => toggleExclude(meta.id));
      }
      if (source.searchInput) {
        let timer = 0;
        const apply = () => {
          src().view.query = source.searchInput.value.trim();
          refreshView(store, meta.id);
        };
        // 边打边搜，但不是每个键都重建一次（重建会清掉选区）。
        source.searchInput.addEventListener("input", () => {
          clearTimeout(timer);
          timer = setTimeout(apply, 120);
        });
        source.searchInput.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            clearTimeout(timer);
            apply();
            gotoHit(meta.id, event.shiftKey ? -1 : 1);
          } else if (event.key === "Escape") {
            source.searchInput.value = "";
            clearTimeout(timer);
            apply();
          }
        });
        source.searchInput.addEventListener("focus", () => setLastPane(meta.id));
      }
    }
    if (source.menuBtn && source.menu) {
      source.menuBtn.addEventListener("click", () => {
        const open = !src().menuOpen;
        closeMenus(store, meta.id);
        src().menuOpen = open;
        updateLogIndicators(store);
      });
    }
    buildMenu(store, meta);
    refreshModuleOptions(source);

    if (source.el) {
      source.el.addEventListener("scroll", () => {
        const el = source.el;
        setFollowState(store, meta.id, el.scrollTop + el.clientHeight >= el.scrollHeight - 8);
        updateLogIndicators(store);
      });
      // Alt+点一行：展开/收起那一组重复（折叠只是显示，所以这里也不需要动证据）。
      source.el.addEventListener("click", (event) => {
        if (event.altKey) altClickExpand(store, meta.id, event);
      });
      // 滚轮到此为止：到边界就拦下来并晃一下，不把滚动链给主页面。
      // （同一个接线也用在结果列表与历史运行那一窗上，见 `edgeWheelTarget`。）
      edgeWheelTarget(source.el);
      // 鼠标拖动选择时，只在正在拖的这个窗口里能选到文本。
      source.el.addEventListener("mousedown", (event) => {
        if (event.button === 0) beginPaneSelection(meta.id);
      });
      // 记住最近操作过的窗口，给 Alt+L 与 Ctrl+F 用；同时也是“当前窗口”高亮的依据。
      source.el.addEventListener("mouseenter", () => setLastPane(meta.id));
      source.el.addEventListener("focusin", () => setLastPane(meta.id));
    }
    // 鼠标停到窗口的任何地方（包括工具条）都算“在看这一窗”。
    if (source.pane) {
      source.pane.addEventListener("mouseenter", () => setLastPane(meta.id));
      source.pane.addEventListener("focusin", () => setLastPane(meta.id));
    }
  }

  // 松开鼠标就恢复可选：不管指针最后飞到哪里（拖到窗口外、拖到浏览器外面）。
  document.addEventListener("mouseup", endPaneSelection);
  window.addEventListener("blur", endPaneSelection);
  // 在窗口**外面**按下鼠标（或者上一次拖拽没收到 mouseup）：立刻取消“只在某个窗口里选”的约束，
  // 否则页头/其它区域会一直选不动。捕获阶段先跑，窗口自己的 mousedown 随后再重新设置。
  document.addEventListener("mousedown", (event) => {
    if (!selectingPane) return;
    const host = store.sources[selectingPane] && store.sources[selectingPane].el;
    const target = event.target instanceof Element ? event.target : null;
    if (!host || !target || !host.contains(target)) endPaneSelection();
  }, true);
  // 拖动中把选区夹住：浏览器会把选区一路扩到别的窗口甚至整页。
  // ⚠️ 不要改用 `selectstart` + preventDefault 去拦：Chrome 的 selectstart 目标是
  // 选择根（常常是 body）而不是被拖的那一行，一拦就变成“窗口里根本选不中”。
  document.addEventListener("selectionchange", () => {
    if (!selectingPane) return;
    const source = store.sources[selectingPane];
    if (source) clampSelection(source.el);
  });

  // 点面板外面就收起来：菜单开着的时候看不到日志是很烦的。
  // ⚠️「排除」按钮也是这个面板的**入口**（没填过条件时点它就是要打开面板），
  //   所以它必须与「选项」按钮一样算“里面” —— 否则那一下点击会先开、再被这条规则
  //   当场关掉（真机验收里就是“点了排除，光标却没进去、面板也没打开”）。
  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    for (const id of SOURCE_IDS) {
      const source = store.sources[id];
      if (!source.menuOpen) continue;
      const inside = target
        && (target.closest(`#panel-${id}`) || target.closest(`#menu-${id}`)
          || target.closest(`#exclude-${id}`));
      if (inside) continue;
      source.menuOpen = false;
    }
    updateLogIndicators(store);
  });

  updateLogIndicators(store);
  applyLayout(store);
  // DOM 只有这一份：把它分给另外两个模式，换模式时才画得出来。
  shareDomRefs(store);
}

/* 用户口径（2026-10-10）：顶栏那排“板 A / 板 B / STM32F103C8 …”设备芯片**整块删掉、不再使用**
 *（`renderDevices` / `DEVICE_LABEL` / `.chip` 一起走）。理由是它们与设备嗅探卡片说的是同一件事，
 * 而且“板 A / 板 B”是本机台架的名字 —— 设备事实一律以**本次嗅探的卡片**为准（口 + 身份 + 兼容性）。
 * 下面紧接着就是  / D5 的设备嗅探与卡片。 */

/* ====================================================  / D5：设备嗅探与卡片
 *
 * 一条规矩：**嗅探只发生在用户点“设备嗅探”的那一刻** —— 页面加载、2 s 状态轮询、
 * 换模式都不许顺手去问设备（需求 §3.1：没有用户动作就没有端口访问）。
 * 这一段只做三件事：把服务端给的结果画成卡片、记住用户选的那一张、把选择交给 run。
 *
 * ⚠️ 选中的是“**本次嗅探的序号 + 口**”，不是“哪块板”：序号只是这一次的显示编号
 *   （`scan_seq_persistent=false`），服务端在 `POST /api/run` 之前会拿它重新复核一遍
 *   （序号真有、口对得上、口可用、固件兼容），页面说了不算。
 */

/* 与 core 的 `IDENTITY_STATES` 一一对应。措辞说的是**这一次问到的结果**，
 * 不是“这块设备坏了” —— “没问到”是一类正常设备（空板/旧固件），首烧路径要从这里过。 */
const SNIFF_IDENTITY_LABEL = {
  confirmed: "已确认身份",
  unknown: "没问到身份",
  conflict: "与预期不符",
  unqueryable: "问不了身份",
};

function sniffCardKey(card) {
  return `${Number(card && card.num) || 0}|${String((card && card.port) || "")}`;
}

/* 本次嗅探里选中的那一张卡片（2 s 轮询会把 `app.sniff` 整块换掉，所以每次都按 key 重找）。 */
function selectedSniffCard() {
  const last = (app.sniff && app.sniff.last) || null;
  const cards = last && Array.isArray(last.cards) ? last.cards : [];
  const key = app.selectedCard ? app.selectedCard.key : "";
  return cards.find((card) => sniffCardKey(card) === key) || null;
}

/* 服务端为这张卡片算好的**逐固件兼容性结论**（`/api/state.sniff.cards[].firmwares`）。
 * ⚠️ 它是**对象列表**（`{firmware_id, allowed, reason, first_flash, target_id, project}`），
 *    **不是** id 列表：服务端把每一份固件都列出来并给出结论与理由。
 *    页面只取 `allowed === true` 的那几份 —— 不自己比名字/协议（那是服务端的唯一判断）。 */
function sniffCardFirmwares(card) {
  const rows = card && Array.isArray(card.firmwares) ? card.firmwares : [];
  // 只留 id：这个函数的用处就是"这台设备**能跑**哪几份固件"（给固件下拉框筛）。
  return rows
    .filter((row) => row && row.allowed === true)
    .map((row) => String(row.firmware_id));
}

/* 这张卡片为什么不能跑：写**服务端给的理由**（不许自己编一个）。
 * 一份都不能用时，把第一条不适合的理由摆出来 —— 那是能行动的那一句
 * （“设备自报的项目是 rtthread，这份固件属于 示例项目”），“没有兼容固件”不是。 */
function sniffCardFirmwareWhy(card) {
  const rows = card && Array.isArray(card.firmwares) ? card.firmwares : [];
  const ok = rows.filter((row) => row && row.allowed === true);
  if (ok.length) {
    const names = ok.slice(0, 2).map((row) => String(row.firmware_id));
    return `可用固件：${names.join("、")}${ok.length > 2 ? " …" : ""}`;
  }
  const first = rows.find((row) => row && row.reason);
  return first ? `不可用：${first.reason}` : "没有能用在它上面的固件";
}

function renderSniffCards() {
  const host = $("sniffCards");
  if (!host) return;
  const last = (app.sniff && app.sniff.last) || null;
  const cards = last && Array.isArray(last.cards) ? last.cards : [];
  // 重新嗅探（或换了候选口）⇒ 旧选择作废。序号是"这一次的"编号，跨轮复用没有意义。
  if (app.selectedCard && !cards.some((card) => sniffCardKey(card) === app.selectedCard.key)) {
    app.selectedCard = null;
  }
  host.textContent = "";
  host.hidden = cards.length === 0;
  const sniffedAt = last && last.ended_utc ? last.ended_utc : "";
  host.dataset.sniffId = last ? String(last.started_utc || "") : "";
  /* 悬停里给“这些卡片是哪一次检测的、为什么少几个口、怎么翻”（用户口径 2026-10-12：
   * 屏幕上不再摆“上一次的结果 / 不是刚问的”这类解释，但它们仍然是事实，进悬停）。 */
  host.title = cards.length
    ? [
      `最近一次检测：${sniffedAt ? shortTime(sniffedAt) : "刚刚"}`,
      sniffSkippedLine(last),
      "一屏最多完整显示 3 张，更多时把鼠标放在这里滚滚轮（逐卡移动）",
    ].filter(Boolean).join("\n")
    : "";
  for (const card of cards) {
    const key = sniffCardKey(card);
    const selected = Boolean(app.selectedCard && app.selectedCard.key === key);
    const unusable = !card.available;
    const firmwares = sniffCardFirmwares(card);
    const noFirmware = firmwares.length === 0;
    const stateLabel = SNIFF_IDENTITY_LABEL[card.identity_state] || String(card.identity_state || "未知");
    /* D8-R（§11.1）：卡片常驻**只有两层** ——
     *   ① `通信口 + 设备名称`：名称就是设备自报的两项（芯片 + 项目），一个字都没改造；
     *   ② `身份状态 + 软件版本`。
     * 序号/最近活动与历史会话/收到字节/可用固件清单/拒绝原因/硬件版本 → 悬停 `title`。
     * ⚠️ 名称缺失时不编名字（卡片就只写口），未自报由第二行的状态词负责说。 */
    const fields = (card.observation && card.observation.identity_fields) || {};
    const name = [fields.mcu, fields.project].filter(Boolean).join(" · ");
    const line2 = [
      stateLabel,
      card.software_version ? String(card.software_version) : "",
      unusable ? "不可用" : "",
    ].filter(Boolean).join(" · ");
    const activity = card.last_activity_utc
      ? `最近活动 ${shortTime(card.last_activity_utc)}${card.last_activity_note ? " · " + card.last_activity_note : ""}`
      : "最近活动：没有记录";
    const why = unusable
      ? (card.available_reason || "这个口现在不能用")
      : sniffCardFirmwareWhy(card);
    const label = document.createElement("label");
    /* ⚠️ **可选性只看“这个口现在能不能用”**，不看“有没有匹配的固件”：
     * “身份未知 / 没有对应固件”是**实时观察**里一条正当的路（原始串口模式，连上再问身份），
     * 而“没有可用固件”只影响自动验证那一栏。以前把两者捆在一起，于是本仓库这种
     * “只观察、不发运构建链”的目标永远选不中卡片（实测踩到）。
     * 没有匹配固件这件事**没删**：它仍然写在卡片悬停的 `why` 里。 */
    label.className = `sniff-card${selected ? " selected" : ""}${unusable ? " unusable" : ""}`;
    label.dataset.state = String(card.identity_state || "unknown");
    label.dataset.key = key;
    /* 悬停里把**被卡片省掉的那些事实**给出来（一个都没删）：硬件版本、可用固件清单。
     * ⚠️ 用户口径 2026-10-12：序号的含义与“逐份固件判定”这类开发信息不再进悬停 ——
     * 屏幕上那个 `#N` 就是卡片自己的显示编号，不需要解释一句原理。 */
    label.title = [
      `#${Number(card.num) || 0} ${String(card.port || "")}`.trim(),
      card.hardware_version ? `硬件 ${card.hardware_version}` : "",
      card.software_version ? `版本 ${card.software_version}` : "",
      activity,
      why,
    ].filter(Boolean).join("\n");
    // 原生 radio：高亮 + 勾选 + 方向键 + 读屏器全部用浏览器自带的（不自己发明一套交互）。
    label.innerHTML = `
      <input type="radio" name="sniffDevice" value="${escapeHtml(key)}"${
        selected ? " checked" : ""}${unusable ? " disabled" : ""}>
      <span class="sniff-head">
        <span class="sniff-num">#${Number(card.num) || 0}</span>
        <span class="sniff-port">${escapeHtml(String(card.port || ""))}</span>
        <span class="sniff-name">${escapeHtml(name)}</span>
      </span>
      <span class="sniff-state">${escapeHtml(line2)}</span>`;
    host.appendChild(label);
  }
  /* 稳定那一行提示（嗅探进行中的临时文案不盖）：只说**当前状态**（选了谁 / 还没选）。
   * ⚠️ 用户口径 2026-10-12：空闲时不摆解释 —— “这些卡片是哪一次检测的、为什么少几个口”
   *    在卡片那一格的悬停里（`#sniffCards.title`）。 */
  const hint = $("sniffHint");
  if (hint && !app.sniffBusy) {
    const picked = selectedSniffCard();
    hint.dataset.kind = "";
    const base = picked
      ? `已选 ${String(picked.port || "")}`.trim()
      : (cards.length ? "还没选设备" : "");
    hint.textContent = base;
    hint.hidden = !base;
  }
}

/* 用户在卡片里点了一个设备：记住它，并让固件列表跟着**服务端的兼容性结论**重算。 */
function selectSniffCard(value) {
  const last = (app.sniff && app.sniff.last) || null;
  const cards = last && Array.isArray(last.cards) ? last.cards : [];
  const card = cards.find((row) => sniffCardKey(row) === String(value)) || null;
  app.selectedCard = card
    ? {
        key: sniffCardKey(card),
        num: Number(card.num) || 0,
        port: String(card.port || ""),
        scan_seq: Number(card.scan_seq) || 0,
        state: String(card.identity_state || ""),
      }
    : null;
  renderSniffCards();
  // 目标与固件都跟着这次选择重算（`renderRunFirmwares` 会按卡片重填固件并同步目标）。
  renderRunCatalog(app.webCatalog || {}, true);
  // D8：换了设备，这一屏的“端口/身份就绪”与按钮状态要立刻跟上；实时观察那一屏还要多一句
  // “连着的是哪个口”（见 `observeSelectionNote`）—— 换了选择不许还挂着上一台设备的说法。
  updateButtons();
  renderObserve(app.observe);
}

/* 这次 run 点名的设备（`{scan_seq, port}`）—— 没选就返回 null（走老路径：按目标声明的口）。 */
function runDeviceSelection() {
  const card = app.selectedCard;
  if (!card) return null;
  return { scan_seq: Number(card.scan_seq) || 0, port: String(card.port || "") };
}

/*  / D8：嗅探按钮的三态 —— 嗅探 / 正在嗅探 / 嗅探完成。
 * ⚠️ 三态**尺寸必须一样**（尺寸由 CSS 定死 min-width/height，不由文字长度决定）：
 *    尺寸一变，它所在的那条横向区域会跟着跳，用户正在看的那排卡片会被推走。
 * ⚠️ 旋转图标只管“正在”，并遵守 `prefers-reduced-motion`（在 CSS 里）。
 * ⚠️ “完成”是一次提示，不是常驻状态：过几秒退回“嗅探”（用户要再嗅就再点）。 */
const SNIFF_STATE_TEXT = { idle: "嗅探", busy: "正在嗅探", done: "嗅探完成" };
const SNIFF_DONE_MS = 6000;

function setSniffButton(state) {
  const button = $("sniffBtn");
  if (!button) return;
  const wanted = SNIFF_STATE_TEXT[state] ? state : "idle";
  button.dataset.state = wanted;
  const label = $("sniffLabel");
  if (label) label.textContent = SNIFF_STATE_TEXT[wanted];
  else button.textContent = SNIFF_STATE_TEXT[wanted];   // 旧页面（没有 label 节点）也还能用
  button.disabled = wanted === "busy";                  // 重复点击不并发（再叠一道 app.sniffBusy）
  if (app.sniffDoneTimer) {
    clearTimeout(app.sniffDoneTimer);
    app.sniffDoneTimer = 0;
  }
  if (wanted === "done") {
    app.sniffDoneTimer = setTimeout(() => setSniffButton("idle"), SNIFF_DONE_MS);
  }
}

/* 需求 §12.3：进度快照 → 页面上那一行。
 *
 * ⚠️ 四个状态与核心的 `SNIFF_STATES` 一一对应（idle / running / completed / failed）；
 *    `percent` 用**服务端派生的那个值**（唯一算法在服务端 `derived_percent()`），
 *    页面不另算一份当事实 —— 这里只把 `percentDerived` 一并给出来，供宿主判据
 *    钉住"两边算的是同一个数"。
 * ⚠️ 它只读进度快照，**不读 `last`**：上一轮的完整结果与"本轮走到哪"是两类事实。
 * ⚠️ 纯函数：喂它一个 `sniff` 块就行（模型回归与浏览器 E2E 都直接驱动它）。 */
const SNIFF_PROGRESS_STATES = ["idle", "running", "completed", "failed"];

function sniffProgressModel(sniff, busy) {
  const block = sniff || {};
  const row = block.progress || null;
  const state = SNIFF_PROGRESS_STATES.includes(String(row && row.state))
    ? String(row.state)
    : "idle";
  const total = Math.max(0, Number((row && row.total_ports) || 0));
  const done = Math.min(total, Math.max(0, Number((row && row.completed_ports) || 0)));
  const port = String((row && row.current_port) || "");
  const percent = Math.min(100, Math.max(0, Number((row && row.percent) || 0)));
  const reason = String((row && row.reason) || "");
  const percentDerived = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  /* 点下去了、但服务端还没把那一轮摆到快照里（轮询 2 s 一拍）⇒ 先说“正在嗅探”，
   * 否则用户点一下看不到任何反应。⚠️ 这不是自己编进度：它只在**本轮还没有数字**时说一句。 */
  const waiting = state !== "running" && Boolean(busy);
  /* 失败要有可操作的提示（不得永久停在“正在嗅探”，也不得默默回到“嗅探”）；
   * 跑完但那句话里有事（例：结果没能落盘）**照样说出来** —— 不静默降级。 */
  let text = "";
  let kind = "";
  if (state === "failed") {
    text = reason ? `嗅探失败：${reason}` : "嗅探失败：这一轮没有正常收口";
    kind = "fail";
  } else if (state === "completed" && reason) {
    text = `嗅探完成，但${reason}`;
    kind = "warn";
  } else if (state === "running") {
    text = port ? `正在嗅探 ${port} · ${done}/${total}` : `正在嗅探 · ${done}/${total}`;
  } else if (waiting) {
    text = "正在嗅探…";
  } else if (state === "completed") {
    /* 跑完留一条**能看出「哪一次」**的记录：完成数 + 上一次完成时刻（北京时间，与时区无关）。
     * ⚠️ 用户实测：连点两次嗅探，屏幕与上一轮逐字节相同 ⇒ 「看起来没重新启动」。 */
    const parts = beijingParts(row && row.updated_utc);
    text = [`嗅探完成 · ${done}/${total}`, parts ? parts.clock : ""].filter(Boolean).join(" · ");
  }
  /* ⚠️ 等待新快照时**不沿用上一轮的数字**（用户实测：刚跑完 100%，再点时条子仍然满着，
   * 看着像「已经跑完了 / 没动」）。 */
  const shownPercent = waiting ? 0 : percent;
  const visible = state === "failed" || state === "running" || state === "completed" || waiting;
  return {
    state, total, done, port, percent: shownPercent, percentDerived,
    reason, visible, text, kind, waiting,
  };
}

/* 把模型画到那一行上。
 * ⚠️ 用 `visibility` 而**不是** `hidden`：这一行**常驻占着那一行的高度**（行高由 CSS 定死），
 *    出现/消失不再把下面的卡片行与四窗整体推一下（用户实测：每次开始嗅探页面跳 22 px）。
 * ⚠️ 文字只改 `textContent`，不动态插节点，也不改宽度。 */
function renderSniffProgress() {
  const host = $("sniffProgress");
  if (!host) return;
  const model = sniffProgressModel(app.sniff, app.sniffBusy);
  host.style.visibility = model.visible ? "visible" : "hidden";
  host.dataset.kind = model.kind;
  const text = $("sniffProgressText");
  if (text) text.textContent = model.visible ? model.text : "";
  const bar = $("sniffProgressBar");
  if (bar) {
    bar.value = model.percent;
    /* 读屏器读的是这一句（进度条自己的 value 只是个 0–100 的数字）。 */
    if (model.visible) bar.setAttribute("aria-valuetext", model.text);
    else bar.removeAttribute("aria-valuetext");
  }
}

/* 这一轮有没有“声明了但当前不存在”的口被跳过（用户口径 2026-10-10：不展示）。
 * 用服务端给的**结构化**字段（`last.skipped`）拼一句短话；完整那句话在证据与 `notes` 里。
 * ⚠️ 不靠解析 prose：那句长说明里有个括号与逗号，从里面抠端口号就是在自找麻烦。 */
function sniffSkippedLine(last) {
  const rows = (last && Array.isArray(last.skipped)) ? last.skipped.map(String).filter(Boolean) : [];
  if (!rows.length) return "";
  return `另有 ${rows.length} 个登记的口当前不存在：${rows.join("、")}`;
}

async function sniffDevices() {
  const button = $("sniffBtn");
  if (app.sniffBusy) return;
  app.sniffBusy = true;
  const selectedPortBeforeSniff = app.selectedCard ? String(app.selectedCard.port || "") : "";
  setSniffButton("busy");
  renderSniffProgress();          // 点下去先把“正在嗅探…”摆上（不等 2 s 轮询）
  const hint = $("sniffHint");
  if (hint) {
    // 用户口径 2026-10-12：进行中**只显示动画**（按钮上那个转环），不摆说明。
    hint.textContent = "";
    hint.hidden = true;
  }
  try {
    await postJson("/api/sniff", {});
    app.sniffedHere = true;
    // ⚠️ 画之前**先读一次状态**：`POST /api/sniff` 的返回是"这一次的结果"，
    //    而卡片上那句"哪些固件能用"是 `/api/state` 里由服务端加上去的（`compatible_firmwares`
    //    —— 全平台唯一的兼容性判断）。直接拿 POST 的返回画，卡片会有一瞬间变成
    //    "没有可用的固件"（甚至一张卡片都没有）—— 那是假的，只是数据还没到。
    await refreshState();
    const cards =
      (app.sniff && app.sniff.last && app.sniff.last.cards) || [];
    // 显示序号每轮都会重排，不能沿用旧序号；通信口是本机台架内的稳定选择键。
    // 同一口仍在且可用时，把选择迁移到本轮的新卡片；真的消失/不可用才清掉。
    const samePort = cards.find((card) =>
      String(card.port || "").toUpperCase() === selectedPortBeforeSniff.toUpperCase()
      && card.available === true);
    app.selectedCard = samePort ? {
      key: sniffCardKey(samePort),
      num: Number(samePort.num) || 0,
      port: String(samePort.port || ""),
      scan_seq: Number(samePort.scan_seq) || 0,
      state: String(samePort.identity_state || ""),
    } : null;
    const usable = cards.filter((card) => card.available).length;
    // ⚠️ 卡片数 = **展示出来的设备数**（当前不存在的口已经不造卡片了）—— 所以这里说“台设备”。“少掉
    //    哪几个口”由下面那一行**稳定的**提示说（`sniffSkippedLine`）：写进这里会被下一次重画冲掉。
    if (hint) {
      hint.textContent = `看到 ${cards.length} 台设备 · ${usable} 台可用`;
      hint.dataset.kind = "";
    }
    /* ⚠️ **一个候选项都没有**时要说清下一步怎么做，而不是留一句“0 台设备”让人自己猜。
     * 候选口 = 系统串口枚举 ∪ 本机台架点名的口；两者都空才到这里，
     * 而最常见的原因是“本机枚举不出来”（没装 pyserial）—— 所以先把这两条路都说出来。 */
    if (cards.length === 0) {
      showMessage(
        "没有候选通信口：本机没枚举到串口（装了 pyserial 才能枚举：pip install pyserial），"
        + "本机台架里也没点名任何口。要把某个口固定下来，把 bench.example.json 抄一份成 bench.local.json，"
        + "填上 devices.<设备键>.port（也可以填 pyserial 的 URL，例如 loop:// 或 socket://主机:端口），"
        + "再重启服务；平台不会按枚举顺序替你猜设备。",
        "warn",
      );
      setSniffButton("done");
      return;
    }
    showMessage(`设备嗅探完成：${cards.length} 台设备，${usable} 台可用`, "");
    setSniffButton("done");
  } catch (error) {
    if (hint) {
      hint.textContent = `嗅探失败：${error.message}`;
      hint.dataset.kind = "fail";
    }
    showMessage(`设备嗅探失败：${error.message}`, "fail");
    setSniffButton("idle");
  } finally {
    app.sniffBusy = false;
    renderSniffProgress();
    updateButtons();
  }
}

/*  / D8：卡片区的“逐卡滚动”。
 * 一屏最多完整展示 3 张（CSS 定），更多的靠**滚轮**一张一张地移，到位后自动吸附；
 * 滚轮在这一格里不再带动页面（鼠标停在这里就是想看下一张卡）。
 * ⚠️ 只有在**真的滚得动**（卡片多于能完整展示的张数）时才接手：否则用户会被困在这一格
 *    （页面滚不动、里面也没得滚）。
 * ⚠️ 不提供左右移动按钮：用户明确说不要。 */
function sniffCardStep(host) {
  if (!host) return 0;
  const card = host.querySelector(".sniff-card");
  if (!card) return 0;
  const gap = parseFloat(getComputedStyle(host).columnGap) || 0;
  const width = card.getBoundingClientRect().width;
  return (width + gap) || (host.clientWidth / 3);
}

function wireSniffCardsWheel() {
  const host = $("sniffCards");
  if (!host) return;
  host.addEventListener("wheel", (event) => {
    const max = host.scrollWidth - host.clientWidth;
    if (max <= 1) return;                       // 装得下：滚轮照旧滚页面
    const delta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
    if (!delta) return;
    const step = sniffCardStep(host);
    if (!step) return;                           // 没有卡片时不抢滚轮
    event.preventDefault();                      // 页面本身不跟着滚
    const next = host.scrollLeft + (delta > 0 ? step : -step);
    host.scrollTo({ left: Math.max(0, Math.min(max, next)), behavior: "smooth" });
  }, { passive: false });
}

/* 自动验证屏的“端口 + 身份就绪”状态（ / D8）：
 * 与设备卡片同一条横向区域，只说事实、**不放连接按钮**（连接/重连/断开是实时观察的事，
 * 在控制面板里（左选口/卡片，右状态与动作，底部紧凑行）。内容全部来自服务端与本次嗅探：选中的卡片、兼容性结论、
 * 这次 run 会用哪个口、以及被别的会话占着时的那句拒绝原话。 */
function renderPortStatus() {
  const host = $("portStatus");
  if (!host) return;
  if (app.mode !== "auto") {
    // 实时观察屏由 `#observeState` 说连接状态 —— 这一格不重复说一遍。
    host.hidden = true;
    host.textContent = "";
    return;
  }
  host.hidden = false;
  host.textContent = "";
  const add = (text, kind, extra) => {
    const span = document.createElement("span");
    span.className = `port-item ${extra || ""}`.trim();
    if (kind) span.dataset.kind = kind;
    span.textContent = text;
    host.appendChild(span);
    return span;
  };
  const last = (app.sniff && app.sniff.last) || null;
  const cards = last && Array.isArray(last.cards) ? last.cards : [];
  const card = selectedSniffCard() || app.selectedCard;
  /* 用户口径（2026-10-11）：「还是很多调试语句」——这一格的铁律现在是：
   * 屏幕上只回答三个问题（**能不能做 / 结果是什么 / 此刻的数字**），其余不进常驻屏幕：
   *   ① **怎么做**（教学语）：按钮自己写着“嗅探”，它的 title 里也说了“只读…不烧录”；
   *      “点左边的嗅探让平台去问一遍”属于看一次就够的话；
   *   ② **同一句话的第二遍**：这一格与嗅探按钮旁那行提示说的是同一件事（见 `sniffHint`）；
   *   ③ **下一步会怎样**：那正是下面“本次 run 的通信口 …”这一行要说的事。 */
  if (!cards.length) {
    add("还没做设备嗅探", "warn");
  } else if (!card) {
    add("还没选设备", "warn");
  } else {
    const port = add(String(card.port || ""), "ok", "port-port");
    port.title = `#${Number(card.num) || 0} ${String(card.port || "")}`.trim();
    const state = SNIFF_IDENTITY_LABEL[card.identity_state] || String(card.identity_state || "未报告");
    add(state, card.identity_state === "confirmed" ? "ok" : (card.identity_state === "conflict" ? "fail" : ""));
    if (!card.available) {
      add(card.available_reason || "这个口现在不能用", "fail", "port-why");
    } else {
      const firmwares = sniffCardFirmwares(card);
      add(firmwares.length ? `可用固件 ${firmwares.length} 份` : "没有可用的固件",
        firmwares.length ? "ok" : "fail");
    }
  }
  // 这次 run 会用哪个口（选卡片就点名卡片，没选就按本机登记的端口）+ 它是不是被占着。
  const port = runTargetPort();
  add(`本次 run 的通信口 ${port || "未声明"}`);
  const conflict = liveRunConflict(app.observe || {}, port);
  if (conflict) add(conflict, "warn", "port-why");
}

/* ---------------------------------------------------------- 自动验证目录 */
function optionRows(select, rows, selected) {
  if (!select) return;
  select.textContent = "";
  for (const row of rows) {
    const option = document.createElement("option");
    option.value = row.value;
    option.textContent = row.label;
    option.disabled = Boolean(row.disabled);
    option.selected = row.value === selected;
    select.appendChild(option);
  }
}

function selectedRunFacts() {
  const catalog = app.webCatalog || {};
  const targetId = $("runTarget")?.value || "";
  const firmwareId = $("runFirmware")?.value || "";
  const suite = $("runSuite")?.value || "";
  const target = (catalog.targets || []).find((row) => row.target_id === targetId) || null;
  const firmware = (catalog.firmwares || []).find((row) => row.firmware_id === firmwareId) || null;
  const project = firmware ? (catalog.projects || {})[firmware.project] || null : null;
  return { targetId, firmwareId, suite, target, firmware, project };
}

/* 这次 run 会用哪个串口：**优先本次嗅探里选中的那张卡片**（ / D5：
 * “通信口是运行时选出来的”，不是从静态的“板 A/板 B”推出来的）；
 * 没选卡片时才退到老的设备登记（每个目标的 device_key → 那块板的口）。
 * 取不到就返回空串 —— 调用方**不许**拿它当"没有冲突"，服务端还会再拒一次。 */
function runTargetPort() {
  const card = app.selectedCard;
  if (card && card.port) return String(card.port);
  const facts = selectedRunFacts();
  const key = (facts.target && facts.target.device_key) || "";
  if (!key) return "";
  const row = (app.devices || []).find((device) => device.key === key);
  return row ? String(row.port || "") : "";
}

/* 纯函数：实时观察会不会挡住这次 run —— **只比端口**，与服务端 `ObserveSession.port_conflict`
 * 同一条判据（逐字符比 `.upper()`；同一个串口不能有两个 owner）。
 *
 * ⚠️ 别写成"有实时观察就拦"：实时观察在 COM<n>（STM32）时，板 A（COM<n>）的自动验证完全能跑，
 *    而曾经写成那样 ⇒ 用户连上 COM<n> 之后"启动自动验证"一直是灰的、没法开发
 *    （2026-10-06 实测：按钮灰着、提示"被 COM<n> 占用"，可他要跑的是 COM<n>）。
 * 说不清（缺端口）时返回 false：宁可让服务端 409 拒一次，也不许凭空拦住一个能跑的 run。 */
function liveRunConflict(live, targetPort) {
  const held = (live && live.holding) ? String(live.port || "").trim().toUpperCase() : "";
  const mine = String(targetPort || "").trim().toUpperCase();
  return Boolean(held) && Boolean(mine) && held === mine;
}

function renderRunCatalog(catalog, preserve = false) {
  app.webCatalog = catalog || { targets: [], firmwares: [], projects: {}, defaults: {} };
  const defaults = app.webCatalog.defaults || {};
  const oldTarget = preserve ? $("runTarget")?.value : "";
  const targetId = oldTarget || defaults.target_id || "";
  optionRows($("runTarget"), (app.webCatalog.targets || []).map((row) => ({
    value: row.target_id,
    label: `${row.label}${row.pipeline_allowed ? "" : "（只读）"}`,
    disabled: !row.pipeline_allowed,
  })), targetId);
  /* 首次渲染的**默认固件**也由服务端定（ / D9，需求 §9.1：“自动验证和实时观察
   * 必须读取同一匹配结果”）：卡片上有唯一匹配就用它，否则退到目录声明的默认值。
   * ⚠️ 只在 `preserve=false`（首次渲染）时给 —— 用户在自动验证屏上显式选的固件不能被
   * 每 2 秒的重画改掉（那也是跨固件烧录那条路要用的选择）。 */
  const matched = observeCombination(selectedSniffCard(), (app.observe && app.observe.targets) || []);
  renderRunFirmwares(
    preserve ? $("runFirmware")?.value : (matched.firmware_id || defaults.firmware_id)
  );
}

function renderRunFirmwares(preferred = "") {
  const all = app.webCatalog?.firmwares || [];
  const card = selectedSniffCard();
  // ⚠️ 两张表共存，但**只有一份判据**：
  //  · 选了设备卡片 ⇒ 用**服务端算好的**兼容固件（`/api/state.sniff.cards[].firmwares`），
  //    页面不自己去比名字/协议（那是服务端的唯一判断）；
  //  · 没选卡片 ⇒ 老路径：按目标挑它自己的固件（未嗅探时也能干活）。
  const allowed = new Set(sniffCardFirmwares(card));
  const rows = card
    ? all.filter((row) => allowed.has(row.firmware_id))
    : all.filter((row) => row.target_id === ($("runTarget")?.value || ""));
  /* 用户口径（2026-10-13）：身份未知 / 零匹配 / 多匹配，而这张卡片上**可能烧录两份以上**固件时，
   * 平台**不替使用者挑**（挑了就可能把另一份固件烧进去）⇒ 这一栏留空，让他在「固件」里自己选；
   * 屏幕上那句话由 `startReadiness()` 给（「请选择要烧录的固件」）。
   * ⚠️ 只有一块候选时照旧自动选上（那没有歧义）；身份唯一匹配时服务端已经定了那一份。 */
  const combo = observeCombination(card, (app.observe && app.observe.targets) || []);
  const askUser = Boolean(card) && !combo.usable && rows.length > 1;
  const firmwareId = askUser
    ? ""
    : (rows.some((row) => row.firmware_id === preferred) ? preferred : (rows[0]?.firmware_id || ""));
  optionRows($("runFirmware"), rows.map((row) => ({ value: row.firmware_id, label: row.label })), firmwareId);
  syncRunTargetFromFirmware();
  renderRunSuites();
}

/* 目标不再由人选：它由**所选固件**推导（固件属于哪个目标适配器是那份固件的事实）。
 * 这个 select 只是把推导结果写出来，让 `selectedRunFacts()` / `runTargetPort()` 照旧工作 ——
 * 页面少一个人选的维度，就少一处“选的目标与选的固件对不上”的错。 */
function syncRunTargetFromFirmware() {
  const box = $("runTarget");
  if (!box) return;
  const firmwareId = $("runFirmware")?.value || "";
  const row = (app.webCatalog?.firmwares || []).find((item) => item.firmware_id === firmwareId);
  if (row && row.target_id) box.value = row.target_id;
}

function renderRunSuites() {
  const facts = selectedRunFacts();
  const targetSuites = new Set(facts.target?.suites || []);
  const suites = (facts.project?.suites || []).filter((name) => targetSuites.has(name));
  const preferred = $("runSuite")?.value || app.webCatalog?.defaults?.suite || "";
  const suite = suites.includes(preferred) ? preferred : (suites[0] || "");
  optionRows($("runSuite"), suites.map((name) => ({ value: name, label: name })), suite);
  const observe = facts.project?.observe || {};
  const seconds = observe.by_suite?.[suite] || observe.default_seconds;
  // 采集窗口的**生效区间由 core 下发**（）：HTML 里那个 `max` 只是一个
  // 无 JS 时的兑底值。写死一个比 core 小的上限，用户输入 240 会被浏览器自己挡回去，
  // 而确认框还会把他输的值拄一遍——看起来像是“要到了”。
  const windowBox = $("observeSeconds");
  if (windowBox) {
    const limits = app.observeLimits || {};
    if (limits.min_seconds) windowBox.min = String(limits.min_seconds);
    if (limits.max_seconds) windowBox.max = String(limits.max_seconds);
    // ⚠️ 默认值**只在选择真的变了的那一刻**写一次（ 的同一个坑）：
    // 这个函数每 2 秒（`/api/state` 轮询）都会再跑一遍，无条件写会把你刚输的秒数
    // 悄悄清回项目包默认值 —— 这正是"240 输不进去"的真凶（浏览器实测：输完 5 s 内被打回）。
    const windowSignature = `${facts.targetId}|${facts.firmwareId}|${facts.suite}`;
    if (seconds && windowBox.dataset.selection !== windowSignature) {
      windowBox.value = String(seconds);
      windowBox.dataset.selection = windowSignature;
    }
  }
  const flashField = $("flashConfirmField");
  const flashInput = $("flashConfirm");
  const resetBox = $("resetPulse");
  if (flashField) flashField.hidden = !facts.firmware?.requires_flash_confirmation;
  // ⚠️⚠️ 这个函数每 2 秒（`/api/state` 轮询）都会被再跑一遍，所以"把两个勾选框恢复成默认值"
  // 只能发生在**选择真的变了**的那一刻（）：无条件 `checked = false` 会把用户刚勾的
  // 那一下悄悄清掉 —— 他点"启动自动验证"时被拒（"请先勾选逐次确认"），而屏幕上那个框看起来
  // 是空的、他根本不知道自己什么时候被清的。判据用 `target|firmware|suite` 的签名，
  // 与 `renderObserveTargets` 的 `dataset.signature` 同一个做法。
  const signature = `${facts.targetId}|${facts.firmwareId}|${facts.suite}`;
  const host = flashInput || flashField;
  const changed = Boolean(host) && host.dataset.selection !== signature;
  if (host) host.dataset.selection = signature;
  // 覆盖 Flash 的逐次授权：只在选择变化时清空（"每次换固件都要重新确认"）。
  if (flashInput) {
    if (changed) flashInput.checked = false;
    if (!facts.firmware?.requires_flash_confirmation) flashInput.checked = false;
  }
  // 复位默认值来自**这个目标自己的声明**（`reset.default_request`）：串口调制线的复位
  // 默认开（A1 要完整启动横幅），走 SWD 探针的复位默认关（会占住探针，）。
  // ⚠️ 页面只把这个值**画出来**：真正的默认值在服务端（调用方没明说时按目标的声明走），
  // 所以拿 curl 直接 POST 也得不到"平台替用户决定的复位"。
  // ⚠️ "默认关" ≠ "禁止"：选择变化之后用户显式勾上的复位不会被轮询清掉
  //（要 SWD 复位就得自己承担探针风险， 说得很清楚）。
  if (resetBox) {
    if (changed) {
      resetBox.checked = Boolean(facts.target?.reset?.default_request);
      // 覆盖 Flash 的固件**默认**不复位（附加复位与烧录共用 ST-LINK）。
      if (facts.firmware?.requires_flash_confirmation) resetBox.checked = false;
    }
    resetBox.title = facts.target?.reset
      ? `复位计划：走 ${facts.target.reset.adapter || "（未声明）"}；`
        + `默认${facts.target.reset.default_request ? "复位" : "不复位"}.`
        + (facts.target.reset.note ? ` ${facts.target.reset.note}` : "")
      : "当前目标没有复位声明：默认不复位";
  }
  renderProjectOptions(facts.project?.run_options || []);
  renderTestCases(facts);
  updateButtons();
}

function renderProjectOptions(options) {
  const host = $("runOptions");
  if (!host) return;
  const previous = projectOptionValues();
  host.textContent = "";
  for (const option of options) {
    if (option.kind !== "boolean") continue;
    const label = document.createElement("label");
    label.className = "field check";
    label.title = option.help || "项目包声明的运行参数";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.projectOption = option.key;
    input.checked = Object.hasOwn(previous, option.key) ? Boolean(previous[option.key]) : Boolean(option.default);
    label.append(input, document.createTextNode(` ${option.label}`));
    host.appendChild(label);
  }
}

function projectOptionValues() {
  const values = {};
  for (const input of document.querySelectorAll("[data-project-option]")) {
    values[input.dataset.projectOption] = Boolean(input.checked);
  }
  return values;
}

/* 这条用例能不能在**自动验证**里启动（纯函数：模型回归与 E2E 直接驱动它）。
 *
 * ⚠️ 用户口径（2026-10-17）：自动验证里**不列**那些只能在实时观察跑的用例。
 * 判据是「宿主用例（`host_scenario`）却没有恰好一个文件参数」：自动验证那一路只实现了
 * 主机协同的**镜像往返**（要上传一份镜像）；而 `system.identity_verify` 这类零参数、
 * 复用当前观察连接的用例只能在实时观察屏启动。以前它照样可勾，勾了要等**烧录之后**才在
 * observe 步报「主机场景必须声明且只声明一个文件参数」。
 * ⚠️ 与服务端 `web_catalog.autotest_can_run_here()` **同一条判据**（两处都要有：
 * 绕过页面的调用方也得被拒，而且要在烧录之前就拒）。 */
function autotestCaseRunsHere(testCase) {
  const row = testCase || {};
  if (String(row.kind || "") !== "host_scenario") return true;
  const specs = Object.values(row.parameters || {});
  return specs.filter((spec) => spec && String(spec.type || "") === "file").length === 1;
}

function renderTestCases(facts) {
  const host = $("autotestPicker");
  const body = $("autotestCaseRows");
  if (!host || !body) return;
  const signature = `${facts.targetId}|${facts.firmwareId}|${facts.suite}`;
  if (body.dataset.selection === signature) return;
  body.dataset.selection = signature;
  body.textContent = "";
  const catalog = facts.project?.test_catalog || {};
  // ⚠️ 只在自动验证里能跑的用例（去掉「硬件身份验证」这种只能在实时观察屏启动的）。
  const cases = (catalog.cases || [])
    .filter((row) => row.suite === facts.suite)
    .filter((row) => autotestCaseRunsHere(row));
  const policies = Object.fromEntries((catalog.safety_policies || []).map((row) => [row.id, row]));
  host.hidden = cases.length === 0;
  for (const testCase of cases) {
    const tr = document.createElement("tr");
    /*  §2：能不能执行由**服务端**说的 `lifecycle` 决定（四态 + 旧定义的 disabled）。
     * ⚠️ 旧载荷没这个键时才退回旧布尔 —— 页面不自已维护一套状态规则
     *    （否则 `status="draft"` 的用例会在页面上看着可选、一问服务端就被拒）。 */
    const selectable = testCase.lifecycle
      ? testCase.lifecycle === "active"
      : Boolean(testCase.enabled) && !testCase.deprecated;
    const why = String(testCase.not_selectable_reason || "");
    const choose = document.createElement("input");
    choose.type = "checkbox";
    choose.dataset.testCase = testCase.id;
    choose.dataset.version = String(testCase.version);
    choose.disabled = !selectable;
    choose.checked = selectable && Boolean(testCase.default_enabled);
    const choiceCell = document.createElement("td");
    choiceCell.appendChild(choose);
    const nameCell = document.createElement("td");
    const label = document.createElement("div");
    label.textContent = testCase.label;
    const identity = document.createElement("code");
    /* 不可选时**照抄服务端的状态名**（`draft`/`deprecated`/`retired`/`disabled`），
     * 不写死「已禁用」—— 那句话对 draft/retired 都是页面自己编的；详细理由在 hover 的 title 里。 */
    identity.textContent = `${testCase.id}@${testCase.version}`
      + (selectable ? "" : ` · ${testCase.lifecycle || "disabled"}`);
    if (!selectable && why) identity.title = why;
    nameCell.append(label, identity);
    const typeCell = document.createElement("td");
    typeCell.textContent = `${testCase.kind} · ${testCase.timeout_s}s${testCase.destructive ? " · 破坏性" : ""}`;
    const controls = document.createElement("td");
    controls.className = "test-params";
    for (const [key, spec] of Object.entries(testCase.parameters || {})) {
      const field = document.createElement("label");
      field.append(document.createTextNode(`${key} `));
      let input;
      if (spec.type === "enum") {
        input = document.createElement("select");
        for (const value of spec.choices || []) {
          const option = document.createElement("option"); option.value = value; option.textContent = value;
          input.appendChild(option);
        }
      } else {
        input = document.createElement("input");
        input.type = spec.type === "boolean" ? "checkbox" : spec.type === "file" ? "file" : "number";
        if (spec.minimum !== undefined) input.min = String(spec.minimum);
        if (spec.maximum !== undefined) input.max = String(spec.maximum);
      }
      input.dataset.testParameter = key;
      input.dataset.parameterType = spec.type;
      if (spec.default !== undefined && spec.type === "boolean") input.checked = Boolean(spec.default);
      else if (spec.default !== undefined) input.value = String(spec.default);
      field.appendChild(input); controls.appendChild(field);
    }
    if (testCase.destructive) {
      const policy = policies[testCase.safety_policy] || {};
      const region = policy.region || {};
      const confirm = document.createElement("label");
      confirm.className = "test-region";
      const input = document.createElement("input");
      input.type = "checkbox"; input.dataset.destructiveConfirm = testCase.id;
      confirm.append(input, document.createTextNode(
        ` 确认覆盖 ${region.id || "未声明区域"} [0x${Number(region.start || 0).toString(16)}, +0x${Number(region.length || 0).toString(16)}]`
      ));
      controls.appendChild(confirm);
    }
    tr.append(choiceCell, nameCell, typeCell, controls);
    body.appendChild(tr);
  }
}

async function selectedTests() {
  const rows = [];
  for (const choose of document.querySelectorAll("[data-test-case]:checked")) {
    const tr = choose.closest("tr");
    const parameters = {};
    for (const input of tr.querySelectorAll("[data-test-parameter]")) {
      const kind = input.dataset.parameterType;
      if (kind === "boolean") parameters[input.dataset.testParameter] = Boolean(input.checked);
      else if (kind === "integer" && input.value !== "") parameters[input.dataset.testParameter] = Number(input.value);
      else if (kind === "enum") parameters[input.dataset.testParameter] = input.value;
      else if (kind === "file" && input.files?.length) {
        const file = input.files[0];
        const bytes = new Uint8Array(await file.arrayBuffer());
        const digest = await crypto.subtle.digest("SHA-256", bytes);
        let binary = "";
        for (const value of bytes) binary += String.fromCharCode(value);
        parameters[input.dataset.testParameter] = {
          name: file.name, size: file.size,
          sha256: Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join(""),
          content_base64: btoa(binary),
        };
      }
    }
    rows.push({
      case_id: choose.dataset.testCase,
      version: Number(choose.dataset.version),
      parameters,
      confirm_destructive: Boolean(tr.querySelector("[data-destructive-confirm]")?.checked),
    });
  }
  return rows;
}

function renderTestResults(tests, summary) {
  const panel = $("testResultsPanel");
  const body = $("testResultRows");
  if (!panel || !body) return;
  body.textContent = "";
  panel.hidden = !Array.isArray(tests);
  if (!Array.isArray(tests)) return;
  $("testSummary").textContent = summary
    ? `${summary.passed || 0}/${summary.total || tests.length} PASS · ${summary.result || ""}` : "report.json";
  for (const row of tests) {
    const tr = document.createElement("tr");
    for (const value of [`${row.case_id}@${row.version}`, row.status, `${row.duration_ms} ms`, row.reason || "—"]) {
      const td = document.createElement("td"); td.textContent = value; tr.appendChild(td);
    }
    tr.children[1].dataset.status = row.status;
    body.appendChild(tr);
  }
}

/* --------------------------------------------------------- 实时观察（模式） */
/* 页面只做两件事：把服务端的所有权状态画出来，以及发起**显式**动作（连接/断开/重连）。
 * 没有任何一个是隐式的：不编译、不烧录、不复位、不操作 DTR/RTS、不清理设备数据。
 *
 * 下面三个文案函数是**纯函数**（宿主回归用 Node 直接驱动）：能力没实现的不许在文案里
 * 假装有 —— 一句"已连接"很容易被读成"已经在采证了"。 */
const IDENTITY_TEXT = {
  unknown: "未查询",
  querying: "查询中",
  ok: "已回应",
  timeout: "无响应",
  unsupported: "设备不支持",
};

/* ------------------------------------------------- 状态句的档位（用户口径 2026-10-10）
 * 用户提的：“平台有很多调试语句，加个按钮能选择性地展示”。
 *
 * 这些“调试语句”其实是**状态句**：本平台把服务端给的事实逐条摊在一行里（设备自报的每个
 * 字段、字典来源与 ELF 指纹、会话与证据路径、不能做什么以及为什么…）。调试的时候每一条
 * 都值钱；平时盯着屏幕看的时候，一行太长就只看得见开头的两个字。
 *
 * 所以这里加的是**用户自己按的档位**，而不是替谁判断“哪一条不重要”：
 *   简洁 = 只留结论            （**屏幕上现在固定用这一档**，2026-10-12 起）
 *   标准 = 结论 + 关键项
 *   详细 = 全量原文            （自报的机器元数据、字典指纹…都摊开，就是改之前的写法）
 * ⚠️ 这三档仍由模型回归直接驱动（喂快照 + 档位名），不是只有“当前那一档”能测。
 *
 * ⚠️⚠️ 铁律（**不许因为藏了字就说出另一句话**）：
 *   ① **结论永远显示**：“身份：已回应”“拒绝解码，不猜”“这一路不做解码”…在任何档位都在；
 *      档位只能收起**支撑结论的原始字段/次级细节**，不能收起“行还是不行”这件事。
 *   ② 被收起的原文一定挂在该元素的 `title` 上（`sayTitle()`），悬停就看得到全量，
 *      而且 title 里明说“当前这一档收起了几项”（屏幕上那句不会被读成“全都在这里了”）。
 *   ③ 档位是**显示**偏好，不是事实：服务端说的那些字一个都没被改写，也没被重算。
 *
 * ⚠️ 常量必须声明在 `app` 之前（ 的 TDZ 坑：模块求值时会读偏好）。 */
const THEME_KEY = "pw.theme";
const THEME_ORDER = ["dark", "warm"];
/* 档位只管**「详情」里铺开多少**（用户口径 2026-10-11 验收 P1）：屏幕上的结论 chip 与这一档无关 ——
 * 验收提的“主界面仅保留身份结论”必须是**结构上**成立的，不能靠用户自己选到简洁档。
 *
 * ⚠️ 2026-10-12（用户口径）：页面上那个「详情」下拉框**已删**（默认简洁）—— 所以三条定义
 * 与下面那套句子机器仍**只留一处**，但它们不再由页头控件驱动：模型回归直接喂三档。 */
const SAY_LEVELS = [
  { id: "brief", label: "简洁", sub: "详情里只留结论那一句" },
  { id: "normal", label: "标准", sub: "详情给关键项" },
  { id: "full", label: "详细", sub: "详情给全量原文：原始字段与机器元数据都摊开" },
];
const SAY_LEVEL_IDS = SAY_LEVELS.map((entry) => entry.id);
/** 页面上**没有**档位控件（2026-10-12 起）：屏幕上固定用这一档。 */
const SAY_DEFAULT = "brief";
const SAY_RANK = { brief: 0, normal: 1, full: 2 };
/* 一个片段要求的最低档位：结论 / 关键项 / 原始字段。 */
const SAY_ALWAYS = 0;
const SAY_KEY_ITEM = 1;
const SAY_RAW = 2;

function loadTheme() {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (THEME_ORDER.includes(stored)) return stored;
  } catch (error) {
    /* 本机读不到就用默认主题 */
  }
  return "dark";
}

function setTheme(themeId) {
  const next = THEME_ORDER.includes(themeId) ? themeId : "dark";
  document.documentElement.dataset.theme = next;
  document.documentElement.style.colorScheme = next === "warm" ? "light" : "dark";
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (error) {
    /* 保存失败时仍按当前选择渲染 */
  }
  paintThemeButtons(next);
  return next;
}

/* 两个主题入口的按下态（用户口径 2026-10-12）：月亮 = 深色调试、太阳 = 暖色工作台。
 * ⚠️ 图标是**内联 SVG**，含义在 `aria-label`/`title` 上 —— 不下任何字符画
 * （字符会跑进 `textContent`， 为此改过一轮）。 */
function paintThemeButtons(themeId) {
  const current = THEME_ORDER.includes(themeId) ? themeId : "dark";
  for (const button of document.querySelectorAll(".theme-btn")) {
    button.setAttribute("aria-pressed", String(button.dataset.themeId === current));
  }
  return current;
}

function loadSayLevel() {
  /* 用户口径 2026-10-12：**删掉「详情」选择栏、默认简洁** ⇒ 屏幕上固定用这一档。
   * ⚠️ 本机以前存过的别的档**不再生效**（否则用户会被永久钉在一个已经没有入口的档位上）。
   * 三档的句子机器一个字都没删：`SAY_LEVELS` / `say()` / `sayTitle()` 仍是唯一一份实现，
   * 模型回归照旧喂三档（“结论在任何档都显示、被收起的原文一定能在 title 里找回来”）。 */
  return SAY_DEFAULT;
}

/** 这一次用哪一档：句子函数都接受一个显式档位（模型回归直接喂三档，不靠本机偏好）。 */
function sayLevel(level) {
  const asked = level === undefined || level === null ? app.sayLevel : level;
  return SAY_LEVEL_IDS.includes(asked) ? asked : SAY_DEFAULT;
}

function sayLevelRow(level) {
  const id = sayLevel(level);
  return SAY_LEVELS.find((entry) => entry.id === id) || SAY_LEVELS[1];
}

/** 一个片段：`seg("文字")` = 结论（任何档位都显示）；`seg("文字", SAY_RAW)` = 只在详细档铺开。 */
function seg(text, min = SAY_ALWAYS) {
  const value = text === null || text === undefined ? "" : String(text);
  return { text: value, min: Number(min) || 0 };
}

/* 按档位把一段段拼成一句。同时给出**没过筛的全文**与**被收起的条数**：
 * 调用方把 `full` 挂到 `title` 上 —— 档位只决定“要不要铺开”，决定不了
 * “这句话到底说了什么”（原文一个字都没改）。 */
function say(level, segments) {
  const rows = [];
  for (const item of (segments || []).flat()) {
    if (!item) continue;
    const row = typeof item === "string" ? seg(item) : item;
    if (!row.text) continue;
    rows.push({ text: String(row.text), min: Number(row.min) || 0 });
  }
  const wanted = sayLevel(level);
  const limit = SAY_RANK[wanted];
  const shown = rows.filter((row) => row.min <= limit);
  return {
    text: shown.map((row) => row.text).join(" · "),
    full: rows.map((row) => row.text).join(" · "),
    hidden: rows.length - shown.length,
    level: wanted,
  };
}

/** 只要文字版（调用方不关心 title 时用它）。 */
function sayText(level, segments) {
  return say(level, segments).text;
}

/* title：被档位收起来的东西在这里交代清楚（悬停即见全量原文）。
 * `note` 是这一格本来就在 title 里说的那句话（会话号 / 证据目录…），原样保留。
 *
 * ⚠️ 屏幕上那句永远不会被读成“全都在这里了”：只要这一档真收起了东西，title 里就先说
 *    一句“当前「简洁」档收起了 N 项细节”，再说全量原文。
 * ⚠️ 页面上**没有**档位控件（2026-10-12 用户口径：删掉「详情」选择栏、默认简洁）⇒ 这句只说
 *    “收起了几项 + 完整原文在下面”，不再叫人去选另一档。
 * ⚠️ 档位从 `said.level` 取（不从当前偏好）：模型回归可以喂任意一档，
 *    那时 title 里说的必须是被喂的那一档。 */
function sayTitle(said, note = "") {
  const parts = [];
  if (note) parts.push(note);
  // 这一档没有收起任何东西（屏幕上那句就是全文）：不补一行重复同一句话的“完整原文”。
  if (!said || !said.full || said.text === said.full) return parts.join("\n");
  const folded = Number(said.hidden) || 0;
  parts.push(folded
    ? `收起了 ${folded} 项细节（完整原文就在下面）`
    : `细节收起来了（完整原文就在下面）`);
  parts.push(`完整原文：${said.full}`);
  return parts.join("\n");
}

/** 把几句拼成一块（`text` 用当前档位，`full` 是没过筛的全文，`title` 给全量 + 收起了几项）。 */
function sayBlock(saids) {
  const rows = (saids || []).filter(Boolean);
  const text = rows.map((row) => row.text).filter(Boolean).join(" · ");
  const full = rows.map((row) => row.full).filter(Boolean).join(" · ");
  const hidden = rows.reduce((sum, row) => sum + (Number(row.hidden) || 0), 0);
  return { text, full, hidden, title: sayTitle({ text, full, hidden, level: sayLevel() }) };
}

/* 端口/波特率/状态/时长/RX/TX/最近一次收发 —— 全是服务端数出来的数，不在这里重算。
 * ⚠️ D8（2026-10-10）：这一行**不再是屏幕上那一行**（它太长，会把控制区撑成两行）；
 *    屏幕上显示的是 `observeStatCompact()` + `#observeCounters`，而这一行的全文改挂在
 *    `#observeStat` 的 `title` 上（悬停可看全：会话号 / 证据目录 / 最近收发 / io 事件）。
 *    它仍然是**唯一**那份实现（不另写一份“详细版”），宿主回归直接喂它。 */
function observeStats(observe) {
  if (!observe) return "—";
  const parts = [];
  const session = observe.session || null;
  const port = session ? session.port : (observe.port || null);
  // 没连过任何设备时不要编一个端口出来（“— @ 115200”看着像已经选中了什么）。
  parts.push(port ? `${port} @ ${observe.baud}` : `波特率 ${observe.baud}`);
  parts.push(`状态 ${observe.state_label || observe.state}`);
  if (session) {
    parts.push(`会话 ${session.session_id}`);
    parts.push(`已连接 ${formatDuration(session.connected_ms)}`);
    // 会话时长与“本次连接时长”分开写：重连之后这两个数不一样（需求 §3.2 里的
    // “持续时间”说的是连接，而证据文件里那份是整次观察）。
    if (session.uptime_ms !== undefined && session.uptime_ms !== null
      && session.uptime_ms !== session.connected_ms) {
      parts.push(`会话时长 ${formatDuration(session.uptime_ms)}`);
    }
    parts.push(`RX ${session.rx_bytes} B${session.rx_chunks ? ` / ${session.rx_chunks} 次` : ""}`);
    parts.push(`TX ${session.tx_bytes} B`);
    parts.push(`io 事件 ${session.io_events} 条`);
    parts.push(`打开过 ${session.opens} 次`);
    parts.push(`证据 ${session.evidence_dir}/`);
    const last = session.last_rx_utc || session.last_tx_utc;
    parts.push(`最近收发 ${last ? shortTime(last) : "—"}`);
  } else if (observe.last) {
    parts.push(`上次会话 ${observe.last.session_id || "?"}`);
    parts.push(`RX ${observe.last.rx_bytes} B / TX ${observe.last.tx_bytes} B`);
    if (observe.last.evidence_dir) parts.push(`证据 ${observe.last.evidence_dir}/`);
  }
  if (observe.rx_error) parts.push(`读取出错 ${observe.rx_error}`);
  return parts.join(" · ");
}

/* 控制区那一行**紧凑**状态（ / D8）：波特率 · 已连接 `M:SS` · 会话时长 · io 事件数。
 * 端口与状态在左边的 `#observeState` 上（“已连接 · COM<n>”），收发计数在 `#observeCounters` 上
 * （`TX 348 B | RX 12.6 KiB`）—— 三块各管一件事，谁都不会把这一行撑成两行。
 * 详细那一行（`observeStats()`）挂在 `title` 上，不占屏幕。 */
function observeStatSay(observe) {
  if (!observe) return say(undefined, [seg("—")]);
  // 端口/状态在左边的 `#observeState` 上，这里从波特率说起（“115200”单独一行太像半个句子）。
  // ⚠️ 这一行是**数字**（用户要的第三件事）⇒ 与档位无关：四各项都常驻（用户口径 2026-10-11）。
  const session = observe.session || null;
  return say(undefined, [
    seg(`波特率 ${observe.baud}`),
    session ? seg(`已连接 ${compactDuration(session.connected_ms)}`) : null,
    session && session.uptime_ms !== undefined && session.uptime_ms !== null
      && session.uptime_ms !== session.connected_ms
      ? seg(`会话 ${compactDuration(session.uptime_ms)}`) : null,
    session ? seg(`io ${session.io_events} 条`) : null,
  ]);
}

function observeStatCompact(observe) {
  return observeStatSay(observe).text;
}

/* 实时计数那一格：`TX 348 B | RX 12.6 KiB`（D8）。
 * ⚠️ 显示的是**换算过的单位**，而服务端的原始字节点数进 title（两个都要看得见）。 */
function observeCounters(observe) {
  const session = (observe && observe.session) || null;
  if (!session) {
    return { tx: "TX —", rx: "RX —", title: "还没连过设备：没有计数" };
  }
  const rx = Number(session.rx_bytes) || 0;
  const tx = Number(session.tx_bytes) || 0;
  return {
    tx: `TX ${byteUnits(tx)}`,
    rx: `RX ${byteUnits(rx)}`,
    title: `本次会话：TX ${tx} B · RX ${rx} B`
      + (session.rx_chunks ? `（RX 分 ${session.rx_chunks} 次到达）` : ""),
  };
}

/*  / D8：换设备之后**不得沿用上一台设备的说法**。
 * 实时观察的身份/能力/计数说的都是**正握着串口的那一台**；用户刚在卡片里选了另一台时，
 * 界面必须把这件事说出来（否则那些字会被读成“我刚选的这台已经连上了”）。
 * 连着的那一台与选中的那一台一致 ⇒ 不说（正常情况不必赘言）。 */
function observeSelectionNote(observe, selectedPort = (app.selectedCard && app.selectedCard.port) || null) {
  const connected = (observe && observe.device && observe.device.port) || null;
  const selected = selectedPort || null;
  if (!connected) return "";
  if (!selected) return `下面这些说的是${connected}（当前没在卡片里选设备）`;
  if (selected === connected) return "";
  return `连的是 ${connected}（不是你刚选的 ${selected}）：要换设备先断开`;
}

/* 身份：**设备自报**与**主机匹配**必须分开说（需求 §6.2）。
 * 没回应的字段写"未提供"，绝不为了好看编一个值。
 *
 * 档位（用户口径 2026-10-10）：
 *   - 结论（“身份：已回应” / “拒绝解码，不猜” / “实时解码 N 行”）在任何档都显示；
 *   - 设备自报的字段属于“关键项”（标准及以上）——那是这一路最直接的证据；
 *   - `schema` / `protocols` 与字典的 ELF 指纹属于**机器对机器**的字段，只在详细档铺开。 */
const SAY_SELF_METADATA_FIELDS = ["schema", "protocols"];

function observeIdentitySay(observe, level) {
  if (!observe) return say(level, []);
  const identity = observe.identity || { state: "unknown", fields: {} };
  const state = IDENTITY_TEXT[identity.state] || identity.state || "未知";
  const rows = Object.entries(identity.fields || {})
    .filter(([, value]) => value !== null && value !== undefined && value !== "");
  const show = (pairs) => pairs
    .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`);
  const mine = show(rows.filter(([key]) => !SAY_SELF_METADATA_FIELDS.includes(key)));
  const meta = show(rows.filter(([key]) => SAY_SELF_METADATA_FIELDS.includes(key)));
  const parts = [seg(`身份：${state}`)];
  if (!rows.length) parts.push(seg("设备没有自报字段"));
  if (mine.length) parts.push(seg(`设备自报 ${mine.join(" ")}`, SAY_KEY_ITEM));
  if (meta.length) parts.push(seg(`握手元数据 ${meta.join(" ")}`, SAY_RAW));
  const archive = identity.archive || null;
  if (archive && archive.path) {
    const hash = (archive.elf_sha256 || "").slice(0, 12);
    // 命中的是哪份字典（名字/来源与条数）是结论；那串 ELF 指纹是核对用的原始字段。
    parts.push(seg(
      `主机匹配 字典 ${archive.origin || archive.path}（${archive.entries} 条）`,
      SAY_KEY_ITEM));
    if (hash) parts.push(seg(`字典指纹 ELF ${hash}`, SAY_RAW));
  } else if (identity.build_id32 !== null && identity.build_id32 !== undefined) {
    // 有 build id 但没命中：这是一个**结论**，不是"还没做"。
    // ⚠️ 简洁档也要留住“拒绝解码”这四个字（那是结论）；具体是哪个号只在标准及以上铺开。
    const hex = `0x${Number(identity.build_id32).toString(16).padStart(8, "0")}`;
    parts.push(seg(sayLevel(level) === "brief"
      ? "主机匹配 没有对应的字典（拒绝解码，不猜）"
      : `主机匹配 没有 build ${hex} 对应的字典（拒绝解码，不猜）`));
  } else if (identity.source !== "none") {
    // ⚠️ 不能把这一种写成"没有对应字典"：我们连**可查的号**都没有，
    // 那是两件不同的事实（一个说查过了没命中，一个说没得可查）。
    parts.push(seg("主机匹配 设备没有报告 build id，没有可查的目标"));
  }
  const decode = observe.decode || {};
  if (decode.ready) parts.push(seg(`实时解码 ${decode.lines} 行`));
  // ⚠️ 服务端没给原因时不能自己编一条：只说“未解码”（简洁档连这一句也收短）。
  else if (decode.reason) {
    parts.push(seg(sayLevel(level) === "brief" ? "未解码" : `未解码：${decode.reason}`));
  }
  return say(level, parts);
}

function observeIdentityLine(observe, level) {
  return observeIdentitySay(observe, level).text;
}

/* 身份**结论**（屏幕上那几枚 chip 的文字）—— 用户口径 2026-10-11 验收 P1：
 * 「主界面仅保留身份结论，例如“身份已确认 / 字典不匹配”；完整原因和字典清单移入悬停或详情入口」。
 *
 * 所以这个函数**只允许**返回短结论：它不能带 build 号、不能带字典路径、不能带可用字典清单。
 * （判据里就把这条钉死了：`JSON.stringify(summary)` 里不许出现那些东西。）
 *
 * 三件事分开说，因为它们是三个不同的事实（与 `observeIdentitySay` 同一套口径）：
 *   ① 设备自报身份查到没有（服务端的状态词，页面不推断）；
 *   ② **主机**能不能拿这个 build 找到字典（命中 / 不匹配 / 连可查的号都没有）；
 *   ③ 这一刻在不在解码。
 * ⚠️ ②是“主机匹配”的结论，不是设备的能力：写“设备不支持字典”会把责任安错人。 */
function identitySummary(observe) {
  const identity = (observe && observe.identity) || {};
  const archive = identity.archive || null;
  const hasBuild = identity.build_id32 !== null && identity.build_id32 !== undefined;
  let label = "身份未查询";
  let kind = "";
  if (identity.state === "ok") {
    label = "身份已确认";
    kind = "ok";
  } else if (identity.state === "querying") {
    label = "身份查询中";
  } else if (identity.state === "timeout") {
    label = "身份无响应";
    kind = "fail";
  } else if (identity.state === "unsupported") {
    label = "设备不支持身份查询";
    kind = "warn";
  }
  let dictLabel = "";
  let dictKind = "";
  if (identity.state === "ok") {
    if (archive) {
      dictLabel = "字典已命中";
      dictKind = "ok";
    } else if (hasBuild) {
      dictLabel = "字典不匹配";
      dictKind = "fail";
    } else {
      dictLabel = "没有可查的 build id";
      dictKind = "warn";
    }
  }
  const decode = (observe && observe.decode) || {};
  let decodeLabel = "";
  let decodeKind = "";
  if (decode.ready) {
    decodeLabel = `解码 ${decode.lines} 行`;
    decodeKind = "ok";
  } else if (decode.reason) {
    // “未解码”是结论；**为什么**（含手上有哪些字典）在详情与悬停里。
    decodeLabel = "未解码";
    decodeKind = "warn";
  }
  return { label, kind, dictLabel, dictKind, decodeLabel, decodeKind };
}

/* 「详情」里那一整段（也是 `#identChip` 悬停里那一份）：自报字段 + 主机匹配 + 能力提示。
 * 档位只管**这里**铺开多少（屏幕上的 chip 永远只有结论）。返回 `sayBlock()` 的形状：
 * `text` = 当前档位下展开的那一段，`title` = 全量原文 + “这一档收起了几项”。 */
function identityDetailSay(observe, level) {
  return sayBlock([
    observeIdentitySay(observe, level),
    observeLimitsSay(observe, level),
  ]);
}

/* 悬停里那一份：**永远**给全量原文（结论 chip 很小，悬停是它的详情入口之一；
 * 另一个是「详情」按钮 —— 两个入口给的是**同一句话**，逐字一致）。 */
function identityHoverTitle(detail) {
  if (!detail || !detail.full) return "";
  const parts = [];
  if (detail.text !== detail.full) {
    // 页面上没有档位控件（2026-10-12）：只说“屏幕上那句是简化过的、全文就在下面”。
    parts.push("屏幕上那句是简化过的（完整原文就在下面）");
  }
  parts.push(`完整原文：${detail.full}`);
  return parts.join("\n");
}

/* 「详情」那一行：**只有用户点了才进 DOM**（不点就一点都不占高度 —— P1 说的
 * “控制区高度膨胀”就死在这一行 hidden 上）。 */
function renderIdentDetail() {
  const host = $("identDetail");
  const button = $("identDetailBtn");
  if (!host || !button) return;
  const said = app.identDetailSay;
  const hasDetail = Boolean(said && said.text);
  const open = Boolean(app.identDetailOpen && hasDetail);
  button.hidden = !hasDetail;
  button.setAttribute("aria-expanded", open ? "true" : "false");
  button.textContent = open ? "收起详情" : "详情";
  host.hidden = !open;
  if (open) {
    host.textContent = said.text;
    host.title = identityHoverTitle(said);
  } else {
    host.textContent = "";
    host.removeAttribute("title");
  }
}

/* ：**这一屏**看的对象声明了“能不能解码”吗？
 *
 * 三屏看的不是同一个对象，所以判据要跟着屏幕走：
 *   ① 实时观察 —— 设备会话的能力（`observe.capabilities`，会话口径四项）；
 *   ② 离线诊断 —— 正在回看的那个会话（`manifest.capabilities`，同样是会话口径）；
 *   ③ 自动验证 —— **这一次 run** 自己声明的（`manifest.capabilities` 九项，需求 §2 的
 *      “历史 run 按其 manifest 中的能力回放”）；还没跑过时，退回“选中的固件所属装配”声明的。
 *
 * 返回值三态：`true` 支持 / `false` 明确不支持 / `null` **对方没声明**（页面不猜）。
 * ⚠️ 这里只回答能力，不回答原因 —— 原因由服务端对**那个对象**说的话（`decode.reason`）负责。 */
function screenDecodeCapability(observe) {
  const decide = (value) => (value === undefined || value === null ? null : value === true);
  if (app.mode === "offline") {
    const caps = ((app.review || {}).manifest || {}).capabilities || {};
    return decide(caps.decode);
  }
  if (app.mode === "live") {
    return decide(((observe || {}).capabilities || {}).decode);
  }
  const view = app.views.auto;
  if (view.viewing && view.capabilities) {
    const mine = decide(view.capabilities.decode);
    if (mine !== null) return mine;
  }
  const row = assemblyForFirmware(currentRunFirmware());
  return row ? decide((row.capabilities || {}).decode) : null;
}

/* 选中的固件 → 装配自述（`/api/state.platform.assemblies[]`，与设备卡同一个来源）。 */
function assemblyForFirmware(firmwareId) {
  if (!firmwareId) return null;
  return (app.platformAssemblies || []).find((row) => row.firmware_id === firmwareId) || null;
}

/* 解码窗口为什么空着：**把前置条件说出来**，而不是留一个空窗口让人猜。
 *
 * 用户问过："原始串口在输出，解码日志完全没动静，是应该有前置条件吗？"
 * 是有的，而且服务端把它们逐条写在 `decode.reason` 里：
 * 平台要声明 decode=supported、项目要有 token_domain、装配处要交进字典库与解码器，
 * 最后还要**精确命中字典**（知道设备是哪个 build：`INFO JSON` / `@PWINFO` 或明文横幅）。
 * 这里只转述服务端说的原因（+当前能不能触发），不自己编一套说法。 */
function decodeWhyText(observe, level) {
  if (!observe) return "";
  const decode = observe.decode || {};
  if (decode.ready) return "";   // 正在解码：这行本来就该消失
  // 简洁档（用户口径 2026-10-10）：只留“空的 + 服务端给的原因”，那些“你可以怎么做”
  // 的引导语属于读过一次就知道的东西，不必每时每刻占着屏。
  const brief = sayLevel(level) === "brief";
  // ⚠️ 服务端没给原因时**不能自己编一条**：`|| "没有匹配到字典"` 是猜的 ——
  //    会话一空闲/断开，服务端就把原因清空（`observe.py` 里那位），于是这句凭空
  //    断定了“没命中字典”（实测：空闲态 `reason=""`，页面却这么说）。空就只说空着。
  const reason = decode.reason || "";
  /* ：**先问“这一屏看的对象”要不要解码，再谈“为什么没内容”**。
   * ⚠️ 实测（2026-10-08）：自动验证屏上写着“主机没有 build 0x93dafbcb 对应的字典” —— 那是
   *    **实时观察**那次会话的设备，末尾还跟着“（先连接设备）”，而这一屏根本没有这个动作
   *    ⇒ 不是措辞问题，是把别的对象的原因贴在了这一屏上。
   * 修法：非实时观察屏**只说自己这一屏的事**；拿不到自己那份原因就不说，不借用别人的。 */
  if (app.mode !== "live") {
    if (screenDecodeCapability(observe) === false) {
      // 需求 §2 点名的措词就是“当前固件未启用 Token Log”；
      // ⚠️ 用户口径 2026-10-12：屏幕上只说「不适用」，这句完整的话与服务端给的原因进悬停。
      return decodeNotApplicable(`当前固件未启用 Token Log`, reason, brief);
    }
    return "";
  }
  const caps = observe.capabilities || {};
  /* 用户口径（2026-10-13）：身份未识别 ⇒ 这一条会话按**原始串口模式**工作，屏幕上说
   * 「不适用」（服务端给的那句完整话在悬停里）。
   * ⚠️ 只说在**还没在解码**的时候：万一连上之后身份查询回来了、字典也精确命中，
   *    解码会自己跑起来 —— 那时 `decode.ready` 为真，上面那一行早把这句说明收掉了。 */
  if (observe.transparent && !decode.ready) {
    return decodeNotApplicable(
      String(observe.transparent_reason || "身份未识别"), reason, brief);
  }
  if (!caps.decode) {
    return decodeNotApplicable("这一路不做解码", reason, brief);
  }
  // 能力上支持解码、但字典还没到位：屏幕上只说「未就绪」（原因在悬停里）。
  const ready = "解码：未就绪";
  if (brief) return ready;
  return `${ready}${reason ? `：${reason}` : ""}`
    + (observe.state === "connected"
      ? "（连接后会再查一次身份；命中字典之后才开始逐行解码）"
      : "（先连接设备）");
}

/* 屏幕上的两个**状态词**（用户口径 2026-10-12：“将长篇前置条件压缩为「未就绪」或「不适用」；
 * 详细原因进入详情或后台证据”）。
 * ⚠️ 「不适用」与「未就绪」不能混：前者是**这条路本来就不做**，后者是**还没到位** ——
 *    混了就会把“不做”读成“等等就好”（这是  那一族错的老毛病）。 */
const DECODE_NOT_APPLICABLE = "解码：不适用";
function decodeNotApplicable(why, reason, brief) {
  if (brief) return DECODE_NOT_APPLICABLE;
  return `${DECODE_NOT_APPLICABLE}（${why}）${reason ? `：${reason}` : ""}`;
}

/* 解码窗底那一句的**两个版本**：屏幕上按当前档位，title 里给全量。
 * ⚠️ 两个版本走**同一个实现**（只是档位不同），不另写一份“详细版”文案。 */
function decodeWhySay(observe, level) {
  const text = decodeWhyText(observe, level);
  const full = decodeWhyText(observe, "full");
  return { text, full, level: sayLevel(level), hidden: text && full && text !== full ? 1 : 0 };
}

/* ：**当前这一屏看的固件**有没有解码能力（判据只有服务端的能力声明一条）。
 *
 * 两个来源，因为三屏看的不是同一份固件：
 *   ① 实时观察屏：会话快照的 `observe.capabilities`（观察目标自己声明的那一份）；
 *   ② 自动验证 / 离线屏：`#runFirmware` 选中的那一份 ⇒ 去 catalog 里查它所属目标的能力声明。
 * ⚠️ 页面**不推断原因**（是没装字典、还是固件根本没编 Token 日志）：那是服务端能力声明的
 *    职责，页面只转述。
 * ⚠️ catalog 里的能力形态兼容两种（`{capabilities:{decode}}` 与包一层 `declared`）：
 *    证据格式将来加了包装层也不该让这一句静默失效。 */
function currentRunFirmware() {
  return ($("runFirmware") || {}).value || "";
}

/* （口径：用户 2026-10-08 定）：解码窗**默认收成标签**，
 * 只有拿到"这一路支持解码 + 本地确实有字典"之后才自动展开。
 *
 * ⚠️ 为什么反过来做（默认收）比"默认展开、发现不支持再收"好：
 *    ① 默认值就是**初始状态**，第一次画屏幕时它已经是"收起" ⇒ 一定画得对；
 *       反过来那条路要等"状态发生变化"才重画，实测踩到"状态早已是 true、屏幕还是展开"
 *       （`localStorage` 里记着 true，而 `data-collapsed` 是 `"0"`）；
 *    ② 不需要纠结"什么时候表态" —— "没就绪就收着"本身就是一个稳定状态。
 * ⚠️ 判据只有服务端说的两件事：`capabilities.decode`（支持吗）与 `decode.ready`
 *    （字典到位、正在解码）。页面**不自己推断**有没有字典。
 * ⚠️ 只在**翻转**时动手（`app.decodeRailFor` 记住上次那个布尔）：同一个状态下每轮轮询
 *    都施加的话，用户手动展开的窗会被反复收回去。 */
function decodeReadyToShow(observe) {
  if (!observe) return false;
  return (observe.capabilities || {}).decode === true
    && (observe.decode || {}).ready === true;
}

function applyDecodeRailDefault(observe) {
  const ready = decodeReadyToShow(observe);
  if (app.decodeRailFor === ready) return;
  app.decodeRailFor = ready;
  if (ready) expandPane(attachedLog, "decoded");
  else collapsePane(attachedLog, "decoded");
  // ⚠️ 补一下强制重画：`collapsePane` / `expandPane` 只写状态，若屏幕与状态的脱节
  //    已经存在（例如从 prefs 读回来、而初始化那一刻没画），这一下能把屏幕拉正。
  // ⚠️ 它**不是**“值没变就不重画”的补救（那句推测已被推翻）：这一行的用处是
  //    拉正“已有脱节”，而不是制造变化。
  applyRowLayout(attachedLog);
}

/* 同一条信息也画在解码窗口里（观察条上那一行在屏幕很上面，正在看这个窗口的人看不见）。 */
function renderDecodeWhy(observe) {
  const host = $("why-decoded");
  if (!host) return;
  const said = decodeWhySay(observe);
  const text = said.text;
  host.textContent = text;
  host.hidden = !text;
  // 档位收起来的那半句挂在 title 上（悬停即见全量）。
  host.title = sayTitle(said);
  // 这一条是高矮会变的（开始解码后它就消失）：重算一次底部占用，正文才能把高度拿回来。
  if (typeof applyDock === "function") applyDock();
}

/* 这一步还**没有**的能力，直接由服务端的能力声明派生（不是这里硬编码的承诺）。 */
function observeLimitsSay(observe, level) {
  if (!observe) return say(level, []);
  const caps = observe.capabilities || {};
  // 每一条都是“结论（任何档都在）+ 为什么/在你手上看到的样子（标准及以上）”。
  const notes = [];
  if (!caps.evidence) {
    notes.push(seg("本切片只统计收发字节"));
    notes.push(seg("尚未写成会话证据（raw.bin / io_events.jsonl 在下一步）", SAY_KEY_ITEM));
  }
  // ⚠️ 这句话过时了（实时解码早就实现了）：`decode=false` 现在**只有**一种含义 ——
  //    这一路按能力声明就不做解码（ 的关闭 Token Log 构型）。说成"尚未实现"
  //    是拿旧的进度当真话讲，与"不猜不编"冲突。
  if (!caps.decode) {
    notes.push(seg("这一路没有解码能力"));
    notes.push(seg("会话里只有原始字节与事件记录", SAY_KEY_ITEM));
  }
  if (!caps.send) {
    notes.push(seg("命令框与终端直输尚未实现"));
    notes.push(seg("现在只收不发", SAY_KEY_ITEM));
  }
  if (!caps.identity) {
    notes.push(seg("身份握手尚未实现"));
    notes.push(seg("不会主动去问设备型号", SAY_KEY_ITEM));
  }
  return say(level, notes);
}

function observeLimits(observe, level) {
  return observeLimitsSay(observe, level).text;
}

/* 当前（或最近一次）会话：下载链接与窗口内容都靠它。
 * 会话结束后回退到 `last`：刚采到的证据不应该因为点了“断开”就下载不了。 */
function evidenceSession(observe) {
  const snapshot = observe || app.observe;
  if (!snapshot) return null;
  if (snapshot.session && snapshot.session.session_id) return snapshot.session;
  return snapshot.last && snapshot.last.session_id ? snapshot.last : null;
}

/* 会话里四个窗口各自对应哪个文件（tool / decoded 不是会话文件，见注释）。
 * ⚠️ 会话目录里没有 `tool.log`（主机侧说明不落盘），`decoded.log` 要等实时解码；
 * 所以这两个窗口指向最能解释自己的那一份，而不是留一个点不动的链接。 */
const SESSION_FILE_BY_SOURCE = {
  tool: "manifest.json",
  raw: "raw.bin",
  events: "app_events.jsonl",
  decoded: "decoded.log",
};

/* 会话证据的下载目标（纯函数）。
 *
 * ⚠️ 判据是"**这个会话里到底有没有那个文件**"，不是"此刻在不在解码"：
 * 会话结束后解码已经停了（`decode.ready=false`），但文件就在那里 —— 拿运行状态去决定
 * 链接，会让刚采到的证据在断开那一刻变成"点不动"（真机验收当场遇到）。
 * 所以：正在观察时看 `decode.ready`，看历史会话时看会话自己的计数（`decoded_lines`）。
 *
 * 没解码时也不要指向一个不存在的文件：
 *  - `decoded.log` 不存在 ⇒ 不给下载，标题里说清原因（字典没命中 / 还没查身份）；
 *  - `app_events.jsonl` 不存在 ⇒ 「结构化事件」窗口改指 `io_events.jsonl`（那一定存在）。
 */
function sessionEvidenceLink(sourceId, observe, session) {
  const decode = (observe || {}).decode || {};
  const current = (observe || {}).session || null;
  const live = Boolean(current && session && current.session_id === session.session_id);
  // 服务端没给原因时只能说**事实**（这次会话没有解码）：猜“没有匹配到字典”
  // 会在空闲/断开时变成一句误诊 —— 原因由服务端清空的那一刻，页面没资格补一个。
  const reason = decode.reason || "这次会话没有解码";
  const decodedLines = live
    ? (decode.ready ? 1 : 0) || (session && session.decoded_lines) || 0
    : (session && session.decoded_lines) || 0;
  const eventCount = live
    ? (decode.ready ? 1 : 0) || (session && session.app_events) || 0
    : (session && session.app_events) || 0;
  if (sourceId === "decoded" && !decodedLines) {
    return { file: "", title: `本次会话没有 decoded.log：${live ? reason : "这次会话没有解码"}` };
  }
  if (sourceId === "events" && !eventCount) {
    return {
      file: "io_events.jsonl",
      title: `本次会话没有解码，就没有 app_events.jsonl：这里下载 io 时间线（${live ? reason : "这次会话没有解码"}）`,
    };
  }
  const file = SESSION_FILE_BY_SOURCE[sourceId] || "";
  return {
    file,
    title: file
      ? `下载本会话的 ${file}（原始证据：不受视图截断、暂停或去重影响）`
      : "本次会话还没有这个文件",
  };
}

/* 实时观察开始：四个窗口清空成“这一次会话”的内容。
 * ⚠️ 必须清空：否则窗口里会留着上一次 run 的历史行，而新来的行是这次会话的 ——
 * 两种证据混在同一个窗口里，看的人分不清哪一行属于哪一次。
 * ⚠️ 这里**不**写“会话开始”那行字：服务端已经在事件流里说过一次（`observe._say`），
 * 两边都写会让工具窗口里出现两条几乎一样的话（真机验收时看到的）。
 * 清除发生在本帧，服务端那行随后到达 —— 顺序正好是先清后写。 */
function startLiveWindows() {
  /* 这一次会话的行归**实时观察**那一份（）：
   * 就算用户此刻在看离线包/自动验证，这些行也只进实时那一份的模型 ——
   * “一边把实时观察存下来、一边分析离线包”就是这一条。 */
  app.receivingMode = "live";
  const view = app.views.live;
  resetSources(receivingStore());
  resetTerminal();      // 按键队列与“哪个窗口拿着键盘”都属于“这一次会话”，不混上一轮
  view.viewing = null;  // 下载链接改指向会话证据（见 updateLogIndicators）
  view.review = null;   // 这一屏是**这次会话**的，不再是在回看某一次历史会话
  /* ⚠️ 断言/指标/恐慌那几个**面板**不再在这里清空（）：它们是“最近一次 run 的”，
   * 属于自动验证那一屏 —— 开始一次实时观察不该把上一轮 run 的结果抹掉（反过来也一样）。 */
}

/* 实时观察的「目标」= **服务端算好的身份匹配结论**（ / D9，需求 §9.1）。
 *
 * 用户只选通信口：固件由服务端按这张卡片的**实测身份**唯一匹配 firmware profile，
 * 唯一命中就自动采用；零匹配 / 多匹配必须阻塞并说明原因。所以页面**不挑固件**：
 * 这里只把服务端的结论（`/api/state.sniff.last.cards[].match`）读出来，再去
 * `observe.targets` 里取那一份的标签 —— 一个字符串都不自己比。
 *
 * ⚠️ 卡片上的「可用固件 N 份」（`compatible_firmwares`）回答的是**另一个**问题：
 * “这一份能不能用在它上面”（含“覆盖同板另一份固件”这种**烧录**路径）。
 * 同一块板上跑着 RT-Thread 时，裸机那份也 `allowed=true`（一烧就把它换掉），
 * 但它**不是**“设备此刻跑的那一份” —— 观察组合、解码能力与受控用例目录都只能按
 * 真命中的那一份装配（ 就是拿错了这一份）。
 */
function observeCombination(card, targets) {
  const rows = Array.isArray(targets) ? targets : [];
  if (!card) {
    return {
      state: "", firmware_id: "", rows: [], available: false, usable: false, note: "",
      blocked: "还没选设备：先选一个通信口（没嗅探过就点一次「嗅探」）",
    };
  }
  const match = card.match || null;
  if (!match || !match.state) {
    return {
      state: "", firmware_id: "", rows: [], available: false, usable: false, note: "",
      blocked: "还没拿到这台设备的身份结论：再点一次「嗅探」",
    };
  }
  const firmwareId = String(match.firmware_id || "");
  const row = rows.find((item) => String(item.firmware_id) === firmwareId) || null;
  const usable = Boolean(match.usable);
  const available = usable && Boolean(row);
  return {
    state: String(match.state),
    firmware_id: available ? firmwareId : "",
    rows: available ? [row] : [],
    available,
    // `usable` = 服务端的匹配结论本身（“这个身份能不能定到一份固件”）。
    // ⚠️ 自动验证那一屏靠它判断“能不能替你决定烧哪一份”（身份未知 + 多份候选 ⇒ 不替使用者挑）。
    usable,
    /* 唯一命中就什么都不说（用户口径 2026-10-12：这一项不显示、也不留句子）。
     * ⚠️ 用户口径（2026-10-13）：**身份未知 / 零匹配 / 多匹配不再拦连接** ⇒ 这里也不再
     *    摆“为什么连不了”的理由 —— 服务端那句原文里带着「多匹配 / 首次烧录声明」这类
     *    **平台内部术语**，而屏幕上要说的事已经有自己的说法（卡片写「没问到身份」，
     *    连上之后状态行说「身份未识别，已进入原始串口模式」）。原句仍在 API 里
     *    （`card.match.reason`），自动验证要用的也是它。
     * 唯一还留在屏幕上的情况是“身份匹配到了、本机却没有这一路的观察会话” —— 那是本机
     * 台架缺登记，跟身份没关系，得说出来。 */
    note: !usable || available
      ? ""
      : `服务端按这次实测身份匹配到 ${firmwareId}，而本机没有登记这一路的观察会话`,
  };
}

/* 实时观察的“选哪一份固件”（W5）：一个 firmware 一条链 —— 换了它，设备登记、字典、
 * 解码器、身份规则与能力表一起换（服务端按 firmware 分派会话）。
 * ⚠️ 这里**只换选中项**：不连接、不打开串口、不发字节。
 * ⚠️ 能力是**逐路**声明的：不做“拿参考目标的能力套所有板子”那种事 ——
 * 所以选项上直接写清这一路有没有解码 / 身份，而不是等用户点了才发现不支持。 */
function renderObserveTargets(observe) {
  const select = $("observeFirmware");
  if (!select) return;
  const all = (observe && observe.targets) || [];
  const card = selectedSniffCard();
  const combo = observeCombination(card, all);
  app.observeCombo = combo;
  const rows = combo.rows;
  const field = $("observeTargetField");
  const note = $("observeTargetNote");
  /* 用户口径（2026-10-12）：**「目标」这一项不再显示**（`field.hidden = true`）。
   *  · 没选设备时同样不问：连都连不了，列一堆本机登记的固件只是噪音；
   *  · 选了设备时它由服务端按身份匹配出来（D9），唯一命中就什么都不说。
   * 这个 select 留在 DOM 里只为把**服务端的结论**写出来：`chain`（连接按钮判「这一路有没有
   * 观察链」）与标题都从它取值。 */
  if (field) field.hidden = true;
  // ⚠️ 说明那一格**不删**：零匹配 / 多匹配时它是「为什么连接按钮是灰的」的答案
  //    （唯一命中时不说话 —— 那是用户口径里被删掉的那类句子）。
  if (note) {
    note.hidden = !combo.note;
    note.textContent = combo.note || "";
    note.dataset.kind = combo.note ? "fail" : "";
  }
  const signature = `${combo.state}|${rows.map((row) => `${row.firmware_id}:${row.label}`).join("|")}`;
  if (select.dataset.signature !== signature) {
    select.textContent = "";
    for (const row of rows) {
      const caps = row.capabilities || {};
      const missing = [];
      if (caps.decode === false) missing.push("无解码");
      if (caps.identity === false) missing.push("无身份握手");
      const node = document.createElement("option");
      node.value = row.firmware_id;
      node.dataset.firmwareId = row.firmware_id;
      node.textContent = `${row.label}${missing.length ? `（${missing.join("、")}）` : ""}`;
      node.title = [
        `${row.label}`,
        `目标 ${row.target_id || "?"} · 设备 ${row.device_key || "?"}`,
        `平台 ${row.platform || "?"} · 项目包 ${row.project || "?"}`,
        `能力：${["send", "identity", "decode", "evidence"]
          .map((name) => `${name}=${caps[name] === undefined ? "未声明" : caps[name] ? "支持" : "不支持"}`)
          .join(" ")}`,
        row.note || "",
      ].filter(Boolean).join("\n");
      select.appendChild(node);
    }
    select.dataset.signature = signature;
  }
  /* 写进去的**只有**服务端匹配到的那一份（唯一命中才有值；零/多匹配时是空 ⇒
   * `chain` 为空 ⇒ 连接按钮禁用，理由在说明那一格与按钮 `title` 上）。
   * ⚠️ 不再退到“上一次选的那份”或“列表第一份” —— 那正是 §9.1 要拆掉的“页面自己猜”。 */
  select.value = combo.firmware_id;
}

/* 状态一变就说一次（SSE 与 2 s 轮询都会走到这里，所以两边只会各说一次）。 */
/* 「要不要为此说一句」是个**纯判断**（用户口径 2026-10-12）：**只在出问题时说**。
 *
 * - 连上了不必通报 —— 状态行已经写着「已连接 · COM<n>」，右边那行还有波特率/时长/io 计数；
 * - 正常断开也不必通报（用户口径 2026-10-12 晚）—— 状态行就是「未连接」，
 *   会话那几个数（RX/TX/证据目录）在悬停与「历史观察」里都能查；
 * - 只有**故障**才弹那一行：打不开 / 串口不在了 / 被别的模式占着 —— 那是“现在不能用了”。
 *
 * ⚠️ 一个事实都没删：服务端那句 `observe.detail`（含“打开时未写 dtr/rts，设备没有被复位”、
 * 断开时的会话小结）现在挂在状态行的悬停上（`#observeState.title`，见 `renderObserve`）。 */
const OBSERVE_TROUBLE_STATES = ["open_failed", "port_missing", "owned_elsewhere"];

/**
 * 「身份未识别，已进入原始串口模式」（用户口径 2026-10-13）。
 *
 * ⚠️ 这句话是**服务端**给的（`observe.transparent_reason`，就一个常量），页面只负责
 *    在有需要时说一遍 —— 服务端那边它也是受控用例“不适用”的理由，两处同一个来源。
 * ⚠️ 它说的是**眼前这件事**（连上了、按原始串口工作），不是“故障”，所以走 warn。
 * ⚠️ 位置只此一处：受控用例那一格屏幕上只说「不适用」（完整那句在它的悬停里）。
 */
function observeNotice(observe) {
  if (!observe) return { text: "", kind: "" };
  const state = String(observe.state || "");
  if (state === "connected" && observe.transparent) {
    return {
      text: String(observe.transparent_reason || ""),
      kind: "warn",
    };
  }
  if (!OBSERVE_TROUBLE_STATES.includes(state)) return { text: "", kind: "" };
  const label = observe.state_label || state;
  const detail = String(observe.detail || "").trim();
  return {
    text: detail ? `实时观察：${label} —— ${detail}` : `实时观察：${label}`,
    kind: "warn",
  };
}

/* 写出那一句：**每次状态变化都写**（包括空串）—— 连接/断开都把上一轮留下的
 * “打开失败…”收掉，否则屏幕上会挂着一条已经不成立的话。 */
function notifyObserve(observe) {
  const notice = observeNotice(observe);
  showMessage(notice.text, notice.kind, "observe");
}

/* ---------------------------------------------- 受控测试（ / L4b）
 * 页面在这条路上只做三件事：把**服务端给的目录**画出来、把选中的用例交上去、显示
 * 这一批跑到哪了。能不能跑、为什么不能，全在服务端（`live_test_catalog` 的逐条
 * `reason`）—— 页面照抄那句话，不自己判兼容性、不自己拼命令、也不自己估耗时
 * （需求 §8）。刷新不重复执行：页面**从不**自动启动，状态全部来自 `/api/state`。 */
function liveTestRows() {
  const block = (app.observe && app.observe.live_test_catalog) || null;
  return block && Array.isArray(block.cases) ? block.cases : [];
}

function liveTestCaseMeta(row) {
  const bits = [];
  if (Number(row.expected_duration_s) > 0) bits.push(`约 ${Number(row.expected_duration_s)} s`);
  if (row.destructive) bits.push("破坏性：要服务端逐次确认");
  /* 用户口径（2026-10-13）：用例不可用时屏幕上只有一句人话「当前设备不支持此用例」——
   * 它由**服务端**给（`screen_reason`，能力类的原因统一收成那一句）；具体卡在哪条能力要求
   * （某个 `cap.`/`device.` 键）留在 API / 证据里，**页面一个都不认识**、也不许出现在这里。 */
  if (!row.selectable) bits.push(row.screen_reason || row.reason || "现在不能选");
  return bits.join(" · ");
}

/* 这一批跑到哪了（纯函数：模型回归直接喂它）。
 *
 * 用户口径（2026-10-12 晚）：屏幕上**只留“跑着：…”**那一句（那件事需要人立刻知道）；
 * 收口之后不再在屏幕上写一条「已收口：PASS（PASS 2）」—— 它是**总结**，
 * 而“结果”已经在每张阶段卡上（PASS/FAIL + 耗时），那句总结挂到悬停里（`full`）。
 * ⚠️ 所以 `text` 是“屏幕上那一句”，`full` 是“完整那一句”（含收口总结）——
 * 调用方不能拿 `full` 当屏幕文案（那正是用户要去掉的那一类句子）。 */
function liveTestStatusSay(snapshot) {
  if (!snapshot || !snapshot.batch) return { text: "", full: "" };
  const batch = snapshot.batch;
  const rows = Array.isArray(batch.executions) ? batch.executions : [];
  const running = rows.find((row) => row.status === "RUNNING");
  if (String(batch.status) === "RUNNING" && running) {
    const stage = (running.steps || []).find((step) => step.status === "running");
    const where = stage ? ` · ${stage.label}（${(Number(stage.duration_ms) / 1000).toFixed(1)} s）` : "";
    const text = `跑着：${running.case_id}${where}`;
    return { text, full: text };
  }
  const counts = (batch.summary && batch.summary.counts) || {};
  const said = Object.entries(counts).map(([key, value]) => `${key} ${value}`).join(" / ");
  const tail = [];
  if (said) tail.push(said);
  if (batch.reason) tail.push(batch.reason);
  return {
    text: "",
    full: `已收口：${batch.result || batch.status}${tail.length ? `（${tail.join(" · ")}）` : ""}`,
  };
}

/** 完整那一句（给悬停与证据口径用；**不再上屏幕**）。 */
function liveTestStatusText(snapshot) {
  return liveTestStatusSay(snapshot).full;
}

function renderLiveTests() {
  const list = $("liveTestCases");
  if (!list) return;
  const block = (app.observe && app.observe.live_test_catalog) || null;
  const snapshot = (app.observe && app.observe.live_tests) || null;
  const fullReason = block ? String(block.reason || "") : "还没有实时观察的会话事实";
  /* `applicable === false` = 这个间题在**这一条会话上不成立**（身份未识别、按原始串口工作，
   * 用户口径 2026-10-13）：屏幕上只说「不适用」，完整那句（服务端给的）在悬停里。 */
  const applicable = !(block && block.applicable === false);
  /* 用户口径（2026-10-12 晚）：“还没连接设备 ⇒ 连接之后才能跑受控测试”这种**当然的事**
   * 不在屏幕上说：那时候面板本来就是空的、两个按钮本来就是灰的。
   * ⚠️ 理由本身没删：它挂在面板的悬停上（`#liveTestPanel.title`）。
   * 连着的时候那些理由照旧显示（“项目包没声明协议”/“这一条能力不满足”都得看得见）。 */
  const connected = String((app.observe && app.observe.state) || "") === "connected";
  const screen = !connected ? "" : (applicable ? fullReason : (fullReason ? "不适用" : ""));
  if ($("liveTestReason")) {
    $("liveTestReason").textContent = screen;
    $("liveTestReason").hidden = !screen;
  }
  if ($("liveTestPanel")) $("liveTestPanel").title = screen === fullReason ? "" : fullReason;
  const pickedBefore = app.liveTestPicked instanceof Set ? app.liveTestPicked : new Set();
  const picked = new Set();
  list.textContent = "";
  for (const row of liveTestRows()) {
    const item = document.createElement("li");
    item.className = "live-test-case";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.id = `liveCase-${row.id}`;
    box.disabled = !row.selectable;
    box.checked = Boolean(row.selectable) && pickedBefore.has(row.id);
    if (!row.selectable) box.title = row.screen_reason || "这一条现在不能选";
    box.addEventListener("change", () => {
      if (box.checked) picked.add(row.id);
      else picked.delete(row.id);
      app.liveTestPicked = picked;
      updateLiveTestButtons();
    });
    if (box.checked) picked.add(row.id);
    const name = document.createElement("label");
    name.htmlFor = box.id;
    name.textContent = row.label;
    const meta = document.createElement("span");
    meta.className = "hint";
    meta.textContent = liveTestCaseMeta(row);
    item.append(box, name, meta);
    list.append(item);
  }
  app.liveTestPicked = picked;
  if ($("liveTestStatus")) {
    // 屏幕上只留“跑着：…”；收口总结进悬停（用户口径 2026-10-12 晚）。
    const say = liveTestStatusSay(snapshot);
    $("liveTestStatus").textContent = say.text;
    $("liveTestStatus").hidden = !say.text;
    $("liveTestStatus").title = say.full;
  }
  updateLiveTestButtons();
}

/** 两个按钮的可用性：能不能开始只看“选了一条可选用例且没有在跑的批次”。 */
function updateLiveTestButtons() {
  const running = liveTestRunningBatch();
  const picked = app.liveTestPicked instanceof Set ? app.liveTestPicked : new Set();
  const selectable = liveTestRows().filter((row) => row.selectable && picked.has(row.id));
  if ($("startLiveTests")) $("startLiveTests").disabled = Boolean(running) || !selectable.length;
  if ($("cancelLiveTests")) $("cancelLiveTests").disabled = !running;
}

function liveTestRunningBatch() {
  const snapshot = (app.observe && app.observe.live_tests) || null;
  const batch = snapshot && snapshot.batch;
  return batch && String(batch.status) === "RUNNING" ? batch : null;
}

async function startLiveTests() {
  const picked = app.liveTestPicked instanceof Set ? app.liveTestPicked : new Set();
  const cases = liveTestRows()
    .filter((row) => row.selectable && picked.has(row.id))
    .map((row) => ({ case_id: row.id, version: row.version, parameters: {} }));
  if (!cases.length) {
    showMessage("先选一条可执行的用例", "warn");
    return;
  }
  if ($("startLiveTests")) $("startLiveTests").disabled = true;
  try {
    await postJson("/api/observe/tests/start", { cases });
  } catch (error) {
    showMessage(`受控测试没能启动：${error.message}`, "error");
  }
  await refreshState();
}

async function cancelLiveTests() {
  if ($("cancelLiveTests")) $("cancelLiveTests").disabled = true;
  try {
    await postJson("/api/observe/tests/cancel", { reason: `使用者在页面上取消了这一批（${CLIENT.label}）` });
  } catch (error) {
    showMessage(`取消失败：${error.message}`, "error");
  }
  await refreshState();
}

/* 需求 §6.10：基础串口参数（波特率 / 数据位 / 校验位 / 停止位）。
 * ⚠️ 选项与现值都**来自服务端**（`observe.serial`）：页面不自己维护第二份默认值 ——
 *    否则“页面显示 115200、服务端其实用 9600”这类错会静默存在。
 * ⚠️ 选项只在第一次填（`dataset.filled`）：每 2 秒一次的重画不能把用户刚选的项冲掉。 */
const SERIAL_LABELS = { baud: "波特率", bytesize: "数据位", parity: "校验位", stopbits: "停止位" };

function serialFields() {
  return {
    baud: $("serialBaud"),
    bytesize: $("serialBytesize"),
    parity: $("serialParity"),
    stopbits: $("serialStopbits"),
  };
}

function renderSerial(serial) {
  const fields = serialFields();
  if (!serial || !fields.baud) return;
  const choices = serial.choices || {};
  const effective = serial.effective || {};
  for (const [name, node] of Object.entries(fields)) {
    if (!node) continue;
    if (!node.dataset.filled) {
      node.textContent = "";
      for (const value of Array.isArray(choices[name]) ? choices[name] : []) {
        const option = document.createElement("option");
        option.value = String(value);
        option.textContent = String(value);
        node.appendChild(option);
      }
      node.dataset.filled = "1";
    }
    // 用户正在改这一格时不要动它（轮询会把光标下的选择冲回去）。
    if (document.activeElement !== node && name in effective) node.value = String(effective[name]);
  }
  const row = $("serialRow");
  if (!row) return;
  const refused = serial.refused || {};
  const names = Object.keys(refused);
  row.dataset.state = names.length ? "partial" : "ok";
  row.title = names.length
    ? "上一次打开时没设上的项："
      + names.map((name) => `${SERIAL_LABELS[name] || name}（${refused[name]}）`).join("；")
    : "串口参数：连接与重连时生效（在打开口**之前**设上去）";
}

/* 连接 / 重连请求里的串口参数（就那几个 select 的现值）。 */
function serialRequest() {
  const rows = {};
  for (const [name, node] of Object.entries(serialFields())) {
    if (node && node.value) rows[name] = node.value;
  }
  return rows;
}

function renderObserve(observe) {
  if (!observe) return;
  renderSerial(observe.serial);
  const first = app.observe === null;
  const previous = app.observe;
  const previousState = previous ? previous.state : null;
  app.observe = observe;
  app.observeSeen = true;
  // 状态一到就把"存着的那批实时行"按会话号判一遍（）。
  flushHeldReplayLines();
  if ($("observeState")) {
    const where = observe.device ? ` · ${observe.device.port}` : "";
    $("observeState").textContent = `${observe.state_label}${where}`;
    $("observeState").dataset.state = observe.state;
    // 屏幕上只留结论；服务端那句“已打开 COM<n> @ 115200；打开时未写 dtr/rts，设备没有被复位”
    // （`observe.detail`）挂在悬停上 —— 它原本是连接成功时那条消息的正文（用户口径 2026-10-12
    // 不再常驻），但事实本身没删：没复位、没写调制线，想核的时候 hover 就能看到。
    $("observeState").title = [`${observe.state_label}${where}`, observe.detail]
      .filter(Boolean).join("\n");
  }
  // D8：控制区**紧凑** —— 状态行只留波特率/时长/io 事件（端口与状态在左边那一行），
  // 收发计数单独一格（`TX 348 B | RX 12.6 KiB`），详细的那一行挂 title 上。
  if ($("observeStat")) {
    const stat = observeStatSay(observe);
    $("observeStat").textContent = stat.text;
    // 详细那一行（有会话号 / 证据目录 / 收发数）挂在 title 上；档位收起了什么也在这里说。
    $("observeStat").title = sayTitle(stat, observeStats(observe));
  }
  const counts = observeCounters(observe);
  if ($("observeTx")) $("observeTx").textContent = counts.tx;
  if ($("observeRx")) $("observeRx").textContent = counts.rx;
  if ($("observeCounters")) $("observeCounters").title = counts.title;
  // 刷新页面后把**这一批**的阶段从状态快照里重建（阶段卡不该因为按了一下 F5 就没了）。
  // 顺序要紧：先确认“现在说的是哪一批”（换了批就清掉上一批的卡），再去重建内容。
  const batch = ((app.observe && app.observe.live_tests) || {}).batch;
  useLiveStageBatch(batch && batch.batch_id);
  restoreLiveStagesFromState();
  if (syncLiveStageConclusion(batch) && app.mode === "live") renderStepCards();
  // 阶段窗的默认值（没有阶段就收成竖排标签）必须在这之后判：这一屏到底有没有卡，
  // 要看状态快照。实测踩过：抢在状态前面就收，会把“刚刷新、这一批已经跑完”的那一窗
  // 收起来——卡还在里面，屏幕上却只剩一个竖排标签。
  applyStagePaneDefault(app.mode);
  if ($("observeIdent")) {
    /* 用户口径（2026-10-11 验收 P1）：「主界面仅保留身份结论，例如“身份已确认 / 字典不匹配”；
     * 完整原因和字典清单移入悬停或详情入口」。
     * ⇒ 这一格屏幕上只有几枚**结论 chip**；自报字段、字典来源与条数、拒绝解码的原因与
     *    可用字典清单、平台还不能做什么，全在「详情」与悬停里（`identityDetailSay()`）。 */
    const summary = identitySummary(observe);
    const chips = [
      ["identSwitch", observeSelectionNote(observe), "warn"],
      ["identChip", summary.label, summary.kind],
      ["identDict", summary.dictLabel, summary.dictKind],
      ["identDecode", summary.decodeLabel, summary.decodeKind],
    ];
    for (const [id, text, kind] of chips) {
      const el = $(id);
      if (!el) continue;
      el.textContent = text || "";
      el.hidden = !text;
      if (kind) el.dataset.kind = kind;
      else el.removeAttribute("data-kind");
    }
    // 依据（长句）**不进 DOM**，只留着给「详情」与 title：不点就一点都不占高度。
    const detail = identityDetailSay(observe);
    const where = (observe.device && observe.device.port) || observe.port || "";
    if (app.identDetailFor !== where) {
      // 换了设备就不沿用上一台展开的详情（ 的同一条口径）。
      app.identDetailFor = where;
      app.identDetailOpen = false;
    }
    app.identDetailSay = detail;
    if ($("identChip")) $("identChip").title = identityHoverTitle(detail);
    renderIdentDetail();
  }
  renderObserveTargets(observe);
  renderLiveTests();
  applyLineEndingDefault(observe);
  // 捕获状态跟“连没连上”有关：没连着就不把键盘给窗口（没设备可发字节）。
  // 平台不识别固件能力，所以这里不判断“支持不支持终端”。
  if (!captureConnected()) releaseCapture();
  else renderCapture();
  // 解码窗口里那条"为什么现在没有内容"的常驻说明：来源是服务端自己写的 decode.reason。
  renderDecodeWhy(observe);
  // 会话换了（或第一次看到）→ 四个窗口清空成这一次会话的。
  // 会话结束时不需要做什么：服务端会在事件流里报“会话 … 结束”，工具窗口自己会说。
  const session = evidenceSession(observe);
  const previousSession = evidenceSession(previous);
  if (session && session.session_id !== (previousSession && previousSession.session_id)
    && (observe.session || first)) {
    startLiveWindows();
  }
  updateButtons();
  if (!first && previousState !== observe.state) notifyObserve(observe);
  // 串口所有权一变（接上/断开）就把会话清单刷新一遍：刚结束的那次应该马上能回看
  // （需求 §9.1-10）。刷新只读目录，不碰串口。
  if (first || Boolean(previous && previous.holding) !== Boolean(observe.holding)) refreshSessions();
  refreshCmdHint();
  // ⚠️ 下载链接的说明跟着**解码状态**走（没解码时不给 decoded.log、结构化事件改指
  //    io_events.jsonl）。不在这里刷一下的话，标题会停在"身份确定之前"的那个快照上 ——
  //    页面上的身份已经更新、链接却还在说"还没查身份"（真机验收看到的）。
  updateLogIndicators(app.log);
}

async function observeAction(path, body) {
  try {
    const payload = await postJson(path, body || {});
    if (payload.observe) renderObserve(payload.observe);
  } catch (error) {
    showMessage(`实时观察请求被拒绝：${error.message}`, "fail", "observe");
  }
  refreshState();
}

/* ------------------------------------------------- 离线诊断：会话回看与诊断包（切片 6） */
/* 需求 §1：离线诊断**不打开串口、不编译、不烧录** —— 它只把已经落盘的会话装进四个窗口，
 * 并把整次会话打成一个诊断包。
 *
 * 两个“会话”概念必须分清（它们以前混在一起）：
 *   - 实时观察快照里的 `session` / `last`：**这次连接的**会话（可能还在写）；
 *   - `app.sessions`：本机所有的会话（清单，只读目录，新的在前）。
 *
 * 四个窗口的内容永远只有一个来源：正在看的 run、正在回看的会话、正在观察的会话。
 * 所以装载任何一边都会把另两边清掉（`app.viewing` / `app.review`）。
 */

// 派生产物（字典精确命中才存在）：缺它们不等于“会话不完整”。
const DERIVED_ARTIFACTS = ["decoded.log", "app_events.jsonl"];

/* 会话的计数口径（**纯函数**）：manifest 里的 `totals` 是**会话结束时**才写的，
 * 所以“正在写”的那一次它是空的 —— 那时候退回到文件大小（现成的、不用读内容）。
 * 两种来源必须说得出区别：它们不是同一个数，也不该被说成一个数。 */
function sessionRowCounts(row) {
  const totals = (row && row.totals) || {};
  const sizes = (row && row.counts) || {};
  const hasTotals = totals.rx_bytes !== undefined && totals.rx_bytes !== null;
  const value = (fromTotals, fromFiles) => (fromTotals !== undefined && fromTotals !== null
    ? fromTotals
    : (fromFiles === undefined ? null : fromFiles));
  return {
    fromTotals: hasTotals,
    rx: value(totals.rx_bytes, sizes.raw_bytes),
    rxChunks: hasTotals ? (totals.rx_chunks ?? null) : null,
    tx: hasTotals ? (totals.tx_bytes ?? 0) : null,
    decodedLines: hasTotals ? (totals.decoded_lines ?? 0) : null,
  };
}

/* 清单里的一行（纯函数：宿主回归直接驱动文案）。
 * ⚠️ 导入来的会话必须一眼就看出来：“别人给的包”与“自己在现场采的”不能长得一样。 */
function sessionOptionLabel(row) {
  const counts = sessionRowCounts(row);
  const parts = [row.session_id + (row.origin === "import" ? "（导入）" : "")];
  if (row.port) parts.push(row.port);
  if (counts.rx !== null) {
    parts.push(`RX ${counts.rx} B${counts.fromTotals ? "" : "（文件）"}`);
  }
  if (counts.tx) parts.push(`TX ${counts.tx} B`);
  if (counts.decodedLines === null) parts.push("还没结束");
  else parts.push(counts.decodedLines ? `解码 ${counts.decodedLines} 行` : "没有解码");
  if (row.live) parts.push("进行中");
  else if (!row.complete) parts.push("没有结束记录");
  return parts.join(" · ");
}

/* 导入会话的核对结果（纯函数）：**核对过**与**没得核对**是两件事。 */
function importNote(row) {
  const record = row && row.import;
  if (!record) return "";
  const parts = [
    `${record.imported_utc ? shortTime(record.imported_utc) : "时间未记录"} 从 `
      + `${record.imported_from || "未知文件"} 导入`,
  ];
  if (record.verified) parts.push(`包内声明的 sha256 逐份核对通过（${(record.checks || []).length} 份）`);
  else parts.push("包里没有可核对的 sha256：只能确认它是个结构合法的包");
  if ((record.warnings || []).length) parts.push(`注意：${(record.warnings || []).join("；")}`);
  return parts.join(" · ");
}

/* 还没装载时面板上的长说明。**缺什么就说缺什么**：不写"加载中"。 */
function sessionRowText(row) {
  if (!row) return "还没有选择会话";
  const counts = sessionRowCounts(row);
  const missing = row.missing || [];
  const required = missing.filter((name) => !DERIVED_ARTIFACTS.includes(name));
  const derived = missing.filter((name) => DERIVED_ARTIFACTS.includes(name));
  const parts = [`会话 ${row.session_id}`];
  if (row.broken) return `${parts[0]}：${row.broken}`;
  if (row.origin === "import") {
    // 导入的：先把“它是别人给的”放在最前面，再说内容。
    const note = importNote(row);
    parts.push(`**导入的包**${note ? `（${note}）` : ""}`);
  }
  if (row.port) parts.push(`${row.port}${row.baud ? ` @ ${row.baud}` : ""}`);
  if (counts.rx !== null) {
    parts.push(`RX ${counts.rx} B${counts.rxChunks ? ` / ${counts.rxChunks} 次` : ""}${counts.fromTotals ? "" : "（按文件大小算，会话还没结束）"}`);
  }
  if (counts.tx) parts.push(`TX ${counts.tx} B`);
  if (required.length) parts.push(`⚠ 缺少 ${required.join("、")}：这次会话不完整`);
  else if (derived.length) parts.push(`没有 ${derived.join("、")}（这次会话没有解码）`);
  if (row.live) parts.push("正在写（回看会拿到当下的快照）");
  else if (row.complete) parts.push(`结束 ${shortTime(row.closed_utc)}`);
  else parts.push("没有结束记录：这次会话没有正常收尾，但已经落盘的字节仍然可回看");
  return parts.join(" · ");
}

/* 屏幕上那一行（短）：只回答“**哪个会话 / 多少 / 有没有解码 / 结束得干不干净**”。
 * ⚠️ 完整那一行（`sessionRowText()`：含导入来源与逐条解释）挂在悬停上 ——
 * 这就是用户口径 2026-10-11 要的那种分法：**屏幕给决定，悬停给来龙去脉**。 */
function sessionRowLine(row) {
  if (!row) return "还没有选择会话";
  if (row.broken) return `会话 ${row.session_id}：${row.broken}`;
  const counts = sessionRowCounts(row);
  const missing = (row.missing || []).filter((name) => !DERIVED_ARTIFACTS.includes(name));
  const derived = (row.missing || []).filter((name) => DERIVED_ARTIFACTS.includes(name));
  const parts = [`会话 ${row.session_id}`];
  if (row.origin === "import") parts.push("**导入的包**");
  if (counts.rx !== null) {
    // “按文件大小算”是**这个数的性质**（清单里没有写侧的总数）：不能收了它，否则
    // 会被读成精确值。
    parts.push(`RX ${counts.rx} B${counts.fromTotals ? "" : "（按文件大小算）"}`);
  }
  if (counts.tx) parts.push(`TX ${counts.tx} B`);
  if (missing.length) parts.push(`⚠ 缺少 ${missing.join("、")}`);
  else if (derived.length) parts.push("没有解码");
  if (row.live) parts.push("正在写");
  else if (row.complete) parts.push(`结束 ${shortTime(row.closed_utc)}`);
  else parts.push("没有结束记录");
  return parts.join(" · ");
}

/* 已经装载时屏幕上那一行（短）：同上，只留“哪个会话 / 多少 / 有没有解码 / 身份 / 结束”。 */
function sessionReviewLine(review) {
  if (!review) return "还没有装载任何会话";
  const counts = review.counts || {};
  const manifest = review.manifest || {};
  const identity = manifest.identity || {};
  const parts = [`会话 ${review.session_id}`];
  if (review.origin === "import") parts.push("**导入的包**");
  parts.push(`RX ${counts.raw_bytes || 0} B`);
  if (counts.tx_events) parts.push(`TX ${counts.tx_events} 条`);
  parts.push(counts.decoded_lines || counts.app_events
    ? `解码 ${counts.decoded_lines || 0} 行`
    : "没有解码");
  parts.push(`身份 ${IDENTITY_TEXT[identity.state] || identity.state || "未查询"}`);
  parts.push(manifest.closed_utc ? `结束 ${shortTime(manifest.closed_utc)}` : "没有结束记录");
  return parts.join(" · ");
}

function sessionReviewText(review) {
  if (!review) return "还没有装载任何会话";
  const counts = review.counts || {};
  const manifest = review.manifest || {};
  const totals = review.manifest_totals || {};
  const identity = manifest.identity || {};
  const parts = [
    `会话 ${review.session_id}`,
    `RX ${counts.raw_bytes || 0} B（${counts.raw_lines || 0} 行）`,
    `TX ${counts.tx_events || 0} 条`,
    `io 事件 ${counts.io_events || 0} 条`,
  ];
  if (review.origin === "import") {
    // 屏幕上一句**结论**（这是导入的包）；来源（什么时候、从哪个 zip、核对了几份哈希）
    // 是“当时怎么来的” ⇒ 后者归悬停（`#sessionStat.title`，用户口径 2026-10-11）。
    parts.unshift("**导入的包**");
  }
  if (counts.decoded_lines || counts.app_events) {
    parts.push(`解码 ${counts.decoded_lines || 0} 行 · 结构化事件 ${counts.app_events || 0} 条`);
  } else {
    parts.push("没有 decoded.log（这次会话没有解码）");
  }
  parts.push(
    `身份 ${IDENTITY_TEXT[identity.state] || identity.state || "未查询"}`
    + (identity.source && identity.source !== "none" ? `（${identity.source}）` : "")
  );
  if (manifest.closed_utc) {
    parts.push(`结束 ${shortTime(manifest.closed_utc)} · ${manifest.end_reason || "未记录原因"}`);
  } else {
    parts.push("没有结束记录");
  }
  // 写侧记的数（manifest.totals）与读侧数出来的数对不上时**说出来**，不挑一个信。
  if (totals.rx_bytes !== undefined && totals.rx_bytes !== null && totals.rx_bytes !== counts.raw_bytes) {
    parts.push(`⚠ manifest 记的 RX ${totals.rx_bytes} B 与文件里数出来的 ${counts.raw_bytes} B 不一致`);
  }
  return parts.join(" · ");
}

/* 会话内容的装载（四窗内容 = 那一次会话的证据，与 run 的历史装载同一条路径）。
 * ⚠️ 会话的「工具输出」是**派生视图**：内容是 `io_events.jsonl` 重画出来的（RX 索引不画），
 *    行号与文件行号不同源 ⇒ 连它每一行的事件号一起装（`tool_seqs`），阶段卡上的
 *    `{ source: "tool", event_seq: N }` 才能落到那一行（）。 */
function buildSessionSources(detail) {
  return {
    tool: toLines(detail && detail.tool),
    tool_seqs: Array.isArray(detail && detail.tool_seqs) ? detail.tool_seqs : null,
    raw: toLines(detail && detail.raw),
    decoded: toLines(detail && detail.decoded),
    events: parseEventLog(detail && detail.app_events).map(eventLine),
  };
}

/* 诊断包的目标（**纯函数**：给定一个会话描述 → 下载目标；没有会话就返回 null）。
 * ⚠️ 没有会话时**不给链接**，而不是给一个点不动的链接。
 * ⚠️ 它是**会话级**的：`bundleScopeNote()` 那句话就是给"人在 run 界面上点它"准备的
 *    （ —— 服务端只有 `.../sessions/<会话号>/bundle` 这一条导出路，
 *    run 的 report.json / tool.log / events.jsonl / steps.jsonl 不在包里）。 */
function bundleHref(session) {
  if (!session || !session.session_id) return null;
  const name = encodeURIComponent(session.session_id);
  const live = Boolean(
    app.observe && app.observe.session && session.session_id === app.observe.session.session_id
  );
  return {
    href: `/api/observe/sessions/${name}/bundle?download=1`,
    sessionId: session.session_id,
    title: `把会话 ${session.session_id} 整个打成一个 zip：原样的 manifest.json / raw.bin / io_events.jsonl`
      + "（有的话还有 decoded.log / app_events.jsonl），再加 README.txt 与 session.json（带每份文件的 sha256）。"
      + (live ? "会话进行中也能下：包里是**生成那一刻**的快照。" : "")
      // ⚠️ 不传 `app.viewing`：这个入口现在只在离线诊断里（那里四个窗口显示的是**会话**，
      //    拿一个上一屏留下的 run 号来写“现在打开的是 run …”只会误导。
      + " " + bundleScopeNote(session, ""),
  };
}

/* 按钮上写清"导的是谁的包"：只写「导出诊断包」时，看的人分不清它指的是不是屏幕上这一份。
 * 文案在  收口时定为 `导出诊断包 · <会话号>`（RUNTIME_MODES §9.4）。 */
function bundleLabel(target) {
  const id = target && target.sessionId;
  return id ? `导出诊断包 · ${id}` : "导出诊断包";
}

/* 一句话说清这个包**是什么范围**，也顺带说清它**不含什么**。 */
function bundleScopeNote(target, viewingRun) {
  const rows = [];
  if (target && target.session_id) {
    rows.push(`⚠️ 这是**会话** ${target.session_id} 的整包（不是某一次 run 的）`);
  }
  rows.push("它不含某一次 run 自己的报告与证据文件");
  if (viewingRun) {
    rows.push(`现在打开的是 run ${viewingRun}，这个按钮给的仍然是会话包`);
  }
  return rows.join("；");
}

/* 诊断包指向（ 收口 / RUNTIME_MODES §9.4）：**只在离线诊断**里，指这一屏正在说的
 * 那个会话（已装载的那一份优先 —— 它与四个窗口显示的内容必然一致；其次才是下拉框选中的）。
 * 实时观察负责采集并自动落盘、自动验证负责 run 证据，两个屏都**不显示**这个入口；
 * 本次也不新增 run 级打包（会话包必含 `io_events.jsonl`，而 run 目录没有这个文件）。 */
function bundleTarget() {
  if (app.mode !== "offline") return null;
  const select = $("sessionPick");
  const id = offlineSelectedId(app.review, select ? select.value : "");
  return id ? bundleHref({ session_id: id }) : null;
}

/* 画那个入口：有目标就写 `导出诊断包 · <会话号>`，没目标就**藏起来**（不给死人链接）。 */
function renderBundleEntry() {
  const link = $("sessionBundle");
  if (!link) return;
  const target = bundleTarget();
  if (!target) {
    link.hidden = true;
    link.removeAttribute("href");
    link.setAttribute("aria-disabled", "true");
    return;
  }
  link.hidden = false;
  link.href = target.href;
  link.textContent = bundleLabel(target);
  link.title = target.title;
  link.removeAttribute("aria-disabled");
}

function renderSessionOptions() {
  const select = $("sessionPick");
  if (!select) return;
  const signature = app.sessions
    .map((row) => `${row.session_id}:${row.complete}:${row.live}:${row.origin}`)
    .join("|");
  if (select.dataset.signature !== signature) {
    select.textContent = "";
    if (!app.sessions.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "（本机还没有会话：先做一次实时观察，或导入一个诊断包）";
      select.appendChild(option);
    }
    for (const row of app.sessions) {
      const option = document.createElement("option");
      option.value = row.session_id;
      option.textContent = sessionOptionLabel(row);
      if (row.broken) option.disabled = true;
      select.appendChild(option);
    }
    select.dataset.signature = signature;
  }
  // 默认选“刚结束/正在跑”的那一次（用户最可能要看的就是它）；不自动装载。
  const wanted = (app.review && app.review.session_id)
    || (app.observe && app.observe.last && app.observe.last.session_id)
    || "";
  if (wanted && app.sessions.some((row) => row.session_id === wanted)) select.value = wanted;
  if (select.value && select.selectedOptions[0] && select.selectedOptions[0].disabled) select.value = "";
  if ($("sessionLoad")) $("sessionLoad").disabled = !select.value;
  if ($("sessionCount")) {
    const local = app.sessions.filter((row) => row.origin !== "import").length;
    const imported = app.sessions.length - local;
    $("sessionCount").textContent = app.sessions.length
      ? `本机 ${local} 次会话${imported ? ` · 导入 ${imported} 个包` : ""}`
      : "本机还没有会话";
  }
}

function renderOffline() {
  if (!$("offlinePanel")) return;
  const select = $("sessionPick");
  const sessionId = offlineSelectedId(app.review, select ? select.value : "");
  const row = (app.sessions || []).find((item) => item.session_id === sessionId) || null;
  const loaded = app.review && app.review.session_id === sessionId ? app.review : null;
  if ($("offlineState")) {
    $("offlineState").textContent = loaded ? `正在回看 ${sessionId}` : "离线诊断";
    $("offlineState").dataset.state = loaded ? "loaded" : "idle";
  }
  if ($("offlineDetail")) {
    // 用户口径（2026-10-11）：一行内说清“这四个窗口看的是什么”就够（原来还带了
    // “下载链接也指向它”这种教学语）。
    $("offlineDetail").textContent = loaded ? "四个窗口显示的是这次会话的证据（原样读）" : "";
  }
  if ($("sessionStat")) {
    // 页脚那一行要说的“证据在哪”跟着这一屏：离线屏说的是**这个会话**的目录。
    if (loaded && loaded.session_dir) app.sessionDir = loaded.session_dir;
    // 屏幕上只留结论（`…Line()`）；完整那一行（含导入来源与逐条解释）进悬停。
    $("sessionStat").textContent = loaded ? sessionReviewLine(loaded) : sessionRowLine(row);
    $("sessionStat").title = loaded ? sessionReviewText(loaded) : sessionRowText(row);
  }
  if ($("sessionLoad")) {
    const label = loaded ? "重新装载" : "回看这次会话";
    if ($("sessionLoad").textContent !== label) $("sessionLoad").textContent = label;
  }
  if ($("sessionNote")) {
    // 用户口径（2026-10-11 → 2026-10-12）：屏幕上只留一句**结论**（导入的包不是本机采集的）；
    // 目录路径与“包在哪导”这类现场不再上屏（也不进悬停）。
    const importedNow = String((row && row.origin) || (loaded && loaded.origin) || "") === "import";
    $("sessionNote").textContent = importedNow ? "这是别人给的诊断包（不是本机采集的）" : "";
    $("sessionNote").hidden = !importedNow;
    $("sessionNote").removeAttribute("title");
  }
  // 选中项一变，诊断包链接跟着变（不用先装载也能下那一次会话的包）。
  updateLogIndicators(app.log);
}

/* 纯函数：离线面板此刻指的是**哪一个**会话。
 *
 * ⚠️ **已经装进四个窗口的那个**优先于下拉框的值：装好了就是装好了，
 * 下拉框只是"挑一个"的入口，它可能装不下那一条（清单默认只列最近 20 条，
 * 一个 9 月的包夹在一堆 10 月的会话里就会被截掉）。真浏览器上撞到过：
 * 四个窗口明明是导入的那次会话，面板却说"还没有选择会话"。
 */
function offlineSelectedId(review, pickerValue) {
  if (review && review.session_id) return String(review.session_id);
  return String(pickerValue || "");
}

/* 读一次会话清单。**只读目录**：不打开串口、不碰设备。失败时**不动**已有清单
 *（把清单清空会让人以为“会话都不见了”）。
 *
 * `include`：这一条一定要在回来的清单里（导入刚发布的那个会话号用它：号可能比
 * 默认条数老，不钉一下就会被截断，于是“自动打开了却再也找不回来”）。 */
async function refreshSessions(include) {
  const query = include ? `?include=${encodeURIComponent(include)}` : "";
  try {
    const payload = await getJson(`/api/observe/sessions${query}`);
    app.sessions = payload.sessions || [];
    app.sessionsDir = payload.sessions_dir || "";
    app.importsDir = payload.imports_dir || "";
    app.sessionsState = "ready";
    app.sessionsError = "";
  } catch (error) {
    // 失败**不动**已有清单（清空会让人以为“会话都不见了”），但历史列表那一段要说“读不到”
    // （§12.1 第 4 条：接口失败必须与“空目录”分得开）。
    app.sessionsState = "error";
    app.sessionsError = error.message;
    showMessage(`读不到会话清单：${error.message}`, "warn", "offline");
    renderHistory();
    return;
  }
  renderSessionOptions();
  renderOffline();
  renderHistory();
}

/* 导入一个诊断包（切片 8 / P4）：把 zip **原样**传上去，校验与解压都在服务端做。
 *
 * ⚠️ 它不碰串口、不编译、不烧录；服务端会先验包再落盘，拒绝时把原因说清楚
 *（不是 zip / 少了 manifest / 多出陌生条目 / 包内声明的 sha256 对不上……）。
 * 成功后直接装载它：用户点“导入”就是想看这个包。
 *
 * ⚠️ 三件 P4 才补齐的事（PACK-UI-01/02）：
 *   ① **不带整份文件进内存**：以前 `await file.arrayBuffer()` 会把 400 MiB 的包整个读进
 *      页面内存；现在直接把 `File` 交给 `XMLHttpRequest` 发（浏览器流式读盘）。
 *   ② **有进度、可取消**：任务号由页面**先给**（`X-Import-Task`），所以上传还没结束时
 *      就能一边查阶段一边取消 —— 取消是个显式动作，不是“页面卡住”。
 *   ③ **不知道总量就不画百分比**：`importProgressModel()` 在总量未知时返回 `null`，
 *      页面显示一段转圈的文字而不是一个假的 0%。 */
function importBundle() {
  const field = $("importFile");
  const file = field && field.files && field.files[0];
  if (!file) {
    showMessage("先选一个诊断包（zip）", "warn", "offline");
    return;
  }
  const taskId = importTaskId();
  app.importTask = {
    id: taskId, file: file.name, size: file.size, sent: 0, uploaded: false,
    cancelRequested: false, task: null,
  };
  renderImportState();
  return sendImport(taskId, file)
    .then((payload) => finishImport(payload))
    .catch((error) => {
      const said = importFailureText(error);
      showMessage(said.text, said.kind, "offline");
    })
    .finally(() => {
      app.importTask = null;
      if (field) field.value = "";
      renderImportState();
    });
}

/* 纯函数：导入失败/取消 → 一句话 + 该用哪种颜色。
 *
 * ⚠️ “用户取消了”与“服务端拒了”是两件事：前者是**用户意图的完成**（warn，并且
 * 要说清半包已删），后者才是 fail。取消**不是**错误。 */
function importFailureText(error) {
  const code = error && error.code ? String(error.code) : "";
  // 两种“取消”都要认：服务端把结论写成了 409 `import_cancelled`，或者上传是被本地中断的。
  const cancelled = code === "import_cancelled" || Boolean(error && error.cancelled);
  if (cancelled) {
    return {
      text: "这次导入已取消（没收完的临时包已删掉）",
      kind: "warn",
      cancelled: true,
      code: code || "import_cancelled",
    };
  }
  return {
    text: `导入被拒：${error && error.message ? error.message : error}`,
    kind: "fail",
    cancelled: false,
    code: code,
  };
}

/* 把一个 `File` 按块发给服务端，并**边传边报**。
 *
 * ⚠️ 用 `XMLHttpRequest` 而不是 `fetch`：`fetch` 拿不到**上传**进度
 *（`ReadableStream` 请求体在浏览器里还没有可靠进度事件）。 */
function sendImport(taskId, file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/observe/import");
    request.setRequestHeader("Content-Type", "application/zip");
    request.setRequestHeader("X-Bundle-Filename", file.name);
    request.setRequestHeader("X-Import-Task", taskId);
    request.upload.onprogress = (event) => {
      if (!app.importTask || app.importTask.id !== taskId) return;
      app.importTask.sent = event.loaded;
      if (event.lengthComputable) app.importTask.size = event.total;
      renderImportState();
    };
    // ⚠️ “请求体交完了”与“请求结束了”是两件事：服务端接手（校验/解压/发布）
    // 都在**这条请求还在飞**的时候发生，所以必须单独记下来。
    request.upload.onload = () => {
      if (!app.importTask || app.importTask.id !== taskId) return;
      app.importTask.uploaded = true;
      app.importTask.sent = app.importTask.size;
      renderImportState();
    };
    request.onload = () => {
      let payload = {};
      try {
        payload = JSON.parse(request.responseText || "{}");
      } catch (error) {
        payload = {};
      }
      if (request.status >= 200 && request.status < 300) {
        resolve(payload);
        return;
      }
      const failure = new Error(payload.error || `HTTP ${request.status}`);
      failure.code = payload.code || "";
      reject(failure);
    };
    // ⚠️ 取消时**服务端会就地关连接**（它拒半包时就是这样做的），所以浏览器这边
    //    看到的是一次“网络中断”而不是 409 `import_cancelled`。这两种中断必须分开：
    //    用户点过取消 → 那是取消（warn）；没点过 → 才是真的断线（fail）。
    request.onerror = () => reject(importInterruption(Boolean(
      app.importTask && app.importTask.cancelRequested
    )));
    request.onabort = () => {
      const stopped = new Error("上传被中断");
      stopped.cancelled = true;
      reject(stopped);
    };
    request.send(file);
    app.importRequest = request;
    // 上传还在跑的时候就开始按任务号查阶段（PACK-UI-01：进度要看得见）。
    watchImportTask(taskId);
  });
}

/* 按任务号轮询阶段与当前成员。**有限次**：拿不到就算了（服务端还在跑，
 * 请求本身结束时会给出结论），不许把页面变成无限轮询。 */
function watchImportTask(taskId, attempt = 0) {
  if (!app.importTask || app.importTask.id !== taskId) return;
  if (attempt >= IMPORT_POLL_MAX) return;
  setTimeout(() => {
    if (!app.importTask || app.importTask.id !== taskId) return;
    getJson(`/api/observe/import/${encodeURIComponent(taskId)}`)
      .then((task) => {
        if (!app.importTask || app.importTask.id !== taskId) return;
        app.importTask.task = task;
        renderImportState();
      })
      .catch(() => {})
      .finally(() => watchImportTask(taskId, attempt + 1));
  }, IMPORT_POLL_MS);
}

/* 纯函数：上传被**本地**弄断时的错 —— 是取消还是真断线。
 *
 * 用户点了取消（旗子早于 POST 就置好了），服务端会立刻关连接拒掉半包，
 * 浏览器于是看到 onerror 而不是 409 —— 这不改变事实：那是用户把事停下的。 */
function importInterruption(cancelRequested) {
  const failure = new Error(cancelRequested
    ? "上传已按你的要求停下"
    : "连接中断（导入未完成）");
  failure.cancelled = Boolean(cancelRequested);
  return failure;
}

/* 取消 = **只置旗子**（服务端会在下一个数据块处停下并把半包删掉）。
 * ⚠️ 必须真走 POST：`getJson()` 只发 GET，用错它的话这个按钮会在 405 上默默失败。 */
function cancelImport() {
  const current = app.importTask;
  if (!current) return Promise.resolve(false);
  // 旗子**先于**请求置好：上传被本地/服务端弄断时，靠它区分“取消”与“断线”。
  current.cancelRequested = true;
  return postJson(`/api/observe/import/${encodeURIComponent(current.id)}/cancel`, {})
    .then(() => {
      showMessage("已请求取消：上传会立刻停下，服务端不会留下半个包", "warn", "offline");
      return true;
    })
    .catch((error) => {
      showMessage(`取消失败：${error.message}`, "warn", "offline");
      return false;
    });
}

/* 导入成功之后：刷新清单 → 选中它 → 装载它（PACK-UI-02：成功即打开离线诊断）。
 *
 * ⚠️ 刷新清单时把**它自己**钉进去（`include`）：导入的包常常是个很老的会话，
 * 而清单默认只列最近 20 条 —— 不钉就会被截掉，于是"自动打开了、列表里却找不到"。 */
async function finishImport(payload) {
  const imported = payload.imported || {};
  await refreshSessions(imported.session_id);
  const select = $("sessionPick");
  if (select && imported.session_id) select.value = imported.session_id;
  renderOffline();
  await loadSession(imported.session_id);
  // ⚠️ 那行“导入了什么、核对过什么”要在 loadSession **之后**写：
  //    装载自己也会往同一行写一句话（已装载会话 …）—— 写在前面的话它一瞬就被盖掉了，
  //    而“包内声明的 sha256 核没核对过”只在这一行里（工具窗口的回看行也有，但那是日志内容）。
  showMessage(importResultText(imported), imported.verified ? "pass" : "warn", "offline");
}

/* 任务号：页面**先给**号（这样上传途中就能查/能取消）。
 * 只允许字母数字与 `-`/`_` —— 服务端会拿它当文件名，非法字符在那里就会被拒。 */
function importTaskId(hex) {
  const raw = String(hex === undefined ? Math.random().toString(16).slice(2, 10) : hex);
  const clean = raw.replace(/[^0-9a-f]/gi, "").slice(0, 12);
  return `import-${clean || "0"}`;
}

/* 纯函数：进度模型（宿主回归直接测）。
 *
 * 两个来源、一个先后：**本地上传**那段用浏览器报的已发/总量；请求体交出去之后
 * 换成**服务端**的阶段（抄它的 `stage_label`）与它记的进度。
 *
 * ⚠️ 为什么不是"只要有服务端任务就用它的数"：上传还在飞的时候，服务端的
 * `received_bytes` 一定落后于浏览器（它就是那些还没到的字节），拿它当进度会让
 * 进度条**往回退**。所以只有"请求体交完了 / 阶段已经越过 upload / 任务已终态"
 * 三种情况才让服务端接手。
 *
 * `percent`：**总量未知就是 null**（不许画一个假的百分比）。 */
function importProgressModel(state) {
  const sent = Math.max(0, Number(state && state.sent) || 0);
  const size = Number(state && state.size) || 0;
  const task = (state && state.task) || null;
  const known = size > 0;
  const handedOver = Boolean(state && state.uploaded);
  const serverish = Boolean(task)
    && (handedOver || task.stage !== "upload" || task.state !== "running");
  if (serverish) {
    const share = task.percent === null || task.percent === undefined
      ? null : Number(task.percent);
    return {
      percent: Number.isFinite(share) ? share : null,
      text: importStageText(task),
      canCancel: task.state === "running",
      finished: task.state !== "running",
      from: "server",
    };
  }
  return {
    percent: known ? Math.min(1, sent / size) : null,
    text: known
      ? `正在上传 ${byteText(sent)} / ${byteText(size)}`
      : `正在上传 ${byteText(sent)}（大小未知）`,
    canCancel: true,
    finished: false,
    from: "browser",
  };
}

/* 纯函数：服务端任务自述 → 一句人话（阶段名 + 结果）。
 * ⚠️ 用户口径 2026-10-12：“上传、校验、解压、发布四段”这类内部流程不常驻 —— 屏幕上只留
 *    阶段名、进度与结果（成员计数也不上屏）。 */
function importStageText(task) {
  if (!task) return "";
  const label = task.stage_label || task.stage || "";
  const parts = [label];
  if (task.state === "done" && task.result && task.result.session_id) {
    parts.push(`已导入 ${task.result.session_id}`);
  }
  if (task.state === "failed" && task.error) parts.push(`失败：${task.error_why || task.error}`);
  if (task.state === "cancelled") parts.push("已取消");
  return parts.filter(Boolean).join(" · ");
}

/* 纯函数：导入成功那句总结（含“有没有核对过 sha256”这件事）。 */
function importResultText(imported) {
  const row = imported || {};
  const check = row.verified
    ? `已逐份核对（${(row.checks || []).length} 份）`
    : "包里没有核对信息，只确认了结构合法";
  return `已导入 ${row.session_id}（原包里的会话号 ${row.origin_session_id}；${check}）`;
}

/* 选好文件之前那颗按钮没意义；选好之后要说清“要传的是什么、多大”。
 *
 * 导入进行中：进度条 + 一句阶段话 + “取消导入”按钮（PACK-UI-01）；
 * 结束后把进度行收起来。⚠️ 总量未知时 `importProgressModel()` 给 `percent: null`，
 * 这里就不设 `value`（进度条转圈），不画一个假的 0%。 */
function renderImportState() {
  const field = $("importFile");
  const button = $("importRun");
  const hint = $("importHint");
  const file = field && field.files ? field.files[0] : null;
  const busy = Boolean(app.importTask);
  if (button) button.disabled = busy || !file;
  if (field) field.disabled = busy;   // 传着的时候不许换文件（任务号与文件是配对的）
  if (hint) {
    hint.textContent = busy
      ? `${app.importTask.file} · ${byteText(app.importTask.size)}（正在导入）`
      : file
        ? `${file.name} · ${byteText(file.size)}`
        : "选择一个诊断包（.zip）";
  }
  const row = $("importProgress");
  if (!row) return;
  row.hidden = !busy;
  if (!busy) return;
  const model = importProgressModel(app.importTask);
  if ($("importBar")) {
    if (model.percent === null) $("importBar").removeAttribute("value");
    else $("importBar").value = Math.round(model.percent * 100);
  }
  if ($("importStep")) $("importStep").textContent = model.text;
  if ($("importCancel")) $("importCancel").hidden = !model.canCancel;
}

/* 装载一个会话到**离线诊断那一屏**。**不会**打开串口。
 *
 * ⚠️ 以前这里有一道护栏："正在实时观察就拒绝装载，先断开再回看" —— 那是**单份模型**
 * 时代的必要条件（装载会把四窗换成会话、而实时还在往同一份里写）。
 * 现在每模式一屏（）：离线那一屏与实时那一屏互不影响，这道护栏只会
 * 挡住用户真正要做的事（"一边实时观察、一边分析离线包"）—— 所以去掉。
 * 真机验收量出来的（连着 COM<n> 时点"回看"，界面上只说了一句"先断开再回看"）。 */
async function loadSession(sessionId) {
  const select = $("sessionPick");
  const id = sessionId || (select ? select.value : "");
  if (!id) {
    showMessage("先选一个会话；本机还没有会话的话，先去实时观察里连一次设备", "warn", "offline");
    return;
  }
  let detail;
  try {
    detail = await getJson(`/api/observe/sessions/${encodeURIComponent(id)}`);
  } catch (error) {
    showMessage(`回看失败：${error.message}`, "fail", "offline");
    return;
  }
  applySessionDetail(detail);
}

function applySessionDetail(detail) {
  const counts = detail.counts || {};
  const missing = detail.missing || [];
  // 回看一个会话 = 离线诊断那一屏的事：先切过去，再装进**那一份**（）。
  if (app.mode !== "offline") setMode("offline");
  // 全文搜索的候选证据就来自这里（详情列了哪些产物）。
  app.artifactsMeta = detail.artifacts_meta || [];
  app.viewing = null;            // 四窗内容只有一个来源：先清掉 run 的历史视图
  app.review = {
    session_id: detail.session_id,
    counts,
    manifest: detail.manifest || {},
    manifest_totals: detail.manifest_totals || {},
    files: detail.files || [],
    // 来源：本机采集的，还是导入的包（导入时带上“怎么来的、核对过什么”）。
    origin: detail.origin || "local",
    import: detail.import || null,
    // 链接判定用的是**这个会话里到底有没有那个文件** → 带会话口径的计数（）。
    decoded_lines: counts.decoded_lines || 0,
    app_events: counts.app_events || 0,
  };
  applyHistoricalSources(
    app.log, app.views.offline, buildSessionSources(detail), detail,
    omittedWindows(detail, SESSION_EVIDENCE_FOR_PANE)
  );
  renderFullSearchChoices();
  //  / L3（）：把这次会话**冻结**的受控测试阶段还原进离线诊断那一屏的
  // 阶段窗。走的还是 `applyTestStep` 那一条路 —— 不另写回放器，也不另写卡片渲染。
  restoreSessionStages(detail.live_tests, app.views.offline);
  /* ⚠️ 不再清空断言/指标/恐慌面板（）：那几个面板是“最近一次 run 的”，
   * 属于自动验证那一屏 —— 分析一个离线包不该把 run 的结果抹掉。 */
  // ⚠️ 不往页脚写会话目录：页脚是**服务状态行**，每 2 s 的状态刷新会把它盖掉 ——
  //    一条出现两秒就消失的说明只会让人以为"刚才那行是错的"。目录写在离线诊断面板里。
  renderOffline();
  const noDecode = !counts.decoded_lines;
  showMessage(
    `已装载会话 ${detail.session_id}`
    + (missing.length ? `（目录里缺 ${missing.join("、")}）` : "")
    + (noDecode ? "；这次会话没有解码（没有 decoded.log）" : ""),
    missing.length ? "warn" : "pass",
    "offline",
  );
  // 解码窗底的说明跟着“正在回看的这个会话”走（）。
  renderDecodeWhy(app.observe);
}

/* 切换模式 = 换一组控件。这是**纯粹的显示动作**：没有任何 build / upload / reset /
 * 串口操作，最多再读一次 /api/state 把当前所有权画准。 */
/** 这条阶段事件属不属于当前模式（T7 / 需求 §8.4）。
 *
 * - 自动验证：只认编排发的那一路（`origin === "run"`）；
 * - 实时观察：只认观察会话自己发的（`origin === "live"`）—— 普通串口观察不伪造阶段；
 * - 离线诊断：回放不走 SSE（直接按事件流喂 `applyTestStep`），这里不参与。
 */

function applyMode() {
  const mode = app.mode;
  // 先把四个窗口接到**这个模式那一份**上（换一屏内容；别的模式那一份留在内存里）。
  attachView(mode);
  for (const button of document.querySelectorAll(".mode-btn")) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  }
  const show = (id, visible) => {
    const el = $(id);
    if (el) el.hidden = !visible;
  };
  show("autoPanel", mode === "auto");
  //  / D8-R（§11.1）：设备控制面板（嗅探 + 卡片 + 本屏状态/动作 + 底部紧凑行）
  // 是**共享**的 —— 自动验证与实时观察问的是同一个问题“哪一台设备”。
  // 离线诊断不显示它：那一路不碰串口，也就不需要选设备。
  show("deviceBar", mode === "auto" || mode === "live");
  // 两屏在**同一个外框**里换内容：
  //   右列：自动验证看 `#portStatus`（端口/身份就绪结论），实时观察看 `#liveStatus`
  //         （连接状态 + 连接/重连/断开）；
  //   底行：自动验证看 `#runControls`（固件/套件/采集/启动 + 更多设置），实时看 `#liveRow`
  //         （TX/RX 持续 身份 解码）。
  show("liveStatus", mode === "live");
  show("liveRow", mode === "live");
  // 实时收发计数（用户口径 2026-10-12）：住在工作台那一栏（“分栏 N 栏”旁边），
  // **只在实时观察那一屏显示** —— 另外两屏没有会话在收数。
  // 它只由 `renderObserve()` 写文字，宽度在 CSS 里定死 ⇒ 2 s 刷新不推动任何控件。
  show("observeCounters", mode === "live");
  //  / L4b：受控用例面板**只属于实时观察那一屏**
  // （离线诊断是回看，不显示启动/取消；需求 §8 末条：历史用例不能再次启动）。
  show("liveTestPanel", mode === "live");
  show("runControls", mode === "auto");
  show("offlinePanel", mode === "offline");
  // 用户口径（2026-10-10）：设备栏在两屏都占整行（嗅探 + 状态 + 卡片），本屏控制栏在下一行。
  // 排布全在 CSS 里（`#workbar[data-mode=…]`）—— 排布不属于"状态"，不该由 JS 自己算。
  if ($("workbar")) $("workbar").dataset.mode = mode;
  // T7：阶段卡**只属于产生它的那一路**（run ⇒ 自动验证、实时观察会话 ⇒ 实时那一屏）。
  // 2026-10-10 起每屏各存一份（`stageBag()`）：所以换屏只需**重画这一屏的那一份**；
  // 不再把别人的卡挂到这一屏上，也不再因为“切了一下屏”把内容弄丢（别的窗口就是这个手感）。
  renderStepCards();
  renderObserve(app.observe);
  renderMetrics(app.metricsViews[mode] || null, mode);
  // 进离线诊断时读一次会话清单（只读目录）：刚结束的会话应该立刻能回看。
  if (mode === "offline") {
    renderSessionOptions();
    renderOffline();
    refreshSessions();
  }
  // 实时观察那一屏也要看观察会话（RUNTIME_MODES §12“历史观察”）：同样只读目录。
  if (mode === "live") refreshSessions();
  // 历史区跟着模式换标题 / 列 / 数据源（三种模式共用这一套组件）。
  renderHistory();
  updateButtons();
  // 换屏 = 换一组窗格（谁算“当前窗格”也变了）⇒ 阅读位那次的高度作废。
  clearReadingHeight();
}

function setMode(mode) {
  app.mode = MODE_IDS.includes(mode) ? mode : "auto";
  try {
    localStorage.setItem(MODE_KEY, app.mode);
  } catch (error) {
    /* 本机存不下也不影响使用 */
  }
  applyMode();
  refreshState();
}

/* ---------------------------------------------------------------- 命令框 */
/* 发送的是**字节**：文本模式 = 正文 + 所选行结束符；HEX 模式 = 你输入的字节，原样。
 *
 * 三条边界（都来自需求 §4.1）：
 *  - 非法十六进制**一个字节也不发**（页面先本地提示，服务端仍是权威）；
 *  - 平台不认识业务命令：没有白名单、没有命令建议；
 *  - HEX 模式不加"高级模式"开关、不截断到 256 B。
 *
 * 历史是**主机侧**的（浏览器本地，按端口隔离）：不依赖设备是否回显，也不冒充设备行为。
 * 方向键只在文本模式接管，HEX 模式下原样交给输入框。 */

function saveCmdHistory() {
  try {
    localStorage.setItem(CMD_HISTORY_KEY, JSON.stringify(app.cmdHistory));
  } catch (error) {
    /* 存不下也不影响本次使用 */
  }
}

/* 一次发送要带的字段（纯函数：宿主回归直接驱动）。 */
function cmdRequest(mode, text, lineEnding) {
  return {
    mode: mode === "hex" ? "hex" : "text",
    text: String(text ?? ""),
    line_ending: lineEnding || "crlf",
  };
}

function utf8Length(text) {
  let bytes = 0;
  for (const char of String(text ?? "")) {
    const code = char.codePointAt(0);
    bytes += code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
  }
  return bytes;
}

/* 十六进制本地预检：与服务端同一套规则（服务端仍是权威）。
 * ⚠️ 只说"行不行 + 为什么"：绝不修正、绝不部分发送。 */
function parseHexText(text) {
  const raw = String(text ?? "").trim();
  if (!raw) return { ok: false, error: "十六进制输入不能为空" };
  const cleaned = raw.replace(/0[xX]/g, "").replace(/[\s,;:]+/g, "");
  if (!cleaned) return { ok: false, error: "十六进制输入不能为空" };
  const bad = [...new Set(cleaned.split("").filter((char) => !/[0-9a-fA-F]/.test(char)))];
  if (bad.length) return { ok: false, error: `不是合法的十六进制字符：${bad.join(" ")}` };
  if (cleaned.length % 2) {
    return { ok: false, error: `十六进制必须是偶数个字符（现在 ${cleaned.length} 个）` };
  }
  const hex = (cleaned.toUpperCase().match(/../g) || []).join(" ");
  return { ok: true, hex, bytes: cleaned.length / 2, error: "" };
}

/* 输入框下面的提示：HEX 模式给出字节预览或错误；文本模式只说会发多少字节。 */
function cmdHint(mode, text, lineEnding) {
  const body = String(text ?? "");
  if (!body.trim()) return { text: "", kind: "" };
  if (mode === "hex") {
    const parsed = parseHexText(body);
    if (!parsed.ok) return { text: parsed.error, kind: "fail" };
    return { text: `${parsed.bytes} 字节 · ${parsed.hex}`, kind: "ok" };
  }
  const ending = { cr: 1, lf: 1, crlf: 2 }[lineEnding] || 1;
  const endingName = { cr: "CR", lf: "LF", crlf: "CRLF" }[lineEnding] || "CRLF";
  return { text: `${utf8Length(body) + ending} 字节（正文 + ${endingName}）`, kind: "" };
}

/* ↑/↓ 翻历史：`cursor=null` 表示"还没翻"，再往下翻回到空白（与 shell 一致）。 */
function historyStep(history, cursor, direction) {
  const items = Array.isArray(history) ? history : [];
  if (!items.length) return { cursor: null, text: null };
  const base = cursor === null || cursor === undefined ? items.length : cursor;
  const next = base + direction;
  if (next < 0) return { cursor: 0, text: items[0] };
  if (next >= items.length) return { cursor: null, text: "" };
  return { cursor: next, text: items[next] };
}

function commandPort() {
  const observe = app.observe || {};
  const session = observe.session || null;
  const device = observe.device || observe.target || null;
  return (session && session.port) || (device && device.port) || "unknown";
}

function rememberCommand(port, text) {
  const body = String(text ?? "").trim();
  if (!body) return;
  const list = app.cmdHistory[port] || [];
  if (list[list.length - 1] !== body) list.push(body);  // 连着重复发同一条只记一遍
  while (list.length > CMD_HISTORY_MAX) list.shift();
  app.cmdHistory[port] = list;
  app.cmdCursor = null;
  saveCmdHistory();
}

function currentCmdMode() {
  return $("observeCmdMode") ? $("observeCmdMode").value : "text";
}

function currentLineEnding() {
  return $("observeLineEnding") ? $("observeLineEnding").value : "crlf";
}

function refreshCmdHint() {
  const hint = $("observeCmdHint");
  if (!hint) return;
  const text = $("observeCmdInput") ? $("observeCmdInput").value : "";
  const info = cmdHint(currentCmdMode(), text, currentLineEnding());
  hint.textContent = info.text;
  hint.dataset.kind = info.kind;
}

/* 设备/项目配置里的默认行结束符（需求 §8）：只在**换设备**时套用，
 * 不动用户当前的选择（他可能刚手动改成 LF）。 */
function applyLineEndingDefault(observe) {
  const select = $("observeLineEnding");
  if (!select || !observe || !observe.commands) return;
  const target = observe.device || observe.target;
  const key = target ? target.key : "";
  if (!key || app.cmdEndingDevice === key) return;
  app.cmdEndingDevice = key;
  select.value = observe.commands.line_ending_default || "crlf";
}

async function consoleSend() {
  const mode = currentCmdMode();
  const text = $("observeCmdInput") ? $("observeCmdInput").value : "";
  if (!text.trim()) return;
  if (mode === "hex") {
    const parsed = parseHexText(text);
    if (!parsed.ok) {
      // 本地就知道不合法：**一个字节也不发**，只说为什么。
      showMessage(`没有发送：${parsed.error}`, "fail", "observe");
      refreshCmdHint();
      return;
    }
  }
  try {
    const payload = await postJson(
      "/api/observe/send", cmdRequest(mode, text, currentLineEnding()),
    );
    if (payload.observe) renderObserve(payload.observe);
    const sent = payload.sent || {};
    if (mode === "text") rememberCommand(commandPort(), text);
    showMessage(`已发送 ${sent.length} B：${sent.display} · ${sent.bytes_hex}`, "pass", "observe");
  } catch (error) {
    showMessage(`发送失败：${error.message}`, "fail", "observe");
  }
  refreshCmdHint();
}

function wireConsoleInput() {
  const input = $("observeCmdInput");
  if (input) {
    input.addEventListener("input", refreshCmdHint);
    input.addEventListener("keydown", (event) => {
      // 输入框是可以多行拉伸的 textarea：**回车发送**，Shift+回车换行
      // （多行命令/粘贴一段带换行的内容时不用绕路）。
      if (event.key === "Enter") {
        if (event.shiftKey) return;      // 让浏览器插入一个换行
        event.preventDefault();
        consoleSend();
        return;
      }
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      if (currentCmdMode() === "hex") return;  // HEX 模式原样输入，不抢方向键
      const step = historyStep(
        app.cmdHistory[commandPort()], app.cmdCursor, event.key === "ArrowUp" ? -1 : 1,
      );
      if (step.text === null) return;  // 没有历史：什么都不做（也不阻止默认行为）
      event.preventDefault();
      app.cmdCursor = step.cursor;
      input.value = step.text;
      refreshCmdHint();
    });
  }
  if ($("observeCmdSend")) $("observeCmdSend").addEventListener("click", consoleSend);
  if ($("observeCmdMode")) {
    $("observeCmdMode").addEventListener("change", () => {
      refreshCmdHint();
      updateButtons();
    });
  }
  if ($("observeLineEnding")) {
    $("observeLineEnding").addEventListener("change", refreshCmdHint);
  }
}

/* ------------------------------------------------------ 窗口内直接敲键（W6-4） */
/* 需求 §4.2 + W6-4：点日志窗口正文的空白处 ⇒ **这个窗口拿到键盘**，按键直接变成字节发给设备。
 *
 * 三条边界（都有测试守着）：
 *  1. **按键 → 字节只有服务端一份实现**：页面把按键描述（`{key, ctrl, alt, shift, meta}`）
 *     发过去，字节由 `console_input.build_keys_payload()` 算。两份映射一定会漂（）。
 *  2. **严格串行**：同一时刻只允许一个请求在飞，攒着的按键等它回来再发。
 *     串口是一个字节流，并发请求会让字节乱序到达 —— 终端最不能容忍的就是乱序。
 *  3. **不抢页面快捷键**：只有“已连接 + 这个窗口正拿着键盘”才吞按键，其它时候一个键都不拦。
 *     拖选（拖出选区）不捕获；正在捕获时拖选会先放开；`Esc` 放开键盘，放开后快捷键原样恢复。
 *
 * ⚠️ 平台**不识别固件能力**，所以这里不判断“这个设备支持不支持终端”，也不给
 * "本窗口支持/不支持终端"的提示：字节发出去之后由设备自己回答（服务端会拒绝不认识的按键）。
 * 换来的是：输入与输出在**同一屏**里（命令输入行就长在解码窗底部）。
 *
 * 你发出去的字节在「工具输出」窗口各占一行（TX），**不进 raw.bin**：那里只有设备说的话。 */

const TERMINAL_QUEUE_MAX = 200;   // 防呆上限：打字再快也不会把队列堆到天上去
const CAPTURE_PANES = ["raw", "decoded"];   // 哪两个窗口可以直接敲键
const CAPTURE_CLICK_SLOP = 4;     // 按下→松开的位移超过它就算"拖选"，不捕获键盘

/* 一次按键 → 描述（纯函数）。 */
function keyDescriptor(event) {
  return {
    key: String((event && event.key) || ""),
    ctrl: Boolean(event && event.ctrlKey),
    alt: Boolean(event && event.altKey),
    shift: Boolean(event && event.shiftKey),
    meta: Boolean(event && event.metaKey),
  };
}

/* 现在这一批该不该发：只有“没有在飞的请求”且“队列里有东西”时才发。
 * ⚠️ 少写一个 `!inFlight` 就会出现两个请求同时在飞 —— 那正是乱序的来源。 */
function shouldFlushTerminal(state) {
  return Boolean(state) && !state.inFlight && state.queue.length > 0;
}

function enqueueTerminalKey(state, descriptor) {
  state.queue.push(descriptor);
  if (state.queue.length > TERMINAL_QUEUE_MAX) {
    const dropped = state.queue.length - TERMINAL_QUEUE_MAX;
    state.queue.splice(0, dropped);
    state.dropped += dropped;
  }
  return state;
}

/* 取走整批（一批 = 一次 write = 一个 tx 事件）。 */
function takeTerminalBatch(state) {
  const batch = state.queue.splice(0, state.queue.length);
  state.batches += batch.length ? 1 : 0;
  return batch;
}

/* 一次鼠标操作该不该让这个窗口拿到键盘（纯函数：宿主回归直接核对它）。
 *
 * “点空白处”与“拖选”必须分得开：拿这个区别来决定要不要把键盘交给设备，
 * 否则想复制一段日志的人会发现自己的拖拽变成了往设备敲键。 */
function captureClickDecision(input) {
  const moved = Number(input && input.movedPx) || 0;
  if (!(input && input.connected)) return { capture: false, why: "没在观察：没有设备可以收字节" };
  if (input && input.hasSelection) return { capture: false, why: "这是选区：那一下是要选字" };
  if (moved > CAPTURE_CLICK_SLOP) return { capture: false, why: "这是拖选，不是点空白" };
  return { capture: true, why: "" };
}

/* 捕获中的那一小条状态（纯函数）。它说的是“**此刻**键盘在往设备发”，
 * 不是“这个窗口支持终端” —— 平台不识别固件能力，也就不作任何支持性承诺。 */
function captureStateText(id) {
  return id ? `⌨ 键盘在发字节给设备（${id === "raw" ? "原始串口" : "解码日志"}）· Esc 放开` : "";
}

function terminalState() {
  if (!app.terminal) {
    app.terminal = { queue: [], inFlight: false, batches: 0, keys: 0, bytes: 0, dropped: 0 };
  }
  return app.terminal;
}

/* 一次真实发送的读法：字节是证据，按键名是说明书。 */
function terminalSentText(sent) {
  const parts = [`已发送 ${sent.length} B`, sent.display, sent.bytes_hex];
  if (sent.keys && sent.keys.length) parts.push(`按键 ${sent.keys.join(" ")}`);
  return parts.join(" · ");
}

/* 现在哪个窗口拿着键盘（一次只允许一个；不是两个直敲窗口的号就是空）。 */
function capturingId() {
  return CAPTURE_PANES.includes(app.capture) ? app.capture : "";
}

function captureConnected() {
  return (app.observe || {}).state === "connected";
}

/* 把一个窗口的捕获状态画出来（窗口属性 + 正文边框 + 那一小条状态）。 */
function renderCapture() {
  const id = capturingId();
  for (const paneId of CAPTURE_PANES) {
    const on = paneId === id;
    const pane = $(`pane-${paneId}`);
    const body = $(`lines-${paneId}`);
    const chip = $(`cap-${paneId}`);
    if (pane) pane.dataset.capture = on ? "1" : "0";
    if (body) body.dataset.capture = on ? "1" : "0";
    if (chip) {
      chip.textContent = on ? captureStateText(paneId) : "";
      chip.title = "点这里放开键盘（也可以按 Esc）：放开之后页面快捷键恢复，按键不再发给设备";
    }
  }
}

/* 把键盘交给这个窗口。焦点一起放过去：按键事件得有个落点。 */
function setCapture(id) {
  if (!CAPTURE_PANES.includes(id)) return "";
  app.capture = id;
  const body = $(`lines-${id}`);
  if (body && body.focus) body.focus({ preventScroll: true });
  renderCapture();
  return id;
}

function releaseCapture() {
  if (!app.capture) return;
  app.capture = "";
  renderCapture();
}

/* 这个事件该不该被拿到键盘的那个窗口接管（全局快捷键让位用；纯函数，正文可注入以便宿主回归）。 */
function paneCaptureOwns(event, captureId, body = $(`lines-${captureId}`)) {
  if (!captureId || !event || !body) return false;
  const target = event.target;
  if (!target) return false;
  return target === body || Boolean(body.contains(target));
}

/* 这次按键值不值得发出去（修饰键自己不发，由服务端给的名单决定）。 */
function terminalKeyIsSendable(descriptor) {
  const terminal = (app.observe || {}).terminal || null;
  const modifiers = terminal ? terminal.modifier_keys : [];
  return !isModifierKeyOnly(descriptor && descriptor.key, modifiers);
}

/* 只按下修饰键（Ctrl / Shift / Alt / Meta）不产生字节：**名单由服务端给**
 * （`observe.terminal.modifier_keys`）。硬写一份名单就是第二次维护映射，
 * 而且漏掉它会让页面把"单按 Ctrl"当一批按键发上去 —— 服务端只能 400，
 * 用户看到的就是一条莫名其妙的"发送失败"（真机验收当场遇到的）。 */
function isModifierKeyOnly(key, modifiers) {
  const name = String(key || "");
  return name === "" || (Array.isArray(modifiers) && modifiers.includes(name));
}

/* 换了一次会话：队列与计数清干净（上一轮的按键不该混进这一轮）。
 * 显示层已经不在页面上了（键盘直接敲在两个窗口里），所以这里只动状态。 */
function resetTerminal() {
  const state = terminalState();
  state.queue.length = 0;
  state.inFlight = false;
  state.batches = 0;
  state.keys = 0;
  state.bytes = 0;
  state.dropped = 0;
  releaseCapture();
}

async function flushTerminal() {
  const state = terminalState();
  if (!shouldFlushTerminal(state)) return;
  const batch = takeTerminalBatch(state);
  state.inFlight = true;
  try {
    const payload = await postJson("/api/observe/keys", { keys: batch });
    if (payload.observe) renderObserve(payload.observe);
    const sent = payload.sent || {};
    state.bytes += sent.length || 0;
    state.keys += (sent.keys || []).length;
    // ⚠️ 成功后**不在窗口里画任何东西**（真机验收提出）：那行“已发送 … B · display · HEX”
    // 会飘在最新几行上、每个键覆写一次 —— 而人正盯着那里看设备输出，被它挡得看不清。
    // 字节本身的去处不变：
    //   · 「工具输出」窗口里一行 TX（服务端发的，与证据同源）；
    //   · 证据在会话目录的 `io_events.jsonl`（`tx` 事件，带 bytes_hex 与按键名）。
    state.last = sent;
  } catch (error) {
    state.dropped += batch.length;
    // ⚠️ **不重发**：请求失败时我们无法判断字节是否已经写进串口，重发可能变成重复发送。
    showMessage(
      `按键发送失败：${error.message}（这批按键不重发：重发有重复发送的风险，请自己确认设备收到了什么）`,
      "fail", "observe",
    );
  } finally {
    state.inFlight = false;
    if (shouldFlushTerminal(state)) flushTerminal();  // 攒在后面的接着按顺序走
  }
}

/* 两个能直敲的窗口：鼠标点空白处就拿到键盘，Esc 放开。
 * `mousedown` 只记起点，`mouseup` 才算位移 —— 拖选（选字）与点击分得开。 */
function wirePaneCapture() {
  for (const paneId of CAPTURE_PANES) {
    const body = $(`lines-${paneId}`);
    const chip = $(`cap-${paneId}`);
    if (chip) chip.addEventListener("click", () => releaseCapture());
    if (!body) continue;
    let down = null;
    body.addEventListener("mousedown", (event) => {
      down = event.button === 0 ? { x: event.clientX, y: event.clientY } : null;
    });
    body.addEventListener("mouseup", (event) => {
      if (!down) return;
      const moved = Math.abs(event.clientX - down.x) + Math.abs(event.clientY - down.y);
      down = null;
      const selection = window.getSelection ? window.getSelection() : null;
      const hasSelection = Boolean(selection && !selection.isCollapsed);
      const decision = captureClickDecision({
        movedPx: moved, hasSelection, connected: captureConnected(),
      });
      if (decision.capture) {
        setCapture(paneId);
        return;
      }
      // 没捕获：只把“正拿着键盘的窗口被拖选了”这件事处理掉（那一下是要选字，不是要敲键）。
      if (hasSelection && capturingId() === paneId) releaseCapture();
    });
    body.addEventListener("keydown", (event) => {
      if (capturingId() !== paneId) return;   // 没拿到键盘：一个键也不吞，页面快捷键照常
      if (event.key === "Escape") {
        // Esc = 放开键盘：Tab / 方向键都是要发给设备的字节，页面里需要一个不产生字节的"离开"入口。
        // （要往设备发 0x1B 的话用命令输入行的 HEX 格式，那是另一个入口。）
        event.preventDefault();
        event.stopPropagation();
        releaseCapture();
        return;
      }
      if (event.isComposing) return;      // 输入法组合中：不抢（本版不做 IME 直输）
      const descriptor = keyDescriptor(event);
      // 只按下修饰键：这次按**没有字节**，但也**不算错误** —— 吞掉它，不发请求。
      // （方向键/字母这类按键仍然要 preventDefault，否则页面会拿它去滚页/换焦点。）
      event.preventDefault();
      event.stopPropagation();
      if (!terminalKeyIsSendable(descriptor)) return;
      enqueueTerminalKey(terminalState(), descriptor);
      flushTerminal();
    });
  }
  // 焦点跑到这两个窗口外面（比如点进了命令输入框、点了别的面板）就放开键盘：
  // 否则“键盘归谁”会与实际情况不符，而那个不符很难被看出来。
  document.addEventListener("focusin", (event) => {
    const id = capturingId();
    if (!id) return;
    const body = $(`lines-${id}`);
    const target = event.target instanceof Element ? event.target : null;
    if (body && target && (target === body || body.contains(target))) return;
    releaseCapture();
  });
}

/* ------------------------------------------------------- 命令输入行（W6-1） */
/* 输入与输出必须在同一屏里：命令输入行就长在**解码窗口的最底端**，
 * 上沿可上下拖动改高度（↑/↓ 微调，双击回默认），拖到最上收成一个 ▲ 按钮，
 * 点 ▲ 展开回记住的高度。高度与折叠状态是阅读偏好（存本机浏览器）。
 *
 * ⚠️ 它跟着「解码日志」窗口走：那一个窗口收成侧边标签（或全屏到别的窗口）时，
 * 输入行跟着一起收起来 —— 它本来就是那个窗口的一部分，不是全局悬浮面板。 */

function dockHeight() {
  return clampCmdHeight(app.prefs && app.prefs.observe ? app.prefs.observe.cmdHeight : undefined);
}

function dockCollapsed() {
  return Boolean(app.prefs && app.prefs.observe && app.prefs.observe.cmdCollapsed);
}

function applyDock() {
  const dock = $("cmdDock");
  if (!dock) return;
  const height = dockHeight();
  const collapsed = dockCollapsed();
  dock.dataset.collapsed = collapsed ? "1" : "0";
  dock.style.setProperty("--cmd-input-h", `${height}px`);
  if ($("cmdOpen")) $("cmdOpen").hidden = !collapsed;
  const handle = $("cmdResize");
  if (handle) {
    handle.setAttribute("aria-valuenow", String(height));
    handle.title = `上下拖动改高度（当前 ${height} px；↑/↓ 微调，双击回到默认 ${CMD_H_DEFAULT} px）；`
      + "拖到最上收成一个 ▲ 按钮";
  }
  // 窗口里的临时提示与正文高度都要知道"底下的东西占了多高"：这一窗底部是 `#paneFootDecoded`
  // （常驻说明 + 命令 dock）。--pane-foot-h 就写它**实测**的高度：
  // `.pane-notice` 靠它避让，`.log-lines` 靠它把高度让出来（不减的话整个网格行会被撑高，
  // 同行那个窗口底下留一块空白 —— 看起来像日志丢了）。
  // ⚠️ 量那个元素本身，别用 `pane.clientHeight - 正文`：窗口被网格拉伸之后，
  //   差里会混进"被拉出来的空余"，那个数只能越量越偏。
  // 只有这一个写入口（`syncPaneFootHeight`）：说明行出现/消失、命令行被拖动都会改变底栏高度，
  // 那边由看着底栏的 ResizeObserver 跟着重量，两边不能各写一份。
  syncPaneFootHeight();
}

/* 拖到最上就收起：这是“收到最小”的那一句要求，不是两个状态。
 *
 * ⚠️ 收起时**不把最小高度记成“记住的高度”**（否则点 ▲ 展开回来只有 26 px，
 * 而要求是“点击展开输入框默认大小”）：所以只记 > 最小值的那些高度。 */
function setDockHeight(value, options = {}) {
  const height = clampCmdHeight(value);
  ensureObservePrefs();
  const collapsed = height <= CMD_H_MIN;
  app.prefs.observe.cmdCollapsed = collapsed;
  if (!collapsed) app.prefs.observe.cmdHeight = height;
  if (options.persist !== false) savePrefs();
  applyDock();
  return height;
}

/* 展开时用哪个高度：**默认大小**。
 * 需求原话是“点击展开输入框默认大小” —— 所以不去猜“要不要恢复你上次拉到的高度”：
 * 猜错时人会以为面板坏了。拉过的高度仍然记在偏好里，刷新页面后还在。 */
function ensureObservePrefs() {
  if (!app.prefs.observe) {
    app.prefs.observe = { cmdHeight: CMD_H_DEFAULT, cmdCollapsed: false };
  }
  return app.prefs.observe;
}

function wireCommandDock() {
  const handle = $("cmdResize");
  const open = $("cmdOpen");
  if (open) open.addEventListener("click", () => setDockHeight(CMD_H_DEFAULT));
  if (!handle) return;
  let drag = null;
  handle.addEventListener("pointerdown", (event) => {
    drag = { pointerId: event.pointerId, y: event.clientY, height: dockHeight() };
    try { handle.setPointerCapture(event.pointerId); } catch (error) { /* 没有指针捕获也能用 */ }
    document.body.classList.add("dock-resizing");
  });
  handle.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    // 往上拖 = 变高：看得见的那块跟着鼠标上沿走。
    setDockHeight(drag.height + (drag.y - event.clientY), { persist: false });
  });
  const stop = (event) => {
    if (!drag || (event && event.pointerId !== drag.pointerId)) return;
    drag = null;
    try { handle.releasePointerCapture(event.pointerId); } catch (error) { /* 已经释放了 */ }
    document.body.classList.remove("dock-resizing");
    savePrefs();
  };
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
  handle.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowUp" ? 6 : (event.key === "ArrowDown" ? -6 : 0);
    if (!step) return;
    event.preventDefault();
    setDockHeight(dockHeight() + step);
  });
  handle.addEventListener("dblclick", () => setDockHeight(CMD_H_DEFAULT));
}

/* -------------------------------------------------------------------- steps */
function renderSteps(steps) {
  const list = $("steps");
  list.textContent = "";
  const byName = new Map((steps || []).map((step) => [step.name, step]));
  for (const name of STEP_ORDER) {
    const step = byName.get(name) || { name, status: "pending" };
    const item = document.createElement("li");
    item.dataset.status = step.status || "pending";
    const value =
      step.status === "running" ? "进行中"
      : step.status === "pass" ? "通过"
      : step.status === "fail" ? "失败"
      : "等待";
    item.innerHTML = `
      <div class="step-name">${escapeHtml(name)}</div>
      <div class="step-value">${escapeHtml(value)}${
        step.duration_ms !== null && step.duration_ms !== undefined
          ? " · " + escapeHtml(formatDuration(step.duration_ms)) : ""
      }</div>
      <div class="step-detail">${escapeHtml(step.detail || "")}</div>`;
    list.appendChild(item);
  }
}

/* ------------------------------------------------------------- 阶段卡片（T3） */
/* 与上面 `renderSteps` 的区别：那是**平台六步**（precheck…assert），这一层是**里面的测试**
 * （要么是用例，要么是受控序列的每一步）。两者共用同一条事件通道，**不是第二套流水线**。 */

/** 终态集合 —— 与后台 `step_lifecycle.TERMINAL_STATUSES` 一致（**不新增口径**）。 */
const STEP_TERMINAL = new Set(["PASS", "FAIL", "ERROR", "SKIPPED", "CANCELLED"]);

const STEP_STATUS_TEXT = {
  pending: "等待", running: "进行中", PASS: "通过", FAIL: "失败",
  ERROR: "出错", SKIPPED: "跳过", CANCELLED: "已取消",
};

function resetTestSteps(view = attachedView()) {
  stageBag(view).rows = new Map();
  stageBag(view).seen = false;
  stageBag(view).note = "";
  stageBag(view).conclusion = "";
  // ⚠️ 批号不在这里清：它说的是“现在说的是哪一批”，与“这一批画了什么”不是一回事
  //    （清了会把“第一次知道批号”误判成“换了一批”）。
  if (app.testStepTimer) {
    clearInterval(app.testStepTimer);
    app.testStepTimer = null;
  }
  renderStepCards();
}

/** 收一条 `test_step` 事件（实时与回放**共用这一条路径**）。
 *
 * ⚠️ “终态不可回退”在前端也要成立：迟到的 `running` 不能把已经 FAIL 的卡片改回“进行中”。
 * 后台会拒绝这种写，页面同样不该画出来 —— 两端同一条规矩。
 */
function applyTestStep(row, view = attachedView()) {
  if (!row || !row.case_id) return;
  const bag = stageBag(view);
  const id = String(row.case_id);
  const previous = bag.rows.get(id);
  if (previous && STEP_TERMINAL.has(previous.status)) return;
  bag.rows.set(id, {
    stage_id: id,
    label: row.label || (previous && previous.label) || id,
    expected: row.expected || (previous && previous.expected) || "",
    version: row.version ?? (previous ? previous.version : 1),
    status: row.status || "pending",
    detail: row.detail || ((previous && previous.detail) || ""),
    started_utc: row.started_utc || ((previous && previous.started_utc) || null),
    ended_utc: row.ended_utc || ((previous && previous.ended_utc) || null),
    duration_ms: row.duration_ms ?? ((previous && previous.duration_ms) ?? 0),
    evidence: (row.evidence && row.evidence.length) ? row.evidence : ((previous && previous.evidence) || []),
    // T5/T6：阶段**当时可得**的证据坐标（tool/raw）；decoded/app_events 由 decode 之后的
    // `test_step_refs` 补进来（见 applyTestRefs）。
    evidence_refs: (row.evidence_refs && row.evidence_refs.length)
      ? row.evidence_refs
      : ((previous && previous.evidence_refs) || []),
  });
  bag.seen = true;
}

/*: 四类证据的固定顺序（与 core 的 `EVIDENCE_SOURCES` 一致）。
 * 第四类叫 `app_events`（对应 `app_events.jsonl`）—— 与平台事件流 `events.jsonl` 不是一回事。 */
const EVIDENCE_REF_ORDER = ["tool", "raw", "decoded", "app_events"];

/** 合并两份坐标：同来源以 `patch`（补写）为准，补写没给的保留实时那一份。
 * 与 core 的 `step_lifecycle.merge_refs()` 同一套规矩 —— 顺序也固定。 */
function mergeEvidenceRefs(base, patch) {
  const bySource = new Map();
  for (const item of [...(base || []), ...(patch || [])]) {
    const source = String((item && item.source) || "");
    if (!EVIDENCE_REF_ORDER.includes(source)) continue;
    const anchor = Number(item.anchor_line);
    bySource.set(source, {
      source,
      start_line: Number(item.start_line),
      end_line: Number(item.end_line),
      /* ⚠️ 中心锚点**合并时不能丢**（用户口径 2026-10-13）：丢了就退回“区间末行”，
         而补写那一份正是带锚点的（`@TASKS` 这类决定性证据行）。旧记录本来就没有这个键
         ⇒ 这里也不写，前端自己按末行回退。 */
      ...(Number.isFinite(anchor) ? { anchor_line: anchor } : {}),
    });
  }
  return EVIDENCE_REF_ORDER.filter((name) => bySource.has(name)).map((name) => bySource.get(name));
}

/** 收一条 `test_step_refs` 事件（T5 补写）：**只补坐标**。
 *
 * ⚠️ 用户 2026-10-09 口径：它不得修改阶段状态、耗时和判定。这里同样是结构性的 ——
 * 这个函数除了 `evidence_refs` 之外**不写任何字段**（连 `status` 都不读）。
 * 版本对不上就不合：声明改了语义（version +1）之后，旧补写不能再贴到新语义上。
 */
function applyTestRefs(row, view = attachedView()) {
  if (!row || !row.case_id) return;
  const previous = stageBag(view).rows.get(String(row.case_id));
  if (!previous) return; // 没见过这个阶段：等它自己的 test_step 先把卡片立起来
  if (Number(row.version) !== Number(previous.version)) return;
  previous.evidence_refs = mergeEvidenceRefs(previous.evidence_refs, row.evidence_refs);
}

/** 会话冻结块里的阶段行（ / L3，）。
 *
 * 素材就在会话自己的 `manifest.live_tests` 里：收口时冻结的 `executions[].steps[]`
 * **本来就是当时广播出去的那几行 `test_step`**（含版本、状态、耗时、证据坐标）——
 * 阶段变化刻意不逐步落盘（见 `ObserveCenter._record_live_tests`），所以回放素材只有这一份，
 * 也**只需要**这一份：不接第二套回放器、不给会话包加成员。
 * 只有定义、还没收口（批次中途结束）的会话退回**定义**：卡片立起来、状态如实是“等待”。
 */
function frozenLiveTestSteps(block) {
  if (!block || typeof block !== "object") return [];
  const rows = [];
  for (const execution of (Array.isArray(block.executions) ? block.executions : [])) {
    const steps = Array.isArray(execution && execution.steps) ? execution.steps : [];
    for (const step of steps) {
      if (step && step.case_id) rows.push(step);
    }
  }
  if (rows.length) return rows;
  for (const definition of (Array.isArray(block.definitions) ? block.definitions : [])) {
    const steps = Array.isArray(definition && definition.steps) ? definition.steps : [];
    for (const step of steps) {
      if (!step || !step.stage_id) continue;
      rows.push({
        kind: "test_step", case_id: step.stage_id, version: step.version,
        label: step.label, expected: step.expected, status: "pending",
      });
    }
  }
  return rows;
}

/** 把一份**冻结的阶段块**还原进某一屏的阶段窗。两个调用方共用它：
 *
 * - **离线诊断**：会话收口时冻进 `manifest.live_tests` 的那一份
 *   （需求 §10.5：会话结束后恢复相同用例版本、状态、耗时；§8.4：有引用就完整恢复、
 *    没有就说清“没有定位信息”）；
 * - **实时观察**：`/api/state` 里的 `live_tests.batch` —— 形状与冻结块**同源**
 *   （`TestStepRecorder.snapshots()` 的 docstring 写的就是“刷新页面时用它重建”）。
 *
 * 与回放一次 run（`loadRun`）只差“素材从哪来”——判断与画法**完全共用** `applyTestStep`。
 *
 * `expand`（默认 true）：这一窗本来是空的（收成竖排标签）时，装进阶段就展开它。
 *   - 离线回看：**展开**——使用者刚点了“回看这次会话”，要看的就是它；
 *   - 刷新/轮询重建（`restoreLiveStagesFromState`）：**不展开**——那是个自动动作，
 *     不能把使用者刚收起来的那一窗又拨开（窗格是**阅读偏好**，落盘在本机）。
 */
function restoreSessionStages(block, view, { expand = true } = {}) {
  const firstOfThisPane = !stageBag(view).seen;
  resetTestSteps(view);
  const rows = frozenLiveTestSteps(block);
  for (const row of rows) applyTestStep(row, view);
  const bag = stageBag(view);
  if (!rows.length) {
    // 需求 §8.4 的第二句：没有素材时说清“没有”，不留一个空位让人猜。
    bag.note = block
      ? "这次会话的冻结块里没有阶段（受控测试没有跑到任何一步）"
      : "这次会话没有受控测试记录：阶段窗只在会话真的跑过受控测试时有内容";
  } else {
    // 结论只在**收口了**的时候当结论说；中途结束的会话如实说“没收口”。
    bag.conclusion = STEP_TERMINAL.has(String(block.status))
      ? liveTestStatusText({ batch: block })
      : `会话结束时这一批还没收口（当时状态 ${block.status || "未记录"}）`;
    const definitions = Array.isArray(block.definitions) ? block.definitions : [];
    bag.note = `离线回放（这次会话冻结的那一份）：用例定义 ${definitions.length} 条`
      + ` · 阶段 ${rows.length} 步 · 按当时那一版定义，不查当前目录`;
  }
  // 与实时那一屏同一条手势：这一窗本来是空的（收成竖排标签）时，装进阶段就展开它。
  if (expand && rows.length && firstOfThisPane) expandPane(storeFor(view), "stage");
  if (attachedView() === view) renderStepCards();
  return rows.length;
}

/** 现在说的是**哪一批**：批号变了就把上一批的卡清掉。
 *
 * ⚠️ 为什么必须清（实测踩到）：`applyTestStep` 拒绝回退终态，而两次批次用的是**同一组**
 *    `case_id`（`write`/`read`/`restore`/`verify`）⇒ 不清的话，第二批的步骤会被上一批的
 *    PASS 挡住（不提不报），屏幕上看到的还是上一批的结果，而面板上写着的是新批次。
 * ⚠️ “第一次知道批号”不算换批：刷新后先知道批号、再去重建内容。
 *
 * 两个调用口：SSE 的 `live_test event=started`（**批次一开始**就清 —— 这样第一批
 * `test_step` 到时这一窗是空的，自动展开照旧生效）与状态轮询（兼底：页面在两次事件
 * 之间才被打开的场合）。
 */
function useLiveStageBatch(batchId) {
  const bag = stageBag(app.views.live);
  const id = String(batchId || "");
  if (!id || bag.batchId === id) return false;
  const first = !bag.batchId && !bag.rows.size;
  bag.batchId = id;
  if (first) return false;
  resetTestSteps(app.views.live);
  return true;
}

/** 这一批收口了没有 —— 把结论写到阶段窗那一行上（活着的批次也要说）。
 *
 * 不这么写的话，只有“刷新之后重建”那条路会写上结论；而边跑边看的场合，那几行
 * 会一直是光秃秃的“N 个阶段”，与同一屏的受控用例面板（写着“已收口：PASS（PASS 2）”）
 * 说法不一致。只改这一行文字，不动任何卡。
 */
function syncLiveStageConclusion(batch) {
  const bag = stageBag(app.views.live);
  if (!bag.rows.size) return false;                       // 没卡就别说话
  const next = batch && STEP_TERMINAL.has(String(batch.status))
    ? liveTestStatusText({ batch }) : "";
  if (bag.conclusion === next) return false;
  bag.conclusion = next;
  return true;
}

/** 刷新/换屏之后把**实时那一批**的阶段重建出来（用户口径 2026-10-12：测试结束后保持展开）。
 *
 * 三个“只”都是实测踩出来的：
 * * **只补已经收口的那一批** —— 跑着的那一批由 SSE 一条条喂（首条到了会自动展开这一窗）。
 *   实测教训：把“还在跑”的那一批也补进来，会① 抢在 SSE 前面把展开手势顶掉；
 *   ② 把当时的中间状态冻成结论（批次都 PASS 了，那一行还写着“还没收口”）。
 * * **只在这一屏还空着时**补：SSE 到了之后就不再碰它 —— 两处都往同一份里写就会出现
 *   “谁最后说话谁对”。
 * * **不动布局**（`expand: false`）：重建是自动动作，窗格收/展是使用者的阅读偏好。
 *   唯一的例外：这一窗现在是收成标签的，而且**不是用户收的**（`layoutTouched` 没记它）
 *   — 那是我们自己的默认值收的，可以拨开（否则“里面卡都在、屏幕上只有一个标签”）。
 */
function restoreLiveStagesFromState() {
  if (app.mode !== "live") return 0;             // 只有看着这一屏时才动它
  const view = app.views.live;
  const bag = stageBag(view);
  if (bag.seen || bag.rows.size) return 0;        // 已经有阶段（SSE 给的）：不覆盖
  const snapshot = (app.observe && app.observe.live_tests) || null;
  const batch = snapshot && snapshot.batch;
  if (!batch || !STEP_TERMINAL.has(String(batch.status))) return 0;
  if (!frozenLiveTestSteps(batch).length) return 0;
  // 这一窗现在是死的（收成标签）且**不是用户收的** ⇒ 拨开：卡在里面，屏幕上却只有一个标签，
  // 那看起来就是“测试没跑过”。用户自己收的那一窗不动（那是他的阅读偏好，已落盘）。
  const expand = paneCollapsed("stage") && !layoutTouched.has("stage");
  return restoreSessionStages(batch, view, { expand });
}

/** 卡片上只写 **raw** 的行号（需求 §8.2）：其余三类留在数据里，不上卡片（免得过载）。
 * 它们不是被丢掉 —— 定位与四窗同步会用完整的那份。 */
function describeRawRef(row) {
  const refs = row.evidence_refs || [];
  const raw = refs.find((item) => item.source === "raw");
  const others = refs.filter((item) => item.source !== "raw").length;
  if (!raw) {
    // 需求 §8.4：旧包/没补写的时候要说清“没有定位信息”，而不是留一个空位让人猜。
    return others ? "定位：这一阶段没有原始串口行号" : "定位：这份证据里没有定位信息";
  }
  return `定位：原始串口第 ${raw.start_line}–${raw.end_line} 行`;
}

/** 一个阶段已经跑了多久（毫秒）。
 * 跑着的时候用**事件里的起点**与当前时刻现算（后台那条 `duration_ms` 是上次事件那一刻的，
 * 拿它当真会显示一条不动的数字）；定局之后就用后台给的那个定格值。 */
function liveDurationMs(row, nowMs) {
  if (row.status !== "running" || !row.started_utc) return row.duration_ms || 0;
  const started = Date.parse(row.started_utc);
  if (Number.isNaN(started)) return row.duration_ms || 0;
  return Math.max(0, nowMs - started);
}

/* ------------------------------------------------- 阶段窗口（第 5 格）的渲染器 */
/* 这一窗的正文不是“行”，是卡片（`<li class="stage">`）。所以行机制（renderPending /
 * renderView / trimSource）在它身上整个跳过（见 `renderView` 的 cards 分支），由这里自己管：
 * 重画 = 按**这一屏那一份**（`stageBag()`）重建 `<ul>`；筛选 = 给卡片打 `hidden`；
 * 命中跳转 = 滚到那一张卡。
 *
 * ⚠️ 为什么不把阶段渲染成文本行：卡片里有按钮（定位）和可展开的细节（预期/现状/证据/坐标），
 *    文本行放不下；而“定位”是需求 §8.1 明确要求的动作。 */

/** 这一窗的卡片列表容器。行机制不碰它；万一有别的路径清过容器，这里把 `<ul>` 装回去
 *  —— 卡片必须永远画得出来，不该因为“谁动了一下 DOM”就整窗空白。 */
function stageListHost() {
  const host = $("lines-stage");
  if (!host) return null;
  let list = $("stageList");
  if (!list || list.parentElement !== host) {
    list = document.createElement("ul");
    list.className = "stage-list";
    list.id = "stageList";
    host.textContent = "";
    host.appendChild(list);
  }
  return list;
}

/** 卡片窗的接线。与行流窗口**共有**的那几件（折叠筛选行 / 全屏 / 收起成标签 / 拖宽 /
 *  分栏 / 独立滚动 / 复制全选）在 `buildWorkspace()` 里走同一个分支，这里只接它自己那套。 */
function wireStagePane(store, meta) {
  const source = store.sources[meta.id];
  const src = () => store.sources[meta.id];
  if (source.pauseBtn) source.pauseBtn.addEventListener("click", () => toggleStagePause(store));
  if (source.tailBtn) source.tailBtn.addEventListener("click", () => stageJumpToLatest(store));
  if (source.pill) source.pill.addEventListener("click", () => stageJumpToLatest(store));
  if (source.prevBtn) source.prevBtn.addEventListener("click", () => stageGotoHit(store, -1));
  if (source.nextBtn) source.nextBtn.addEventListener("click", () => stageGotoHit(store, 1));
  if (source.searchInput) {
    let timer = 0;
    const apply = () => {
      src().view.query = source.searchInput.value.trim();
      src().view.hitCursor = 0;
      applyStageFilter(store);
    };
    // 与行流窗口同一个口径：边打边筛，但不是每个键都重建一次。
    source.searchInput.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(apply, 120);
    });
    source.searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        clearTimeout(timer);
        apply();
        stageGotoHit(store, event.shiftKey ? -1 : 1);
      } else if (event.key === "Escape") {
        source.searchInput.value = "";
        clearTimeout(timer);
        apply();
      }
    });
    source.searchInput.addEventListener("focus", () => setLastPane(meta.id));
  }
}

/* 暂停 = 冻结这一窗的画面（模型照收：那一份（`stageBag()`）一直在长）。冻结期间到达的阶段记在
 * 药丸上（“新阶段 N 个”），点药丸或点“继续”补齐 —— 与行流窗口同一个口径。 */
function toggleStagePause(store) {
  const source = store.sources.stage;
  if (source.paused) {
    resumeSource(store, "stage");
    source.pending = 0;
    renderStepCards({ force: true });
  } else {
    setPaused(store, "stage", true);
  }
  updateLogIndicators(store);
}

/* 跳尾 = 补齐暂停期间的阶段并滚到最新的一张卡（行流窗口的“跳尾”是滚到最新一行）。 */
function stageJumpToLatest(store) {
  const source = store.sources.stage;
  resumeSource(store, "stage");
  source.pending = 0;
  source.followTail = true;
  renderStepCards({ force: true });
  if (source.el) source.el.scrollTop = source.el.scrollHeight;
  updateLogIndicators(store);
}

/** 当前**可见**（通过筛选）的卡片，按屏幕顺序。命中跳转与统计都用它。 */
function stageVisibleItems() {
  const list = $("stageList");
  if (!list) return [];
  return Array.from(list.querySelectorAll(".stage")).filter((item) => !item.hidden);
}

/* 这一窗的“搜索”= 筛选阶段卡：命中的留下、其余收起来（不是删掉 —— 改一下条件就回来）。
 * 只改 `hidden`，不重建 DOM：滚动位置与展开状态都保住（行流窗口的过滤也是这个口径）。 */
function applyStageFilter(store) {
  const source = store.sources.stage;
  const query = (source.view.query || "").toLowerCase();
  const list = $("stageList");
  if (!list) return;
  for (const item of list.querySelectorAll(".stage")) {
    const text = (item.textContent || "").toLowerCase();
    item.hidden = Boolean(query) && !text.includes(query);
    item.dataset.hit = "0";
  }
  const visible = stageVisibleItems();
  if (source.view.hitCursor >= visible.length) source.view.hitCursor = 0;
  if (query && visible.length) stageMarkHit(visible[Math.min(source.view.hitCursor, visible.length - 1)]);
  stageIndicators(source);
}

/** 把“当前命中”标出来并滚到视野中间（只在这一窗内）——与行流窗口的 gotoHit 同一个行为。
 * ⚠️ 只动**这一窗自己的** `scrollTop`：`scrollIntoView` 会把页面一起滚（PW-UX-001 用户
 *    看到的“一跳转整页乱跳”），这个项目里已经明令不许再用（宿主契约盯着这个 API 的调用）。 */
function stageMarkHit(item) {
  const list = $("stageList");
  if (!list) return;
  for (const node of list.querySelectorAll('.stage[data-hit="1"]')) node.dataset.hit = "0";
  if (!item) return;
  item.dataset.hit = "1";
  const host = item.closest(".log-lines");
  if (!host) return;
  const hostRect = host.getBoundingClientRect();
  const itemRect = item.getBoundingClientRect();
  host.scrollTop += (itemRect.top - hostRect.top) - (hostRect.height - itemRect.height) / 2;
}

/* 上/下一个命中（Enter / Shift+Enter / ↑ / ↓）。没有命中就什么也不做 —— 并且说出来。 */
function stageGotoHit(store, step) {
  const source = store.sources.stage;
  const visible = stageVisibleItems();
  if (!(source.view.query || "").trim() || !visible.length) {
    paneNotice(source, visible.length ? "先输入要筛选的关键字（回车跳第一个命中）" : "没有命中的阶段");
    return;
  }
  const current = Math.min(Math.max(0, source.view.hitCursor | 0), visible.length - 1);
  const next = (current + step + visible.length) % visible.length;
  source.view.hitCursor = next;
  stageMarkHit(visible[next]);
  stageIndicators(source);
}

/** 这一窗自己的两个数（统计行 + 命中计数）。行流窗口的这两个数由 `updateLogIndicators`
 *  按行模型算；卡片窗没有行模型，所以由这里算 —— 那边对卡片窗**跳过**这两个写入，
 *  否则会拿“0 行”覆盖掉卡片自己数出来的数。 */
function stageIndicators(source) {
  const list = $("stageList");
  const items = list ? Array.from(list.querySelectorAll(".stage")) : [];
  const visible = items.filter((item) => !item.hidden);
  const query = source.view.query || "";
  if (source.hitsLabel) {
    const shown = visible.length
      ? Math.min(Math.max(0, source.view.hitCursor | 0), visible.length - 1) + 1
      : 0;
    source.hitsLabel.textContent = query ? `${shown}/${visible.length}` : "0/0";
    source.hitsLabel.dataset.state = query && !visible.length ? "none" : "";
    source.hitsLabel.title = query
      ? `命中 ${visible.length} 个阶段（回车或 ↓ 跳下一个，Shift+Enter 或 ↑ 跳上一个）`
      : "输入关键字筛选阶段卡；回车跳到第一个命中";
  }
  if (source.stat) {
    const bag = stageBag();
    const parts = [`${items.length} 个阶段`];
    if (query) parts.push(`命中 ${visible.length}`);
    if (source.paused) parts.push(source.pending ? `暂停中·新到 ${source.pending}` : "暂停中");
    if (source.running) parts.push("运行中");
    // 这一批的**冻结结论**（ 加的那一条）现在是**悬停**的内容，不再占屏幕
    // （用户口径 2026-10-12 晚：屏幕上不放「已收口：PASS（PASS 2）」这类总结 ——
    //  结果已经在每张卡上，长说明与结论都在 title 里）。离线回放同样走这一条。
    source.stat.textContent = parts.join(" · ");
    source.stat.title = [parts.join("\n"), bag.conclusion, bag.note].filter(Boolean).join("\n");
  }
}

/** 重画这一窗（唯一入口）。`force` = 忽略暂停（恢复 / 跳尾 / 切模式时用）。 */
function renderStepCards({ force = false } = {}) {
  const list = stageListHost();
  if (!list) return;
  const source = attachedLog.sources.stage;
  const rows = Array.from(stageBag().rows.values());
  if (source.paused && !force) {
    source.pending = Math.max(0, rows.length - (source.drawnStages || 0));
    updateLogIndicators(attachedLog);
    return;
  }
  // 跑着的时候每秒重画一次：已经展开的卡片重画后仍然展开（否则一秒收一次，没法读）。
  const opened = new Set();
  for (const node of list.querySelectorAll("details[open]")) {
    if (node.dataset.stageId) opened.add(node.dataset.stageId);
  }
  if (!rows.length) {
    // 一条阶段事件都没有：旧 run（T1 之前跑的）、还没跑过、以及**刚切到这一屏**（阶段卡
    // 只属于产生它的那一路，切屏会清空 —— T7）都是这个情况。
    // 如实说“这一屏还没有”，**不摆一排空卡片**（空卡片看起来像“测试没跑”）。
    list.textContent = "";
    const empty = document.createElement("li");
    empty.className = "stage-empty";
    empty.textContent = stageBag().note || "这一屏还没有阶段卡：跑一轮 run（或在历史里打开一次 run）就会出现";
    list.append(empty);
    source.drawnStages = 0;
    source.pending = 0;
    source.running = false;
    stageIndicators(source);
    scheduleStepTicker(false);
    return;
  }
  list.textContent = "";
  const nowMs = Date.now();
  let running = false;
  for (const row of rows) {
    if (row.status === "running") running = true;
    const status = STEP_STATUS_TEXT[row.status] || row.status;
    const item = document.createElement("li");
    item.className = "stage";
    item.dataset.status = row.status;
    // 只有一个“当前定位目标”；换一个阶段时旧卡片的高亮随之取消。
    item.dataset.located = row.stage_id === app.locatedStage ? "1" : "0";
    item.dataset.stageId = row.stage_id;
    // 筛选用：`hidden` 是过滤结果，`data-hit` 是“当前那一个命中”。
    item.dataset.hit = "0";
    const details = document.createElement("details");
    details.dataset.stageId = row.stage_id;
    if (opened.has(String(row.stage_id))) details.open = true;
    const summary = document.createElement("summary");
    const bullet = document.createElement("span");
    bullet.className = "stage-bullet";
    bullet.setAttribute("aria-hidden", "true");
    const title = document.createElement("span");
    title.className = "stage-label";
    title.textContent = row.label || row.stage_id;
    const meta = document.createElement("span");
    meta.className = "stage-meta";
    // 定位按钮放在“结果与耗时”**之前**（需求 §8.1）；没坐标就禁用。
    const locate = document.createElement("button");
    locate.type = "button";
    locate.className = "stage-locate";
    locate.textContent = "定位";
    locate.title = "把四个证据窗滚到这个阶段对应的行并高亮";
    locate.disabled = !(row.evidence_refs || []).length;
    locate.addEventListener("click", (event) => {
      // ⚠️ 别顺手把 `<details>` 开/关：点的是“定位”，不是“展开/收起”。
      event.preventDefault();
      event.stopPropagation();
      void locateStage(row.stage_id);
    });
    const ms = liveDurationMs(row, nowMs);
    meta.textContent = ms > 0 ? `${status} · ${formatDuration(ms)}` : status;
    summary.append(bullet, title, locate, meta);
    const body = document.createElement("div");
    body.className = "stage-body";
    const id = document.createElement("div");
    id.className = "stage-id";
    id.textContent = `${row.stage_id} · v${row.version}`;
    const expected = document.createElement("div");
    expected.className = "stage-kv";
    expected.textContent = `预期：${row.expected || "（声明里没写）"}`;
    const detail = document.createElement("div");
    detail.className = "stage-kv";
    detail.textContent = `现状：${row.detail || "（还没有可说的）"}`;
    const evidence = document.createElement("div");
    evidence.className = "stage-kv";
    evidence.textContent = `证据：${row.evidence.length ? row.evidence.join("、") : "（没引用具体文件）"}`;
    // 行号只给 raw（需求 §8.2）；其余三类留在数据里给定位用。
    const located = document.createElement("div");
    located.className = "stage-kv";
    located.textContent = describeRawRef(row);
    body.append(id, expected, detail, evidence, located);
    details.append(summary, body);
    item.append(details);
    list.append(item);
  }
  source.drawnStages = rows.length;
  source.pending = 0;
  source.running = running;
  // 筛选条件仍然生效（跑着的时候每秒重画一次，过滤不能因为重画而丢掉）。
  applyStageFilter(attachedLog);
  scheduleStepTicker(running);
}

/** 跑着的时候每秒重画一次（只推“已跑多久”，**不改状态** —— 状态只由事件推进）。 */
function scheduleStepTicker(running) {
  if (app.testStepTimer) {
    clearInterval(app.testStepTimer);
    app.testStepTimer = null;
  }
  if (!running) return;
  app.testStepTimer = setInterval(() => renderStepCards(), 1000);
}

/* ------------------------------------------------------------------ events */
/* 结构化事件现在是工作台里的第四个来源，一行一条（见 eventLine()）：
 * 卡片视图已取消 —— 四份证据共用一个查看器，事件也按“行”读，
 * 这样复制、滚动、暂停的语义与其它三路完全一致。 */

/* ----------------------------------------------------------------- metrics */
/* 指标趋势：横轴是**设备 uptime**，不是主机时间。
 *
 * 三件必须画对的事（错了比不画更糟）：
 *  1. 方向。`tx_stack` 是**剩余**字节，越大越安全 —— 方向来自后台的语义表
 *     （metrics.jsonl 表头行），不在这里再猜一遍。
 *  2. 重启。uptime 会归零，跨重启直接连线会画出一条**不存在的数据下降**，
 *     所以 t_s 回退处必须断开并标出来。
 *  3. 空数据。"没有点"和"点都是 0"长得不一样：没有点就不画线，只说明原因。 */
/* 曲线的画布尺寸。窗格高 = 单个图表卡片的两倍（style.css 的
 * `--metric-pane-h`），所以这里的 viewBox 高度也跟着从 120 变成 240 —— 只把框放大
 * 而 viewBox 不动的话，`preserveAspectRatio="none"` 会把曲线拉平在更大的框里，
 * 纵向分辨率一点也没多出来。 */
const PANE_CHART_SIZE = { width: 640, height: 240 };

const DIRECTION_TEXT = { higher: "越大越安全", lower: "越小越好", neutral: "无方向" };
const UNIT_TEXT = { count: "次" };

/** 一次捕获里出现过的指标 key（顺序 = 后台 `by_key` 的插入顺序 = 项目包的指标表顺序）。 */
function metricKeys(metrics) {
  return Object.keys((metrics && metrics.by_key) || {});
}

/* 某个指标**最近**的一个点。堆占用读数要用"最近一次的空闲量与总量"：两者都是
 * 固件报的测量，同一次采样里读出来的，不在页面里另算一个基准。 */
function lastMetricPoint(metrics, key) {
  const entry = metrics && metrics.by_key ? metrics.by_key[key] : null;
  const points = (entry && entry.points) || [];
  return points.length ? points[points.length - 1] : null;
}

/** 一个窗格该画哪个指标：**用户选过的**这次还在就继续用它；否则按该窗格的首选梯子退；
 *  都没有就退到第一项；一个指标都没有 ⇒ null（空窗格是事实，不是错误，不编一个 key 出来）。 */
function resolvePaneMetric(keys, preferred, current) {
  if (current && keys.includes(current)) return current;
  for (const key of preferred || []) {
    if (keys.includes(key)) return key;
  }
  return keys.length ? keys[0] : null;
}

/** 两个窗格各自解析出来的 key（把 `app.metricPanes` 里用户选过的值当"当前值"）。 */
function paneMetricChoices(keys, panes) {
  return Array.from({ length: METRIC_PANE_COUNT }, (_, index) =>
    resolvePaneMetric(keys, METRIC_PANE_DEFAULTS[index], panes ? panes[index] : null)
  );
}

/* 堆占用读数：由固件报的**两个测量**现算（空闲堆 / 堆总量）。
 * ⚠️ 没有 `heap_total`（旧固件、或这一轮没打）就返回 null —— **不拿一个猜出来的分母**
 * 算出一个看起来像证据的百分比。它是**读数**：曲线画的仍然是固件报的字节。 */
function heapUsageReading(metrics) {
  const free = lastMetricPoint(metrics, "metric.sys.heap");
  const total = lastMetricPoint(metrics, "metric.sys.heap_total");
  if (!free || !total || !total.value) return null;
  const used = total.value - free.value;
  return { free: free.value, used, total: total.value, pct: (used / total.value) * 100, t_s: free.t_s };
}

function heapUsageText(reading) {
  if (!reading) return "";
  return `占用 ${reading.pct.toFixed(1)}%（= 1 − 空闲/总量；${reading.used} / ${reading.total} B，按最近一次的总量算）`;
}

/** 用户在某个窗格的下拉框里换了一个指标：只改**那一个**窗格，落盘并重画。 */
function setPaneMetric(index, key) {
  if (!(index >= 0 && index < METRIC_PANE_COUNT)) return;
  app.metricPanes = Array.from({ length: METRIC_PANE_COUNT }, (_, slot) =>
    slot === index ? key : app.metricPanes[slot] || null
  );
  app.prefs.metrics = app.metricPanes.slice();
  savePrefs();
  renderMetrics(app.metrics);
}

/** 同一个窗格的**时间窗** / **纵轴刻度**：同样只改那一窗、落盘、重画。
 *  ⚠️ 这两个不改变任何数据，只改变"怎么看" —— 所以连刻度也存成偏好，刷新后还是自己看惯的那档。 */
function setPaneView(key, choice, index, value, fallback) {
  if (!(index >= 0 && index < METRIC_PANE_COUNT)) return;
  if (!choice.some((row) => row.key === value)) return;
  app[key] = Array.from({ length: METRIC_PANE_COUNT }, (_, slot) =>
    slot === index ? value : app[key][slot] || fallback
  );
  return app[key].slice();
}

function setPaneWindow(index, key) {
  const next = setPaneView("metricWindows", METRIC_WINDOW_CHOICES, index, key, METRIC_WINDOW_DEFAULT);
  if (!next) return;
  app.prefs.metricWindows = next;
  savePrefs();
  renderMetrics(app.metrics);
}

function setPaneScale(index, key) {
  const next = setPaneView("metricScales", METRIC_SCALE_CHOICES, index, key, METRIC_SCALE_DEFAULT);
  if (!next) return;
  app.prefs.metricScales = next;
  savePrefs();
  renderMetrics(app.metrics);
}

/** 给"选项是死的那几档"的下拉接线（选项灌一次、onchange 直接覆盖 —— 与指标下拉同理）。 */
function wirePaneSelect(select, choices, current, onPick) {
  if (!select) return;
  if (!select.options.length) {
    for (const choice of choices) {
      const option = document.createElement("option");
      option.value = choice.key;
      option.textContent = choice.label;
      select.appendChild(option);
    }
  }
  select.disabled = false;
  select.value = current;
  select.onchange = () => onPick(select.value);
}

/* 指标载荷的两种形状（）：
 *   - **整份**（run 的 decode 步、离线包里的 metrics.jsonl、旧录音）：直接替换；
 *   - **增量**（实时观察每一帧只带新增的点）：并进**本会话**已有的那一份。
 * 为什么要有增量：整份推送时一帧的字节数随会话线性增长（单帧曾到 6.8 MB）⇒ 整场会话
 * O(n²)，页面被拖成逐行慢放，看起来像"未连接却一直在输出"。
 * 除 `by_key[*].points` 之外的字段都是服务端"到此刻为止"算出来的（比上一份新），
 * 所以直接采用 incoming 的；只把点接在底子上。 */
function mergeMetrics(previous, incoming) {
  if (!incoming) return previous || null;
  if (!incoming.incremental) return incoming;
  const base = previous && previous.schema === incoming.schema ? previous : null;
  if (!base) return incoming;   // 没有底子：这一帧就是全部（图上从这一帧开始）
  const byKey = {};
  for (const [key, row] of Object.entries(incoming.by_key || {})) {
    const before = (base.by_key || {})[key];
    const points = before ? before.points.slice() : [];
    const seen = new Set(points.map((point) => `${point.seq}:${point.t_s}`));
    for (const point of row.points || []) {
      const id = `${point.seq}:${point.t_s}`;
      if (seen.has(id)) continue;
      seen.add(id);
      points.push(point);
    }
    byKey[key] = { ...row, points, count: points.length, ...valueBounds(points) };
  }
  // 这一帧没有新点的键要接着留着（服务端只带"有新点"的键），否则曲线会突然消失。
  for (const [key, row] of Object.entries(base.by_key || {})) {
    if (!(key in byKey)) byKey[key] = row;
  }
  const total = Object.values(byKey).reduce((sum, row) => sum + (row.count || 0), 0);
  return { ...incoming, by_key: byKey, point_count: total, merged: true };
}

function valueBounds(points) {
  let min = null;
  let max = null;
  for (const point of points || []) {
    const value = point && point.value;
    if (typeof value !== "number") continue;
    if (min === null || value < min) min = value;
    if (max === null || value > max) max = value;
  }
  return { min, max };
}

/* 把一帧指标载荷落到图上：增量就并、整份就换。
 * ⚠️ 会话一换就不并 —— `app.metricsSession` 记的是这份图的会话号。 */
function applyMetrics(payload, mode = app.mode) {
  if (!payload) return;
  if (!payload.incremental) {
    app.metricsSession[mode] = null;
    renderMetrics(payload, mode);
    return;
  }
  const session = currentLiveSessionId();
  const base = app.metricsSession[mode] === session ? app.metricsViews[mode] : null;
  app.metricsSession[mode] = session;
  renderMetrics(mergeMetrics(base, payload), mode);
}

/* 把一次捕获的指标画进**两个窗格**：每窗一个指标、各自一个下拉框。
 * 窗格数量固定在 HTML 里（`metricPane-0/1`）——页面结构是死的，数据是活的。 */
function renderMetrics(metrics, mode = app.mode) {
  app.metricsViews[mode] = metrics;
  if (mode !== app.mode) return;
  app.metrics = metrics;
  const note = $("metricsNote");
  const links = $("metricsLinks");
  const panes = Array.from({ length: METRIC_PANE_COUNT }, (_, index) => ({
    index,
    body: $(`metricBody-${index}`),
    pick: $(`metricPick-${index}`),
    window: $(`metricWindow-${index}`),
    scale: $(`metricScale-${index}`),
  }));
  if (links) links.textContent = "";
  // ⚠️ 下拉框的 <option> **不在这里清**：实时观察每 1～2 秒就重画一次这张图，
  // 每次都重建选项会把用户正打开的下拉框关掉/重置（下面按“指标集合的签名”重建）。
  for (const pane of panes) {
    if (pane.body) pane.body.textContent = "";
  }

  const keys = metricKeys(metrics);
  if (!metrics || !metrics.point_count || !keys.length) {
    // "没有指标记录"与"有记录但一个点都画不出来"是两件事，但都**说清原因**，不画空图。
    if (note) note.innerHTML = '<span class="muted">本次捕获没有指标记录</span>';
    for (const pane of panes) {
      for (const select of [pane.pick, pane.window, pane.scale]) {
        if (select) select.disabled = true;
      }
      if (pane.pick) {
        pane.pick.textContent = "";
        delete pane.pick.dataset.signature;
      }
      if (pane.body) {
        pane.body.innerHTML = '<p class="empty">没有可画的数据（事件流里没有 metric/dht/fs 记录）</p>';
      }
    }
    return;
  }

  // 复核入口：导出物本身就在证据目录里，任何人（或 agent）都能拿去重算。
  if (app.viewing && links) {
    for (const name of ["metrics.jsonl", "app_events.jsonl"]) {
      const a = document.createElement("a");
      a.href = `/api/runs/${encodeURIComponent(app.viewing)}/evidence/${name}?download=1`;
      a.textContent = name;
      links.appendChild(a);
    }
  }

  // 顺序用后台给的（``by_key`` 的插入顺序 = 项目包指标表的顺序：先整机状态、再采样、
  // 最后存储）。这里**不排序**：按字母排序会把"空闲堆"和"平均湿度"排在一起。
  const chosen = paneMetricChoices(keys, app.metricPanes);
  app.metricPanes = chosen.slice();
  app.metricWindows = loadMetricWindows(app.metricWindows);
  app.metricScales = loadMetricScales(app.metricScales);

  for (const pane of panes) {
    if (!pane.pick) continue;
    // 选项只在**指标集合变了**（换了一次运行 / 实时观察里出现了新的 key）时才重建。
    const signature = keys
      .map((key) => `${key}|${metrics.by_key[key].label}|${metrics.by_key[key].unit}`)
      .join(",");
    if (pane.pick.dataset.signature !== signature) {
      pane.pick.textContent = "";
      for (const key of keys) {
        const entry = metrics.by_key[key];
        const option = document.createElement("option");
        option.value = key;
        option.textContent = `${entry.label} · ${UNIT_TEXT[entry.unit] || entry.unit}`;
        option.title = `${key} · ${DIRECTION_TEXT[entry.better] || entry.better}`;
        pane.pick.appendChild(option);
      }
      pane.pick.dataset.signature = signature;
    }
    pane.pick.disabled = false;
    pane.pick.value = chosen[pane.index] || "";
    // ⚠️ 用 `onchange` 而不是 addEventListener：`renderMetrics` 会被反复调用
    // （每次 app_events / 换运行 / 换模式），监听器会一次一次叠上去。
    pane.pick.onchange = () => setPaneMetric(pane.index, pane.pick.value);
    // 时间窗 / 纵轴刻度：选项只有几档、且是**定死的**，装配时灌一次就够（不跟着每次重画重建）。
    wirePaneSelect(pane.window, METRIC_WINDOW_CHOICES, app.metricWindows[pane.index],
      (key) => setPaneWindow(pane.index, key));
    wirePaneSelect(pane.scale, METRIC_SCALE_CHOICES, app.metricScales[pane.index],
      (key) => setPaneScale(pane.index, key));
  }

  const parts = [`${metrics.point_count} 个点`];
  if (metrics.span_s) parts.push(`t=${metrics.span_s[0]}–${metrics.span_s[1]} s`);
  if (metrics.restarts && metrics.restarts.length) {
    parts.push(`⚠ uptime 回退 ${metrics.restarts.length} 次`);
  }
  if (metrics.untimed && metrics.untimed.length) {
    parts.push(`另有 ${metrics.untimed.length} 条记录没有时间轴（不画）`);
  }
  const missing = Object.keys(metrics.missing || {});
  if (missing.length) parts.push(`缺字段：${missing.join("、")}`);
  // 实时增量（）：这一帧只带新增的点 —— 说清楚，免得有人拿“这幅图多少个点”
  // 与证据里的总数对不上时，以为是丢了（完整序列在 metrics.jsonl 里）。
  if (metrics.incremental) parts.push("实时增量（每帧只带新增点；完整序列在 metrics.jsonl）");
  if (metrics.dropped_points) parts.push(`⚠ 增量丢点 ${metrics.dropped_points} 个（已报数）`);
  if (note) {
    note.textContent = parts.join(" · ");
    note.dataset.kind = missing.length || (metrics.restarts || []).length ? "warn" : "";
  }

  for (const pane of panes) {
    const key = chosen[pane.index];
    if (!key || !pane.body || !metrics.by_key[key]) continue;
    pane.body.appendChild(chartCard(metrics, metrics.by_key[key], metrics.restarts || [], {
      windowKey: app.metricWindows[pane.index],
      scaleKey: app.metricScales[pane.index],
    }));
  }
}

/* A restart is where the device's uptime went backwards. Two things happen there:
   the polyline is split (and the split is labelled — an unlabelled gap looks like
   lost data), and the x axis **accumulates** the previous segment's duration
   instead of jumping back to zero. Without that, one reboot would make the line
   zigzag leftwards, which reads as "the value fell" rather than "the clock reset". */
function chartAxis(points, restarts, seconds) {
  const breaks = new Set((restarts || []).map((row) => row.seq));
  const rows = [];
  let offset = 0;
  let previous = null;
  for (const point of points) {
    if (previous !== null && point.t_s < previous) offset += previous;
    previous = point.t_s;
    rows.push({ point, x: offset + point.t_s });
  }
  // **滚动时间窗**：只画最近 `seconds` 秒（null = 全部）。
  // ⚠️ 先按累加后的横轴裁，再分段 —— 否则跨重启的断点会落在窗口外，画出一条不存在的连接。
  const span = Number.isFinite(seconds) ? seconds : null;
  const last = rows.length ? rows[rows.length - 1].x : 0;
  const kept = span === null ? rows : rows.filter((row) => row.x >= last - span);
  const segments = [[]];
  for (const row of kept) {
    const tail = segments[segments.length - 1];
    const uptimeWentBack = tail.length && row.point.t_s < tail[tail.length - 1].point.t_s;
    if ((breaks.has(row.point.seq) || uptimeWentBack) && tail.length) segments.push([]);
    segments[segments.length - 1].push(row);
  }
  return {
    rows: kept,
    segments: segments.filter((segment) => segment.length),
    hidden: rows.length - kept.length,
  };
}

const METRIC_SERIES_COLORS = [
  "#0078d4", "#d83b01", "#107c10", "#8764b8", "#ca5010", "#038387", "#e3008c", "#4f6bed",
  "#498205", "#c239b3", "#8e562e", "#00b7c3", "#a4262c", "#5c2d91", "#69797e", "#ffb900",
];

/** Stable across redraws: ordering comes from the project's first-seen task order. */
function metricSeriesColor(index) {
  return METRIC_SERIES_COLORS[index % METRIC_SERIES_COLORS.length];
}

function taskStackDetail(point) {
  if (!point || !Number.isFinite(point.used_b) || !Number.isFinite(point.total_b)) return "";
  return `峰值占用 ${point.used_b} / ${point.total_b} B`;
}

function drawMultiChart(entry, axes, size, range) {
  const width = (size && size.width) || PANE_CHART_SIZE.width;
  const height = (size && size.height) || PANE_CHART_SIZE.height;
  const pad = { top: 10, right: 12, bottom: 16, left: 52 };
  const all = axes.flatMap((axis) => axis.rows);
  if (!all.length) return `<text x="${pad.left}" y="${pad.top + 20}">这个时间窗里没有点</text>`;
  const xMin = Math.min(...all.map((row) => row.x));
  const xMax = Math.max(...all.map((row) => row.x));
  const usable = width - pad.left - pad.right;
  const x = (value) => pad.left + (xMax === xMin ? usable / 2 : ((value - xMin) / (xMax - xMin)) * usable);
  const y = (value) => pad.top + (1 - (value - range.min) / (range.max - range.min))
    * (height - pad.top - pad.bottom);
  const shapes = [0.25, 0.5, 0.75].map((fraction) => {
    const gy = pad.top + fraction * (height - pad.top - pad.bottom);
    return `<line class="grid" x1="${pad.left}" y1="${gy.toFixed(1)}" x2="${width - pad.right}" y2="${gy.toFixed(1)}"/>`;
  });
  axes.forEach((axis, index) => {
    for (const segment of axis.segments) {
      const d = segment.map((item, pointIndex) =>
        `${pointIndex ? "L" : "M"}${x(item.x).toFixed(1)},${y(item.point.value).toFixed(1)}`).join(" ");
      shapes.push(`<path class="line metric-series-line" data-series="${index}" style="--series-color:${metricSeriesColor(index)}" d="${d}"/>`);
    }
    const latest = axis.rows.length ? axis.rows[axis.rows.length - 1] : null;
    const detail = latest ? taskStackDetail(latest.point) : "";
    if (latest && detail) {
      const cy = y(latest.point.value);
      const labelY = Math.max(pad.top + 10, Math.min(height - pad.bottom - 4, cy + (cy < height / 2 ? 14 : -8)));
      shapes.push(`<text class="metric-series-detail" data-series="${index}" x="${Math.max(pad.left + 4, x(latest.x) - 132).toFixed(1)}" y="${labelY.toFixed(1)}" style="--series-color:${metricSeriesColor(index)}">${escapeHtml(detail)}</text>`);
    }
  });
  return `
    <line class="axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"/>
    <line class="axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"/>
    <text x="4" y="${pad.top + 8}">${range.max}</text>
    <text x="4" y="${height - pad.bottom}">${range.min}</text>
    <text x="${pad.left}" y="${height - 5}">${xMin}s</text>
    <text x="${width - pad.right - 24}" y="${height - 5}">${xMax}s</text>
    ${shapes.join("")}`;
}

function multiSeriesCard(entry, restarts, pane) {
  const card = document.createElement("div");
  card.className = "chart chart-multi";
  const seconds = windowSeconds(pane.windowKey);
  const axes = entry.series.map((series) => chartAxis(series.points, restarts, seconds));
  const rows = axes.flatMap((axis) => axis.rows);
  const range = axisRange(entry, rows, pane.scaleKey, null);
  const hidden = axes.reduce((sum, axis) => sum + axis.hidden, 0);
  const unit = UNIT_TEXT[entry.unit] || entry.unit;
  card.innerHTML = `
    <div class="title">
      <span>${escapeHtml(entry.label)}</span><span class="key">${escapeHtml(entry.key)}</span>
      <span class="dir">${escapeHtml(DIRECTION_TEXT[entry.better] || entry.better)} · 单位 ${escapeHtml(unit)} · 刻度 ${escapeHtml(range.source)}</span>
      <span class="now">${entry.series.length} 个任务</span>
    </div>
    <div class="metric-legend">${entry.series.map((series, index) =>
      `<button type="button" data-series="${index}" title="${escapeHtml(series.id)}"><i style="--series-color:${metricSeriesColor(index)}"></i>${escapeHtml(series.label)} ${escapeHtml(Number(series.points[series.points.length - 1].value).toFixed(1))}${escapeHtml(unit)}</button>`
    ).join("")}</div>
    <div class="stats">${entry.count} 个点 · ${entry.series.length} 个任务 · min ${entry.min} / max ${entry.max}${hidden ? ` · ${hidden} 点在窗外` : ""}</div>
    <svg viewBox="0 0 ${PANE_CHART_SIZE.width} ${PANE_CHART_SIZE.height}" preserveAspectRatio="none" role="img"
         aria-label="${escapeHtml(entry.label)}多任务趋势">${drawMultiChart(entry, axes, PANE_CHART_SIZE, range)}</svg>`;
  const focus = (index) => {
    card.querySelectorAll("[data-series]").forEach((node) => {
      node.classList.toggle("is-focus", index !== null && node.dataset.series === String(index));
      node.classList.toggle("is-dim", index !== null && node.dataset.series !== String(index));
    });
  };
  card.querySelectorAll(".metric-legend button").forEach((button) => {
    button.onmouseenter = () => focus(button.dataset.series);
    button.onmouseleave = () => focus(null);
    button.onfocus = () => focus(button.dataset.series);
    button.onblur = () => focus(null);
  });
  return card;
}

/** 上界抬到"好看"的数（1/2/5 × 10^n）—— 轴标签才不是 123232 这种读不出量级的值。 */
function niceCeil(value) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  const scaled = value / power;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * power;
}

/** 这一份捕获里固件报的堆总量（占用率的分母）。只会有微小漂移 ⇒ 取最大的那个。 */
function heapTotalOf(metrics) {
  const entry = metrics && metrics.by_key ? metrics.by_key["metric.sys.heap_total"] : null;
  let top = 0;
  for (const point of (entry && entry.points) || []) if (point.value > top) top = point.value;
  return top;
}

/** 纵轴范围。见 `METRIC_SCALE_CHOICES` 的说明：
 *  `full` 的两条口径都**不随窗口滚动而变** —— 轴要是每重画一次就换一次，高低就没参照了。 */
function axisRange(entry, rows, scaleKey, heapTotal) {
  if (scaleKey === "full") {
    if (entry.unit === "percent") return { min: 0, max: 100, source: "0–100%" };
    const total = Number.isFinite(heapTotal) && heapTotal > 0 ? heapTotal : null;
    if (total !== null && HEAP_SCALE_KEYS.includes(entry.key)) {
      return { min: 0, max: total, source: "0–总量" };
    }
    // 没有"天然满量程"的指标：用**整段捕获**的最大值（不是窗口内的）向上取整。
    const top = Number.isFinite(entry.max) && entry.max > 0 ? entry.max : 1;
    return { min: 0, max: niceCeil(top), source: "0–上限" };
  }
  const values = rows.map((row) => row.point.value);
  let min = values.length ? Math.min(...values) : 0;
  let max = values.length ? Math.max(...values) : 1;
  if (min === max) {
    // A flat line still needs vertical room, otherwise it sits exactly on the axis
    // and reads as "no data".
    const span = Math.abs(min) || 1;
    min -= span * 0.05;
    max += span * 0.05;
  }
  return { min, max, source: "自动" };
}

/** 时间窗的秒数（`all` ⇒ null = 全部）。 */
function windowSeconds(key) {
  const row = METRIC_WINDOW_CHOICES.find((item) => item.key === key);
  return row ? row.seconds : null;
}

/** 标题旁那个**大号当前值**。堆那一窗给占用率（和任务管理器一样：先给比例），
 *  字节数留在下面那行读数里。它只是"把最后那个点写成一行字"，不是第二条证据。 */
function nowText(entry, rows, reading) {
  if (reading) return `占用 ${reading.pct.toFixed(1)}%`;
  if (!rows.length) return "";
  const last = rows[rows.length - 1].point.value;
  return `${last} ${UNIT_TEXT[entry.unit] || entry.unit}`;
}

function drawChart(entry, axis, size, range) {
  const width = (size && size.width) || PANE_CHART_SIZE.width;
  const height = (size && size.height) || PANE_CHART_SIZE.height;
  const pad = { top: 10, right: 12, bottom: 16, left: 52 };
  const { rows, segments } = axis;
  if (!rows.length) {
    // 窗口里一个点都没有 = **事实**，说清并给出下一步（不画一条空线冒充"数据到了"）。
    return `<text x="${pad.left}" y="${pad.top + 20}">这个时间窗里没有点（把时间窗调大，或选“全部”）</text>`;
  }
  const min = range.min;
  const max = range.max;
  const xMin = rows[0].x;
  const xMax = rows[rows.length - 1].x;
  const usable = width - pad.left - pad.right;
  const x = (value) => pad.left + (xMax === xMin ? usable / 2 : ((value - xMin) / (xMax - xMin)) * usable);
  const y = (value) =>
    pad.top + (1 - (value - min) / (max - min)) * (height - pad.top - pad.bottom);

  // 水平网格（任务管理器式的浅灰横线）：没有它，中间那段高度读不出量。
  const axisLine = (fraction) => {
    const gy = pad.top + fraction * (height - pad.top - pad.bottom);
    return `<line class="grid" x1="${pad.left}" y1="${gy.toFixed(1)}" `
      + `x2="${width - pad.right}" y2="${gy.toFixed(1)}"/>`;
  };
  const shapes = [0.25, 0.5, 0.75].map(axisLine);
  for (const segment of segments) {
    const d = segment
      .map((item, index) => `${index ? "L" : "M"}${x(item.x).toFixed(1)},${y(item.point.value).toFixed(1)}`)
      .join(" ");
    shapes.push(`<path class="line" d="${d}"/>`);
    if (segment.length === 1) {
      shapes.push(
        `<circle class="dot" cx="${x(segment[0].x).toFixed(1)}" cy="${y(segment[0].point.value).toFixed(1)}" r="2.5"/>`
      );
    }
  }
  for (let i = 1; i < segments.length; i += 1) {
    const previous = segments[i - 1][segments[i - 1].length - 1];
    const next = segments[i][0];
    const mid = (x(previous.x) + x(next.x)) / 2;
    shapes.push(
      `<line class="gap" x1="${mid.toFixed(1)}" y1="${pad.top}" x2="${mid.toFixed(1)}" y2="${(height - pad.bottom).toFixed(1)}"/>`
    );
  }

  return `
    <line class="axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"/>
    <line class="axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"/>
    <text x="4" y="${pad.top + 8}">${max}</text>
    <text x="4" y="${height - pad.bottom}">${min}</text>
    <text x="${pad.left}" y="${height - 5}">${xMin}s</text>
    <text x="${width - pad.right - 24}" y="${height - 5}">${xMax}s</text>
    ${shapes.join("")}`;
}

function chartCard(metrics, entry, restarts, pane) {
  if (entry.series && entry.series.length) return multiSeriesCard(entry, restarts, pane);
  const card = document.createElement("div");
  card.className = "chart";
  const axis = chartAxis(entry.points, restarts, windowSeconds(pane.windowKey));
  const range = axisRange(entry, axis.rows, pane.scaleKey, heapTotalOf(metrics));
  const first = entry.points[0];
  const last = entry.points[entry.points.length - 1];
  const unit = UNIT_TEXT[entry.unit] || entry.unit;
  const restarted = (restarts || []).length;
  // 堆占用是**读数**（由固件报的两个测量现算），只有堆那一张卡才挂它。
  const reading = entry.key === "metric.sys.heap" ? heapUsageReading(metrics) : null;
  const usage = reading ? heapUsageText(reading) : "";
  card.innerHTML = `
    <div class="title">
      <span>${escapeHtml(entry.label)}</span>
      <span class="key">${escapeHtml(entry.key)}</span>
      <span class="dir">${escapeHtml(DIRECTION_TEXT[entry.better] || entry.better)} · 单位 ${escapeHtml(unit)} · 刻度 ${escapeHtml(range.source)}</span>
      <span class="now">${escapeHtml(nowText(entry, axis.rows, reading))}</span>
    </div>
    <div class="stats">n=${entry.count} · min ${entry.min} / max ${entry.max} ·
      首 ${first.value} (${first.t_s}s) → 末 ${last.value} (${last.t_s}s)${
        restarted
          ? ` · ⚠ t_s 回退 ${restarted} 次（设备重启过：折线已断开，横轴按段累加）`
          : ""
      }${axis.hidden ? ` · 本窗 ${axis.rows.length}/${entry.points.length} 点（另 ${axis.hidden} 点在窗外）` : ""}</div>
    ${usage ? `<div class="stats heap-use">${escapeHtml(usage)}</div>` : ""}
    <svg viewBox="0 0 ${PANE_CHART_SIZE.width} ${PANE_CHART_SIZE.height}" preserveAspectRatio="none" role="img"
         aria-label="${escapeHtml(entry.label)} 趋势">${drawChart(entry, axis, PANE_CHART_SIZE, range)}</svg>`;
  return card;
}

/* -------------------------------------------------------------------- panic */
/* A crash is the one result a screenshot of numbers cannot convey, so it gets its
   own card above the structured events: the frames, and the exact command that
   reproduces the decoding. `summary` is the compact view, `evidence` holds the
   frames and the addr2line invocation (live: finding.backtrace, history:
   report.fault_evidence). */
function renderPanic(summary, evidence) {
  const host = $("panicCard");
  if (!host) return;
  host.textContent = "";
  if (!summary) return;

  const card = document.createElement("div");
  card.className = "assert panic";
  card.dataset.status = summary.verdict === "fault" ? "fail" : "pass";

  if (summary.verdict !== "fault") {
    card.innerHTML = '<div class="id">崩溃扫描：未发现 panic / 看门狗 / 断言 / 掉电</div>';
    host.appendChild(card);
    return;
  }

  const frames = (evidence && evidence.frames) || [];
  const unresolved = (evidence && evidence.unresolved) || [];
  const command =
    (evidence && (evidence.command || evidence.addr2line_command)) || [];
  const lines = frames.length
    ? frames
        .map((frame) => {
          if (frame.resolved) {
            const inlined = (frame.inlined || [])
              .map((entry) => `\n         (inlined) ${escapeHtml(entry)}`)
              .join("");
            return `${escapeHtml(frame.address)}  ${escapeHtml(frame.symbol)} @ ${escapeHtml(frame.location)}${inlined}`;
          }
          return `${escapeHtml(frame.address)}  ?? 未解析`;
        })
        .join("\n")
    : "（没有可解码的地址）";

  const mismatch =
    evidence && evidence.elf_matches_firmware === false
      ? '<div class="kv">⚠ 所解码的 ELF 与本次烧录固件哈希不一致，下面的帧不可信</div>'
      : "";

  card.innerHTML = `
    <div class="id">崩溃 ${escapeHtml((summary.kinds || []).join("/"))} · 地址 ${summary.addresses} · 已解析 ${summary.resolved} · 未解析 ${unresolved.length}</div>
    <div class="frames">${lines}</div>
    ${mismatch}
    ${summary.error ? `<div class="kv">${escapeHtml(summary.error)}</div>` : ""}
    ${command.length ? `<div class="cmd">复现：${escapeHtml(command.join(" "))}</div>` : ""}`;
  host.appendChild(card);
}

/* 结论域：一次 run 的结论里混着两件不同的事时就分开念 ——
 * "证据与日志健康"（采到了吗/解码了吗/完整性）与"业务结论"（被测对象的判据）。
 * ⚠️ 分开**只影响怎么念**：失败的行照旧逐条列出、照旧计入总数，绝不被折叠掉。
 * 域由判据自己声明（`domain`），页面**不替谁猜**；一条都没声明就退回只报总数。 */
const DOMAIN_TEXT = { evidence: "证据与日志健康", business: "业务结论" };

function summarizeDomains(rows) {
  const buckets = new Map();
  for (const row of rows || []) {
    const name = String((row && row.domain) || "");
    if (!name) continue;
    const bucket = buckets.get(name) || { domain: name, total: 0, passed: 0, failed: [] };
    bucket.total += 1;
    if (row.status === "pass") bucket.passed += 1;
    else bucket.failed.push(row.id);
    buckets.set(name, bucket);
  }
  return Array.from(buckets.values());
}

function renderAssertions(assertions) {
  app.assertionRows = Array.isArray(assertions) ? assertions.slice() : [];
  const host = $("paneAssert");
  host.textContent = "";
  if (!app.assertionRows.length) {
    host.innerHTML = '<p class="muted">尚未运行</p>';
    return;
  }
  const domains = summarizeDomains(app.assertionRows);
  // 只有真的分得开（>1 个域）才分开展示：一个域的时候再加一行就是噪音。
  if (domains.length > 1) {
    const head = document.createElement("div");
    head.className = "assert-domains";
    for (const bucket of domains) {
      const line = document.createElement("div");
      line.className = "assert-domain";
      line.dataset.status = bucket.failed.length ? "fail" : "pass";
      const name = DOMAIN_TEXT[bucket.domain] || bucket.domain;
      // ⚠️ 分开念**不等于**把红藏起来：不合规的 id 照样逐条写在结论行上，
      //    下面那排卡片也照样把它们列出来。
      line.textContent = bucket.failed.length
        ? `${name}：${bucket.passed}/${bucket.total} · 不合规 ${bucket.failed.join("、")}`
        : `${name}：${bucket.total}/${bucket.total} · 全部通过`;
      head.appendChild(line);
    }
    host.appendChild(head);
  }
  for (const row of app.assertionRows) {
    const card = document.createElement("div");
    card.className = "assert";
    card.dataset.status = row.status;
    card.innerHTML = `
      <div class="id">${row.status === "pass" ? "✔" : "✖"} ${escapeHtml(row.id)} · ${escapeHtml(row.evidence)}</div>
      <div class="desc">${escapeHtml(row.description)}</div>
      <div class="kv">要求 ${escapeHtml(row.expected)}</div>
      <div class="kv">实测 ${escapeHtml(row.observed)}</div>`;
    host.appendChild(card);
  }
}

/* ------------------------------------------------------------------ run meta */
/* ===========================================  / D6：历史里的那一台设备
 *
 * 一句话：**回放只看它自己那份 manifest**。当前嗅探结果与当前台架配置一律不得
 * 参与改写 —— 否则"上一次跑的是哪一块板"就永远说不清了（需求 §7）。
 *
 * 三种情形分开写，不混成一句：
 *  · 新 run（有 `device` 块）⇒ 报**当时记下的**序号与口；
 *  · 旧 run（没有）⇒ 如实说"按旧格式回放"，并把当时记下的 `target`/`port` 摆出来
 *    （旧 run 里的"板 A / 板 B"、device key 继续兼容展示，**不迁移、不补全**）；
 *  · 两者都没有 ⇒ 说"这一趟没记下设备"，不编一个。
 */
function recordedDeviceText(recorded) {
  const row = recorded || {};
  const port = String(row.port || "");
  if (!port && !row.device_key && !row.target_id) return "设备：这一趟没有记下设备";
  if (row.legacy) {
    return `设备：按旧格式回放（${row.device_key || "?"}${port ? " · " + port : ""}）`
      + "—— 当时没记下设备序号与身份，不补";
  }
  const num = Number(row.scan_seq) ? `#${Number(row.scan_seq)} ` : "";
  const state = (row.identity_before || {}).state || "";
  const label = SNIFF_IDENTITY_LABEL[state] || state;
  return `设备：${num}${port || "?"}${label ? `（${label}）` : ""}`;
}

/* 历史表那一格里的小徽标：新 run 报"#3 COM<n>"，旧 run 报"旧格式"（悬停看详情）。 */
function recordedDeviceBadge(recorded) {
  const row = recorded || {};
  if (row.legacy) return "旧格式";
  const num = Number(row.scan_seq) ? `#${Number(row.scan_seq)}` : "设备";
  return `${num}${row.port ? " " + row.port : ""}`;
}

function renderRunMeta(run) {
  const idEl = $("runId");
  const subEl = $("runSub");
  if (!run) {
    idEl.textContent = "尚未发起任何 run";
    subEl.textContent = "";
    delete subEl.dataset.result;
    return;
  }
  idEl.textContent = `run ${run.run_id}`;
  /* 用户口径（2026-10-11）：这一行只回答“**结果是什么**”。
   * 从前它把“当时怎么跑的”也铺在同一行：板 <TARGET>（**内部目标键**）、`#2`（本次嗅探的
   * 显示序号）、`复位脉冲 否`（当时的选项）、`设备：#2 COM<n>（已确认身份）`（口在设备栏上
   * 已经写着）—— 那些属于**详情**，不是“现在该看什么”。一个事实都没删：
   * 它们全在 `#runSub` 的悬停提示里逐字可查。 */
  const parts = [`结果 ${run.result || "进行中"}`];
  if (run.assertion_summary) {
    parts.push(`断言 ${run.assertion_summary.passed}/${run.assertion_summary.total}`);
  }
  parts.push(`耗时 ${formatDuration(run.duration_ms)}`);
  parts.push(`采集 ${run.observe_seconds ?? "?"} s`);
  subEl.textContent = parts.join(" · ");
  /* 结果值只给**样式用**（用户口径 2026-10-13：主状态 PASS 就说绿、FAIL 说红）：
     这句话本身一个字没变，`txt` 与悬停里的事实都照旧。 */
  if (run.result) subEl.dataset.result = String(run.result);
  else delete subEl.dataset.result;
  const detail = [
    `run ${run.run_id}`,
    `目标 ${run.target || "?"}`,
    `通信口 ${run.port || "?"}`,
    `复位脉冲 ${run.reset_pulse ? "是" : "否"}`,
  ];
  const origin = run.client ? run.client.label || run.client.id : "";
  if (origin) detail.push(`发起者 ${origin}`);
  const runOptions = run.project_options || {};
  if (Object.keys(runOptions).length) {
    detail.push(`项目参数 ${Object.entries(runOptions).map(([key, value]) => `${key}=${value}`).join(", ")}`);
  }
  //  / D6：这一趟**自己记下**的设备（旧 run 会明写"按旧格式回放"）。
  if (run.device) detail.push(recordedDeviceText(run.device));
  subEl.title = detail.join(" · ");
}

/* 消息一行两处：自动验证那一栏一个（窗口里），实时观察那一栏一个。
 * ⚠️ 自动验证的消息**只**进它自己那一栏：否则切到实时观察时会看到一条属于上一轮 run
 * 的旧消息（实测就是“失败于 用户取消”排在“未连接”下面，看着像实时观察失败了）。
 * ⚠️ 实时观察的消息也**只**进它自己那一栏（用户口径 2026-10-12）：这两个元素住在同一个
 * 控制面板里、且**同时可见** ⇒ 两个都写就是同一句话在屏幕上一字不差地说两遍
 * （用户看到的就是这个）。它们都是同一个面板的子节点，自动验证那一屏也看得见
 * `#observeMessage`，所以最初“要写两份”的理由已经不成立。 */
function showMessage(text, kind, channel) {
  const hosts = channel === "observe" ? ["observeMessage"]
    : channel === "offline" ? ["offlineMessage"]
    : ["runMessage"];
  for (const id of hosts) {
    const el = $(id);
    if (!el) continue;
    /* 记住**这一行是谁写的**：自动验证装载一次 run 时要收走自己上一轮留下的那一行，
     * 但实时观察的话就挂在这个同一个元素上（上面那条 hosts 规则）—— 不能连它一起擦掉。 */
    el.dataset.channel = channel || "run";
    if (!text) {
      el.hidden = true;
      continue;
    }
    el.hidden = false;
    el.textContent = text;
    el.dataset.kind = kind || "";
  }
}

/* 自动验证这一屏没有话要说时（PASS / 占位报告）把**自己**留下的那一行收掉。
 * ⚠️ 只收 `channel === "run"` 的那一行：实时观察的消息写在同一元素上，别把别人的话擦掉。 */
function clearRunMessage() {
  const el = $("runMessage");
  if (!el || (el.dataset.channel || "run") !== "run") return;
  el.hidden = true;
  el.textContent = "";
  el.dataset.kind = "";
}

/* ============================================== 模式化历史列表（RUNTIME_MODES §12）
 *
 * 三种模式共用**同一套**列表组件（标题 + 列定义 + 行 + 点击动作），但数据源、字段与
 * 证据语义必须分开：
 *   自动验证 → **历史运行**：`/api/runs`（`runs/`），列是 run/结果/目标/发起者/耗时/开始/证据；
 *   实时观察 → **历史观察**：现有观察会话清单（只读目录），列含状态/设备/时长/TX·RX/结束原因；
 *   离线诊断 → **诊断会话**：同一份会话清单（含导入的诊断包），点一行走**现有**离线装载链。
 *
 * ⚠️ 观察会话**不包装成 run**：它没有 report.json / steps.jsonl，也不该被读成一次自动验证。
 *    点“历史观察”只调用现有的 `loadSession()`（离线诊断那一路），不另建第二套回放器。
 * ⚠️ 这里全是**派生视图**：只读列表响应，不写任何证据文件、不碰串口。
 *
 * 全部是纯函数（`historyModel()` 一行不碰 DOM），宿主模型回归直接喂它三类数据。 */
const HISTORY_TITLES = { auto: "历史运行", live: "历史观察", offline: "诊断会话" };
const HISTORY_HINTS = {
  auto: "最近 10 次运行（自己滚）",
  live: "点一行去离线诊断回看这次会话",
  offline: "本机会话与导入的诊断包；点一行回看",
};
const HISTORY_COLUMNS = {
  auto: ["run", "结果", "目标", "发起者", "耗时", "开始时间（北京时间）", "证据"],
  live: ["会话", "状态", "设备/通信口", "持续时间", "开始时间（北京时间）", "TX/RX", "结束原因", "证据"],
  offline: ["会话", "状态", "设备/通信口", "持续时间", "开始时间（北京时间）", "TX/RX", "结束原因", "证据"],
};
/* 会话状态：**只说这一份记录的状态**，不用 PASS/FAIL 那套 run 语义（会话说的是观察，
 * 不是“测试通过”）。空会话目录 / 接口失败 / 会话不存在各有各的话（§12.1 第 4 条）。 */
const HISTORY_SESSION_STATES = {
  live: { text: "观察中", kind: "live" },
  closed: { text: "已结束", kind: "closed" },
  open: { text: "没有结束记录", kind: "open" },
  broken: { text: "读不出来", kind: "broken" },
};
const SESSION_EVIDENCE_LINKS = [
  ["manifest.json", "manifest"],
  ["raw.bin", "raw"],
  ["io_events.jsonl", "events"],
  ["decoded.log", "decoded"],
  ["app_events.jsonl", "app_events"],
];

function historyTitle(mode) { return HISTORY_TITLES[mode] || HISTORY_TITLES.auto; }
function historyHint(mode) { return HISTORY_HINTS[mode] || HISTORY_HINTS.auto; }
function historyColumns(mode) { return (HISTORY_COLUMNS[mode] || HISTORY_COLUMNS.auto).slice(); }

function sessionStateOf(row) {
  if (!row) return "broken";
  if (row.broken) return "broken";
  if (row.live) return "live";
  return row.complete ? "closed" : "open";
}

/* 会话持续时间 = manifest 记的开始/结束时刻之差；还在写的那次 = 开始到现在（`now`）。
 * ⚠️ 不拿 `totals` 编一个数（那份里根本没有时长）—— 时间戳本身就是记录下来的事实。 */
function sessionDurationMs(row, now) {
  const started = Date.parse(String((row && row.opened_utc) || ""));
  if (!Number.isFinite(started)) return null;
  const closedRaw = row && row.closed_utc;
  const closed = closedRaw ? Date.parse(String(closedRaw)) : Number(now);
  if (!Number.isFinite(closed)) return null;
  return Math.max(0, closed - started);
}

/* 设备 / 通信口：先给设备（自报的标签），再给口与波特率。都没有就如实说“未知”。 */
function sessionDeviceText(row) {
  const device = (row && (row.device_label || row.device)) || "";
  const port = (row && row.port) || "";
  const baud = row && row.baud ? ` @ ${row.baud}` : "";
  if (!device && !port) return "未知";
  return [device || "", port ? `${port}${baud}` : ""].filter(Boolean).join(" · ");
}

/* TX/RX：**缺失就说“未知”**，不用 0、空白或 PASS 顶替（§12 明令）。
 * `totals` 是会话结束时才写的 ⇒ 进行中的那次只有文件大小可看，用“（文件）”标出来。 */
function sessionTrafficText(row) {
  const counts = sessionRowCounts(row);
  const rx = counts.rx === null ? "未知" : `${counts.rx} B${counts.fromTotals ? "" : "（文件）"}`;
  const tx = counts.tx === null ? "未知" : `${counts.tx} B`;
  return `RX ${rx} · TX ${tx}`;
}

function sessionEndReasonText(row) {
  if (!row || row.broken) return "未知";
  if (row.live) return "不适用（进行中）";
  const reason = row.end_reason;
  if (reason) return String(reason);
  return row.complete ? "未知" : "没有收尾记录";
}

function sessionOriginBadge(row) {
  return row && row.origin === "import" ? ' <span class="muted">（导入）</span>' : "";
}

function sessionEvidenceHtml(row) {
  const files = new Set((row && row.files) || []);
  const links = SESSION_EVIDENCE_LINKS
    .filter(([name]) => files.has(name))
    .map(([name, label]) => `<a href="/api/observe/sessions/${encodeURIComponent(row.session_id)}`
      + `/evidence/${name}?download=1">${label}</a>`);
  return links.length ? links.join("") : '<span class="muted">—</span>';
}

function runHistoryRows(runs) {
  return (runs || []).map((run) => {
    const available = new Set(run.artifact_names || []);
    const evidenceLinks = MANDATORY.concat(
      TEST_EVIDENCE.filter(([name]) => available.has(name))
    );
    const links = evidenceLinks.map(
      ([name, label]) =>
        `<a href="/api/runs/${encodeURIComponent(run.run_id)}/evidence/${name}?download=1">${label}</a>`
    ).join("");
    const recorded = run.device
      ? ` <span class="lock" title="${escapeHtml(recordedDeviceText(run.device))}">${escapeHtml(recordedDeviceBadge(run.device))}</span>`
      : "";
    return {
      action: { kind: "run", id: run.run_id },
      cells: [
        `<span class="run">${escapeHtml(run.run_id)}</span>`
          + ` <a href="#run=${encodeURIComponent(run.run_id)}" title="回看本次证据">回看</a>`,
        `<span class="badge" data-result="${escapeHtml(run.result || "unknown")}">${escapeHtml(run.result || "unknown")}</span>`,
        `${escapeHtml(run.target || "—")} · ${escapeHtml(run.port || "—")}${run.suite ? " · " + escapeHtml(run.suite) : ""}${recorded}`,
        escapeHtml(run.client_label || "—"),
        escapeHtml(formatDuration(run.duration_ms)),
        escapeHtml(shortTime(run.started_utc)),
        `<div class="links">${links}</div>`,
      ],
    };
  });
}

function sessionHistoryRows(sessions, now) {
  return (sessions || []).map((row) => {
    const state = HISTORY_SESSION_STATES[sessionStateOf(row)];
    return {
      action: { kind: "session", id: row.session_id },
      cells: [
        `<span class="run">${escapeHtml(row.session_id)}</span>${sessionOriginBadge(row)}`
          + ` <a href="#" data-history-action="session" data-history-id="${escapeHtml(row.session_id)}"`
          + ' title="回看这次会话">回看</a>',
        `<span class="badge" data-state="${state.kind}">${state.text}</span>`,
        escapeHtml(sessionDeviceText(row)),
        escapeHtml(formatDuration(sessionDurationMs(row, now))),
        escapeHtml(shortTime(row.opened_utc)),
        escapeHtml(sessionTrafficText(row)),
        escapeHtml(sessionEndReasonText(row)),
        `<div class="links">${sessionEvidenceHtml(row)}</div>`,
      ],
    };
  });
}

/* 空 / 加载中 / 接口失败：三种状态各有各的话，**不冒充加载成功**（§12.1 第 4 条）。 */
function historyNote(mode, state, error, count) {
  if (state === "error") return `读不到${mode === "auto" ? " run 列表" : "会话清单"}：${error || "接口失败"}`;
  if (state === "loading") return "加载中…";
  if (!count) {
    return mode === "auto"
      ? "还没有任何 run 目录"
      : "本机还没有会话：先做一次实时观察，或导入一个诊断包";
  }
  return "";
}

/* 三种模式共用的一份列表模型（纯函数）：给定模式与数据，说出标题、提示、列、行和状态行。 */
function historyModel(mode, view) {
  const spec = view || {};
  const kind = MODE_IDS.includes(mode) ? mode : "auto";
  const columns = historyColumns(kind);
  const rows = kind === "auto"
    ? runHistoryRows(spec.runs || [])
    : sessionHistoryRows(spec.sessions || [], spec.now);
  const state = spec.state || "ready";
  return {
    mode: kind,
    title: historyTitle(kind),
    hint: historyHint(kind),
    columns,
    rows,
    state,
    note: historyNote(kind, state, spec.error, rows.length),
  };
}

/* 把模型画到屏幕上。列定义先比签名再重画，避免每 2 s 把表头重建一遍（DOM 抖动）。 */
function renderHistory() {
  const head = $("runsHead");
  const body = $("runsBody");
  if (!head || !body) return;
  const mode = MODE_IDS.includes(app.mode) ? app.mode : "auto";
  const view = mode === "auto"
    ? { runs: app.runs, state: app.runsState, error: app.runsError, now: Date.now() }
    : { sessions: app.sessions, state: app.sessionsState, error: app.sessionsError, now: Date.now() };
  const model = historyModel(mode, view);
  const titleEl = $("historyTitle");
  const hintEl = $("historyHint");
  if (titleEl && titleEl.textContent !== model.title) titleEl.textContent = model.title;
  if (hintEl && hintEl.textContent !== model.hint) hintEl.textContent = model.hint;
  const signature = model.columns.join("|");
  if (head.dataset.signature !== signature) {
    head.innerHTML = `<tr>${model.columns.map((label) => `<th>${escapeHtml(label)}</th>`).join("")}</tr>`;
    head.dataset.signature = signature;
  }
  body.textContent = "";
  if (model.note) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = model.columns.length;
    td.className = model.state === "error" ? "bad" : "muted";
    td.textContent = model.note;
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of model.rows) {
    const tr = document.createElement("tr");
    tr.className = "history-row";
    tr.dataset.historyAction = row.action.kind;
    tr.dataset.historyId = row.action.id;
    tr.title = row.action.kind === "session"
      ? "点这一行回看这次会话"
      : "点这一行回看这次 run 的证据";
    tr.innerHTML = row.cells.map((cell) => `<td>${cell}</td>`).join("");
    body.appendChild(tr);
  }
}

/* 一次历史列表点击。**只有两件事**：打开该 run 的证据，或去离线诊断回看该会话。
 * 证据下载链接（`<a>`）照旧走链接本身 —— 行点击不许把它的默认行为吃掉。 */
function historyRowClick(event) {
  const trigger = event.target.closest("[data-history-action]");
  if (!trigger) return;
  const link = event.target.closest("a");
  if (link && !link.hasAttribute("data-history-action")) return;
  const id = trigger.dataset.historyId || "";
  if (!id) return;
  if (link) event.preventDefault();
  if (trigger.dataset.historyAction === "session") openHistorySession(id);
  else openHistoryRun(id);
}

/* 打开一次历史观察：严格**复用现有离线诊断装载链**（`loadSession` → `applySessionDetail`
 * 会切到离线诊断那一屏）。它只发只读 GET（会话详情），不连接设备、不建会话、不改证据。 */
async function openHistorySession(sessionId) {
  await loadSession(sessionId);
}

/* 打开一次历史 run：与那一行里的“回看”链接同一条路（`#run=` → `loadRunOrReport`）。 */
function openHistoryRun(runId) {
  window.location.hash = `run=${encodeURIComponent(runId)}`;
}

/* -------------------------------------------------------------------- fetch */
async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `${url} -> HTTP ${response.status}`);
  }
  return payload;
}

/* 装载一次 run 到**自动验证那一屏**。
 *
 * `focus`（默认 true）= 这是一次**用户动作**（点历史里那一条 / URL 带 `#run=`）⇒
 * 切到那一屏给人看。**自动装载**（run 刚结束、页面初始装最新那次）必须传 `focus: false`：
 * 那两个场合下用户可能正看着离线包或实时那一屏 —— 真机验收量到的：run 一结束，
 * 页面把离线那一屏换成了 run，正在读的包当场消失（“自动装载不该拖走人“）。 */
async function loadRun(runId, { focus = true } = {}) {
  if (focus && app.mode !== "auto") setMode("auto");
  const view = app.views.auto;
  const store = storeFor(view);
  view.viewing = runId;
  view.review = null;    // 一屏里只有一个来源：装载 run 就不再是在回看会话
  const detail = await getJson(`/api/runs/${encodeURIComponent(runId)}`);
  // ：这一次 run 自己声明的能力（旧 run 没这个键 ⇒ `null` = 未声明，不猜）。
  view.capabilities = (detail.manifest && detail.manifest.capabilities) || null;
  const report = detail.report;
  renderAssertions(report ? report.assertions : null);
  renderTestResults(report?.tests, report?.test_summary);
  const steps = detail.live && detail.steps ? detail.steps : (report ? report.steps : []);
  renderSteps(steps);
  //  T3：阶段卡片。回放与实时**共用** `applyTestStep` 这一条路径，
  // 于是“刷新后看到的”与“跑的时候看到的”是同一套判断（含“终态不可回退”）。
  // ⚠️ 这里会把事件流再解析一次（`buildRunSources` 也要用）：首屏只做一次，
  //    且只发生在 run 详情这一条路径上，换来的是两条路径不必共享中间状态。
  resetTestSteps(view);
  for (const event of parseEventLog(detail.event_stream)) {
    if (event.kind === "test_step") applyTestStep(event, view);
    // ⚠️ 回放也必须合并补写：否则实时跑着看得到四窗坐标，刷新一下反而变少了。
    if (event.kind === "test_step_refs") applyTestRefs(event, view);
  }
  stageBag(view).note = stageBag(view).seen ? "" : "这一轮没有阶段记录（旧 run 只有最终结果）";
  // 不在眼前那一屏（后台装载）就不重画：`renderStepCards()` 画的是贴在工作台上的那一份。
  if (attachedView() === view) renderStepCards();
  if (detail.live) {
    app.steps = steps || [];
  }
  renderRunMeta(
    report
      ? {
          run_id: runId,
          target: report.target,
          port: report.port,
          result: report.result,
          duration_ms: report.duration_ms,
          observe_seconds: report.observe_seconds,
          reset_pulse: report.reset_pulse,
          assertion_summary: report.assertion_summary,
          //  / D6：**这一趟自己记下的设备**（由服务端按这一份 manifest 读出）。
          device: detail.device || null,
        }
      : { run_id: runId, result: "running", observe_seconds: "?", device: detail.device || null }
  );
  // 四份证据各自装进自己那一路：一次整体装载，绝不把 A 的文本带进 B。
  // ⚠️ L3：raw.bin 超阈值时**不再整份取回**（那正是要消灭的全量读取）——
  //    改成区间窗口，只取尾部一段。
  const windows = omittedWindows(detail, RUN_EVIDENCE_FOR_PANE);
  loadRunSources(view, store, detail, windows.raw ? "" : await fetchRawEvidence(runId), windows);
  renderMetrics(detail.metrics, "auto");
  renderPanic(report ? report.panic : null, report ? report.fault_evidence : null);
  /* 用户口径（2026-10-11）：页脚那一行现在由**唯一一个写口**（2 s 轮询的 `refreshState`）
   * 维护（两个写入方抢同一行，屏幕上就会每 2 秒跳一次）。这里只把“这一趟的证据在哪”
   * 记下来，让它出现在那一行里；绝对路径进 title（`refreshState` 里写）。 */
  app.runDir = detail.run_dir || "";
  /* ⚠️ 用户口径（2026-10-12）：**占位报告不是失败**。`report.json` 在 run 开工时先写一份
   *    `result = "INCOMPLETE"` 的占位（`runstore.create()`），跑完才被真判决覆盖 ——
   *    于是「刷新页面就看到的那句『失败于 运行尚未结束（此文件是创建时的占位报告）』」
   *    是拿占位当判决，它对使用没有任何影响（用户口径：删掉）。
   *    真正的失败原因照旧写出来；这一屏没话要说时把上一轮那一行收掉（PASS 不留横幅）。 */
  const placeholder = String((report && report.result) || "") === "INCOMPLETE";
  if (report && report.failure_reason && !placeholder) {
    showMessage(`失败于 ${report.failure_reason}`, "fail");
  } else if (report && report.result !== "PASS" && !placeholder) {
    showMessage(
      `run ${runId} 结果 ${report.result}` +
        (report.assertion_summary
          ? ` · 断言 ${report.assertion_summary.passed}/${report.assertion_summary.total}`
          : ""),
      "fail"
    );
  } else {
    // 结果 PASS（或这份报告还是占位）：**不留横幅**；上一轮留下的那一行也在这里收掉，
    // 否则它会一直挂在屏幕上（用户口径 2026-10-12：那种常驻标识不要）。
    clearRunMessage();
  }
  // ⚠️ **PASS 不再写常驻横幅**：结果就写在标题位上（`#runId` + `#runSub` 的“结果 PASS ·
  //    断言 9/9”），再在下面重说一遍就是同一句话的第二、第三遍（用户口径 2026-10-11）。
  //    失败/取消照旧写：那是“为什么是这样”的答案，屏幕上必须有。
  // 解码窗底那一行说明跟着**这一屏看的对象**走 ⇒ 换 run 就重画一次
  // （不然要等下一次 2 s 轮询，中间那一段说的还是上一个对象的事）。
  renderDecodeWhy(app.observe);
}

/* raw.bin 是字节流（可能不是合法 UTF-8），所以它只能当二进制取回来再当文本看，
 * 下载入口仍然直接给原始字节。取不到时如实给空视图，不编造内容。 */
async function fetchRawEvidence(runId) {
  try {
    const response = await fetch(
      `/api/runs/${encodeURIComponent(runId)}/evidence/raw.bin`,
      { cache: "no-store" }
    );
    return response.ok ? await response.text() : "";
  } catch (error) {
    return "";
  }
}

function loadRunSources(view, store, detail, rawText, windows) {
  applyHistoricalSources(store, view, buildRunSources(detail, rawText), detail, windows || {});
  // 详情里的产物元数据决定"全文搜索能选哪几份证据"（不另编一份清单）。
  view.artifactsMeta = detail.artifacts_meta || [];
  // 后台装载（不在眼前那一份）不去碰搜索栏 —— 否则会把用户当前那一屏的候选换掉。
  if (store === attachedLog) renderFullSearchChoices();
}

/* 四窗内容只有这一条装载路径（run 的历史视图与离线回看共用）：
 * 一次整体装载，绝不把 A 的来源带进 B。 */
function applySources(store, payload) {
  for (const meta of SOURCES) {
    /* 派生视图的**行身份**列（事件号）与内容一起来：有它 ⇒ 这一窗按事件号定位；
     * 没它 ⇒ 它按真行号定位（run 的 tool.log、原始串口…，）。 */
    setSourceLines(store, meta.id, payload[meta.id] || [], payload[`${meta.id}_seqs`] || null);
    rebuildSource(store, meta.id);
    refreshModuleOptions(store.sources[meta.id]);   // 换来源后模块候选项跟着换
  }
  updateLogIndicators(store);
}

/* ---------------------------------------------------- 区间窗口（运行时） */
/* 与上面那组纯函数分开：这一段的职责是"发请求 / 收字节 / 写模型"，
 * 而它要经得起两件事：用户中途换了来源，用户中途要取消。 */

/* 每一路的证据文件名：run 与实时会话**不一样**（会话的"工具输出"窗口讲的是
 * `io_events.jsonl`，而且会话没有 tool.log）。所以两份表分开列，
 * 不拿一个"大概差不多"的映射硬套。 */
const RUN_EVIDENCE_FOR_PANE = {
  tool: "tool.log", raw: "raw.bin", decoded: "decoded.log", events: "app_events.jsonl",
};
const SESSION_EVIDENCE_FOR_PANE = {
  tool: "io_events.jsonl", raw: "raw.bin", decoded: "decoded.log", events: "app_events.jsonl",
};

/* 详情里"因为超阈值而没内联"的那些产物 → 每个窗口一份窗口描述（纯函数）。 */
function omittedWindows(detail, names) {
  const rows = (detail && detail.content_omitted) || [];
  const out = {};
  for (const [paneId, name] of Object.entries(names || {})) {
    const row = rows.find((item) => String(item && item.name) === name);
    if (row) {
      out[paneId] = {
        evidence: name,
        url: row.url,
        downloadUrl: row.download_url || "",
        bytes: Number(row.bytes) || 0,
      };
    }
  }
  return out;
}

function cancelWindow(store, id) {
  const source = (store || attachedLog).sources[id];
  const win = source && source.window;
  if (win && win.abort) win.abort.abort();
}

/* 装了一段之后，"已装载的区间"该怎么算。
 *
 * ⚠️ **换一段（跳到某个字节）与往前补一段是两件事**：跳到某处会把模型换成
 * 那一段（前面的内容已经不在了），所以区间要**重置**再合；合并的话状态行会把
 * 早就不在页面上的那几十兆说成"已装载"—— 那是拿一个假数字骗人。
 */
function windowRangesAfter(current, range, { replace = false } = {}) {
  return mergeByteRanges(replace ? [range] : [...current, range]);
}

/* 行号/时刻是按下标存的；往前插 k 行之后要给已有的键一起后移，
 * 否则"第 3 行的时刻"会跑到别的行上（这种错看不出来）。 */
function shiftTimes(source, offset) {
  if (!offset || !source.times.size) return;
  const moved = new Map();
  for (const [key, value] of source.times) moved.set(key + offset, value);
  source.times = moved;
}

/* 尾部窗口的边界：起点不在 0 字节 ⇒ 这一路的第 1 行是半行。 */
function planHeadPartial(range, win) {
  return Number(range && range[0]) > 0 && Number(win && win.totalBytes) > 0;
}

/* 窗口装载的**行号基准**（）：请求头 `X-Line-Base: 1` 问服务端，响应头里带回来。
 * 值 = 这一段的第一行在**文件**里是第几行（1 基）；没给 / 不可信就是 `null`。
 *
 * 以前没人告诉模型这件事，于是 `baseIndex` 只剩“被裁掉的行数”这一个含义 ——
 * 面板左边那列跟着「向前加载」一路涨（每窗 +约 3 万），
 * 跟搜索结果里那个**文件真行号**对不上（实测：字节 321683181 是第 4515116 行，
 * 面板却显示 32792）。小文件看不见这个洞：整份装载时 `baseIndex` 恒为 0，恰好就是真行号。 */

/* 上面那个数是**1 基行号**，而面板用的是 0 基的 `baseIndex`（行号 = baseIndex + 下标 + 1）。
 * ⚠️ 差一就是“面板第一行总比文件行号大 1”：我第一版就是这样漏了一步，
 *    真浏览器上量一下就看出来了（面板首行 4514679、真值 4514678）。
 */

async function loadWindowRange(id, range, { prepend = false, replace = false, lines = null } = {}) {
  const source = app.log.sources[id];
  const win = source && source.window;
  // ⚠️ 按**行**装载（T6 定位）：阶段坐标给的是行号，字节区间由服务端换算（`X-Line-Range`）。
  //    这条路上 `range` 允许为空 —— 真正的字节区间从响应的 `Content-Range` 里读回来。
  if (!win || (!range && !lines) || win.loading) return false;
  const generation = ++win.generation;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  win.abort = controller;
  win.loading = true;
  updateLogIndicators(app.log);
  try {
    const response = await fetch(win.url, {
      cache: "no-store",
      headers: lines
        ? { "X-Line-Range": `${lines[0]}-${lines[1]}`, "X-Line-Base": "1" }
        : { Range: `bytes=${range[0]}-${range[1]}`, "X-Line-Base": "1" },
      signal: controller ? controller.signal : undefined,
    });
    // ⚠️ 两道闸门：来源换了（`source.window !== win`）或又发了一轮（generation 变了）
    //  ⇒ 这份响应是**旧对象**的，绝不能写进去。
    if (source.window !== win || win.generation !== generation) return false;
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const buffer = new Uint8Array(await response.arrayBuffer());
    if (source.window !== win || win.generation !== generation) return false;
    const total = parseContentRangeTotal(response.headers.get("Content-Range"));
    if (Number.isFinite(total) && total > 0) win.totalBytes = total;
    // 设备字节可能不是合法 UTF-8：TextDecoder 默认把坏字节换成 U+FFFD，
    // 不抛异常（"看着像乱码"比"整窗报错"诚实）。
    const text = new TextDecoder("utf-8").decode(buffer);
    // 服务端把“这一段的第一行是文件第几行”带在响应头里（没给就是 `null`）。
    const base = lineBaseIndex(response.headers.get("X-Line-Base"));
    // 按行装载时，真正的字节区间在 `Content-Range` 里（那时 `range` 是空的）。
    const covered = /bytes (\d+)-(\d+)\//.exec(response.headers.get("Content-Range") || "");
    const loaded = covered ? [Number(covered[1]), Number(covered[2])] : range;
    win.ranges = windowRangesAfter(win.ranges, loaded, { replace });
    if (prepend) {
      const plan = olderMergePlan(source.lines, source.baseIndex, text, range[0]);
      if (plan.stitch) source.lines[0] = plan.stitch + source.lines[0];
      if (plan.head.length) {
        const added = plan.head.length;
        source.lines = plan.head.concat(source.lines);
        // 事件号列与 `lines` 逐行对齐 ⇒ 往前插 k 行就在前面补 k 个"没有事件号"。
        // （区间窗口这一路只服务真文件来源，工具窗不走这里；补 null 只是把不变式钉住。）
        if (source.seqs) source.seqs = new Array(added).fill(null).concat(source.seqs);
        // 时刻表的键是“文件行号”（= baseIndex + 下标），插进 k 行之后要把已有的键一起挪。
        // 服务端给了真基准就以它为准（那时编号本来就是连着的，不用挪）；
        // 没给才退回老办法（按插入的行数往后挪）。
        shiftTimes(source, base === null ? added : base + added - source.baseIndex);
        // ⚠️ 这里**不把插进来的行算成 `dropped`**：行数上限的记账由 `trimSource()` 一处做，
        //    两边都记就是把同一批行数数两遍（真机上 dropped 直接多出整整一倍）。
        //    `baseIndex` 稍后由服务端给的**真行号**覆盖 —— 那是行号基准，不是裁剪记账。
      }
      source.partialHead = plan.headPartial;
    } else {
      appendMany(app.log, id, toLines(text), null);
      // 按行装载时服务端从**行首**给，所以这一路没有“半行”。
      source.partialHead = lines ? false : planHeadPartial(range, win);
    }
    if (base !== null) {
      source.baseIndex = base;
      // 服务端给的**真行号**（这一段的第一行是文件第几行）：这一窗的行号可信。
      source.baseTrust = "exact";
      source.baseIssue = "";
    } else if (loaded && Number(loaded[0]) === 0) {
      // 从**文件开头**装载的那一段：第 1 行就是第 1 行，不需要服务端换算。
      source.baseIndex = 0;
      source.baseTrust = "exact";
      source.baseIssue = "";
    } else {
      // 服务端说不清“这一段的第一行是文件第几行” ⇒ 这一窗的行号只是**假定值**
      // （加载得成，但不拿它去定位 —— 见 `paneFrameIssue`）。
      source.baseTrust = "assumed";
      source.baseIssue = "";
    }
    source.rendered = 0;              // 结构变了：整体重画（历史装载本来就是整体替换）
    source.pending = 0;
    rebuildSource(app.log, id);
    updateLogIndicators(app.log);
    return true;
  } catch (error) {
    if (error && (error.name === "AbortError" || error.name === "TypeError")) return false;
    if (source.window === win) {
      showMessage(`装载 ${win.evidence} 的区间失败：${error.message}`, "warn", app.mode);
    }
    return false;
  } finally {
    if (source.window === win) {
      win.loading = false;
      source.window.abort = null;
      updateLogIndicators(app.log);
    }
  }
}

async function loadWindowTail(id) {
  const source = app.log.sources[id];
  const win = source && source.window;
  if (!win) return false;
  const range = tailByteRange(win.totalBytes, win.tailBytes);
  if (!range) return false;
  return loadWindowRange(id, range, { prepend: false });
}

/* 【向前加载】取相邻的更早一段。到头了就不再发请求（也不是"点了没反应"）。 */
async function loadOlderWindow(id) {
  const source = app.log.sources[id];
  const win = source && source.window;
  if (!win || win.loading) return false;
  const range = previousByteRange(win.ranges, win.tailBytes);
  if (!range) {
    showMessage(`${win.evidence} 已经加载到开头了`, "warn", app.mode);
    return false;
  }
  return loadWindowRange(id, range, { prepend: true });
}

/* 历史装载的**窗口版**：整份装不下的证据走区间窗口。
 * 没超阈值的那几路完全不变（还是 `applySources()` 的老路）。 */
function applyHistoricalSources(store, view, payload, detail, windows) {
  applySources(store, payload);
  const contract = windowContract(detail);
  view.windowContract = contract;   // 按模式存：这份契约是“这一屏正在看的那份证据”的
  for (const meta of SOURCES) {
    const source = store.sources[meta.id];
    source.window = null;
    source.partialHead = false;
  }
  const ids = Object.keys(windows || {});
  for (const id of ids) {
    const source = store.sources[id];
    const spec = windows[id];
    if (!source || !spec || !Number(spec.bytes)) continue;
    cancelWindow(store, id);
    setSourceLines(store, id, []);   // 这一路改成窗口装载：先把旧内容清掉
    source.window = {
      evidence: spec.evidence,
      url: spec.url,
      downloadUrl: spec.downloadUrl || "",
      totalBytes: Number(spec.bytes) || 0,
      tailBytes: contract.tailBytes,
      ranges: [],
      generation: 0,
      abort: null,
      loading: false,
      from: contract.from,
    };
    rebuildSource(store, id);
  }
  updateLogIndicators(store);
  for (const id of ids) if (store.sources[id].window) void loadWindowTail(id);
}

/* ------------------------------------------------- 全文件搜索（L4：纯函数） */
/* 这一组只负责"把事实说成一句话"与"算出该取哪一段字节"，不碰网络也不碰 DOM ——
 * 宿主回归（`test_webconsole.py` 的模型回归）直接驱动它们。 */

function searchHitLabel(hit, index) {
  const row = hit || {};
  const line = Number(row.line_no);
  const where = Number.isFinite(line) && line > 0 ? `第 ${line} 行` : "行号未定";
  const flag = row.at_block_start ? "（上下文可能从中间开始）" : "";
  return `${index ? index + ". " : ""}字节 ${row.offset} · ${where}${flag} · ${row.text}`;
}

/* 一条命中该按哪个 Range 取回来：以命中的字节为中心，两端夹到文件内。
 * 返回值就是 `bytes=start-end` 那两个数；算不出来（没有大小）就给 null。 */
function searchJumpRange(hit, totalBytes, pad) {
  const size = Number(totalBytes) || 0;
  const offset = Number(hit && hit.offset);
  if (!size || !Number.isFinite(offset) || offset < 0) return null;
  const width = Math.max(1, Number(pad) || SEARCH_JUMP_PAD_BYTES);
  return [Math.max(0, offset - width), Math.min(size - 1, offset + width)];
}

function searchRunningText(payload) {
  const progress = (payload && payload.progress) || {};
  const scanned = Number(progress.scanned_bytes) || 0;
  // 刚起完任务时还没有进度快照：那就用任务自己的快照边界，
  // 不能写成"0 B / 0 B" —— 那看起来像在扫一个空文件。
  const total =
    Number(progress.total_bytes)
    || Number(payload && payload.task && payload.task.size)
    || 0;
  const hits = Number(progress.hits) || 0;
  return `扫描中：${byteText(scanned)} / ${byteText(total)} · 已命中 ${hits} 条`;
}

/* 收尾那句话必须**分得清**四种结束：扫完 / 到上限 / 取消 / 超时。
 * 把后面三种说成"没命中"就是拿一个假结论骗人。 */
function searchDoneText(payload) {
  const row = payload || {};
  const hits = row.hits || [];
  const progress = row.progress || {};
  const scanned = Number(progress.scanned_bytes) || 0;
  const total = Number(progress.total_bytes) || 0;
  if (row.state === "cancelled") {
    return `已取消：扫到 ${byteText(scanned)} / ${byteText(total)}，命中 ${hits.length} 条（后面还没扫）`;
  }
  if (row.state === "timeout") {
    return `超时停下：扫到 ${byteText(scanned)} / ${byteText(total)}，命中 ${hits.length} 条（后面还没扫）`;
  }
  if (row.state === "error") {
    return `搜索失败：${row.error || "服务端报错"}`;
  }
  if (row.truncated) {
    return `命中已达上限，只报前 ${hits.length} 条（扫描停在 ${byteText(scanned)} / ${byteText(total)}，后面还没扫）`;
  }
  return `扫完全部 ${byteText(total)}，命中 ${hits.length} 条`;
}

/* 拿到"命中那一段文字"在已加载行里的下标（拿来高亮 / 滚到可见处）。
 * 找不到就返回 -1 —— 不猜一个位置出来。 */
/* 服务端给的“命中上下文”不一定能直接拿去找行（真机实测，）：
 * ① 超长行会被**截断**，两头带 `…` —— 那是**服务端的标记**，文件里没有这个字符；
 * ② 上下文里可能有**单独的 CR**，而面板把 CR 也当断行（`toLines`）⇒
 *    一条服务端“行”在窗口里是好几行，整段前缀跨了行。
 * 所以先把它拆成几条**候选片段**（长的先试）：去省略号 → 按 CR/LF 拆。
 * 实测（raw.bin 搜 `842`，26 条命中）：前两条正因此找不到行，第 3～5 条刚好是干净的一行。
 */
const SEARCH_FRAGMENT_MIN = 4;

function searchTextCandidates(text) {
  const whole = String(text == null ? "" : text).trim();
  if (!whole) return [];
  const body = whole.replace(/^…+/, "").replace(/…+$/, "").trim();
  const seen = new Set();
  const out = [];
  for (const piece of [whole, body, ...body.split(/\r\n|\r|\n/)]) {
    const value = piece.trim();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  const long = out.filter((value) => value.length >= SEARCH_FRAGMENT_MIN);
  return (long.length ? long : out).sort((a, b) => b.length - a.length);
}

/* 拿到“命中那一段文字”在已加载行里的下标（拿来高亮 / 滚到可见处）。找不到就返回 -1 —— 不猜。
 * ⚠️ 逐条候选试：**唯一命中**优先（先整行相等、再子串）；全都多处能对上时用第一条
 *    （不猜“更准”的位置，也不因为猜不准就什么都不做）。 */
function findLineIndex(lines, text) {
  const rows = lines || [];
  if (!rows.length) return -1;
  let fallback = -1;
  for (const candidate of searchTextCandidates(text)) {
    const exact = rows.indexOf(candidate);
    if (exact >= 0) return exact;
    const matches = [];
    for (let index = 0; index < rows.length; index += 1) {
      if (String(rows[index]).indexOf(candidate) >= 0) matches.push(index);
    }
    if (matches.length === 1) return matches[0];
    if (matches.length && fallback < 0) fallback = matches[0];
  }
  return fallback;
}

/* 把某一行在**它自己那一窗里**居中。
 *
 * ⚠️ 不用 `element.scrollIntoView()`：它会把**所有**能滚的祖先一起滚 —— 页面也跟着跑，
 *    于是“点一条结果”看起来像整页乱跳（用户报的“阅读位异常”就是这一路）。
 *    要动页面就**显式**动（见 `alignReadingPosition`）。返回是否真的动了。
 */
/*: 证据引用里的来源 → 窗口 id。⚠️ 第四类名字不一样：`app_events` 对应 `events` 窗。 */
const PANE_FOR_EVIDENCE_REF = { tool: "tool", raw: "raw", decoded: "decoded", app_events: "events" };

/* 1 基证据行号 → 0 基 DOM 下标（`data-src` = “文件里第几行 − 1”，
 * `baseIndex` = 已装载那一段的**第一行**在文件里的 0 基行号）。
 *
 * ⚠️ **两个都要减**：证据行号是 1 基的，而 `baseIndex` 是 0 基的。
 *    下标 = line - 1 - baseIndex（用户口径 2026-10-13）。少减那个 1 就整体**差一行**：
 *    要定位第 1～13 行，实际圈的是第 2～14 行 —— 看起来就是“点定位停在启动日志首行”。
 * 下标 < 0 = 这一行还没装载 / 不在这一段里（调用方跳过它，不假装标上了）。 */

/* 事件号 → 行下标（纯函数，宿主回归直接驱它）。
 *
 * 派生视图（工具输出）的行**身份**就是它来自的那条 io 事件（）：`seqs` 与
 * `lines` 逐行对齐。找不到返回 **-1**（不返回 0 —— 那会假装第一条就是它）。 */

/* 一条证据引用 → 定位计划（纯函数，宿主回归直接驱动它）：要轻微高亮的**区间**
 * 与要滚到窗口中间的**中心锚点**，两串都是 0 基下标。
 *
 * 锚点优先级（用户口径 2026-10-13）：
 *   ① 后端给的 `anchor_line`（决定性证据行，如 `@TASKS`）—— 但它必须**落在区间内**；
 *   ② 区间**末行** —— 旧记录没有 `anchor_line` 时的诚实回退（也是后端的默认值）；
 *   ③ 区间只有一行时 ② 就是那一行。
 * 坐标无效（行号不是数字 / 小于 1 / 末行在首行之前）返回 `null`：**什么也不做** ——
 * 不得把用户带到与这一段证据无关的位置。 */

/** 点阶段卡上的“定位”：四类证据窗各自滚到该阶段的行区间并高亮（T6 / 需求 §8.2、§8.3）。
 *
 * ⚠️ 只动滚动、高亮与“当前定位目标”这一件事：**不暂停采集、不修改证据、不改 PASS/FAIL**。
 * ⚠️ 某一类定位不到就显式说一句（**含原因**），但不拖累别的窗格（需求 §8.4：不整块失效）。
 * ⚠️ ：行号能不能信**按窗**判 —— 一个窗不可信不影响另外三个，也绝不拿它的行号去跳。
 */
async function locateStage(stageId) {
  const row = stageBag().rows.get(String(stageId));
  if (!row) return;
  app.locatedStage = String(stageId); // 只有一个当前目标（卡片与按钮同源）
  renderStepCards();
  const moved = [];
  const missed = [];
  const refs = row.evidence_refs || [];
  for (const item of refs) {
    const paneId = PANE_FOR_EVIDENCE_REF[item.source];
    const why = paneId ? await locatePaneLines(paneId, item) : "没有对应的窗口";
    if (why) missed.push(`${item.source}：${why}`);
    else moved.push(item.source);
  }
  /* 另有一类“没动”，与上面那类完全不是一回事：**这一阶段根本没给那一类坐标**
   *（那几份证据在这一步没有新增行 —— 比如「发起身份查询」那一步，设备还没回话，
   * 三个会话证据窗里一行都没写）⇒ 那几窗保持原位是**对的**，但必须说出来，
   * 否则用户看到“只动了一个窗口”会以为定位坏了（2026-10-13 用户报的就是这个）。 */
  const given = new Set(refs.map((item) => String(item && item.source)));
  const absent = Object.keys(PANE_FOR_EVIDENCE_REF).filter((source) => !given.has(source));
  if (!moved.length) {
    // 一个窗都没动就**不能说自己在定位**（那个记号会让卡片看起来已经跳过去了）。
    app.locatedStage = "";
    renderStepCards();
    const why = missed.length ? missed.join("；") : "这一阶段没有任何一类坐标";
    showMessage(`定位：这一次没能定位 —— ${why}`, "warn", "observe");
  } else if (missed.length) {
    showMessage(`定位：${moved.length} 类已就位；${missed.join("；")}`, "warn", "observe");
  } else if (absent.length) {
    const names = absent.map((source) => {
      const meta = SOURCE_BY_ID[PANE_FOR_EVIDENCE_REF[source]];
      return meta ? meta.label : source;
    });
    showMessage(
      `定位：${moved.length} 类已就位；${names.join(" / ")}在这一阶段没有新增行（那几窗保持原位）`,
      "", "observe",
    );
  }
}

/** 让一个窗滚到这条引用指的区间并高亮，中心锚点居中。
 * 返回**空串** = 真的动了；非空 = 没动的原因（宿主回归直接核对它）。
 *
 * 四类证据窗**各用各自那条引用**（`ref.source` 决定哪个窗）：raw 的行号绝不拿去套 decoded。
 */
async function locatePaneLines(paneId, ref) {
  const source = app.log.sources[paneId];
  if (!source) return "没有这个窗口";
  // 先问行号可不可信：不可信就明说（带上原因），**不去猜一个位置**。
  const issue = paneFrameIssue(source, app.replayTruncated);
  if (issue) return issue;
  /* 派生视图（工具输出）：它的行没有与文件对齐的行号 ⇒ 坐标只有一种写法 ——
   * 那条 io 事件的**事件号**（`{ source: "tool", event_seq: N }`，）。
   * 按**行号**给它坐标就如实拒掉（不拿一个差不多的行号跳过去）。 */
  const hasSeqRef = ref && ref.event_seq !== undefined && ref.event_seq !== null;
  if (hasSeqRef) return markEventSeqRow(source, ref.event_seq);
  if (source.derived) {
    return "这一窗是派生视图（内容由 io_events.jsonl 画出来，行号不等于文件行号）——只能按事件号定位";
  }
  const first = Number(ref && ref.start_line);
  const end = Number(ref && ref.end_line);
  const last = Number.isFinite(end) ? end : first;
  if (!Number.isFinite(first) || first < 1) return "坐标无效";
  const win = source.window;
  if (win) {
    // 大文件：目标区间还没装载就先取回来（需求 §8.3）。
    // ⚠️ 要连**上下留白**一起取：只装载证据区间本身的话，锚点贴着装载段的一端，
    //    滚动条没有余量 ⇒ “居中”做不到（旧行为：1～13 行装进来，第 13 行永远贴底）。
    const pad = locateContextLines(paneId);
    setSourceLines(app.log, paneId, []);
    const ok = await loadWindowRange(paneId, null, {
      replace: true,
      lines: [Math.max(1, first - pad), last + pad],
    });
    if (!ok) return "这一段没装载上来";
  }
  const plan = locatePlan(ref, source.baseIndex);
  if (!plan) return `第 ${first}–${last} 行不在这一窗里`;
  return markLineRange(source, plan) ? "" : "那几行被过滤或裁掉了";
}

/* 定位时装载的**上下留白**（行数）：取可见行数的一半 —— 锚点能真的落在视区中部。 */
function locateContextLines(paneId) {
  const host = $(`lines-${paneId}`);
  const lineHeight = parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue("--log-line-h")
  ) || 17;
  const height = host ? Number(host.clientHeight) || 0 : 0;
  return Math.max(0, Math.floor(height / 2 / lineHeight));
}

/** 派生视图（工具输出）的一行按**事件号**定位（）。
 * 返回**空串** = 真的动了；非空 = 没动的原因（与 `locatePaneLines` 同一条契约）。
 *
 * ⚠️ 它不走 `locatePlan`：那一路的输入是**文件行号**，而这一窗的行号不与文件对齐。
 *    下标算出来之后仍复用 `markLineRange` —— 高亮与居中的那套只认下标。
 */
function markEventSeqRow(source, eventSeq) {
  const index = seqRowIndex(source.seqs, eventSeq);
  if (index < 0) {
    return source.derived
      ? `这条事件（#${Math.floor(Number(eventSeq))}）不在这一窗里（被裁掉、被过滤，或者它本来就不画）`
      : "这一窗不是按事件号画的（它有自己的文件行号），事件号在这里找不到行";
  }
  return markLineRange(source, { first: index, last: index, anchor: index })
    ? "" : "那一条被过滤或裁掉了";
}

/** 按定位计划标出区间并把**中心锚点**滚到窗口中间；一个都没标上就返回 false。
 *
 * ⚠️ 锚点优先用计划里的那一行（决定性证据行）；它不在 DOM 里（被过滤/被裁剪）时才
 *    退到区间里最后一个标上的行 —— 仍在区间内，不把用户带到无关位置。
 */
function markLineRange(source, plan) {
  if (!source.el || !plan) return false;
  // 上一次定位的记号一次清完：`data-hit` = 被定位的证据**区间**，`data-center` = 区间里
  // 滚到窗口中间的**中心行**（两个都只给样式用，见 style.css 的 `--loc*`）。
  for (const node of source.el.querySelectorAll(".log-line[data-hit='1'], .log-line[data-center='1']")) {
    node.removeAttribute("data-hit");
    node.removeAttribute("data-center");
  }
  const base = Number(source.baseIndex) || 0;
  // `data-src` = 文件里的 0 基行号 = baseIndex + 下标。
  const nodeAt = (index) => source.el.querySelector(`.log-line[data-src="${base + index}"]`);
  let anchorNode = null;
  for (let index = plan.first; index <= plan.last; index += 1) {
    const node = nodeAt(index);
    if (!node) continue; // 被裁剪或被过滤掉的行：跳过，不假装标上了
    node.dataset.hit = "1";
    anchorNode = node;
  }
  if (!anchorNode) return false;
  /* 中心行：优先锚点那一行，其次刚才最后一个标上的行（仍在区间内）。 */
  const center = nodeAt(plan.anchor) || anchorNode;
  center.dataset.center = "1";
  source.followTail = false; // 用户在看某一处，不是尾巴
  /* 居中用**矩形差**算（与 `centerLineInPane` 同一套）。
   * ⚠️ 不能用 `offsetTop`：它是相对 `offsetParent`（窗口那一层）量的，窗口头 + 筛选行的高度
   *    也算了进去 ⇒ 目标行停在“正中偏上”（实测差 52 px，而窗口头正好 53 px）。
   *    偏上看起来就像“没居中”，用户报的就是这件事。 */
  const hostRect = source.el.getBoundingClientRect();
  const rowRect = center.getBoundingClientRect();
  const delta = (rowRect.top - hostRect.top) - (source.el.clientHeight - rowRect.height) / 2;
  source.el.scrollTop = Math.max(0, source.el.scrollTop + delta);
  return true;
}

function centerLineInPane(paneId, index) {
  const source = app.log.sources[paneId];
  const host = $(`lines-${paneId}`);
  const rows = host ? host.querySelectorAll(".log-line") : [];
  const row = rows[index];
  if (!source || !source.el || !row) return false;
  const hostRect = source.el.getBoundingClientRect();
  const rowRect = row.getBoundingClientRect();
  const delta = (rowRect.top - hostRect.top) - (source.el.clientHeight - rowRect.height) / 2;
  source.el.scrollTop += delta;
  return true;
}

function scrollLineIntoView(paneId, index) {
  const host = $(`lines-${paneId}`);
  if (!host || index < 0) return;
  const rows = host.querySelectorAll(".log-line");
  const target = rows[index];
  if (!target) return;
  // 一次只留一个“刚才跳到这里”的记号：否则满屏黄条，反而看不出刚跳的是哪一行。
  for (const other of host.querySelectorAll(".log-line[data-hit='1'], .log-line[data-center='1']")) {
    delete other.dataset.hit;
    delete other.dataset.center;
  }
  target.dataset.hit = "1";
  target.dataset.center = "1";
  centerLineInPane(paneId, index);
}

/* ------------------------------------------------- 全文件搜索（L4：运行时） */
/* 服务端分块扫（有进度、能取消、有结果上限），这里只做三件事：
 * 起任务、按 task_id 轮询、把一条命中点成"跳到那个字节附近的窗口"。
 *
 * ⚠️ 与每一窗自己的搜索框**不是同一件事**：那一栏只搜索**已经加载到页面上的**
 * 内容（窗口模式下就是那一段字节），这一栏搜索**整个文件**。两者谁也不冒充谁 ——
 * 窗口模式的搜索框会自己写明"只搜索已加载区间"。 */

const SEARCH_POLL_MS = 400;
const SEARCH_JUMP_PAD_BYTES = 32 * 1024;

function searchEvidenceMap() {
  return app.mode === "offline" ? SESSION_EVIDENCE_FOR_PANE : RUN_EVIDENCE_FOR_PANE;
}

function searchPaneFor(evidence) {
  for (const [paneId, name] of Object.entries(searchEvidenceMap())) {
    if (name === evidence) return paneId;
  }
  return "";
}

/* 这次全文搜索针对哪一份实体：run 还是会话 —— 由"现在在看什么"决定。 */
function searchEntity() {
  if (app.viewing) return { run_id: app.viewing };
  const review = app.review;
  if (review && review.session_id) return { session_id: review.session_id };
  return null;
}

/* 结果里的证据名 → 下拉框里能选的那些（详情给过的产物，含只装了尾部一段的）。
 *
 * ⚠️ 入参就是证据元数据本身（纯函数）：宿主回归直接喂它，不靠真页面。
 * 会话详情以前**不带** `artifacts_meta`（只有 run 详情带），于是回看一个会话时
 * 那些可搜的证据一个都列不出来 —— 真浏览器上就是这样露的馅，已从服务端修掉。
 */
function searchEvidenceChoices(rows) {
  const names = new Set(Object.values(searchEvidenceMap()));
  const out = [];
  for (const row of rows || []) {
    if (row && row.name && (names.has(row.name) || Number(row.bytes) > 0)) out.push(row);
  }
  return out;
}

/* 默认搜哪一份：**最大**的那份（会话里通常就是 raw.bin）。
 * ⚠️ 这只是兑底；真正的“默认值”在下面 `preferredSearchEvidence()`（它优先选正在滚动的那一份）。
 */
function largestSearchEvidence(rows) {
  let best = "";
  let size = -1;
  for (const row of searchEvidenceChoices(rows)) {
    const bytes = Number(row.bytes) || 0;
    if (bytes > size) {
      size = bytes;
      best = row.name;
    }
  }
  return best;
}

function cancelSearchPoll() {
  if (app.fullSearchTimer) {
    clearTimeout(app.fullSearchTimer);
    app.fullSearchTimer = null;
  }
}

function renderFullSearchStatus(text, state) {
  const row = $("fullSearchStatus");
  if (!row) return;
  row.textContent = text;
  if (state) row.dataset.state = state;
  else delete row.dataset.state;
}

async function startFullSearch() {
  const entity = searchEntity();
  const evidence = $("fullSearchEvidence") ? $("fullSearchEvidence").value : "";
  const query = $("fullSearchQuery") ? $("fullSearchQuery").value.trim() : "";
  if (!entity) {
    renderFullSearchStatus("先打开一个 run 或一次会话，才能全文搜索", "warn");
    return;
  }
  if (!evidence) {
    renderFullSearchStatus("没有可搜的证据（这一份详情里没有列出产物）", "warn");
    return;
  }
  if (!query) {
    renderFullSearchStatus("关键词是空的：空查询会被服务端按 400 拒掉", "warn");
    return;
  }
  cancelSearchPoll();
  const started = await postJson("/api/search", {
    ...entity,
    evidence,
    mode: $("fullSearchMode") ? $("fullSearchMode").value : "text",
    query,
  }).catch((error) => {
    renderFullSearchStatus(`没起得来：${error.message}`, "fail");
    return null;
  });
  if (!started || !started.task) return;
  const mode = $("fullSearchMode") ? $("fullSearchMode").value : "text";
  app.fullSearch = { taskId: started.task.task_id, evidence, query, mode };
  if ($("fullSearchCancel")) $("fullSearchCancel").hidden = false;
  renderFullSearchStatus(searchRunningText(started), "running");
  renderFullSearchResults([]);
  pollFullSearch();
}

/* 全文搜索的接线：按钮、下拉框、回车提交。都是显式动作，没有一个是隐式的。 */
function wireFullSearch() {
  const startBtn = $("fullSearchStart");
  const cancelBtn = $("fullSearchCancel");
  const query = $("fullSearchQuery");
  const evidence = $("fullSearchEvidence");
  if (startBtn) startBtn.addEventListener("click", () => void startFullSearch());
  if (cancelBtn) cancelBtn.addEventListener("click", () => void cancelFullSearch());
  // 换一份证据不重置下拉框里的选项（产物清单由“打开了什么”决定），所以没有 change 监听。
  if (query) {
    query.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        void startFullSearch();
      }
    });
  }
}

/* 下拉框默认选哪一个：优先选**正在走窗口的那一份**（用户多半就是想搜它），
 * 否则选字节数最大的那一份。默认值也要说得出道理 —— 不能随手选列表里的第一个
 * （那份常常是几十字节的 manifest，搜完说"全文无命中"，看起来像搜了正文）。 */
function preferredSearchEvidence(rows) {
  for (const name of Object.values(searchEvidenceMap())) {
    const paneId = searchPaneFor(name);
    const source = paneId ? app.log.sources[paneId] : null;
    if (source && source.window) return name;
  }
  return largestSearchEvidence(rows);
}

/* 打开了哪一份证据就列哪一些：下拉框跟着详情走，不自己编一个清单。 */
function renderFullSearchChoices() {
  const select = $("fullSearchEvidence");
  if (!select) return;
  const keep = select.value;
  select.textContent = "";
  const rows = searchEvidenceChoices(app.artifactsMeta);
  for (const row of rows) {
    const option = document.createElement("option");
    option.value = row.name;
    option.textContent = `${row.name}（${byteText(Number(row.bytes) || 0)}）`;
    select.appendChild(option);
  }
  if (keep && rows.some((row) => row.name === keep)) {
    select.value = keep;
    return;
  }
  const preferred = preferredSearchEvidence(app.artifactsMeta);
  if (preferred && rows.some((row) => row.name === preferred)) select.value = preferred;
}

function renderFullSearchResults(hits) {
  const host = $("fullSearchResults");
  if (!host) return;
  host.textContent = "";
  clearReadingHeight(); // 结果列表变更时，上一轮“阅读位”的高度不该继续压住四窗预算。
  if (!hits.length) {
    host.hidden = true;
    syncPaneViewportBudget();   // 结果列表收起 ⇒ 四窗能把那几行要回来
    return;
  }
  host.hidden = false;
  hits.forEach((hit, index) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "ws-fs-hit";
    row.dataset.offset = String(hit.offset);
    row.textContent = searchHitLabel(hit, index + 1);
    row.title = "跳到这一条所在的位置";
    row.addEventListener("click", () => void gotoSearchHit(hit, row));
    host.appendChild(row);
  });
  // 结果列表一出来就占掉一块屏幕：立刻重算四窗的正文上限（不等 ResizeObserver 那一拍），
  // 这样“搜索栏 + 五条结果 + 四窗”立刻就是一屏装得下的状态。
  syncPaneViewportBudget();
}

/* 点过的那一条留个记号：不然"我点的是哪一条、跳到哪儿了"只能靠记（ 的观感问题）。
 * 结果列表每次重绘都是新建的按钮，所以重跑搜索时记号自然就没了。 */
function markChosenHit(row) {
  const host = $("fullSearchResults");
  if (!host) return;
  for (const other of host.querySelectorAll(".ws-fs-hit[data-current='1']")) {
    delete other.dataset.current;
  }
  if (row && row.isConnected) row.dataset.current = "1";
}

async function pollFullSearch() {
  const current = app.fullSearch;
  if (!current || !current.taskId) return;
  const payload = await getJson(
    `/api/search/${encodeURIComponent(current.taskId)}`
  ).catch(() => null);
  if (!payload) {
    // ⚠️ 一次读不到 ≠ 搜索结束了。以前这里直接 return，轮询就**静默断掉**，
    // 页面永久停在"扫描中"—— 看起来像服务端卡住了。现在重试几次再说。
    app.fullSearchMisses = (app.fullSearchMisses || 0) + 1;
    if (app.fullSearchMisses <= 5) {
      renderFullSearchStatus(
        `读进度失败，正在重试（第 ${app.fullSearchMisses} 次）`, "warn"
      );
      app.fullSearchTimer = setTimeout(pollFullSearch, SEARCH_POLL_MS * 3);
      return;
    }
    renderFullSearchStatus(
      "读进度连续失败：任务可能还在服务端跑；重新点「开始」会新起一个", "fail"
    );
    return;
  }
  app.fullSearchMisses = 0;
  if (!payload.finished) {
    renderFullSearchStatus(searchRunningText(payload), "running");
    app.fullSearchTimer = setTimeout(pollFullSearch, SEARCH_POLL_MS);
    return;
  }
  app.fullSearchTimer = null;
  if ($("fullSearchCancel")) $("fullSearchCancel").hidden = true;
  renderFullSearchResults(payload.hits || []);
  renderFullSearchStatus(searchDoneText(payload), payload.state === "done" ? "" : "warn");
}

async function cancelFullSearch() {
  const current = app.fullSearch;
  if (!current || !current.taskId) return;
  await postJson(`/api/search/${encodeURIComponent(current.taskId)}/cancel`, {}).catch(
    () => null
  );
  renderFullSearchStatus("已请求取消", "warn");
}

/* 把整份搜索用的那个词**搬进这一窗自己的搜索框**：那一窗里所有命中的内容就都用既有的
 * `<mark>` 高亮起来了（用户要的正是“对应窗口所属的搜索内容也同时高亮”）。
 *
 * ⚠️ 只搬**文本模式**的查询：字节/十六进制那两种写法（`\n`、`6e 64`）不是这一窗的口径，
 *    搬过去只会得到一堆假命中。
 */
function focusPaneOnQuery(paneId, text, mode) {
  const source = app.log.sources[paneId];
  if (!source || String(mode || "text") !== "text") return false;
  const needle = String(text || "");
  if (!needle) return false;
  if (source.searchInput) source.searchInput.value = needle;
  source.view.query = needle;
  refreshView(app.log, paneId);
  return true;
}

/* 点一条命中：把**那个字节附近**的一段按 Range 取回来装进对应的窗口。 */
async function gotoSearchHit(hit, row) {
  const current = app.fullSearch || {};
  const evidence = current.evidence || "";
  const paneId = searchPaneFor(evidence);
  const source = paneId ? app.log.sources[paneId] : null;
  markChosenHit(row);
  if (!source) {
    renderFullSearchStatus(
      `${evidence} 没有对应的窗口`, "warn"
    );
    return;
  }
  const win = source.window;
  if (!win) {
    // 这一路是小文件（整份都在页面上）：不重新请求，直接在有内容的行里找那一段。
    const index = findLineIndex(source.lines, hit.text);
    focusPaneOnQuery(paneId, current.query, current.mode);
    if (index >= 0) scrollLineIntoView(paneId, index);
    alignReadingPosition();
    // ⚠️ 跳成功了就**不写状态行**（用户要求：不要再挂一段“已取回字节…本窗口第 N 行…”的说明）。
    //    定位不到才说一句短的 —— 静默会让人以为“点了没反应”。
    if (index < 0) {
      renderFullSearchStatus(
        `这一条没能在窗口里定位到行`, "warn"
      );
    }
    return;
  }
  const range = searchJumpRange(hit, win.totalBytes, SEARCH_JUMP_PAD_BYTES);
  if (!range) return;
  setSourceLines(app.log, paneId, []);
  // ⚠️ `replace: true`：这一跳把模型换成了命中附近的那一段，已装载区间也跟着重置 ——
  // 不然状态行会把之前那一大段（已经不在页面上了）继续算作“已装载”。
  const loaded = await loadWindowRange(paneId, range, { replace: true });
  if (!loaded) return;
  const source2 = app.log.sources[paneId];
  // 先把这个词搬进这一窗（于是内容高亮），再找行、居中、摆页面。
  focusPaneOnQuery(paneId, current.query, current.mode);
  const index = findLineIndex(source2.lines, hit.text);
  if (index >= 0) scrollLineIntoView(paneId, index);
  // 页面也摆到能读的位置：目标那一行的居中靠上面那一步，这里管的是“整块东西在屏幕上在哪儿”。
  alignReadingPosition();
  if (index < 0) {
    renderFullSearchStatus(
      `这一条没能在窗口里定位到行`, "warn"
    );
  }
}

/* ------------------------------------------- 实时大流量的 UI 降载（L5） */
/* 采集路径与证据落盘**不经过这里**：SSE 到达的事件先进队列，DOM 一批一批地写。
 * 四条规矩（宿主回归直接驱动下面的纯函数）：
 *
 * 1. **只合并、不丢内容**：攒到一次 flush 时把队列**全部**交给模型
 *    （`renderPending` 一次就写完 `rendered..lines.length`，所以"多攒一会儿"不改变内容）。
 *    被攒下来的是**时间**，不是内容。
 * 2. **慢就加大间隔、快就收回来**：一批超过预算就把 flush 间隔翻倍（有上限），
 *    连续变快再折半收回 —— 这就是"超过预算时扩大 UI 延迟，不阻塞采集"。
 * 3. **降载必须记账**：合并了多少条、慢批几次、当前间隔、队列还压着多少、
 *    有没有因为队列满而丢过 —— 全部写进状态行。"没说明的丢弃"是不允许的。
 * 4. **广播也要记账**：服务端在订阅者跟不上时会挤掉事件（`EventBus.stats()`），
 *    页面拿服务端的水位与自己的游标比，落后多少、有没有根本没见过的东西都说出来。
 *
 * 阈值（`SHED_*`）定在文件开头 —— `createShedState()` 在 `app` 字面量里就被调用了。
 */

/* 只有"高频行"事件才排队。控制类事件（开始/结束/步骤/断言/崩溃）必须**立刻**生效：
 * 批处理是为了让界面跟得上，不是为了让"点一下"等半秒。 */
const SHED_QUEUED_KINDS = new Set(["tool", "raw", "decoded", "app_events"]);

function createShedState() {
  return {
    delayMs: SHED_FLUSH_MIN_MS,
    batches: 0,
    events: 0,
    coalesced: 0,
    slowBatches: 0,
    lastMs: 0,
    maxMs: 0,
    queued: 0,
    peakQueued: 0,
    overflow: 0,
    deferred: 0,
    metricsSkipped: 0,
    behind: 0,
    broadcastDropped: 0,
    // 最近几批的**实测耗时**（需求 §6 的预算是"单批 p95 < 16 ms"，那就算出 p95 来）。
    samples: [],
  };
}

/* 一批写完之后按**实测耗时**调整下一次的间隔（纯函数）。 */
function shedAfterFlush(state, count, elapsedMs) {
  const next = Object.assign({}, state);
  const rows = Math.max(0, Number(count) || 0);
  const spent = Math.max(0, Number(elapsedMs) || 0);
  next.batches = state.batches + 1;
  next.events = state.events + rows;
  next.coalesced = state.coalesced + Math.max(0, rows - 1);
  next.lastMs = spent;
  next.maxMs = Math.max(state.maxMs, spent);
  next.queued = Math.max(0, state.queued - rows);
  next.samples = state.samples.concat([spent]).slice(-SHED_SAMPLE_CAP);
  if (spent > SHED_BATCH_BUDGET_MS) {
    // 超预算：下一批攒得更久一点（界面上跟着变慢，但采集与落盘一点不受影响）。
    next.slowBatches = state.slowBatches + 1;
    next.deferred = state.deferred + 1;
    next.delayMs = Math.min(
      SHED_FLUSH_MAX_MS, Math.max(SHED_FLUSH_MIN_MS * 2, state.delayMs * 2)
    );
  } else if (spent * 2 < SHED_BATCH_BUDGET_MS && state.delayMs > SHED_FLUSH_MIN_MS) {
    next.delayMs = Math.max(SHED_FLUSH_MIN_MS, Math.round(state.delayMs / 2));
  }
  return next;
}

/* 入队。到上限就丢**最旧的**并记账 —— 丢的是"还没写进 DOM 的事件"，
 * 证据文件在服务端，仍然完整；但"丢过"这件事必须在界面上说得出。 */
function shedPush(queue, state, items) {
  const next = (queue || []).concat(items || []);
  const shed = Object.assign({}, state);
  if (next.length > SHED_QUEUE_MAX) {
    const overflow = next.length - SHED_QUEUE_MAX;
    next.splice(0, overflow);
    shed.overflow = state.overflow + overflow;
  }
  shed.queued = next.length;
  shed.peakQueued = Math.max(state.peakQueued, shed.queued);
  return { queue: next, shed };
}

function shedDrain(queue, state) {
  const items = queue || [];
  const shed = Object.assign({}, state);
  shed.queued = 0;
  return { items, queue: [], shed };
}

/* 最近几批耗时的分位数（默认 p95）。样本不够就返回 0 —— 不拿两三个数冒充分位数。 */
function shedPercentile(state, quantile) {
  const samples = (state && state.samples) || [];
  if (samples.length < 8) return 0;
  const q = Math.min(0.999, Math.max(0, Number(quantile) || 0.95));
  const sorted = samples.slice().sort((left, right) => left - right);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1));
  return sorted[index];
}

/* 状态行上那句话。**降载没有发生时不写**（空话会让人以为一直在降载）；
 * 一旦有丢弃或落后，就必须写出来（含"证据文件仍然完整"这句事实）。
 * ⚠️ 用户口径 2026-10-12：屏幕上只留**结果与“数据不完整”的警告**；
 *    批处理次数/耗时/队列水位这些实现记账进悬停（它们只影响显示）。 */
function shedText(state, running) {
  const shed = state || {};
  const parts = [];
  if (shed.overflow) parts.push(`队列满丢弃 ${shed.overflow} 条（证据文件仍然完整）`);
  if (shed.behind) parts.push(`落后服务端 ${shed.behind} 条（正在补）`);
  if (shed.broadcastDropped) {
    parts.push(`服务端广播挤掉过 ${shed.broadcastDropped} 条（这一页看不到它们；证据文件仍然完整）`);
  }
  if (!parts.length) return "";
  return parts.join(" · ");
}

/* --------------------------------------------------------------------- SSE */
/* 事件入口（L5）。高频行事件**先排队**、控制事件**立刻**执行：
 * 批处理是为了让界面跟得上，不是为了让"点一下"等半秒。
 * ⚠️ 续行事件（一条长行被拆成几次读）仍然走立即路径：它改的是**最后一行**、不是追加，
 * 跟批处理不是一件事，而且频率低得多。 */
function handleEvent(event) {
  if (event.seq) {
    if (event.seq <= app.lastSeq) return;
    app.lastSeq = event.seq;
  }
  if (SHED_QUEUED_KINDS.has(event.kind) && !event.continuation) {
    const pushed = shedPush(app.queue, app.shed, [event]);
    app.queue = pushed.queue;
    app.shed = pushed.shed;
    updateShedIndicator();
    scheduleFlush();
    return;
  }
  // 顺序要紧：先把攒着的高频事件写完再处理这一条 ——
  // 否则"run 结束了"会插到它前面那批还没落 DOM 的行之前。
  flushRealtime();
  applyEvent(event, null);
}

function scheduleFlush() {
  if (app.flushTimer) return;   // 已经排过了：这一批会一起写（**合并**就是在这里发生的）
  app.flushTimer = setTimeout(() => {
    app.flushTimer = null;
    flushRealtime();
  }, Math.max(SHED_FLUSH_MIN_MS, Math.round(app.shed.delayMs)));
}

/* 把队列里的事件一次性交给模型，再**一次**落 DOM。返回写了多少条。 */
function flushRealtime() {
  if (app.flushTimer) {
    clearTimeout(app.flushTimer);
    app.flushTimer = null;
  }
  const drained = shedDrain(app.queue, app.shed);
  app.queue = drained.queue;
  let shed = drained.shed;
  const items = drained.items;
  if (!items.length) {
    app.shed = shed;
    updateShedIndicator();
    return 0;
  }
  const startedAt = nowMs();
  /* ⚠️ ：一批里**可能混着两条路的行**（实时观察在收 + run 刚开始）。
   * 所以落 DOM 的账要**按份记**，不能拿一个全局的"此刻是谁"去 settle。 */
  const touchedByStore = new Map();
  const touchedFor = (store) => {
    let counts = touchedByStore.get(store);
    if (!counts) {
      counts = new Map();
      touchedByStore.set(store, counts);
    }
    return counts;
  };
  const metrics = new Map();
  let metricsSkipped = 0;
  for (const event of items) {
    if (event.kind === "app_events" && event.metrics) {
      const mode = String(event.origin || event.lane || "") === "live" ? "live" : "auto";
      if (metrics.has(mode)) metricsSkipped += 1;
      metrics.set(mode, event.metrics);
    }
    applyEvent(event, touchedFor);
  }
  for (const [store, counts] of touchedByStore) {
    for (const [id, added] of counts) settle(store, id, added);
  }
  if (touchedByStore.size) updateButtons();
  // 指标按**批**节流：一批最多画一次（数据本身仍然完整地留在证据文件里）。
  for (const [mode, payload] of metrics) applyMetrics(payload, mode);
  shed = shedAfterFlush(shed, items.length, nowMs() - startedAt);
  shed.metricsSkipped = shed.metricsSkipped + metricsSkipped;
  app.shed = shed;
  updateShedIndicator();
  return items.length;
}

function nowMs() {
  return typeof performance !== "undefined" && performance.now
    ? performance.now()
    : Date.now();
}

/* 降载的记账**只在实时观察那一屏**有意义（那是唯一“一边收一边显示”的场合）。
 * 用户报的：读证据（离线诊断 / 自动验证）时旁边一直挂着一串“批 718 · 合并 5841 条 · 间隔 16 ms…”，
 * 那不是他现在要看的东西。
 * ⚠️ 唯一的例外必须留着：**真丢过东西**（队列满丢弃 / 服务端广播挤掉）时，哪一屏都说 ——
 * 那是“你看到的不是全部”的事实，不能因为切了屏就不提。 */
function shedVisible(state, mode) {
  const shed = state || {};
  if (String(mode || "") === "live") return true;
  return Boolean(shed.overflow || shed.broadcastDropped);
}

/* 降载的记账写在状态行旁边：**没有降载时不写空话**，一旦有合并/丢弃/落后就必须写。 */
function updateShedIndicator() {
  const row = $("shedStat");
  if (!row) return;
  const text = shedVisible(app.shed, attachedView().mode) ? shedText(app.shed, app.running) : "";
  row.textContent = text;
  row.hidden = !text;
  const state = !text
    ? ""
    : (app.shed.overflow || app.shed.broadcastDropped)
      ? "warn"
      : app.shed.slowBatches
        ? "busy"
        : "ok";
  if (state) row.dataset.state = state;
  else delete row.dataset.state;
  row.title = [
    "实时降载只影响这里的显示；采集与证据落盘不经过它。",
    `批 ${app.shed.batches} 次 · 合并 ${app.shed.coalesced} 条 · 慢批 ${app.shed.slowBatches} 次`,
    `间隔 ${Math.round(app.shed.delayMs)} ms（预算 ${SHED_BATCH_BUDGET_MS} ms/批）· 最近一批 ${Math.round(app.shed.lastMs)} ms · 峰值 ${Math.round(app.shed.maxMs)} ms`,
    `队列上限 ${SHED_QUEUE_MAX} 条：满时丢**最旧**的（本轮已丢 ${app.shed.overflow}）`,
    `指标图按批节流：本批跳过 ${app.shed.metricsSkipped} 次（数据仍在证据文件里）`,
  ].join("\n");
}

/* 一条事件真正生效的地方。`touched` 非空 = 批量路径：**只进模型**，
 * 由 `flushRealtime()` 结束时统一落 DOM（大流量下卡住的正是"一行一次 DOM 写"）。
 * `touched` 是一个**按份取账本**的函数（）：一批里可能混着两条路的行。 */
function applyEvent(event, touched) {
  const defer = Boolean(touched);
  const store = laneStore(event);
  if (store === null) {
    /* 上一次会话的积压：不进任何窗口（`laneStore` 已经说过一次为什么）。
     * 控制类事件（状态/消息）也走同一道闸 —— **状态不许来自历史**。 */
    if (!defer) updateButtons();
    return;
  }
  // 契约闸：未分类的事件**不进任何窗口**，也不允许被任何一个 `case` 兜底捡走。
  const routed = eventToSource(event);
  if (routed.source === null) {
    noteUnclassified(event, routed.reason);
    if (!defer) updateButtons();
    return;
  }
  const add = (id, added) => {
    if (!touched || !added) return;
    const counts = touched(store);
    counts.set(id, (counts.get(id) || 0) + added);
  };
  switch (event.kind) {
    case "run_started":
      app.running = true;
      app.runId = event.run_id;
      /* 这一次 run 的行归**自动验证**那一份（）：
       * 用户此刻可能正在看离线包或实时观察 —— 那些窗口一动不动。 */
      app.receivingMode = "auto";
      app.views.auto.viewing = event.run_id;
      app.views.auto.review = null;
      resetSources(receivingStore());
      renderAssertions([]);
      renderTestResults(undefined, undefined);
      renderMetrics(null, "auto");
      renderPanic(null);
      renderSteps([]);
      showMessage(event.message, "");
      renderRunMeta({ run_id: event.run_id, result: "running", observe_seconds: $("observeSeconds").value });
      break;
    case "step":
      updateStepFromEvent(event);
      break;
    case "test_step":
      // 被测阶段（）：一条事件一次更新，卡片按 id 折叠。
      // ⚠️ T7：阶段卡只属于产生它的那一路 ⇒ 按**事件自己的来源**落进那一屏（run ⇒ auto、
      //    会话 ⇒ live）。以前是按“当前在看哪一屏”判的，于是人只要看着实时屏，
      //    一次 run 的阶段事件就被**丢掉**了（切回去是空的）。
      {
        const view = viewFor(stageEventMode(event));
        const firstOfThisPane = !stageBag(view).seen;
        applyTestStep(event, view);
        //  / 需求 §8.4：普通观察没有阶段时这一窗是竖排标签，**首条有效
        // test_step 到达就把它展开**（受控测试正好是“平时没有阶段、跑起来才有”的用法：
        // 受控测试的 PROGRESS 走的就是这条 test_step 事件）。之后仍可手动收起/展开。
        if (firstOfThisPane) expandPane(storeFor(view), "stage");
        if (app.mode === stageEventMode(event)) renderStepCards();
      }
      break;
    case "test_step_refs":
      // T5 补写（decode 之后）：**只补坐标**，状态/耗时/判定一概不动。
      applyTestRefs(event, viewFor(stageEventMode(event)));
      if (app.mode === stageEventMode(event)) renderStepCards();
      break;
    case "live_test":
      // 受控批次的生命周期（`event=started|execution|finished`）：页面只用它回答
      // “屏幕上是哪一批”。阶段卡本身仍只由 `test_step` 喂 —— 不另建通道。
      // ⚠️ 一定要在**批次开始**那一刻清：等到收口再清就晚了 —— 第二批的 `test_step`
      //    会被上一批的终态挡掉（同一组 case_id），屏幕上还停在上一批的结果。
      if (String(event.event) === "started" && useLiveStageBatch(event.batch_id)
          && app.mode === "live") {
        renderStepCards();
      }
      break;
    case "tool":
      // ⚠️ 会话的「工具输出」窗口是 `io_events.jsonl` 的**派生视图**（跳过分块、离线还多一行
      //    标题）⇒ 它没有与文件对齐的行号，服务端也不给它 `line_no`（）。
      //    ⇒ 它按**行号**定位永远不成立（契约：拒绝，不拿一个差不多的行号跳过去）。
      //    但这一行的**身份**是服务端那条 io 事件 ⇒ 带上它的事件号（）：
      //    阶段卡上的 `{ source: "tool", event_seq: N }` 就是按它落到这一行的。
      add("tool", feed(store, "tool", event.line, event.ts, { defer, event_seq: event.event_seq }));
      break;
    case "raw":
      if (event.continuation) {
        continueLine(store, "raw", event.line, event.ts, { lineNo: event.line_no });
      } else {
        add("raw", feed(store, "raw", event.line, event.ts, { defer, lineNo: event.line_no }));
      }
      // 终端视图：按服务端给的**原始字节**重放（行视图与证据都没有变，只多一个显示层）。
      termFeedEvent(event);
      break;
    case "decoded":
      add("decoded", feed(store, "decoded", event.line, event.ts, { defer, lineNo: event.line_no }));
      break;
    case "app_events":
      // 事件只追加未见过的行；视图是否跟着走由暂停/离底状态决定。
      // 指标序列与事件同源（后台一次解析），所以“图上的数”与“事件里的数”不会是两次解析；
      // 批量路径下由 `flushRealtime()` 一批画一次（节流）。
      add("events", appendEvents(store, event.events, event.ts, { defer, lineNo: event.line_no }));
      if (event.metrics && !defer) {
        const mode = String(event.origin || event.lane || "") === "live" ? "live" : "auto";
        applyMetrics(event.metrics, mode);
      }
      break;
    case "replay":
      /* 服务端的积压自述（）：**跳过了才说**，没跳过就安静 ——
       * 每次开页面都报一句"重放了 N 条"只会变成噪音。 */
      if (event.skipped) {
        // ⚠️ 截断这件事**要记下来**（）：从 SSE 接上来的窗从此不能假定"窗口第一行
        //    就是文件第 1 行"。**带行号的窗不受影响**（基准由第一行的 `line_no` 落），
        //    没带行号的窗（老服务 / run 那一路）才按 `paneFrameIssue` 逐窗降级。
        app.replayTruncated = true;
        showMessage(event.message, "warn", "observe");
      }
      break;
    case "panic":
      renderPanic(event.summary, (event.finding || {}).backtrace);
      break;
    case "assert": {
      // 与回放**同一条路径**：先把行收进模型，再整屏重画（域小结跟着对）。
      // 以前是直接往 DOM 追加一张卡 —— 那样“两个结论分开念”就只在回放里成立。
      app.assertionRows.push(event.assertion);
      renderAssertions(app.assertionRows);
      break;
    }
    case "info":
    case "error":
      showMessage(event.message, event.kind === "error" ? "fail" : "");
      break;
    case "observe_state":
      // 实时观察的状态迁移：连接/断开/重连，以及“被其他模式占用/串口消失”。
      // 状态与计数器都由服务端给，页面不自己推算（“大约连上了”不是一种状态）。
      if (event.observe) renderObserve(event.observe);
      break;
    case "request_rejected":
      // Another tab (or an agent) tried to take the board and was refused. Say so
      // here too, otherwise their rejected click is invisible in this tab.
      showMessage(event.message, "warn");
      break;
    case "run_finished":
      app.running = false;
      showMessage(event.message, event.result === "PASS" ? "pass" : "fail");
      noteRunInAddress(event.run_id);
      refreshState();
      refreshRuns();
      // ⚠️ 自动装载：**不要**切走用户的屏（他可能正在看离线包或实时观察）。
      loadRunOrReport(event.run_id, { focus: false });
      break;
    default:
      break;
  }
  // 落窗的账：正文类事件记下它进了哪一窗（同一事件号进两窗 = 串窗，能被当场抓出来）。
  if (routed.source !== "control") noteRenderedEvent(event, routed.source);
  // 批量路径下由 `flushRealtime()` 一批调一次（按钮状态不需要每行都重算）。
  if (!defer) updateButtons();
}

function updateStepFromEvent(event) {
  const existing = app.steps.find((step) => step.name === event.step);
  if (existing) {
    existing.status = event.status;
    if (event.detail !== undefined) existing.detail = event.detail;
    if (event.duration_ms !== undefined) existing.duration_ms = event.duration_ms;
  } else {
    app.steps.push({ name: event.step, status: event.status, detail: event.detail || "", duration_ms: event.duration_ms });
  }
  renderSteps(app.steps);
}

/* 把事件游标退回“什么都不知道”并重连：
 * 服务重启后新总线的序号是小的，而页面手里的是旧的**大**号 —— 不复位就永远接不上。
 * 从 0 重连就是让服务端给一段**有界积压**（`replay_window`），而不是把整包重发。 */
function rebaseEventCursor() {
  app.lastSeq = 0;
  connectStream();
  return true;
}

function connectStream() {
  if (app.source) app.source.close();
  const source = new EventSource(`/api/stream?since=${app.lastSeq}`);
  app.source = source;
  source.onmessage = (message) => {
    try {
      handleEvent(JSON.parse(message.data));
    } catch (error) {
      /* ignore malformed frame */
    }
  };
  source.onerror = () => {
    source.close();
    if (app.source === source) app.source = null;
    setTimeout(connectStream, 2000);
  };
}

/* ------------------------------------------------- 页面是不是旧的（） */
/* 前端改动是**每次请求重读**的（刷新页面就能拿到新的），服务重启会丢掉进程内的状态；
 * 但**已经打开的页面**两件事都不知道 —— 于是会出现"你改了、我刷新了才看到"，
 * 甚至"你改了但看起来没生效"（ 的两次现身）。
 *
 * 服务在 `/api/state` 里带一个构建戳，页面把自己加载时那个戳记下来，
 * 2 秒轮询发现戳变了就在工作台顶上提示一句。**不自动刷新**：
 * 自动刷新会把正在读的阅读位置、暂停状态与未读数都弄丢。 */

/* 戳变了没有（纯函数，宿主回归直接核对）。返回原因或 null。 */
function buildMoved(previous, next) {
  if (!previous || !next) return null;      // 还没拿到基线：不动
  if (previous.pid !== next.pid || previous.started_utc !== next.started_utc) return "restart";
  if (previous.static !== next.static) return "static";
  return null;
}

/* 那条提示的文案（纯函数）："服务重启了"与"前端改了"是两件事，不能合成一句含糊的话。 */
function stalePageText(previous, next, reason) {
  if (reason === "restart") {
    return "服务重启过：这个页面还连着重启前的那一份，四个窗口可能不跟着走"
      + " —— 点「刷新页面」重新拉一次（证据在磁盘上，刷新不会丢）";
  }
  if (reason === "static") {
    return "界面文件已经更新：这个页面还在跑旧的一份 —— 点「刷新页面」用新的（证据不会丢）";
  }
  return "";
}

function showStaleBanner(previous, next, reason) {
  const host = $("staleBanner");
  if (!host) return;
  host.textContent = "";
  const why = document.createElement("span");
  why.className = "stale-why";
  why.textContent = "页面是旧的";
  // 屏幕上那句话已经说清是哪一种（服务重启 / 界面文件更新）⇒ 不再挂内部读数：
  // “前端戳 6 → 6”这种只报文件数的字串看上去像变了，实际什么都没说（2026-10-12 实测）。
  const text = document.createElement("span");
  text.textContent = stalePageText(previous, next, reason);
  const reload = document.createElement("button");
  reload.type = "button";
  reload.className = "primary";
  reload.textContent = "刷新页面";
  reload.addEventListener("click", () => window.location.reload());
  const later = document.createElement("button");
  later.type = "button";
  later.textContent = "先不管";
  later.title = "只是不显示这条提示：刷新后如果还是旧的，它会再出现";
  later.addEventListener("click", () => { host.hidden = true; });
  host.append(why, text, reload, later);
  host.hidden = false;
}

/* 每次拿到 `/api/state` 都过一下它：第一次只记基线（不动）。
 * 返回换戳的原因（`"restart"` / `"static"` / `null`）—— 调用方还要用它决定
 * **要不要复位事件游标**：服务重启后总线序号从头开始，不复位就永远接不上。 */
function noteBuild(build) {
  if (!build) return null;
  const previous = app.build;
  if (!previous) {
    app.build = build;
    return null;
  }
  const reason = buildMoved(previous, build);
  if (!reason) return null;
  showStaleBanner(previous, build, reason);
  app.build = build;      // 只提示一次：再轮询到同一个新戳不该反复弹
  return reason;
}

/* -------------------------------------------------------------------- state */
/* 采集窗口的**进度与最近证据**（）。
 *
 * 为什么必须有：窗口上限现在是 240 s，而受控序列在两次重启之间会有很长时间只是安静地等；
 * 没有这行时，操作者无法把"还在跑"与"卡死了"分开。这里只说三件**有据可查**的事：
 * 实际生效值、当前阶段、最近证据。
 *
 * ⚠️ "最近证据"只认设备发来的**原始字节**（服务端 `last_evidence_utc`）；
 * 主机自己写的 info 事件不算 —— 那会把"设备已经不响了"报成一切正常。 */
function renderRunProgress(run, steps) {
  const host = $("runProgress");
  if (!host) return;
  if (!run || !run.observe_started_utc) {
    host.hidden = true;
    host.textContent = "";
    return;
  }
  const total = Number(run.observe_seconds) || 0;
  const startedAt = Date.parse(run.observe_started_utc);
  const now = Date.now();
  const elapsed = Number.isNaN(startedAt) ? 0 : Math.max(0, (now - startedAt) / 1000);
  const runningStep = (steps || []).find((step) => step.status === "running");
  const runningStage = Array.from(stageBag().rows.values()).find((row) => row.status === "running");
  const lastAt = Date.parse(run.last_evidence_utc || "");
  const since = Number.isNaN(lastAt) ? null : Math.max(0, (now - lastAt) / 1000);
  host.textContent = [
    `进行中 · ${elapsed.toFixed(0)} / ${total ? total.toFixed(0) : "?"} s`,
    `当前：${runningStage ? runningStage.label : (runningStep ? runningStep.name : "（准备中）")}`,
    since === null ? "最近证据：还没收到字节" : `最近证据：${since.toFixed(0)} s 前`,
  ].join("\n");
  host.hidden = false;
}

async function refreshState() {
  try {
    const state = await getJson("/api/state");
    const buildMovedReason = noteBuild(state.build);
    /* ⚠️ 用户实测（2026-10-17）：服务重启后总线序号**从头开始**，而页面的 `app.lastSeq`
     * 还停着重启前那个大数 ⇒ `handleEvent()` 里 `event.seq <= app.lastSeq` 把之后**每一条**
     * 事件都静默丢掉：六个阶段永远“等待”、run 的结果与结束提示都到不了页面
     * （刷新一下又全对，所以特别难认）。两条判据任一条成立就当换了新总线：
     * ① 构建戳说进程换了；② 广播水位比我的游标还低（序号回退）。 */
    const watermarkSeq = Number((state.broadcast || {}).seq) || 0;
    if (buildMovedReason === "restart" || app.lastSeq > watermarkSeq) rebaseEventCursor();
    // 设备登记要在手上：`updateButtons()` 里判断"实时观察会不会挡住这次 run"要用它
    // （每个目标的 device_key → 那块板的口）。
    app.devices = Array.isArray(state.devices) ? state.devices : [];
    //  / D5：嗅探结果只是**读回来画**（这一段每隔 2 s 跑一遍，而嗅探必须是
    // 用户点的 —— 这里绝不许自己去发 `/api/sniff`）。
    app.sniff = state.sniff || null;
    renderSniffCards();
    // 需求 §12.3：进度只读服务端那份快照（本函数每 2 s 跑一遍，**不发**任何动作）。
    renderSniffProgress();
    // 采集窗口的生效区间（）：输入框的 min/max 跟着 core 走。
    app.observeLimits = state.observe_limits || {};
    renderRunCatalog(state.web_catalog, Boolean(app.webCatalog));
    app.platformAssemblies = (state.platform && state.platform.assemblies) || [];
    renderObserve(state.observe);
    // ：这一路不解码时把解码窗默认收成标签。
    // ⚠️ 放在**这里**（每次状态刷新必经，且在 catalog 更新之后）而不是 `renderDecodeWhy()` 里：
    //    那个函数只画“解码窗底那一行说明”，它在这条路径上不一定被调到 —— 我第一版放在那里，
    //    浏览器里一连串“为什么没收”就是这个原因。
    applyDecodeRailDefault(state.observe);
    app.running = Boolean(state.busy);
    /* ⚠️ 用户实测（2026-10-17）：跑完那一刻 `active_run` 变空，于是**结果行与六个阶段都不再刷新** ——
     * 屏幕上永远停在开跑前的占位（`结果 INCOMPLETE`、六步“等待”），PASS/FAIL 都没有提示。
     * 服务端 `last_run` 在跑完之后**仍然带着终态**（result/duration/assertion_summary + 六步），
     * 所以这里一并用它：只要用户没有在回看**另一次** run，就按服务端给的画。
     * 回看另一次 run 时不许被覆盖（那是他自己选的那一份）。 */
    const liveRun = state.active_run || state.last_run || null;
    const reviewingAnother = Boolean(app.viewing && liveRun && app.viewing !== liveRun.run_id);
    if (state.steps && state.steps.length) {
      app.steps = state.steps;
      if (state.busy || !reviewingAnother) renderSteps(app.steps);
    }
    if (liveRun && !reviewingAnother) {
      app.runId = liveRun.run_id;
      renderRunMeta(liveRun);
    }
    renderRunProgress(liveRun, state.steps || []);
    // 广播自述（L5）：服务端的序号水位与"因为订阅者跟不上而挤掉"的条数。
    // 落后多少条、有没有我**根本没见过**的事件，都要在状态行里说出来 ——
    // 页面自己看不见的东西，不能靠"看起来正常"蒙过去。
    const broadcast = state.broadcast || {};
    const shed = Object.assign({}, app.shed);
    const watermark = Number(broadcast.seq) || 0;
    shed.behind = watermark > app.lastSeq ? watermark - app.lastSeq : 0;
    shed.broadcastDropped = Number(broadcast.dropped) || 0;
    app.shed = shed;
    updateShedIndicator();
    /* 页脚（用户口径 2026-10-11 → 2026-10-12）：**正常态什么都不说** ——
     * 证据目录、服务在跑、锁归属这些都属“平台自己的现场”，诊断时才要看；
     * 只有**真的被挡住**时才在这里写一句直接原因（其余一个字段都不上屏）。 */
    const lock = state.server.workspace_lock;
    const blocked = lock ? `工作区被 ${lock.kind || "别的实例"} 占着` : "";
    $("footer").textContent = blocked;
    $("footer").hidden = !blocked;
    $("footer").title = "";
    updateButtons();
  } catch (error) {
    showMessage(`无法读取服务状态：${error.message}`, "fail");
  }
}

async function refreshRuns() {
  try {
    const payload = await getJson("/api/runs");
    app.runs = payload.runs || [];
    app.runsState = "ready";
    app.runsError = "";
  } catch (error) {
    /* 历史列表不是关键路径：**记下失败**并如实画出来，不抛、也不清掉上一次的行冒充成功。 */
    app.runsState = "error";
    app.runsError = error.message;
  }
  renderHistory();
}

/* “启动自动验证”现在能不能按、按不下去还差什么（）。
 *
 * 用户的现场原话是：点了“启动自动验证”**没有反应**。三个原因都真的存在过：
 *  1. 那个口正被实时观察拿着 —— 按钮本来是灰的，原因只在 tooltip 里；
 *  2. 所选固件会覆盖 Flash 而“逐次确认”没勾 —— 按钮**看着能按**，点完只往四个窗口
 *     下面的 `#runMessage` 写一行（那个位置在按钮上面一屏之外，屏幕上什么都没变）；
 *  3. 浏览器把 `confirm()` 吞掉了 —— 代码里 `if (!confirmed) return;`，一句都不说。
 *
 * 所以这里把“为什么”算成一份**可以显示**的东西：`blocked`（按钮灰掉）+ `hint`
 * （就在按钮旁边的一行话）+ `reason`（tooltip）。纯函数：宿主模型回归直接喂它组合。
 */
function startReadiness(facts, options) {
  const spec = facts || {};
  const state = options || {};
  const missing = !spec.target || !spec.firmware || !spec.suite;
  if (state.running) {
    return { blocked: true, hint: "", reason: "已有 run 正在执行：等它结束或先取消" };
  }
  if (state.mode !== "auto") {
    return { blocked: true, hint: "", reason: "只有“自动验证”模式能启动 run：实时观察不编译、不烧录" };
  }
  if (state.portBlocked) {
    return {
      blocked: true,
      hint: "",
      reason: `串口被实时观察会话占用（${state.ownPort || "?"}）：先在实时观察里断开再启动`,
    };
  }
  if (missing) {
    /* 用户口径（2026-10-13）：身份未知、而这台设备上**可能烧录两份以上**固件时，
     * “烧哪一份”只能由人定（实时观察那条路不受影响 —— 它不烧录、也不靠身份进门）。
     * 那一栏在 `renderRunFirmwares()` 里已经留空（不替使用者挑），这里的提示就是
     * **为什么按钮按不下去**的那句话。 */
    if (!spec.firmware) {
      return {
        blocked: true,
        hint: "请选择要烧录的固件",
        reason: "请选择要烧录的固件：身份未识别时平台不替你决定烧哪一份",
      };
    }
    return { blocked: true, hint: "", reason: "先选好目标 / 固件 / 套件" };
  }
  if (spec.firmware.requires_flash_confirmation && !state.confirmFlash) {
    return {
      blocked: false,
      hint: "还差一步：勾选“我确认覆盖设备 Flash”（就在上面那个选项里）",
      reason: "所选固件会覆盖设备 Flash：每次运行都要重新确认",
    };
  }
  return { blocked: false, hint: "", reason: "" };
}

/* 一次被拒绝的启动：说清原因，并把“要动的那一个控件”括起来（）。
 * 只往屏幕另一头的 `#runMessage` 写一行，等于没说 —— 现场看到的就是“点了没反应”。
 * ⚠️ **不许用 `scrollIntoView`**：PW-UX-001 是用户实测踩出来的（一跳转整页跟着乱跳），
 *    那是全家禁止的。这里要指的控件（逐次确认框 / 测试用例表）本来就紧挨着按钮。
 * ⚠️ D8-R（§11.1）：低频选项收在默认折叠的「更多设置」里 ⇒ 要指的控件可能正藏着，
 *    先把它摊开，否则  好不容易做出来的“原因就在旁边”会因为折叠而失效。
 */
function refuseStart(text, anchorId) {
  showMessage(text, "fail");
  const anchor = anchorId ? $(anchorId) : null;
  if (!anchor) return;
  if (anchor.closest("#moreOptions")) moreOptionsOpen(true);
  anchor.classList.add("attn");
  setTimeout(() => anchor.classList.remove("attn"), 2400);
}

/* 「更多设置」（§11.1：低频选项默认收起）——只在同一个外框里多一行，不另起面板。 */
function moreOptionsOpen(open) {
  const host = $("moreOptions");
  const button = $("moreToggle");
  if (!host) return;
  const want = Boolean(open);
  host.hidden = !want;
  app.moreOpen = want;
  if (button) {
    button.setAttribute("aria-expanded", want ? "true" : "false");
    button.textContent = want ? "收起设置" : "更多设置";
  }
}

function updateButtons() {
  const observe = app.observe || {};
  const holding = Boolean(observe.holding);
  const live = app.mode === "live";
  const start = $("startRun");
  if (start) {
    // 三种情况下不允许启动：已经有 run、不在自动验证模式、**这次 run 要用的那个串口**被实时观察拿着。
    // ⚠️ 第三条必须按端口比（`liveRunConflict`），不能"有实时观察就拦" —— 实时观察在 COM<n> 而这次
    //    要跑板 A（COM<n>）时，它本来就该能启动（服务端的闸门也是按端口的）。
    // 服务端会再拒一次（409）—— 页面上的禁用只是让原因显出来。
    const selection = selectedRunFacts();
    const blocked = liveRunConflict(observe, runTargetPort());
    // ⚠️ ：“逐次确认没勾”**不**变成 disabled：灰按钮说不出话（点下去也没有任何
    //    反应，那正是用户报的那件事）。它变成按钮旁边一行**看得见**的提示。
    const readiness = startReadiness(selection, {
      running: app.running,
      mode: app.mode,
      portBlocked: blocked,
      ownPort: observe.port,
      confirmFlash: Boolean($("flashConfirm") && $("flashConfirm").checked),
    });
    start.disabled = readiness.blocked;
    start.title = readiness.reason;
    const hint = $("startHint");
    if (hint) {
      hint.hidden = !readiness.hint;
      hint.textContent = readiness.hint;
      hint.dataset.kind = readiness.hint ? "warn" : "";
    }
  }
  if ($("cancelRun")) $("cancelRun").disabled = !app.running || !app.runId;
  const sniffButton = $("sniffBtn");
  if (sniffButton) {
    // ⚠️ 只挡“我正在嗅”这一件事。**不许**写成“有 run 在跑就不让嗅” —— 那正是  的
    // 同一个错：闸门要按**端口**判（服务端 `_port_conflict` 就是逐口判的），
    // 一个口被 run 占着，不代表别的口不能嗅；写成全局拦会把能做的事也挡掉。
    // 真被占着的那个口会在卡片上如实写成“端口被其他 owner 占用”。
    // ⚠️ D8：禁用与文字现在由三态机（`setSniffButton`）管，这里只改 title。
    sniffButton.disabled = app.sniffBusy;
    sniffButton.title = app.sniffBusy
      ? "正在检测…"
      : "检测本机已登记的通信口：只读身份，不烧录、不复位、不清设备数据";
  }
  const connect = $("observeConnect");
  if (connect) {
    //  / D6 + 用户口径（2026-10-10）：不再有“设备”下拉 —— 连哪一台设备**只**由
    // 本次嗅探选中的那张卡片决定（口就是服务端算出来的硬件通信口）。没选卡片就不让连，
    // 宁可把按钮禁掉也不猜一个口（“不确定的事不允许用猜去补”）。
    // ⚠️ 用户口径（2026-10-13）：**连接的条件只有“这个通信口存在、可用、没被占用”**。
    //    身份未知 / 零匹配 / 多匹配一律**不再拦连接**（连上之后会话自己会去问身份，
    //    问不出来就按原始串口收字节）—— D9 那套“身份驱动观察组合”只管**装配**
    //    （哪一份固件、字典、解码能力、用例目录），不是进门的门槛。
    // “这个口能不能连”只看卡片上服务端算好的 `available`（它带着理由）。
    const combo = app.observeCombo || { available: false, note: "", firmware_id: "", usable: false };
    const card = app.selectedCard;
    const portOk = Boolean(card) && card.available !== false;
    connect.disabled = !live || holding || app.running || !card || !portOk;
    connect.title = app.running
      ? "自动验证正在占用串口：等它结束或先取消"
      : holding ? "已经连接：要换设备请先断开"
        : !card ? "先在设备卡片里选一个通信口（没检测过就点一次「嗅探」）"
          : !portOk
            ? `${card.port} 现在不能连接：${card.available_reason || "这个口不可用"}`
            : `连到 #${card.num} ${card.port}`
              + (combo.firmware_id ? `（按实测身份：${combo.firmware_id}）` : "");
  }
  if ($("observeDisconnect")) $("observeDisconnect").disabled = !live || !holding;
  const again = $("observeReconnect");
  if (again) {
    again.disabled = !live || app.running || !(observe.target || observe.device);
    again.title = app.running
      ? "自动验证正在占用串口：等它结束"
      : "关闭并重新打开串口：不复位、不清设备数据；成功后自动查询身份";
  }
  // 连着的时候不让换设备：一次只连一个（服务端也会拒绝直接切）。
  // （“连哪一台”的入口只有一个：上面那排设备卡片 —— 见 2026-10-10 的用户口径。）
  // 换“观察哪一份固件”也是同一个规矩：正握着串口时不让换（先断开）。
  if ($("observeFirmware")) {
    $("observeFirmware").disabled = !live || holding;
    $("observeFirmware").title = holding
      ? "正在观察：要换目标（固件）请先断开（服务端也会拒绝直接切）"
      : "换一份固件观察：只换选中项，不连接、不打开串口、不发字节";
  }
  // 命令框：只有「已连接」才能发字节；HEX 模式下不追加行结束符，那个选择框就没意义。
  const canSend = live && observe.state === "connected";
  if ($("observeCmdSend")) {
    $("observeCmdSend").disabled = !canSend;
    $("observeCmdSend").title = canSend ? "把输入变成字节发出去（回车也可以）" : "只有「已连接」时才能发送字节";
  }
  if ($("observeCmdInput")) $("observeCmdInput").disabled = !canSend;
  if ($("observeCmdMode")) $("observeCmdMode").disabled = !live;
  if ($("observeLineEnding")) {
    $("observeLineEnding").disabled = !live || currentCmdMode() === "hex";
  }
  // 窗口内直敲：没连着就不把键盘给窗口（没设备可收字节）；连着时窗口自己的状态说了算。
  if (!captureConnected()) releaseCapture();
  //  / D8：端口/身份就绪那一格要跟着“选中的卡片 / 撞不撞口”一起动。
  // 它由状态轮询、切模式、选卡片三处驱动 —— 放在 `updateButtons()` 末尾就只算一处。
  renderPortStatus();
}

/* ------------------------------------------------------------------ actions */
/* 这次 run 要动的设备，给人看的那一行（确认框与提示都用它）。
 * ⚠️ 没选卡片时**不说“未声明端口”**（那是把“没点名”说成了“设备没说”，会让人
 *    以为要去查设备声明）：而是明说这次按**本机登记**的端口走。
 * ⚠️ 用户口径 2026-10-13：屏幕上不出现“目标声明”这类内部词。 */
function runDeviceLine() {
  const card = app.selectedCard;
  if (card) {
    const stateLabel = SNIFF_IDENTITY_LABEL[card.state] || card.state || "未知";
    return `设备：#${card.num} ${card.port}（${stateLabel}）`;
  }
  const fallback = (selectedRunFacts().target || {}).transport?.port || "";
  return `设备：未点名（按本机登记的端口 ${fallback || "未登记"} 走）`;
}

async function startRun() {
  const selection = selectedRunFacts();
  if (!selection.target || !selection.firmware || !selection.suite) {
    showMessage("没有可运行的目标 / 固件 / 套件组合", "fail");
    return;
  }
  const seconds = Number($("observeSeconds").value || 40);
  const reset = $("resetPulse").checked;
  const projectOptions = projectOptionValues();
  const tests = await selectedTests();
  const missingTestConfirmation = tests.find((row) => {
    const testCase = (selection.project?.test_catalog?.cases || []).find(
      (item) => item.id === row.case_id && item.version === row.version
    );
    return testCase?.destructive && !row.confirm_destructive;
  });
  if (missingTestConfirmation) {
    refuseStart(
      `破坏性用例 ${missingTestConfirmation.case_id} 需要本轮单独确认测试区域覆盖`,
      "autotestPicker"
    );
    return;
  }
  const confirmFlash = Boolean($("flashConfirm")?.checked);
  if (selection.firmware.requires_flash_confirmation && !confirmFlash) {
    // ：原因必须写得离按钮近（那句提示就在按钮旁边），并把勾选框括起来 ——
    // 否则用户看到的就是“点了没反应”。
    refuseStart("所选固件会覆盖设备 Flash：请先勾选“我确认覆盖设备 Flash”", "flashConfirmField");
    return;
  }  const optionLines = (selection.project?.run_options || []).map((option) =>
    `${option.label}：${projectOptions[option.key] ? "是" : "否"}`
  );
  // 身份靠什么确定由**项目包**声明（`run_defaults.probe_command`）：没有它就抓不到明文
  // 身份横幅，decode 只能如实拒绝选字典。页面只把它**照抄出来**，不自己编一条命令。
  const probe = selection.project?.run_defaults?.probe_command || "";
  const declaredLines = [
    probe ? `身份探测：${probe}（向设备要一次明文身份，否则无法选字典）` : "",
  ].filter(Boolean);
  const confirmed = window.confirm(
    "将按顺序执行：precheck → build → upload → observe → decode → assert\n\n" +
      `目标：${selection.target.label}\n` +
      //  / D5：说清这一次要动**哪一个设备** —— 选了卡片就报卡片（口是它给的），
      // 没选就如实说"没点名"（那时候口来自目标自己的声明）。
      `${runDeviceLine()}\n` +
      `固件：${selection.firmware.label}\n` +
      `项目包：${selection.firmware.project || "（未声明）"}\n` +
      `套件：${selection.suite}\n` +
      `采集窗口：${seconds} s\n` +
      `显式复位：${reset ? "是" : "否"}`
      + (selection.target.reset?.default_request === undefined
        ? "\n"
        : `（这个目标默认${selection.target.reset.default_request ? "复位" : "不复位"}）\n`) +
      (declaredLines.length ? `${declaredLines.join("\n")}\n` : "") +
      (selection.firmware.requires_flash_confirmation ? "覆盖 Flash：已逐次确认\n" : "") +
      (optionLines.length ? `${optionLines.join("\n")}\n` : "") +
      "该流程可能执行构建、烧录与设备复位。确认开始？"
  );
  // ：浏览器把 `confirm()` 吞掉时（比如用户在浏览器里勾了“不再弹窗”）
  // 以前是 `return` 一句不说 —— 那也是“点了没反应”的一种。拒绝也要出声。
  if (!confirmed) {
    showMessage("已取消启动（没有在确认框里点“确定”）", "warn");
    return;
  }
  try {
    const result = await postJson("/api/run", {
      target_id: selection.targetId,
      firmware_id: selection.firmwareId,
      suite: selection.suite,
      observe_seconds: seconds,
      reset,
      confirm_flash: confirmFlash,
      project_options: projectOptions,
      tests,
      //  / D2+D5：这次 run 点名的设备（没选卡片就是 null ⇒ 走老路径）。
      // ⚠️ 这只是“点名”：服务端会拿本次嗅探结果复核序号/口/可用性/兼容性，页面说了不算。
      device: runDeviceSelection(),
      client: { id: CLIENT.id, label: CLIENT.label },
    });
    app.runId = result.run_id;
    showMessage(`已发起 run ${result.run_id}（发起者 ${CLIENT.label}），等待编排服务执行`, "");
    updateButtons();
  } catch (error) {
    showMessage(`无法启动：${error.message}`, "fail");
  }
}

async function cancelRun() {
  if (!app.runId) return;
  try {
    await postJson(`/api/runs/${encodeURIComponent(app.runId)}/cancel`, {});
    showMessage("已请求取消当前 run", "");
  } catch (error) {
    showMessage(`取消失败：${error.message}`, "fail");
  }
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

/* -------------------------------------------------------------------- boot */
function clientIdentity() {
  // Per-tab identity. The service can be driven by a human in one tab and by an
  // agent in another, so every run records who asked for it.
  let id = sessionStorage.getItem("pw.clientId");
  if (!id) {
    id = Math.random().toString(16).slice(2, 6);
    sessionStorage.setItem("pw.clientId", id);
  }
  return { id, label: `浏览器-${id}` };
}

const CLIENT = { id: "", label: "" };

/* 宿主回归用的极小接口：`仓库根/tools/test_webconsole.py` 用 Node 加载本文件，
 * 直接驱动下面这套真模型（不把前端逻辑在测试里重写一遍）；浏览器里也可以用
 * `receive()` 走真实 SSE 入口。它不碰串口，也不写任何证据文件。 */

/* ------------------------------------------------------------------ Hook 契约
 * `PWLogWorkspace` 是模型回归与浏览器 E2E 的**唯一驱动口**，也是文档里给用户的
 * 调试接口（`PWLogWorkspace.tune.set(…)` / `prefs.current()` / `store()` / `receive()`）。
 * 它一旦被改名或删掉，两边都会**静默失效** —— 所以这里把**必须保留的稳定子集**
 * 写成运行时数据，由宿主回归与 E2E 直接核对（新增键允许，删除/改名当场红）。
 *
 * ⚠️ 有意识的不兼容升级：同时升 `HOOK_CONTRACT_VERSION`、改这张表，
 * 并更新 `公开文档` 与 `pass/07-console-log-workspace.md` 里引用它的地方。
 *
 * ⚠️ **版本 2（ / D9，2026-10-12）**：`observe.targetChoices` 改名并改变语义 ——
 * 它从前喂的是"页面自己从卡片允许列表里挑一份"，现在读的是**服务端算好的身份匹配结论**
 * （`card.match`）；输入形状（卡片要带 `match`）与输出形状都变了，所以按约定**改名**，
 * 而不是留一个名字没变、意思偷偷换了的键。 */
const HOOK_CONTRACT_VERSION = 2;
const HOOK_SURFACE = {
  top: [
    "SOURCES", "MAX_LINES", "model", "observe", "offline", "console", "terminal",
    "modes", "tune", "prefs", "store", "receive", "buildRunSources", "parseEventLog",
    "eventLine", "toLines", "stripAnsi", "metrics", "summarizeDomains", "history",
  ],
  deep: [
    "model.create", "model.viewItems", "model.paneView", "model.compileExclude",
    "prefs.current", "tune.set", "observe.stats", "offline.buildSources",
    "console.parseHexText", "terminal.keyDescriptor", "modes.current",
    "metrics.panes", "metrics.heapUsage",
    //  / D8：控制区紧凑那几块（状态行 / 计数 / 两个格式化 / 换设备那句说明）。
    "observe.statCompact", "observe.counters", "observe.byteUnits", "observe.compactDuration",
    "observe.selectionNote",
    // 用户口径（2026-10-10）：实时观察的“目标”**不由页面挑**； / D9 起，
    // 它是**服务端算好的身份匹配结论**（唯一命中自动采用，零/多匹配阻塞并说明原因）。
    "observe.combination",
    // 用户口径（2026-10-10）：“不存在的口不展示”那一句短话（端口名单来自服务端）。
    "observe.sniffSkippedLine",
    // 需求 §12.3：逐通信口的进度那一行（纯函数；模型回归与 E2E 的驱动口）。
    "observe.sniffProgress",
    // RUNTIME_MODES §12（模式化历史列表）：标题 / 列 / 行 / 状态行都出自这一个纯函数。
    "history.model", "history.columns", "history.title",
    // 用户口径（2026-10-13）：阶段“定位”的坐标换算、中心锚点，以及**这一窗的行号能不能信**。
    // 三个都是纯函数（1 基证据行号 → 0 基 DOM 下标；一条引用 → 区间 + 锚点；
    // 一条流式行 → 这一窗的基准/缺口），模型回归直接驱动它们 ——
    // 这两个数一旦错了，定位就会停在“启动日志首行”那个位置（错一行也是错）。
    "locate.lineIndex", "locate.plan", "locate.paneIssue", "locate.frame",
  ],
};

/* 缺哪些（空数组 = 契约满足）。写成纯函数是为了能**注入式自检**：
 * 给它一个被挖掉键的对象，它必须报出名字 —— 否则这条契约可能永远通过。 */
function hookSurfaceGaps(target, spec) {
  const gaps = [];
  const source = target && typeof target === "object" ? target : {};
  const wanted = spec && typeof spec === "object" ? spec : {};
  for (const name of wanted.top || []) {
    if (!(name in source)) gaps.push(name);
  }
  for (const path of wanted.deep || []) {
    let node = source;
    for (const part of String(path).split(".")) {
      node = node && typeof node === "object" ? node[part] : undefined;
    }
    if (typeof node !== "function") gaps.push(path);
  }
  return gaps;
}

/* ---------------------------------------------- 原始串口的终端视图（O14B）
 * `raw` 事件带着**这一行的原始字节**（服务端 `bytes_hex`）；这里按字节重放：
 *   `\r` 回行首覆盖、`\n` 换行、`\b` 退一格、`\t` 跳到下一个 8 的倍数；
 *   已支持的 CSI：`K`（清到行尾）、`2J`/`3J`（清屏）、`H`/`f`（定位）、`A/B/C/D`（移光标）；
 *   `m`（颜色）、`?25h/l`（光标可见性）等**吞掉不渲染** —— 不宣称完整 ANSI 兼容。
 * 未知序列不崩：吞掉它，原始字节始终完整地留在 `raw.bin` 与行视图里。
 * 终端只是**显示层**：行模型、筛选、搜索、下载都不受影响。
 */
const TERM_MAX_ROWS = 2000;
const TERM_MAX_COLS = 400;
const TERM_TAB = 8;

function createTermState() {
  return {
    rows: [[]], row: 0, col: 0, follow: true, partial: new Uint8Array(0),
    lns: [null], times: [null], nextLn: null, nextTs: "",
  };
}

let TERM_STATE = null;

function termState() {
  if (!TERM_STATE) TERM_STATE = createTermState();
  return TERM_STATE;
}

function termReset() {
  TERM_STATE = createTermState();
  renderTerminalNow();
}

function terminalOn() {
  const pane = $("pane-raw");
  return Boolean(pane && pane.dataset.terminal === "1");
}

function setTerminalView(on) {
  const pane = $("pane-raw");
  if (!pane) return;
  if (on) pane.dataset.terminal = "1";
  else delete pane.dataset.terminal;
  const button = $("terminal-raw");
  if (button) button.setAttribute("aria-pressed", on ? "true" : "false");
  if (on) renderTerminalNow();
}

function termRowChars(state, index) {
  while (state.rows.length <= index) {
    state.rows.push([]);
    state.lns.push(state.nextLn);
    state.times.push(state.nextTs);
  }
  return state.rows[index];
}

function termWriteChar(state, ch) {
  const chars = termRowChars(state, state.row);
  if (state.col > chars.length) {
    while (chars.length < state.col) chars.push(" ");
  }
  if (state.col < TERM_MAX_COLS) {
    chars[state.col] = ch;
    state.col += 1;
  }
}

function termNewline(state) {
  state.row += 1;
  state.col = 0;
  termRowChars(state, state.row);
}

function termEraseToEnd(state) {
  const chars = termRowChars(state, state.row);
  if (state.col < chars.length) chars.length = state.col;
}

function termClearScreen(state) {
  state.rows = [[]];
  state.row = 0;
  state.col = 0;
}

function termCsi(state, params, final) {
  const parts = String(params || "").replace(/^\?/, "").split(";");
  const first = Math.max(1, Math.min(200, Number(parts[0] || 1) || 1));
  if (final === "K") {
    termEraseToEnd(state);
    return;
  }
  if (final === "J" && (parts[0] === "2" || parts[0] === "3" || parts[0] === "")) {
    termClearScreen(state);
    return;
  }
  if (final === "H" || final === "f") {
    const rowNo = Number(parts[0] || 1) || 1;
    const colNo = Number(parts[1] || 1) || 1;
    state.row = Math.max(0, Math.min(TERM_MAX_ROWS - 1, rowNo - 1));
    state.col = Math.max(0, Math.min(TERM_MAX_COLS, colNo - 1));
    termRowChars(state, state.row);
    return;
  }
  if (final === "A") {
    state.row = Math.max(0, state.row - first);
    termRowChars(state, state.row);
    return;
  }
  if (final === "B") {
    state.row += first;
    termRowChars(state, state.row);
    return;
  }
  if (final === "C") {
    state.col = Math.min(TERM_MAX_COLS, state.col + first);
    return;
  }
  if (final === "D") {
    state.col = Math.max(0, state.col - first);
    return;
  }
  /* 其它（m 颜色、?25 光标、私有序列……）：不渲染、不崩。 */
}

/* 一段原始字节 → 终端状态。不足一个 UTF-8 字符的尾巴留着等下一段（不画半个字）。 */
function termApplyBytes(bytes) {
  const state = termState();
  const feed = state.partial.length
    ? new Uint8Array([...state.partial, ...bytes])
    : bytes;
  state.partial = new Uint8Array(0);
  const decoder = new TextDecoder("utf-8", { fatal: false });
  let index = 0;
  while (index < feed.length) {
    const byte = feed[index];
    if (byte === 0x1b) {
      if (index + 1 >= feed.length) {
        state.partial = feed.slice(index);
        return;
      }
      if (feed[index + 1] === 0x5b) {
        let at = index + 2;
        let params = "";
        while (at < feed.length) {
          const ch = feed[at];
          const digit = ch >= 0x30 && ch <= 0x39;
          if (!digit && ch !== 0x3b && ch !== 0x3f) break;
          params += String.fromCharCode(ch);
          at += 1;
        }
        if (at >= feed.length) {
          state.partial = feed.slice(index);
          return;
        }
        termCsi(state, params, String.fromCharCode(feed[at]));
        index = at + 1;
        continue;
      }
      index += 2;   // 非 CSI 的 ESC 序列：吞掉这两个字节
      continue;
    }
    if (byte === 0x0d) {
      state.col = 0;
      index += 1;
      continue;
    }
    if (byte === 0x0a) {
      termNewline(state);
      index += 1;
      continue;
    }
    if (byte === 0x08) {
      if (state.col > 0) state.col -= 1;
      index += 1;
      continue;
    }
    if (byte === 0x09) {
      const next = (Math.floor(state.col / TERM_TAB) + 1) * TERM_TAB;
      while (state.col < next && state.col < TERM_MAX_COLS) termWriteChar(state, " ");
      index += 1;
      continue;
    }
    if (byte < 0x20 || byte === 0x7f) {
      index += 1;
      continue;
    }
    const need = byte < 0x80 ? 1
      : (byte >> 5) === 0x6 ? 2
        : (byte >> 4) === 0xe ? 3
          : (byte >> 3) === 0x1e ? 4 : 0;
    if (!need) {
      index += 1;
      continue;
    }
    if (index + need > feed.length) {
      state.partial = feed.slice(index);
      return;
    }
    termWriteChar(state, decoder.decode(feed.slice(index, index + need)));
    index += need;
  }
  if (state.rows.length > TERM_MAX_ROWS) {
    const drop = state.rows.length - TERM_MAX_ROWS;
    state.rows.splice(0, drop);
    state.row = Math.max(0, state.row - drop);
  }
}

function termHexBytes(hex) {
  const text = String(hex || "");
  const bytes = new Uint8Array(Math.floor(text.length / 2));
  for (let at = 0; at < bytes.length; at += 1) {
    bytes[at] = Number.parseInt(text.slice(at * 2, at * 2 + 2), 16) || 0;
  }
  return bytes;
}

function termFeedEvent(event) {
  if (!terminalOn()) return;
  const state = termState();
  state.nextLn = Number((event && event.line_no) || 0) || null;
  state.nextTs = String((event && event.ts) || "");
  if (event && event.bytes_hex) {
    termApplyBytes(termHexBytes(event.bytes_hex));
  } else {
    // 旧服务 / 旧录音没有字节：按解码后的这一行 + 换行显示（行号/时间戳仍然可用）。
    termApplyBytes(new TextEncoder().encode(`${String((event && event.line) || "")}\n`));
  }
  renderTerminalNow();
}

function termSnapshotRows(state) {
  return state.rows.map((chars, index) => {
    const text = chars.join("");
    if (index !== state.row) return { before: text, at: "", after: "" };
    return {
      before: chars.slice(0, state.col).join(""),
      at: chars[state.col] || " ",
      after: chars.slice(state.col + 1).join(""),
    };
  });
}

/* 这一窗当前挂着的视图（行视图与终端视图**共用同一份条件**：排除、只看错误、级别/模块、
 * 折叠重复、搜索、行号/时间戳）。取不到就返回 null，终端就只画正文，不自己编一套条件。 */
function termRawView() {
  const sources = (typeof attachedLog !== "undefined" && attachedLog && attachedLog.sources) || null;
  const source = sources && sources.raw;
  return (source && source.view) || null;
}

function updateTerminalHits(total, cursor, hasQuery) {
  const label = $("hits-raw");
  if (!label) return;
  label.textContent = hasQuery ? `${total ? Math.min(Math.max(1, cursor + 1), total) : 0}/${total}` : "0/0";
  label.dataset.state = hasQuery && !total ? "none" : "";
}

function renderTerminalNow() {
  const host = $("termLines");
  if (!host) return;
  const state = termState();
  const view = termRawView();
  const filtering = Boolean(view && typeof view.level === "string");
  const dedupe = Boolean(view && view.dedupe);
  host.dataset.wrap = view && view.wrap === false ? "0" : "1";
  host.dataset.prefix = view && view.lineNumbers && view.timestamps ? "both"
    : view && view.lineNumbers ? "ln"
      : view && view.timestamps ? "time" : "none";
  const compiled = view && view.query ? compileQuery(view) : { re: null };
  const re = compiled.re || null;
  const currentSlot = Number((view && view.hitCursor) || 0);
  let hits = 0;
  let previousText = null;
  let repeatNode = null;
  let repeatCount = 0;
  let scrollTarget = null;
  const fragment = document.createDocumentFragment();
  const snapshot = termSnapshotRows(state);
  for (let index = 0; index < snapshot.length; index += 1) {
    const row = snapshot[index];
    const text = row.at ? `${row.before}${row.at}${row.after}` : row.before;
    // 条件与行视图**同一条口径**：看不见的行一个节点也不建（为何看不见由行视图的模型回答）。
    if (filtering && hiddenReason(view, text)) continue;
    if (dedupe && previousText === text && repeatNode) {
      repeatCount += 1;
      repeatNode.dataset.repeat = String(repeatCount);
      continue;
    }
    const line = document.createElement("div");
    line.className = "term-row";
    if (state.lns[index]) line.dataset.ln = `${state.lns[index]} `;
    if (state.times[index]) line.dataset.time = `${beijingClock(state.times[index])} `;
    const ranges = re ? matchRanges(re, text) : [];
    const isCursorRow = index === state.row;
    if (isCursorRow) {
      // 光标行：正文、光标块、后文三段拼起来（命中高亮不嵌在光标块里）。
      line.append(document.createTextNode(row.before));
      const cursor = document.createElement("span");
      cursor.className = "term-cursor";
      cursor.textContent = row.at || " ";
      line.append(cursor);
      line.append(document.createTextNode(row.after));
      if (ranges.length) scrollTarget = scrollTarget || line;
    } else if (ranges.length) {
      let slot = hits;
      let cursorAt = 0;
      for (const [start, end] of ranges) {
        if (start > cursorAt) line.append(document.createTextNode(text.slice(cursorAt, start)));
        const mark = document.createElement("mark");
        mark.className = "hit";
        if (slot === currentSlot) {
          mark.classList.add("current");
          scrollTarget = line;
        }
        mark.textContent = text.slice(start, end);
        line.append(mark);
        cursorAt = end;
        slot += 1;
      }
      if (cursorAt < text.length) line.append(document.createTextNode(text.slice(cursorAt)));
    } else {
      line.textContent = text;
    }
    hits += ranges.length;
    previousText = text;
    repeatNode = line;
    repeatCount = 1;
    fragment.append(line);
  }
  host.replaceChildren(fragment);
  if (scrollTarget) {
    const target = scrollTarget.offsetTop - host.clientHeight / 2 + scrollTarget.offsetHeight / 2;
    host.scrollTop = Math.max(0, target);
    state.follow = false;
  } else if (state.follow) {
    host.scrollTop = host.scrollHeight;
  }
  updateTerminalHits(hits, currentSlot, Boolean(view && view.query));
}

function wireTerminalView() {
  const button = $("terminal-raw");
  if (button) button.addEventListener("click", () => setTerminalView(!terminalOn()));
  const host = $("termLines");
  if (host) {
    host.addEventListener("scroll", () => {
      const state = termState();
      state.follow = host.scrollHeight - host.scrollTop - host.clientHeight < 24;
    });
  }
}

globalThis.PWLogWorkspace = {
  contractVersion: HOOK_CONTRACT_VERSION,
  hookContract: HOOK_SURFACE,
  hookSurfaceGaps,
  /* 指标趋势的**两个窗格**（每窗一个指标）的驱动口。模型回归直接驱动这几个
   * 纯函数 —— 页面里那两个下拉框选出来的 key，就是它们算出来的。 */
  metrics: {
    paneCount: METRIC_PANE_COUNT,
    defaults: METRIC_PANE_DEFAULTS,
    resolve: resolvePaneMetric,
    panes: paneMetricChoices,
    heapUsage: heapUsageReading,
    heapUsageText,
    lastPoint: lastMetricPoint,
    keys: metricKeys,
    /* 2026-10-07（任务管理器那个效果）：横轴滚动时间窗 + 纵轴固定刻度。
     * 模型回归直接喂下面这几个纯函数。 */
    windowChoices: METRIC_WINDOW_CHOICES,
    windowSeconds,
    scaleChoices: METRIC_SCALE_CHOICES,
    axisRange,
    heapTotalOf,
    nowText,
    niceCeil,
    /* ：增量的合并（模型回归直接驱动它 —— 服务端只发新点，合并规则在这里）。 */
    merge: mergeMetrics,
    apply: applyMetrics,
    valueBounds,
    seriesColor: metricSeriesColor,
    taskStackDetail,
  },
  /* L3：历史证据的**区间窗口**驱动口（模型回归与浏览器验收都用它）。
   * 它不是 Hook 契约里那份"不许改名"的子集 —— 但模型回归直接驱动它，所以删除同样会红。 */
  windows: {
    contractOf: windowContract,
    noteOf: windowNote,
    byteText,
    mergeByteRanges,
    rangesAfter: windowRangesAfter,
    tailRange: tailByteRange,
    olderRange: previousByteRange,
    parseContentRangeTotal,
    prependPlan,
    olderMergePlan,
    omittedWindows,
    tailFor: loadWindowTail,
    olderFor: loadOlderWindow,
    cancel: cancelWindow,
  },
  /* L4：全文搜索的驱动口（模型回归直接驱动；不是 Hook 契约里"不许改名"的那一份）。 */
  search: {
    hitLabel: searchHitLabel,
    jumpRange: searchJumpRange,
    runningText: searchRunningText,
    doneText: searchDoneText,
    /* 窗口行号基准（请求/响应头）：面板那列的行号就靠它。是纯函数，宿主回归直接喂它。 */
    lineBaseFromHeader,
    lineBaseIndex,
    findLineIndex,
    // 命中上下文 → 可用来比对的候选片段（纯函数；省略号与单独 CR 都在这里处理）。
    textCandidates: searchTextCandidates,
    paneFor: searchPaneFor,
    entity: searchEntity,
    start: startFullSearch,
    cancel: cancelFullSearch,
    gotoHit: gotoSearchHit,
  },
  /* L5：实时降载的驱动口（纯函数 + 队列 + 立即 flush）。
   * `state()` / `queued()` 给模型回归与浏览器验收读现场。 */
  shed: {
    create: createShedState,
    afterFlush: shedAfterFlush,
    percentile: shedPercentile,
    sampleCap: SHED_SAMPLE_CAP,
    push: shedPush,
    drain: shedDrain,
    /* 这条记账该不该显示：只有实时观察那一屏（或真丢过东西）才显示（纯函数）。 */
    visible: shedVisible,
    text: shedText,
    queuedKinds: SHED_QUEUED_KINDS,
    budgetMs: SHED_BATCH_BUDGET_MS,
    queueMax: SHED_QUEUE_MAX,
    minDelayMs: SHED_FLUSH_MIN_MS,
    maxDelayMs: SHED_FLUSH_MAX_MS,
    state: () => app.shed,
    queued: () => app.queue.length,
    flush: flushRealtime,
    reset: () => {
      app.queue = [];
      app.shed = createShedState();
      if (app.flushTimer) {
        clearTimeout(app.flushTimer);
        app.flushTimer = null;
      }
      updateShedIndicator();
      return app.shed;
    },
  },
  SOURCES,
  MAX_LINES,
  // 离线回放：会话冻结块 → 阶段行（纯函数，宿主回归直接喂真实冻结块）。
  frozenLiveTestSteps,
  model: {
    create: createLogStore,
    appendText,
    appendMany,
    feed,
    continueLine,
    // 五窗来源隔离：事件→窗口的显式映射 + 串窗自检（纯函数，宿主回归直接驱动）。
    eventToSource,
    eventWindowByKind: EVENT_WINDOW_BY_KIND,
    windowSourceViolations,
    appendEvents,
    setSourceLines,
    setSourceText,
    setPaused,
    setFollowState,
    isWatched,
    resumeSource,
    flushPlan,
    markRendered,
    trimSource,
    // Slice B：派生视图（搜索/过滤/折叠/复制）全是纯函数，宿主回归直接驱动它们。
    createViewState,
    advanceView,
    resetView,
    rebuildSource,
    pruneViewState,
    // 滚轮边界判定：只在 {scrollTop, scrollHeight, clientHeight} 这种普通对象上算。
    wheelEdge,
    // 布局形态：给定 layout 算出应该显示哪几个窗口（纯函数，宿主回归直接测）。
    visiblePanes,
    nextFocus,
    // 分栏与一行布局：状态规范化、展开/收起视图、拖分隔条的宽度分配（全是纯函数）。
    normalizeSplit,
    normalizeWidths,
    normalizeCollapsed,
    paneView,
    nextExpandedForRail,
    dragPaneWidths,
    paneDragPlan,
    equalizedWidths,
    dragSlop: DRAG_SLOP,
    minPaneWidth: PANE_MIN_W,
    // “最佳阅读位置”要滚到哪里（纯函数：给定底部位置/视口高/最大滚动量）。
    readingPlan,
    clearReadingHeight,
    sanitizeHeadHeight,
    // 工作台栏与四窗之间那几行占掉多少高度（纯函数）：它决定四窗的正文上限。
    paneChromeGap,
    // 视口还没成形（后台标签页打开：0×0）时量到的几何值全是垃圾，不许写进去（纯函数）。
    layoutIsSane,
    // 统计行文字（窗口头显示短版，title 放长版）也是纯函数。
    statParts,
    visibleItem,
    viewItems,
    viewText,
    totalHits,
    locateHit,
    linePasses,
    parseLine,
    isErrorLine,
    lineTime,
    // W6：时间列与北京时间（毫秒）。主机时刻优先，设备 uptime 只在没有主机时刻时兜底。
    lineTimeLabel,
    beijingClock,
    beijingStamp,
    beijingParts,
    compileQuery,
    matchRanges,
    // 文本排除（切片 6，需求 §7）：条件编译、是否生效、这一行为什么看不见、隐藏了多少行。
    compileExclude,
    excludeActive,
    lineExcluded,
    hiddenReason,
    excludedCount,
  },
  buildRunSources,
  parseEventLog,
  eventLine,
  /* ：按**结论域**把断言行分桶（纯函数）。模型回归直接驱动它 ——
   * “证据与日志健康”与“业务结论”分开念，但失败行照旧留在自己的桶里。 */
  summarizeDomains,
  DOMAIN_TEXT,
  toLines,
  stripAnsi,
  /* ：三个模式各一份四窗内容。`create` 给判据造两份对比（互不干扰），
   * `for/attached/isAttached/capture` 是“现在看的是哪一份、往哪一份写”的只读问答。 */
  views: {
    ids: MODE_IDS,
    create: createLogView,
    for: viewFor,
    attached: attachedView,
    shell: (mode) => viewFor(mode).shell,
    isAttached,
    receiving: receivingView,
    lane: laneStore,
    domRefKeys: DOM_REF_KEYS,
  },
  /* 状态句的档位（用户口径 2026-10-10，2026-10-12 收敛为固定在简洁档）：三档的定义、当前那一档，
   * 以及“一段段拼一句 + 给 title 兑底”这套机器 —— 全是纯函数，模型回归直接喂三档
   * （不依赖本机偏好）。⚠️ 页面上**没有**档位控件了：`set` 是给模型回归的驱动口，不是用户入口。 */
  say: {
    levels: SAY_LEVELS,
    defaultLevel: SAY_DEFAULT,
    ranks: SAY_RANK,
    min: { always: SAY_ALWAYS, keyItem: SAY_KEY_ITEM, raw: SAY_RAW },
    current: () => sayLevel(),
    set: setSayLevel,
    load: loadSayLevel,
    row: sayLevelRow,
    segment: seg,
    build: say,
    text: sayText,
    title: sayTitle,
    block: sayBlock,
  },
  // 实时观察的展示文案（纯函数：能力声明没实现的就不许在文案里假装有）
  observe: {
    stats: observeStats,
    /*  / D8：控制区**紧凑**那两块 —— 状态行与 `TX … | RX …` 计数，以及
     * 两个格式化（字节单位、跨天时长）。模型回归直接喂它们（判据不靠真串口）。 */
    statCompact: observeStatCompact,
    counters: observeCounters,
    byteUnits,
    compactDuration,
    /* 用户口径（2026-10-12）：**只在出问题时通报**（连上/正常断开都不弹那一行；
     * 打不开 / 串口不在了 / 被别的模式占着才说）。纯函数：模型回归直接喂一份快照。 */
    notice: observeNotice,
    /* 用户口径（2026-10-12 晚）：受控测试那一批**屏幕上只留“跑着：…”**；
     * 收口总结（`已收口：PASS（PASS 2）`）只进悬停。`text` vs `full` 就是这个分工。 */
    liveTestStatusSay,
    /* D8：换设备之后“不得沿用上一台设备的说法” —— 这句话由它产出（纯函数）。 */
    selectionNote: observeSelectionNote,
    /* 用户口径（2026-10-10）：“口都选了，为什么还要选目标板？” —— “观察哪一份固件”
     * 读服务端算好的身份匹配结论（`card.match`）：唯一命中才可用，零/多匹配要说得出口。
     * 纯函数：模型回归直接喂卡片（带 `match` 的那种）+ 登记表钉它。 */
    combination: observeCombination,
    /* 用户口径（2026-10-10）：“系统里不存在的设备就不要展示”。不存在的口由服务端就不造
     * 卡片（结构化给在 `last.skipped`，完整那句在 `notes`），页面只把端口列出来说一句短话。 */
    sniffSkippedLine,
    /* 需求 §12.3：进度那一行的**纯函数**（模型回归与浏览器 E2E 直接喂它一个 `sniff`
     * 块）。⚠️ 它只读 `sniff.progress`，不读 `last` —— 两者是两类事实。 */
    sniffProgress: sniffProgressModel,
    /* 当前这一块 `sniff`（只读）。浏览器验收摆完仿真快照之后要**原样放回去** ——
     * 拿不到原来那一份就只能清成 null，那会让卡片消失到下一次轮询。 */
    sniffBlock: () => app.sniff,
    /* 浏览器验收用（与下面那个清选择的口子同一类）：把一份**仿真**的进度快照摆到页面上
     * —— 不发请求、不碰串口；2 s 轮询会把它冲回服务端的事实（那正是它该有的行为）。
     * ⚠️ 用例要在**同一次 evaluate** 里摆完就量：中间被轮询插进来就变成另一种现场了。 */
    showSniffProgress: (block, busy) => {
      app.sniff = block || null;
      app.sniffBusy = Boolean(busy);
      renderSniffProgress();
    },
    limits: observeLimits,
    identity: observeIdentityLine,
    /* 状态句的档位（用户口径 2026-10-10）：上面这三句各自还有一个 `…Say()` 版本，
     * 它带一个**显式档位**参数并返回 `{text, full, level}` —— 模型回归直接喂三档，
     * 钇住“哪一段在哪一档出现”（不依赖本机偏好）。 */
    statCompactSay: observeStatSay,
    identitySay: observeIdentitySay,
    /* P1（验收 2026-10-11）：屏幕上那几枚**结论 chip** 的纯函数（它不许带 build 号、
     * 字典路径或可用字典清单 —— 那些在 `identityDetailSay()` 与悬停里）。 */
    identitySummary,
    identityDetailSay,
    identityHoverTitle,
    limitsSay: observeLimitsSay,
    decodeWhy: decodeWhyText,
    decodeWhySay,
    evidenceSession,
    evidenceLink: sessionEvidenceLink,
    /* 实时观察会不会挡住这次 run：**只比端口**（纯函数，与启动按钮同一条实现）。
     * 模型回归直接喂它 {holding, port} + 目标口，钉住"别的口被占时不拦"。 */
    runConflict: liveRunConflict,
    /*  / D5：卡片身份（“本次序号 + 口”）与“这次 run 点名了哪个设备”的纯函数。
     * 模型回归直接喂它一个 `card`/`selectedCard`，钉住“重新嗅探后旧序号不跨轮复用”。 */
    sniffCardKey,
    selectedSniffCard,
    runDeviceSelection,
    /* 清掉这一次的选择 —— 与“重新嗅探清选择”是同一件事（浏览器验收用例之间要互不污染，
     * 但它们不该各自去发一次真嗅探）。 */
    clearSniffSelection: () => {
      app.selectedCard = null;
      renderSniffCards();
      renderRunCatalog(app.webCatalog || {}, true);
    },
    /* ：重放的行按**会话号**过闸 —— 这两个问答就是那道闸的依据。 */
    liveSessionId: currentLiveSessionId,
    replaySkip: () => ({ ...app.replaySkip }),
    /* 模型回归要能摆出"现在跑的是哪一次会话"（那道闸的依据）。只动一个字段：
     * 不跑 `renderObserve` 那一堆 DOM 工作 —— 判据要的是路由，不是重画。
     * `setSession` = "状态到了"（顺带把存着的行判一遍）；`forgetSession` = "还没收到状态"。 */
    setSession: (sessionId) => {
      app.observe = sessionId ? { session: { session_id: sessionId } } : null;
      app.observeSeen = true;
      flushHeldReplayLines();
      return currentLiveSessionId();
    },
    forgetSession: () => {
      app.observe = null;
      app.observeSeen = false;
      app.replayHold.waiting = true;
      app.replayHold.events = [];
      return app.replayHold.events.length;
    },
    heldReplay: () => app.replayHold.events.length,
    flushHeldReplay: flushHeldReplayLines,
  },
  // 离线诊断（切片 6 / P4）：会话清单文案、回看装载的来源、诊断包目标、导入进度模型。
  offline: {
    optionLabel: sessionOptionLabel,
    rowText: sessionRowText,
    rowCounts: sessionRowCounts,
    importNote,
    reviewText: sessionReviewText,
    selectedId: offlineSelectedId,
    buildSources: buildSessionSources,
    bundleHref,
    bundleTarget,
    /* ：按钮上写清“这是谁的包”，title 里写清“它不含什么”。 */
    bundleLabel,
    bundleScopeNote,
    derivedArtifacts: DERIVED_ARTIFACTS,
    /* P5/P6：全文件搜索“要搜哪一份证据”的候选与默认值（纯函数，喂它详情里的
     * `artifacts_meta` 就行）。会话详情以前不带这一项 ⇒ 回看会话时下拉框是空的。 */
    searchChoices: searchEvidenceChoices,
    preferredSearchEvidence: preferredSearchEvidence,
    import: {
      taskId: importTaskId,
      progress: importProgressModel,
      stageText: importStageText,
      failureText: importFailureText,
      interruption: importInterruption,
      resultText: importResultText,
      pollMs: IMPORT_POLL_MS,
      pollMax: IMPORT_POLL_MAX,
      current: () => app.importTask,
    },
  },
  // 命令框的纯函数：HEX 预检、历史翻页、提示文案（宿主回归与 Python 侧对同一批样本）
  console: {
    parseHexText,
    historyStep,
    cmdHint,
    cmdRequest,
    utf8Length,
    historyKey: CMD_HISTORY_KEY,
    historyMax: CMD_HISTORY_MAX,
  },
  // 窗口内直敲的纯函数（W6-4）：按键描述、串行队列、“点空白还是拖选”的判定、状态文案。
  // ⚠️ 这里**没有**按键 → 字节的映射：那份映射只在服务端（`build_keys_payload`），
  // 页面只说“按了什么”，字节由服务端算 —— 两份映射一定会漂（）。
  terminal: {
    keyDescriptor,
    shouldFlushTerminal,
    enqueueTerminalKey,
    takeTerminalBatch,
    captureClickDecision,
    captureStateText,
    capturePanes: () => CAPTURE_PANES.slice(),
    paneCaptureOwns,
    isModifierKeyOnly,
    terminalKeyIsSendable,
    sentText: terminalSentText,
    capturing: capturingId,
    queueMax: TERMINAL_QUEUE_MAX,
    clickSlop: CAPTURE_CLICK_SLOP,
  },
  // 原始串口的终端视图（O14B）：按字节重放的纯模型 + 开关（宿主回归直接驱动）。
  term: {
    reset: termReset,
    state: termState,
    applyBytes: termApplyBytes,
    render: renderTerminalNow,
    snapshot: termSnapshotRows,
    hexBytes: termHexBytes,
    on: terminalOn,
    setView: setTerminalView,
    maxRows: TERM_MAX_ROWS,
    maxCols: TERM_MAX_COLS,
  },
  // 命令输入行的高度（W6-1）：拖动条改的就是它，越界值一律夹回区间。
  dock: {
    height: dockHeight,
    collapsed: dockCollapsed,
    clamp: clampCmdHeight,
    min: CMD_H_MIN,
    max: CMD_H_MAX,
    defaultHeight: CMD_H_DEFAULT,
  },
  // “页面是不是旧的”（）：戳的比较与那句提示都是纯函数。
  stale: {
    buildMoved,
    stalePageText,
    noteBuild,
    current: () => app.build,
  },
  modes: { list: MODES, load: loadMode, key: MODE_KEY, current: () => app.mode },
  /* RUNTIME_MODES §12：模式化历史列表。标题、列定义、行与状态行都是纯函数，
   * 模型回归直接喂三类数据（run / 会话 / 空 / 失败）。 */
  history: {
    model: historyModel,
    columns: historyColumns,
    title: historyTitle,
    hint: historyHint,
    sessionState: sessionStateOf,
    render: renderHistory,
    current: () => ({
      mode: app.mode,
      title: historyTitle(app.mode),
      columns: historyColumns(app.mode),
    }),
  },
  /* ：“启动自动验证”能不能按 / 还差哪一步。纯函数，模型回归直接喂它组合。 */
  start: { readiness: startReadiness, refuse: refuseStart },
  /* 用户口径（2026-10-13）：阶段卡上的“定位”。
   * `lineIndex` = 1 基证据行号 → 0 基 DOM 下标（`data-src`）；`plan` = 一条引用 →
   * 要轻微高亮的区间 + 要居中的**中心锚点**（优先 `anchor_line`，否则区间末行）；
   * `paneIssue` = 这一窗的行号现在可不可信（不可信就不定位，并说出原因）；
   * `frame` = 一条**带行号**的流式行 → 这一窗要落的基准 / 缺口结论；
   * `seqIndex` = 事件号 → 行下标（派生视图的坐标落点，）。
   * 五个都是纯函数 —— 宿主回归直接喂各种坐标、截断、缺口与半行。 */
  locate: {
    lineIndex: evidenceLineIndex,
    plan: locatePlan,
    paneIssue: paneFrameIssue,
    frame: streamLineFrame,
    seqIndex: seqRowIndex,
  },
  /* ：这一次页面加载是刷新还是导航（浏览器验收要问它）。 */
  nav: { reload: isReloadNavigation },
  // 高亮“当前窗口”的调试接口（只改 CSS 变量；tuneCss 是纯函数，宿主可测）
  tune: { set: tuneSet, show: tuneShow, reset: tuneReset, css: tuneCss, vars: TUNE_VARS, defaults: TUNE_DEFAULTS },
  prefs: { load: loadPrefs, key: PREFS_KEY, current: () => app.prefs },
  store: () => app.log,
  receive: (event, options) => {
    // 默认"注进去就看得见"（模型回归与浏览器验收都靠它）；
    // 传 `{ flush: false }` 才是"只排队"，用来验批处理本身。
    handleEvent(event);
    if (!options || options.flush !== false) flushRealtime();
    return app.queue.length;
  },
};

/* 地址栏跟着“屏幕上的东西”走。以前不留记录的话，地址栏还指着上一个看过的 run，一刷新
 * （或把链接发出去）就会静默显示那一个旧 run 的四个窗口 —— 解码日志看上去“跟刚才一样”。
 * `replaceState` 而不是给 `location.hash` 赋值：不触发 hashchange、不重新加载。
 *
 * ⚠️ 2026-10-06 真机验收量出来的第四条：用户在离线诊断那一屏时，后台跑完的 run 也把地址栏
 *    改成了 `#run=<刚跑完的那个>` ⇒ 一刷新就被拽回自动验证屏（正在读的包当场消失）。
 *    地址栏里只有 run 这一种东西，所以在别的屏上**不改**它，保持“最后一次真正看过的那一条”。
 */
function noteRunInAddress(runId) {
  if (app.mode !== "auto") return;
  try {
    history.replaceState(null, "", `#run=${encodeURIComponent(runId)}`);
  } catch (error) {
    /* history API unavailable in this context: the panes are still correct */
  }
}

/* 这一次页面加载是**刷新**（F5 / 重新打开那个标签）还是真的导航（）。
 * 判据只有一条：Navigation Timing 里那条 `type`（`reload` / `navigate` / `back_forward`）。
 * 拿不到（老浏览器 / 无该 API）就当作“不是刷新”—— 旧口径继续用，宁可抢屏也不要让内容不装
 * （那会变成四个空窗口）。纯函数：模型回归直接喂它组合。 */
function isReloadNavigation(kind) {
  if (typeof kind === "string") return kind === "reload";
  try {
    const entries = typeof performance !== "undefined" && performance.getEntriesByType
      ? performance.getEntriesByType("navigation")
      : null;
    return Boolean(entries && entries.length && entries[0].type === "reload");
  } catch (error) {
    return false;
  }
}

/* 打不开证据时要说清“为什么看不见日志”，而不是丢下四个空窗口
 * —— 窗口里的内容全是刷新时从服务读 runs/ 目录回来的，服务不在了它们就是空的。 */
function loadRunOrReport(runId, options) {
  return loadRun(runId, options).catch((error) => {
    const reason = error && error.message ? error.message : String(error);
    showMessage(
      `无法加载 run ${runId} 的证据：${reason} —— 调试台服务还在运行吗？`
        + "（窗口内容靠服务读 runs/ 目录，服务停了就会是空的）",
      "fail"
    );
  });
}

function isTypingTarget(target) {
  if (!target || !target.tagName) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable === true) {
    return true;
  }
  // 正拿着键盘的日志窗口：按下的键是**要发给设备的字节**，和输入框一样不该被快捷键抢走。
  return Boolean(target.closest && target.closest('.log-lines[data-capture="1"]'));
}

/* 换档：**只剩模型回归这一条驱动路**（页面上没有控件）。
 * 用户口径 2026-10-12：把页头那个「详情」选择栏删掉、默认简洁 —— 连“选项怎么画”那个函数
 * 与两个元素 id 一起删了（不留一份没人用的实现）。
 * 它只改内存里那一档并重画那几句状态句：**不重新取数、不重开串口、不动任何证据**。
 * ⚠️ 不再写本机偏好（那个偏好键随控件一起删了）：刷新之后屏幕一律回到简洁档。 */
function setSayLevel(id) {
  if (!SAY_LEVEL_IDS.includes(id)) return sayLevel();
  app.sayLevel = id;
  if (app.observe) renderObserve(app.observe);
  renderDecodeWhy(app.observe);
  return id;
}

function boot() {
  Object.assign(CLIENT, clientIdentity());
  buildWorkspace(app.log);
  applyPrefs();
  applyDock();          // 命令输入行的高度/折叠是阅读偏好：刷新后还是自己拉好的样子
  if ($("fontMinus")) $("fontMinus").addEventListener("click", () => bumpFont(-1));
  if ($("fontPlus")) $("fontPlus").addEventListener("click", () => bumpFont(1));
  if ($("alignWorkspace")) $("alignWorkspace").addEventListener("click", () => alignReadingPosition());
  if ($("foldAll")) {
    $("foldAll").addEventListener("click", () => {
      const store = app.log;
      const allFolded = SOURCE_IDS.every((id) => store.sources[id].view.folded);
      setAllFindsFolded(store, !allFolded);
    });
  }
  wirePaneRails(app.log);
  wireSplitBar(app.log);
  wireFloatingControls(app.log);
  wireFullSearch();   // L4：全文搜索的按钮 / 回车 / 取消（都是显式动作）
  if ($("fontFamily")) {
    $("fontFamily").addEventListener("change", (event) => {
      app.prefs.fontFamily = event.target.value;
      applyPrefs();
      savePrefs();
    });
  }
  // 主题（用户口径 2026-10-12）：两个图标按钮 —— 读本机偏好后把按下态画准，点一下就换。
  setTheme(loadTheme());
  for (const button of document.querySelectorAll(".theme-btn")) {
    button.addEventListener("click", () => setTheme(button.dataset.themeId));
  }
  // 状态句的档位固定在「简洁」（页面上没有那个控件了：`loadSayLevel()` 只说这一件事）。
  app.sayLevel = loadSayLevel();
  // 「详情」入口（P1）：只改本屏的显示（把已经算好的那段原文摊开/收起），不发任何请求。
  if ($("identDetailBtn")) {
    $("identDetailBtn").addEventListener("click", () => {
      app.identDetailOpen = !app.identDetailOpen;
      renderIdentDetail();
    });
  }
  // 「更多设置」（§11.1）：低频选项默认收起，这是它唯一的开关。
  if ($("moreToggle")) {
    $("moreToggle").addEventListener("click", () => moreOptionsOpen($("moreOptions").hidden));
  }
  moreOptionsOpen(false);
  applyMode();   // 先把记住的模式画出来（纯显示，不发任何动作）
  app.layout.split = { ...normalizeSplit(app.prefs.split), on: false };
  app.layout.collapsed = normalizeCollapsed(app.prefs.workspace && app.prefs.workspace.collapsed);
  app.layout.widths = normalizeWidths(app.prefs.workspace && app.prefs.workspace.widths);
  app.metricPanes = loadMetricPanes(app.prefs.metrics);
  app.metricWindows = loadMetricWindows(app.prefs.metricWindows);
  app.metricScales = loadMetricScales(app.prefs.metricScales);
  applyRowLayout(app.log);
  // 窗口宽度跨过 1044 px 时，“一行 ↔ 2×2”才是另一套布局：要重新算一次（折叠/拖宽在窄屏无效）。
  let lastRow = isRowLayout();
  window.addEventListener("resize", () => {
    const row = isRowLayout();
    if (row === lastRow) return;
    lastRow = row;
    applyRowLayout(app.log);
    updateLogIndicators(app.log);
  });
  $("startRun").addEventListener("click", startRun);
  $("cancelRun").addEventListener("click", cancelRun);
  // ：“还差一步”那行就贴在按钮旁边 —— 勾选框一变就得立刻跟上，
  // 不能等下一次 /api/state 轮询（那最多快 2 秒后，用户会以为提示是假的）。
  for (const id of ["flashConfirm", "resetPulse"]) {
    if ($(id)) $(id).addEventListener("change", updateButtons);
  }
  if ($("runTarget")) $("runTarget").addEventListener("change", () => renderRunFirmwares());
  // 换固件：只换选中项（目标跟着它推导，套件跟着换）—— 不连接、不烧录。
  if ($("runFirmware")) $("runFirmware").addEventListener("change", () => {
    syncRunTargetFromFirmware();
    renderRunSuites();
  });
  if ($("runSuite")) $("runSuite").addEventListener("change", () => renderRunSuites());
  //  / D5：嗅探只有这一个入口（点了才动端口），卡片选择只改本地状态。
  if ($("sniffBtn")) $("sniffBtn").addEventListener("click", () => sniffDevices());
  if ($("sniffCards")) {
    // D8：这一格最多完整展示 3 张，更多的靠滚轮逐卡移动（并吸附），页面不跟着滚。
    wireSniffCardsWheel();
    $("sniffCards").addEventListener("change", (event) => {
      const field = event.target;
      if (field && field.name === "sniffDevice") selectSniffCard(field.value);
    });
  }
  // 模式菜单与实时观察的三个显式动作（没有一个是隐式的）。
  for (const button of document.querySelectorAll(".mode-btn")) {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  }
  if ($("observeConnect")) {
    $("observeConnect").addEventListener("click", () => observeAction("/api/observe/connect", {
      // 连哪一台设备**只**由本次嗅探选中的那张卡片决定（`{scan_seq, port}`）——
      // 由服务端拿那一次嗅探的结果复核，再定用哪一条设备登记（页面说了不算）。
      // 没有卡片时按钮本来就是禁的（见 `updateButtons`）。
      // ⚠️  / D9（需求 §9.1）：**不带 `firmware_id`** —— 固件由服务端按这张卡片的
      // 实测身份唯一匹配（唯一命中自动采用；零/多匹配 409 并说明原因）。页面挑固件这条
      // 路就是 §9.1 要拆掉的那一条。
      device: runDeviceSelection(),
      client: { id: CLIENT.id, label: CLIENT.label },
      // 需求 §6.10：本次连接的串口参数（不传 = 沿用上一次；非法值服务端当场 400）。
      serial: serialRequest(),
    }));
  }
  if ($("observeDisconnect")) {
    $("observeDisconnect").addEventListener("click", () => observeAction(
      "/api/observe/disconnect", { reason: `用户在页面上断开（${CLIENT.label}）` },
    ));
  }
  if ($("observeReconnect")) {
    $("observeReconnect").addEventListener("click", () => observeAction(
      "/api/observe/reconnect", { serial: serialRequest() },
    ));
  }
  //  / L4b：受控测试**只在人点了按钮时**才发请求（需求 §8：启动必须显式＋
  // 刷新不重复执行 —— 页面从不自动启动）。
  if ($("startLiveTests")) $("startLiveTests").addEventListener("click", () => startLiveTests());
  if ($("cancelLiveTests")) $("cancelLiveTests").addEventListener("click", () => cancelLiveTests());
  // 离线诊断：回看一个已有会话（只读目录与文件，不碰串口）。
  if ($("sessionLoad")) $("sessionLoad").addEventListener("click", () => loadSession());
  if ($("sessionRefresh")) $("sessionRefresh").addEventListener("click", () => refreshSessions());
  if ($("sessionPick")) $("sessionPick").addEventListener("change", () => renderOffline());
  // 模式化历史列表（RUNTIME_MODES §12）：行点击是委托的 —— 两件事：打开 run 证据 / 去离线诊断回看会话。
  if ($("runsBody")) $("runsBody").addEventListener("click", historyRowClick);
  renderHistory();
  // 导入诊断包（切片 8）：选文件只是本地动作，点了「导入」才真的传上去。
  if ($("importFile")) $("importFile").addEventListener("change", renderImportState);
  if ($("importRun")) $("importRun").addEventListener("click", () => importBundle());
  if ($("importCancel")) $("importCancel").addEventListener("click", () => cancelImport());
  renderImportState();
  wireConsoleInput();
  wirePaneCapture();
  wireTerminalView();
  wireCommandDock();
  window.addEventListener("hashchange", () => {
    const match = /run=([^&]+)/.exec(window.location.hash);
    if (match) loadRunOrReport(decodeURIComponent(match[1]));
  });
  // 快捷键的作用域：只在“没在输入”时才接管，而且只作用于最近操作过的那个窗口。
  // Ctrl+L 被浏览器留给地址栏，所以“跳尾”的可靠入口是按钮和 Alt+L。
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const typing = isTypingTarget(target);
    // 某个窗口正把按键发给设备时，全局快捷键**整个让位**：那些键都要变成字节。
    // （窗口自己的 handler 已经 stopPropagation，这里是第二道保险。）
    if (paneCaptureOwns(event, capturingId())) return;
    if (event.key === "Escape") {
      closeMenus(app.log, null);
      // 全屏/分栏都是临时形态：Esc 先退出它们（菜单同时收起）。
      if (app.layout.mode === "focus") setFocus(app.log, app.layout.focus);
      else if (app.layout.mode === "split") setSplit(app.log, { on: false });
      updateLogIndicators(app.log);
    }
    if ((event.ctrlKey || event.metaKey) && !event.altKey
      && (event.key === "f" || event.key === "F")) {
      if (!typing && focusPaneSearch(app.log, app.lastPane)) event.preventDefault();
    }
    if ((event.ctrlKey || event.metaKey) && (event.key === "a" || event.key === "A")) {
      // Ctrl+A：只有焦点本来就在某个日志窗口里时才全选它；否则交给浏览器全选页面。
      const pane = target instanceof Element ? target.closest(".log-pane") : null;
      if (!typing && pane && pane.dataset.source) {
        selectAllInPane(pane.dataset.source);
        event.preventDefault();
      }
    }
    if (!typing && event.altKey && (event.key === "l" || event.key === "L")) {
      jumpToLatest(app.lastPane);
      event.preventDefault();
    }
    // Alt+F：收起/展开当前窗口的搜索与筛选行（同一行里也有“筛选”按钮）。
    if (!typing && event.altKey && (event.key === "f" || event.key === "F")) {
      const source = app.log.sources[app.lastPane];
      if (source) setFindFolded(app.log, app.lastPane, !source.view.folded);
      event.preventDefault();
    }
    // Alt+Enter：当前窗口在“四窗”与“单窗全屏”之间切换。
    if (!typing && event.altKey && event.key === "Enter") {
      setFocus(app.log, app.lastPane);
      event.preventDefault();
    }
  });

  connectStream();

  const requested = /run=([^&]+)/.exec(window.location.hash);
  refreshState().then(async () => {
    await refreshRuns();
    const payload = await getJson("/api/runs").catch(() => ({ runs: [] }));
    // 正在实时观察时**不**自动加载上一次 run：四个窗口该显示的是这次会话，
    // 混进上一轮的历史行就没法分辨哪一行是谁说的（显式带 #run= 的除外）。
    // 离线诊断模式下也不自动装 run：那个模式在看**会话**，塞一份 run 的证据进去会让
    // 面板上写的和窗口里显示的变成两件事（用户点会话或点历史 run 就会装载）。
    const live = evidenceSession();
    const autoRun = live || app.mode === "offline"
      ? null
      : (payload.runs[0]?.run_id || null);
    const requestedId = requested ? decodeURIComponent(requested[1]) : null;
    const requestedExists = requestedId
      ? payload.runs.some((row) => row.run_id === requestedId)
      : false;
    if (requestedId && !requestedExists) {
      history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      showMessage(`历史 run ${requestedId} 不存在或不满足证据契约，已回到最新可用记录。`, "fail");
    }
    const runId = requestedExists ? requestedId : autoRun;
    if (runId) {
      if (!requestedExists) window.location.hash = `run=${encodeURIComponent(runId)}`;
      // ⚠️ ：刷新时**不许**把屏幕拽到自动验证。地址栏里那个 `#run=` 是**页面自己**
      //    写进去的（`noteRunInAddress`），所以 F5 一定会带着它回来 —— 上一次的实现把它当成
      //    “用户点了一个 run”，于是 `loadRun` 把模式切成 auto：用户正看着实时观察，过几秒
      //    就被拽走了（他报的就是这个）。刷新只该**填内容**（`focus:false`），不该换屏；
      //    真的导航（点链接 / 地址栏改 hash）继续抢屏（那条走 `hashchange`，不经过这里）。
      await loadRunOrReport(runId, { focus: !isReloadNavigation() });
    }
  });
  setInterval(refreshState, STATE_POLL_MS);
}

document.addEventListener("DOMContentLoaded", boot);
