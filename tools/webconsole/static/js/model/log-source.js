/* 日志行模型（文本→行、追加、裁剪、行号基准、视图推进）。
 *
 * 边界（架构约定）：不读 DOM、不发请求、不读写浏览器存储、不设定时器。
 * 这里只改传进来的 `store` / `source` 对象，不改任何证据文件。
 *
 * 依赖：`log-filter.js` 必须先装载（`advanceView` 复用同一份 `visibleItem` 规则）。
 * 装载顺序：format → log-filter → log-source → app。
 */
(function (global) {
  "use strict";

  const filter = global.PWModelLogFilter;
  if (!filter) {
    throw new Error("PWModelLogFilter 必须先于 PWModelLogSource 装载");
  }
  const { visibleItem } = filter;

  /* 每个窗口最多保留多少行（超出就丢最旧的，并如实记账）。 */
  const MAX_LINES = 5000;

  const stripAnsi = (text) => String(text ?? "").replace(/\u001b\[[0-9;]*[A-Za-z]/g, "");

  /* 控制字符 → 可见转义（`\x08` 这种写法）。还留着它是为了"这一窗里确实有控制字符"
   * 这种诊断场景；**行正文不靠它显示** —— 见下面的 `visibleLineText`。 */
  function visibleControls(text) {
    return String(text ?? "").replace(/[\u0000-\u0008\u000a-\u001f\u007f]/g, (ch) =>
      "\\x" + ch.charCodeAt(0).toString(16).padStart(2, "0"));
  }

  /* 一行文本的**可见内容**：把这一行按终端语义“画”一遍。
   *
   * 为什么要有它（用户口径）：设备回显退格发的是 `\x08 \x08`（退格、空格、退格）。
   * 直接把这些字节摆出来，屏上就是 `msh >trial\x08 \x08` —— 用户看到的是“奇怪字符”，
   * 而他要的是“我敲 trial，按一次退格就是 tria，再按一次就是 tri”。所以这一行要像终端那样
   * 带一个**光标**重放：
   *   · 普通字符：写在光标处（**覆盖**，不是插入），光标右移；
   *   · 退格（0x08 / 0x7F）：光标左移一格（擦不擦由后面那个空格决定，与真终端一致）；
   *   · 回车（0x0D）：光标回到行首（后面的字覆盖前面）；
   *   · 其它控制字符（响铃、换页……）：不画 —— 终端本来也不为它们画字形。
   *
   * ⚠️ 只管**显示**：模型里的字符串、`raw.bin`、下载与证据一个字都不改。
   * ⚠️ 光标之后的内容不显示：那是被退格擦掉/被回车盖住的区域（终端上那里是空格）。
   */
  function visibleLineText(text) {
    const chars = [];
    let cursor = 0;
    for (const ch of String(text ?? "")) {
      const code = ch.charCodeAt(0);
      if (ch === "\r") {
        cursor = 0;
        continue;
      }
      if (code === 0x08 || code === 0x7f) {
        if (cursor > 0) cursor -= 1;
        continue;
      }
      if (code < 0x20 && code !== 0x09) continue;
      while (chars.length < cursor) chars.push(" ");
      chars[cursor] = ch;
      cursor += 1;
    }
    return chars.slice(0, cursor).join("");
  }

  /* 文本 → 展示行。末尾的换行是行终止符，不是“一行空内容”。
   *
   * ⚠️ **只按 `\n` 断行**，行尾的 `\r` 当终止符吃掉，行**中间**的 `\r` 留在原地
   *    （显示层会把它写成 `\x0d`）。以前写成 `replace(/\r\n?/g, "\n")`，等于把行内的
   *    一个 `\r` 也当成换行 —— 于是一条实时行事件可能变成**两行显示行**，而“行号 ↔
   *    显示行”是一对一的：多出来的那一行会让后面的续行判定全对不上，页面就判“行号有缺口”，
   *    再把每一截半行另起一行（真机实测：敲字符串 + 退格，行视图里一字符一行、并挂上
   *    “行号不可信”）。一条行事件就该只长一行。
   */
  function toLines(text) {
    const clean = stripAnsi(text);
    if (!clean) return [];
    const lines = clean.split("\n").map((line) => (line.endsWith("\r") ? line.slice(0, -1) : line));
    if (lines[lines.length - 1] === "") lines.pop();
    return lines;
  }

  /* “这个窗口正看着自己的尾巴” —— 只有这种情况，新行才不需要提示、才跟着滚。 */
  function isWatched(store, id) {
    const source = store.sources[id];
    return Boolean(source) && !source.paused && source.followTail;
  }

  /* 往模型里追加。`lines` 里元素可以是字符串，也可以是 `{ text, ts }`（一条一行自己的时刻）。
   * `ts` 是整个批次的时刻（一次串口读到的 N 行共享**到达那一刻**）——
   * 这不是偷懒：`io_events.jsonl` 里记的就是“这一次读到的这些字节”，同一批行本来就同时到达。
   * 两种都给时，行自己的 `ts` 优先。 */
  function appendLineText(entry) {
    return entry !== null && typeof entry === "object" ? String(entry.text ?? "") : String(entry ?? "");
  }

  function appendLineTime(entry, batchTs) {
    if (entry !== null && typeof entry === "object" && entry.ts !== undefined && entry.ts !== null) {
      return entry.ts;
    }
    return batchTs;
  }

  function appendMany(store, id, lines, ts, eventSeq = null) {
    const source = store.sources[id];
    if (!source) return 0;
    let added = 0;
    for (const entry of lines) {
      source.lines.push(appendLineText(entry));
      /* 派生视图逐行记下“这一行来自哪条 io 事件”（`eventSeq`）：一条事件画出多行时只有第一行带
       * 事件号（其余行是同一件事的续行，没有自己的事件）—— 不拿第一行的事件号冒充它们。 */
      if (source.seqs) source.seqs.push(added === 0 ? eventSeq : null);
      const when = appendLineTime(entry, ts);
      // 源行号是**证据里的序号**：裁剪不会让它跳回去，时刻也就跟着它走。
      if (when !== undefined && when !== null && when !== "") {
        source.times.set(source.baseIndex + source.lines.length - 1, when);
      }
      added += 1;
      if (!isWatched(store, id)) source.pending += 1;
    }
    return added;
  }

  function appendText(store, id, text, ts, eventSeq = null) {
    return appendMany(store, id, toLines(text), ts, eventSeq);
  }

  /* 整体装载（历史 run、切换 run、清空）：视图被权威证据替换，
   * 所以这一路不再处于“冻结”状态 —— “暂停着但显示最新”是自相矛盾的。 */
  function setSourceLines(store, id, lines, seqs = null) {
    const source = store.sources[id];
    if (!source) return 0;
    const entries = Array.from(lines || []);
    source.lines = entries.map(appendLineText);
    /* 事件号列与 `lines` **同长**（没对应行的那一格是 null）。给了 `seqs` ⇒ 这一窗现在是
     * 派生视图（内容是画出来的，行号不当行身份用）；没给 ⇒ 行号就是它的身份（run 的
     * tool.log、原始串口…）。不是派生视图的窗没有这一列，不凭空造一个。 */
    if (source.seqs) {
      const column = Array.isArray(seqs) ? seqs : [];
      source.seqs = entries.map((entry, index) => {
        const value = column[index];
        return value === undefined || value === null ? null : Number(value);
      });
      source.derived = Array.isArray(seqs);
    }
    // 整体装载：时刻也整体换掉（上一份证据的时刻绝不留到新证据上）。
    source.times.clear();
    entries.forEach((entry, index) => {
      const when = appendLineTime(entry, null);
      if (when !== undefined && when !== null && when !== "") source.times.set(index, when);
    });
    source.rendered = 0;   // DOM 必须整体重画
    source.pending = 0;    // 这是权威证据，不是“你还没看到的新行”
    source.dropped = 0;
    // 行号基准回 0：整体装载 = 从文件开头开始的完整内容。
    // 不归零的话，一个窗口装载过的证据再接整份装载时，左边那列会从上一个基准继续数。
    source.baseIndex = 0;
    // ⚠️ 基准的**可信度**也回默认：整体装载只是“这一窗现在从自己的第 1 行算”，
    //    它**不**证明那一行就是文件第 1 行 —— 要不要升级成 `exact` 由装载/行事件那边说。
    source.baseTrust = "assumed";
    source.baseIssue = "";
    source.streamed = false;   // 整体装载 = 不是从 SSE 接的（行号基准要么为 0、要么随响应头回来）
    source.paused = false;
    source.followTail = true;
    source.seen.clear();
    return source.lines.length;
  }

  function setSourceText(store, id, text) {
    return setSourceLines(store, id, toLines(text));
  }

  /* 视图落后模型多少行（暂停期间才会 > 0）。 */
  function flushPlan(store, id) {
    const source = store.sources[id];
    if (!source || source.rendered >= source.lines.length) return null;
    return { from: source.rendered, to: source.lines.length };
  }

  /* 视图追上模型：`rendered` 是“已画进 DOM 的行数”，只有 DOM 层会推进它。
   * 暂停时它原地不动 —— 差距就是待显示的行数。 */
  function markRendered(store, id, count) {
    const source = store.sources[id];
    if (source) source.rendered = Math.max(0, Math.min(count, source.lines.length));
  }

  /* 有界缓冲：丢掉最旧的行并记账。DOM 层负责同步删掉最前面的同数节点。 */
  function trimSource(store, id, maxLines = MAX_LINES) {
    const source = store.sources[id];
    if (!source) return 0;
    const extra = source.lines.length - maxLines;
    if (extra <= 0) return 0;
    source.lines.splice(0, extra);
    // 事件号列与 `lines` 逐行对齐 ⇒ 裁剪必须同步，否则它会把旧行的事件号配给新行。
    if (source.seqs) source.seqs.splice(0, extra);
    source.dropped += extra;
    source.baseIndex += extra;   // 源行号从证据的开头算，裁剪不应该改变已经显示过的编号
    source.rendered = Math.max(0, source.rendered - extra);
    // 顶上那一行已经不是原来那一行了（被裁掉了）⇒ 它不再是“边界上那条不完整的行”。
    if (source.partialHead) source.partialHead = false;
    // 时刻表跟着裁：键是**递增插入**的，所以一遇到 >= baseIndex 就可以停
    // （不能只增不减：长会话会把每一行的时刻都留在内存里）。
    for (const key of source.times.keys()) {
      if (key >= source.baseIndex) break;
      source.times.delete(key);
    }
    return extra;
  }

  /* 视图状态的记账：**两种渲染路径共用**（有 DOM 的与只推模型的宿主回归），
   * 所以“可见计数”“折叠组”“命中清单”不可能在两条路径里算出两个结果。 */
  function acceptItem(view, item, absoluteIndex) {
    if (item.merged) {
      view.tail.repeats = item.repeats;
      return false;
    }
    view.tail = item;
    view.visible += 1;
    if (item.ranges.length) view.hits.push({ index: absoluteIndex, ranges: item.ranges });
    return true;
  }

  /* 只推模型、不建 DOM：宿主回归用它驱动与浏览器完全相同的判定。 */
  function advanceView(store, id) {
    const source = store.sources[id];
    const view = source.view;
    let appended = 0;
    for (let index = source.rendered; index < source.lines.length; index += 1) {
      const item = visibleItem(source, index, view.tail);
      if (!item) continue;
      if (acceptItem(view, item, source.baseIndex + index)) appended += 1;
    }
    markRendered(store, id, source.lines.length);
    return appended;
  }

  /* 视图回到“什么都没画”的状态（重建或整体装载的第一步）。 */
  function resetView(store, id) {
    const source = store.sources[id];
    if (!source) return;
    const view = source.view;
    markRendered(store, id, 0);
    view.visible = 0;
    view.hits = [];
    view.hitCursor = -1;
    view.tail = null;
  }

  /* 一条带行号的流式行到了：这一窗的基准还信不信得过？（纯函数，宿主回归直接驱动它）
   *
   * `lineNo` = 这一行在证据文件里的行号（1 基）；`rowsBefore` = 收下它之前这一窗有几行；
   * `continuation` = 这条只是把最后一行**接完**（不新增行）。
   * 返回 `{ base, issue }`：
   *   · `base` 非 null ⇒ 用这个数当新的 `baseIndex`（这一窗从这一行开始）；
   *   · `issue` 非空 ⇒ 这一窗的行号**不可信**（含人话原因）；
   *   · 两者都没有 ⇒ 对得上，什么都不用改。
   * 没有行号（`lineNo` 不是正整数）时不改结论 —— 老服务/旧录音就是这一路。 */
  function streamLineFrame(source, lineNo, rowsBefore, continuation) {
    const value = Math.floor(Number(lineNo));
    if (!Number.isFinite(value) || value < 1) return { base: null, issue: "" };
    const base = Number(source.baseIndex) || 0;
    const rows = Math.max(0, Math.floor(Number(rowsBefore) || 0));
    if (rows === 0) return { base: value - 1, issue: "" };   // 这一窗从这一行开始
    if (continuation) {
      // 续行只允许接在**它自己那一行**后面：那一行是文件第 `base + rows` 行。
      if (value === base + rows) return { base: null, issue: "" };
      // 一种是“第一行是没带行号的半行，现在接完了”：这一行自己就把基准说清了。
      if (rows === 1 && source.baseTrust !== "exact") return { base: value - 1, issue: "" };
      return { base: null, issue: streamGapIssue(base + rows, value) };
    }
    const expected = base + rows + 1;
    if (value === expected) return { base: null, issue: "" };
    return { base: null, issue: streamGapIssue(expected, value) };
  }

  /* 缺口那句话（纯函数）：说清“从哪里跳到了哪里”，不说“可能丢了几条”这种估算。 */
  function streamGapIssue(expected, actual) {
    return `实时行有缺口（第 ${expected} 行之后直接到了第 ${actual} 行）`;
  }

  /* 把一条判定落到这一窗上：基准/可信度就位，缺口则记账（**只说一次原因**）。
   *
   * ⚠️ 升级到 `exact` 的条件是**刚刚钉上了一个由行号算出来的基准**（`frame.base` 非空）。
   *    “这一行没带行号”（老服务 / 旧录音那条路）与“续行落在末尾那一行”都**什么都没验证**：
   *    它们的不改结论 ≠ 核实过。 */
  function applyStreamFrame(source, frame) {
    if (!source || !frame) return;
    source.streamed = true;   // 走到这里 = 这一窗的行是流式接上来的（见 `paneFrameIssue`）
    if (frame.issue) {
      if (source.baseTrust !== "broken") {
        source.baseTrust = "broken";
        source.baseIssue = frame.issue;
      }
      return;
    }
    if (frame.base !== null && frame.base >= 0) {
      source.baseIndex = frame.base;
      // 这个边界被服务端的行号**验证过** ⇒ “假定第 1 行”的前提成立，可以升级。
      if (source.baseTrust === "assumed") source.baseTrust = "exact";
    }
  }

  /* 这一窗现在能不能按**文件行号**定位（纯函数：给现场，回答“不能的话为什么”）。
   *
   * ① `broken`：流里出现过缺口 ⇒ 从缺口之后，这一窗的行号不再指向文件行；
   * ② `assumed` + 重放被截断：这一窗的行是从 SSE 接上来的，刷新时服务端只重放了**有界的一段**
   *    ⇒ “窗口第一行 = 文件第一行”这个前提已经不成立。
   * `exact` 的两条路（事件带行号 / 证据装载带 `X-Line-Base`）都不受这两条影响。
   * ⚠️ 派生视图（工具输出）不用行号定位（它的行身份是事件号）⇒ 这两条不适用；
   *    但按行号给它的坐标会被如实拒掉。
   */
  function paneFrameIssue(source, replayTruncated) {
    if (!source) return "";
    if (source.derived) return "";
    if (source.baseTrust === "broken") {
      return source.baseIssue || "这一窗的实时行有缺口，行号不可信";
    }
    // ⚠️ `streamed` 那一半不能少：证据装载/区间装载的窗**不是**从 SSE 接的，
    //    它们的基准确来自响应头 —— 那一屏不该因为“这一页曾经重放被截断”而被降级。
    if (source.baseTrust === "assumed" && source.streamed && replayTruncated) {
      return "这一窗是刷新之后接上来的（积压被截断过），窗口行号不等于证据文件的行号";
    }
    return "";
  }

  global.PWModelLogSource = Object.freeze({
    MAX_LINES,
    stripAnsi,
    visibleControls,
    visibleLineText,
    toLines,
    isWatched,
    appendLineText,
    appendLineTime,
    appendMany,
    appendText,
    setSourceLines,
    setSourceText,
    flushPlan,
    markRendered,
    trimSource,
    acceptItem,
    advanceView,
    resetView,
    streamLineFrame,
    streamGapIssue,
    applyStreamFrame,
    paneFrameIssue,
  });
})(globalThis);
