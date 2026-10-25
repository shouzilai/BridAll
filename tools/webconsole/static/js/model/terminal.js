/* 原始串口的**终端状态机**（纯函数）：把字节流按终端语义重放成「行 × 列」的字符网格。
 *
 * 它只换显示，不改原始字节与证据：喂进来的永远是 `raw.bin` 的字节（或 `bytes_hex`）。
 * 支持范围是**子集**：CR / LF / BS / DEL / TAB、CSI 里的 K、J（2/3）、H/f、A/B/C/D；
 * 其它序列（颜色 `m`、私有 `?25` 等）一律吞掉、不渲染也不崩 —— 不假装支持终端仿真。
 *
 * ⚠️ BS(0x08) 与 DEL(0x7F) 都算退格：设备回显退格用哪个字节看 shell，不认其中一个
 * 就会“删了字还在”，后面回显叠上来看着像乱码。
 *
 * 分块安全：一段字节的尾巴如果不是完整 UTF-8 字符或 CSI 序列，留到下一段再解，
 * 不画半个字、也不把序列拆坏（RS-485 / 串口一次读到的本来就不是整行）。
 *
 * 不碰 DOM、不发请求、不碰定时器；状态对象由宿主自己持有并传进来。
 *
 * 装载顺序：sources → … → 本文件 → … → app
 */
(function (global) {
  "use strict";

  const TERM_MAX_ROWS = 2000;
  const TERM_MAX_COLS = 400;
  const TERM_TAB = 8;

  function createTermState() {
    return {
      rows: [[]], row: 0, col: 0, follow: true, partial: new Uint8Array(0),
      lns: [null], times: [null], nextLn: null, nextTs: "",
    };
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
  function termApplyBytes(state, bytes) {
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
      if (byte === 0x08 || byte === 0x7f) {
        // 设备回显的退格只是“光标左移”（擦不擦由后面的空格决定）。
        // ⚠️ BS(0x08) 与 DEL(0x7F) 都要认：同一块板子换个 shell 就换一个字节。
        //    以前只认 0x08、把 0x7F 当控制字符吞掉 —— 屏上就成了“删了但字还在”，
        //    后面的回显再叠上来，看起来就是乱码（COM3 上敲 trial 再退格复现过）。
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
      if (byte < 0x20) {
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

  /* 每行拆成「光标前 / 光标处那个字符 / 光标后」三段：渲染要高亮命中，
   * 但**不能把命中嵌进光标块里**，所以切分在这儿做（纯函数，宿主回归直接驱动）。 */
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

  /* 终端正文的**行计划**：哪些行显示、连续重复几次、命中在哪、当前命中是哪一条、
   * 页面该滚到哪一行。
   *
   * 条件与行视图**同一条口径**：`hidden` / `ranges` 由宿主传进来（就是行视图那两个
   * 纯函数）。看不见的行一个节点也不建 —— 为什么看不见由行视图的模型回答，不另立一套。
   *
   * `slotStart` 是该行第一个命中在整个窗口里的序号（渲染时用来判“这一条是不是当前命中”）。
   */
  function terminalRowPlan(input) {
    const snapshot = (input && input.snapshot) || [];
    const view = (input && input.view) || null;
    const cursorRow = Number(input && input.row) || 0;
    const lns = (input && input.lns) || [];
    const times = (input && input.times) || [];
    const hidden = (input && input.hidden) || (() => false);
    const rangesOf = (input && input.ranges) || (() => []);
    const filtering = Boolean(view && typeof view.level === "string");
    const dedupe = Boolean(view && view.dedupe);
    const currentSlot = Number((view && view.hitCursor) || 0);
    const rows = [];
    let hits = 0;
    let previousText = null;
    let repeatRow = null;
    let repeatCount = 0;
    let scrollIndex = -1;
    for (let index = 0; index < snapshot.length; index += 1) {
      const snap = snapshot[index];
      const text = snap.at ? `${snap.before}${snap.at}${snap.after}` : snap.before;
      if (filtering && hidden(text)) continue;
      if (dedupe && previousText === text && repeatRow) {
        repeatCount += 1;
        repeatRow.repeat = repeatCount;
        continue;
      }
      const ranges = rangesOf(text) || [];
      const isCursorRow = index === cursorRow;
      const row = {
        index, before: snap.before, at: snap.at, after: snap.after, text,
        ln: lns[index] || null, ts: times[index] || null,
        ranges, slotStart: hits, isCursorRow, repeat: 0,
      };
      if (isCursorRow) {
        if (ranges.length && scrollIndex < 0) scrollIndex = rows.length;
      } else {
        let slot = hits;
        for (let at = 0; at < ranges.length; at += 1) {
          if (slot === currentSlot) { scrollIndex = rows.length; break; }
          slot += 1;
        }
      }
      hits += ranges.length;
      rows.push(row);
      previousText = text;
      repeatRow = row;
      repeatCount = 1;
    }
    return { rows, hits, currentSlot, scrollIndex, filtering, dedupe };
  }

  /* 终端视图下「复制」拿到的文本：看得见什么就复制什么（行尾空白去掉、末尾空行不带走）。
   * 行号/时间戳是 CSS `::before`，本来就不在 textContent 里。 */
  function terminalCopyText(rowTexts) {
    const rows = (rowTexts || []).map(
      (text) => String(text == null ? "" : text).replace(/\s+$/, "")
    );
    while (rows.length && rows[rows.length - 1] === "") rows.pop();
    return rows.join("\n");
  }

  global.PWModelTerminal = Object.freeze({
    TERM_MAX_ROWS, TERM_MAX_COLS, TERM_TAB,
    createTermState, termRowChars, termWriteChar, termNewline, termEraseToEnd,
    termClearScreen, termCsi, termApplyBytes, termHexBytes, termSnapshotRows,
    terminalRowPlan, terminalCopyText,
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
