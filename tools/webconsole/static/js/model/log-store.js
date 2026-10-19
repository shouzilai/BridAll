/* 日志工作台的**状态容器**与布局选择器。
 *
 * 这一层只造状态、改状态、算“该显示哪几个窗口”：不碰 DOM、不发请求、不读 storage。
 * 组合根（`app.js`）拿到状态对象之后再去接线、量尺寸、重画。
 *
 * ⚠️ 每个窗口一份完整状态：**没有**“当前源”这种全局概念，所以暂停/未读/跟随
 *    都不可能跨窗口连坐 —— 这条是下面这些形状存在的理由，不是巧合。
 *
 * 依赖：`sources.js`（窗口清单）与 `preferences.js`（分栏/折叠规范化）先装载。
 *
 * 装载顺序：
 *   sources → format → log-filter → log-source → terminal → locate → preferences → 本文件 → app
 */
(function (global) {
  "use strict";

  const sources = global.PWModelSources;
  const prefs = global.PWModelPreferences;
  if (!sources || !prefs) {
    throw new Error("PWModelSources / PWModelPreferences 必须先于 PWModelLogStore 装载");
  }
  const { SOURCE_IDS, SOURCE_BY_ID } = sources;
  const { normalizeSplit, normalizeWidths, normalizeCollapsed, SPLIT_MIN_N, SPLIT_MAX_N } = prefs;

  /* 窗口最小可用宽度：拖到比它还窄就“收成标签”（而不是挤成一条缝）。
   * 它远大于标签条本身（--rail-w: 26px）—— 收起是为了省地方。 */
  const PANE_MIN_W = 160;
  /* 拖动分隔条要先动过这么多像素才算“在拖”：否则单击一下就会把窗口挪半个条的宽度，
   * 而且双击的第一下先挪动第二下就换地方了 —— 双击很难触发（用户报过）。 */
  const DRAG_SLOP = 3;

  /* ------------------------------------------------ 派生视图：搜索、过滤、折叠 */
  /* 每个窗口一份视图状态：没有“全局搜索”，一个窗口的查询不会影响另三个。 */
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

  /* 每个窗口一份完整状态（见文件头的说明）。 */
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

  /* 三份 store（三屏各一份）。`folded` 是**筛选行的开合**这条阅读偏好：
   * ⚠️ 必须在这里（唯一的工厂）装 —— 从前只装在启动那一屏的装配里，
   *    切到另两屏是展开的，同一个页面里两屏不一样。 */
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

  /* ------------------------------------------------------------ 状态动作 */
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

  /* ------------------------------------------------------- 布局选择器 */
  /* 一行布局：每个窗口是“展开”还是“收成标签”。纯函数，宿主回归直接测。
   * rowMode = 宽到自己能排下几列（窄屏回到 2×2 / 单列，那时折叠与拖宽都不生效）。 */
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
    // grid：五个都在，折叠是用户自己按的（但至少留一个展开）
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

  function visiblePanes(layout) {
    if (!layout) return SOURCE_IDS.slice();
    if (layout.mode === "focus" && SOURCE_IDS.includes(layout.focus)) return [layout.focus];
    if (layout.mode === "split") return normalizeSplit(layout.split).ids;
    // 兜底：任何说不清的形态都回到五个窗口 —— 绝不能让工作区变成空的。
    return SOURCE_IDS.slice();
  }

  function nextFocus(current, id) {
    return current === id ? "" : id;
  }

  /* 拖某一根分隔条：在**相邻两个窗口**之间重新分配宽度（别的窗口一点不动）。
   * 某一侧窄过 `PANE_MIN_W` 时不再挤它，而是判定“收成标签”。
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

  /* 拖动分隔条的第一步：先看指针到底动没动。
   * ⚠️ 分两件事很重要：
   *   1) **单击不许移动窗口** —— 指针位置本身就在条上（≈ 左窗口右边缘），没有阈值的话
   *      单击一下就会把分栏挪半个条的宽度；
   *   2) 不动就不算拖动 → 双击的第一下不会先挪一下（不然第二下可能已经落到别的元素上）；
   *      双击才能靠得住地触发。 */
  function paneDragPlan({ startX, clientX, startLeftWidth, slop = DRAG_SLOP }) {
    const dx = Number(clientX) - Number(startX);
    return { moved: Number.isFinite(dx) && Math.abs(dx) >= slop, leftPx: Number(startLeftWidth) + dx };
  }

  /* **一个窗口头上那些话**（工作区那栏的“收起/展开筛选行”、窗头悬停、
   * 收成标签后的 aria/悬停、暂停/继续、折叠筛选行、单窗全屏）。
   *
   * 入参显式：`source` 是这一窗的状态，其余（是哪个窗口、是不是卡片、是不是收成标签、
   * 是不是全屏中、一共收起了几个）由调用方读好递进来 —— 模型不读 `app`、不量 DOM。 */

  global.PWModelLogStore = Object.freeze({
    PANE_MIN_W, DRAG_SLOP, createViewState, createSourceState, createLogStore, setPaused,
    setFollowState, paneView, nextExpandedForRail, visiblePanes, nextFocus, dragPaneWidths,
    paneDragPlan
  });
})(globalThis);
