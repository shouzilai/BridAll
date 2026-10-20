/* 命令框与“窗口内直接敲键”的**纯函数**。
 *
 * 命令框（需求 §4.1）三条边界都在这里成立：
 *   · 非法十六进制**一个字节也不发**（本地先提示，服务端仍是权威）；
 *   · 平台不认识业务命令：没有白名单、没有命令建议；
 *   · HEX 模式不加“高级模式”开关、不截断到 256 B。
 *
 * 敲键队列（W6-4）三条边界：按键名 → 描述（字节由服务端算）、**严格串行**（一次只有一个
 * 请求在飞，否则串口字节会乱序）、点空白 vs 拖选要分得开（想复制日志的人不该往设备敲键）。
 *
 * 这里不碰 DOM、不发请求、不写存储：本机历史读写留在组合根。
 *
 * 装载顺序：sources → format → … → say → … → 本文件 → transport → app
 */
(function (global) {
  "use strict";

  const TERMINAL_QUEUE_MAX = 200;   // 防呆上限：打字再快也不会把队列堆到天上去
  const CAPTURE_PANES = ["raw", "decoded"];   // 哪两个窗口可以直接敲键
  const CAPTURE_CLICK_SLOP = 4;     // 按下→松开的位移超过它就算"拖选"，不捕获键盘

  /* ---------------------------------------------------------------- 命令框 */

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

  /* 记一条历史（纯函数：给旧清单，返回新清单）。连着重复发同一条只记一遍，超上限从头丢。 */

  /* -------------------------------------------------- 窗口内直接敲键的队列 */

  /* 一次按键 → 描述（纯函数）。字节由服务端算（两份映射一定会漂，）。 */
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
    return id ? `⌨ 键盘在发字节给设备（${id === "raw" ? "原始串口" : "解码日志"}）· 点圆点放开` : "";
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

  /* 命令窗口那个拖高度手柄的悬停说明（当前多高、默认多高、拖到头会怎样）。 */

  global.PWModelCommand = Object.freeze({
    TERMINAL_QUEUE_MAX, CAPTURE_PANES, CAPTURE_CLICK_SLOP, cmdRequest, utf8Length,
    parseHexText, cmdHint, historyStep, keyDescriptor, shouldFlushTerminal, enqueueTerminalKey,
    takeTerminalBatch, captureClickDecision, captureStateText, terminalSentText, capturingId
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
