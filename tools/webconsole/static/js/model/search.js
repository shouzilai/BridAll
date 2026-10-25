/* 全文件搜索的**纯函数**：把事实说成一句话、算出该取哪一段字节、在已加载行里找命中那一段。
 * 不发请求、不碰 DOM、不碰定时器 —— 宿主回归直接驱动它们。
 *
 * ⚠️ 它与每一窗自己的搜索框**不是同一件事**：那一栏只搜索已经加载到页面上的内容
 * （窗口模式下就是那一段字节），这里描述的是**整个文件**的分块扫描 —— 服务端给进度、
 * 能取消、有结果上限。两者谁也不冒充谁。
 *
 * 依赖：`locate.js` 的 `byteText`（字节单位的写法）。
 *
 * 装载顺序：sources → format → … → locate → 本文件 → … → app
 */
(function (global) {
  "use strict";

  const locate = global.PWModelLocate;
  if (!locate) {
    throw new Error("PWModelLocate 必须先于 PWModelSearch 装载");
  }
  const { byteText } = locate;

  /* 一条命中取多大一段回来（`bytes=start-end` 的宽度）。 */
  const SEARCH_JUMP_PAD_BYTES = 32 * 1024;
  /* 服务端给的“命中上下文”未必能直接拿去找行：太短的片段满屏都是，先只试长的。 */
  const SEARCH_FRAGMENT_MIN = 4;

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

  /* 服务端给的“命中上下文”不一定能直接拿去找行：
   * ① 超长行会被**截断**，两头带 `…` —— 那是**服务端的标记**，文件里没有这个字符；
   * ② 上下文里可能有**单独的 CR**，而面板把 CR 也当断行（`toLines`）⇒
   *    一条服务端“行”在窗口里是好几行，整段前缀跨了行。
   * 所以先把它拆成几条**候选片段**（长的先试）：去省略号 → 按 CR/LF 拆。 */
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

  /* 结果里的证据名 → 窗口 id（映射表由调用方给：它取决于“现在在看 run 还是会话”）。 */
  function paneFor(evidence, map) {
    for (const [paneId, name] of Object.entries(map || {})) {
      if (name === evidence) return paneId;
    }
    return "";
  }

  /* 结果里的证据名 → 下拉框里能选的那些（详情给过的产物，含只装了尾部一段的）。
   * 入参就是证据元数据本身；`names` 是这一屏会走窗口的那几个证据名。 */
  function evidenceChoices(rows, names) {
    const known = new Set(names || []);
    const out = [];
    for (const row of rows || []) {
      if (row && row.name && (known.has(row.name) || Number(row.bytes) > 0)) out.push(row);
    }
    return out;
  }

  /* 默认搜哪一份：**最大**的那份（会话里通常就是 raw.bin）。
   * 这只是兑底；真正的“默认值”在组合根（它优先选正在滚动的那一份）。 */
  function largestEvidence(rows, names) {
    let best = "";
    let size = -1;
    for (const row of evidenceChoices(rows, names)) {
      const bytes = Number(row.bytes) || 0;
      if (bytes > size) {
        size = bytes;
        best = row.name;
      }
    }
    return best;
  }

  global.PWModelSearch = Object.freeze({
    SEARCH_JUMP_PAD_BYTES, SEARCH_FRAGMENT_MIN,
    searchHitLabel, searchJumpRange, searchRunningText, searchDoneText,
    searchTextCandidates, findLineIndex, paneFor, evidenceChoices, largestEvidence,
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
