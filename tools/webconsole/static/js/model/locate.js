/* 证据定位与区间窗口的纯模型。
 *
 * 两件事都属于“把证据里的坐标说清楚”，而且都**只算数、不碰页面**：
 *
 * 1) 定位：1 基文件行号 → 0 基 DOM 下标、阶段引用 → 高亮区间 + 中心锚点、
 *    事件号 → 行下标、响应头 `X-Line-Base` → 0 基基准；
 * 2) 区间窗口（LARGELOG-001 / L3）：字节区间合并、尾部/向前一页的区间、
 *    服务端契约的出处、窗口的诚实标注、更早一段的拼接计划。
 *
 * 边界（架构约定）：不读 DOM、不发请求、不读写浏览器存储、不设定时器。
 * 不依赖其它模型模块（含单位换算与区间运算都是自足的）。
 *
 * 装载顺序：`format.js` → `log-filter.js` → `log-source.js` → `terminal.js` →
 * 本文件 → `app.js`（`index.html` 与两处 Node 宿主回归同一顺序）。
 */
(function (global) {
  "use strict";

  /* ------------------------------------------------------------ 单位与区间 */
  /* 字节数 → 人话。证据用 1024 进制（写成 MiB 而不是 MB，免得和十进制混）。 */
  function byteText(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return "—";
    if (value < 1024) return `${Math.round(value)} B`;
    const units = ["KiB", "MiB", "GiB", "TiB"];
    let size = value / 1024;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
  }

  /* 区间合并：**重叠或相接**都合并（连着就是同一段字节）。
   * ⚠️ 与 core 的 `large_evidence.merge_ranges()` 同一条规则：中间缺至少一个字节的两段
   * 绝不合并 —— 合并会把"这里没加载"说成"已加载"，而页面正是拿它告诉用户看的是哪一段。 */
  function mergeByteRanges(ranges) {
    const rows = [];
    for (const item of ranges || []) {
      const start = Number(item && item[0]);
      const end = Number(item && item[1]);
      if (Number.isFinite(start) && Number.isFinite(end) && end >= start) rows.push([start, end]);
    }
    rows.sort((left, right) => left[0] - right[0]);
    const merged = [];
    for (const row of rows) {
      const last = merged[merged.length - 1];
      if (last && row[0] <= last[1] + 1) last[1] = Math.max(last[1], row[1]);
      else merged.push([row[0], row[1]]);
    }
    return merged;
  }

  /* 尾部窗口：最后 `bytes` 字节（文件更小就整份）。0 字节 ⇒ null。 */
  function tailByteRange(totalBytes, bytes) {
    const total = Number(totalBytes);
    if (!Number.isFinite(total) || total <= 0) return null;
    const width = Math.max(1, Math.min(Number(bytes) || 0, total));
    return [total - width, total - 1];
  }

  /* 向前翻一页：已加载区间里**最靠前那一段之前**的一段。到头（已经在 0 字节）⇒ null。 */
  function previousByteRange(ranges, bytes) {
    const merged = mergeByteRanges(ranges);
    if (!merged.length || merged[0][0] <= 0) return null;
    const end = merged[0][0] - 1;
    const start = Math.max(0, end - Math.max(1, Number(bytes) || 0) + 1);
    return [start, end];
  }

  /* ---------------------------------------------------------- 服务端契约 */
  /* 服务端详情里没有 `evidence_contract.tail_window_bytes` 时退回的窗口大小。
   * 用到它的**每一次**都要看得见（`windowContract().from === "fallback"`）。 */
  const FALLBACK_TAIL_BYTES = 2 * 1024 * 1024;

  /* 服务端给的契约：尾部窗口大小 + 它的**出处**。 */
  function windowContract(detail) {
    const contract = (detail && detail.evidence_contract) || null;
    const tail = Number(contract && contract.tail_window_bytes);
    if (Number.isFinite(tail) && tail > 0) return { tailBytes: tail, from: "server", contract };
    return { tailBytes: FALLBACK_TAIL_BYTES, from: "fallback", contract: null };
  }

  /* 从 `Content-Range` 里取文件总大小：正常形式是 `bytes a-b/total`；
   * 区间不可能满足时服务端发的是「星号 + 斜杠 + total」那一种 —— 两种都要认。
   * ⚠️ 这条注释里**不能**直接写出那两个字面字符：注释会在那里提前结束（踩过一次）。 */
  function parseContentRangeTotal(value) {
    const match = /\/(\d+)\s*$/.exec(String(value || ""));
    return match ? Number(match[1]) : null;
  }

  /* 区间窗口的**诚实标注**（纯函数，宿主回归直接核对文字）：
   * 总大小 / 已加载字节 / 省略方向 / 顶行是否不完整。
   * 一句也不许把"只加载了尾部一段"说成"完整日志"（需求 §3.3）。 */
  function windowNote(win) {
    if (!win || !Number(win.totalBytes)) return null;
    const merged = mergeByteRanges(win.ranges);
    const loaded = merged.reduce((sum, row) => sum + (row[1] - row[0] + 1), 0);
    const head = merged.length ? merged[0][0] : 0;
    const last = merged.length ? merged[merged.length - 1][1] : 0;
    const omittedBefore = head;
    const omittedAfter = Math.max(0, win.totalBytes - 1 - last);
    const whole = merged.length > 0 && omittedBefore === 0 && omittedAfter === 0;
    const span = merged
      .map((row) => (row[0] === row[1] ? `${row[0]}` : `${row[0]}–${row[1]}`))
      .join(",");
    const label = win.evidence || "这份证据";
    const short = whole
      ? `全部 ${byteText(win.totalBytes)}`
      : [
          `${byteText(loaded)}/${byteText(win.totalBytes)}`,
          omittedBefore ? `省前 ${byteText(omittedBefore)}` : "",
          omittedAfter ? `省后 ${byteText(omittedAfter)}` : "",
          win.headPartial ? "顶行不完整" : "",
        ].filter(Boolean).join(" · ");
    const long = whole
      ? `${label} 已全部加载（${byteText(win.totalBytes)}）`
      : `${label} 共 ${byteText(win.totalBytes)}，当前只加载了 ${byteText(loaded)}`
        + (omittedBefore ? `；前面还有 ${byteText(omittedBefore)} 没加载，点「向前加载」接着往上看` : "")
        + (omittedAfter ? `；后面还有 ${byteText(omittedAfter)} 没加载` : "")
        + (win.headPartial ? "；最上面那一行的开头在没加载的区间里，它不完整" : "")
        + "。下载拿到的始终是完整文件。";
    return { short, long, loaded, omittedBefore, omittedAfter, whole, ranges: merged };
  }

  /* 把"更早的一段"接到模型前面（纯函数：不碰 DOM，也不改传进来的数组）。
   *
   * 返回 `{ head, headPartial, stitchWith }`：
   * - `head`：要插到模型最前面的行；
   * - `headPartial`：这段的第一行是不是半行（字节起点落在行中间）；
   * - `stitchWith`：这段的尾巴要与模型第几行拼成一条完整行（-1 = 不用拼）。
   *
   * ⚠️ 拼接是**必须**的：相邻两段之间没有重叠，所以更早那一段的最后一个片段正好是
   * 模型第一行的开头 —— 不拼就是把同一行显示成两条，而且两条都不完整。 */
  function prependPlan(modelLines, chunkText, startByte) {
    const lines = modelLines || [];
    const text = String(chunkText ?? "").replace(/\r\n?/g, "\n");
    if (!text) return { head: [], headPartial: false, stitchWith: -1, dangling: "" };
    const pieces = text.split("\n");
    let dangling = "";
    if (pieces[pieces.length - 1] === "") pieces.pop();
    else if (lines.length) dangling = pieces.pop();
    return {
      head: pieces,
      headPartial: pieces.length > 0 && Number(startByte) > 0,
      stitchWith: dangling ? 0 : -1,
      dangling,
    };
  }

  /* 决定"更早的一段"到底怎么接上去（纯函数，宿主回归直接驱动）。
   *
   * ⚠️ 这里有一条**真机上摔出来的**规矩：只有模型**还保留着尾部窗口的第一行**
   * （`modelBaseIndex === 0`，也就是说没被行数上限裁过）时，拼接才有意义。
   * 模型有上限（`MAX_LINES`），前置了一大段之后最先被裁掉的恰好就是那行 ——
   * 那时候再把尾部片段拼到"现在的第一行"上，就是给一行**凭空加一个它没有的开头**
   * （真机上看到的是 `[I][I][demo] line=…`）。拼不成时那个片段只能如实留着。
   */
  function olderMergePlan(modelLines, modelBaseIndex, chunkText, startByte) {
    const plan = prependPlan(modelLines, chunkText, startByte);
    const stitchable = plan.stitchWith === 0 && Number(modelBaseIndex) === 0;
    const head = plan.head.slice();
    if (plan.dangling && !stitchable) head.push(plan.dangling);
    return {
      head,
      stitch: stitchable ? plan.dangling : "",
      headPartial: plan.headPartial,
      stitched: stitchable,
    };
  }

  /* -------------------------------------------------------------- 行号定位 */
  /* 窗口装载的**行号基准**：请求头 `X-Line-Base: 1` 问服务端，响应头里带回来。
   * 值 = 这一段的第一行在**文件**里是第几行（1 基）；没给 / 不可信就是 `null`。
   *
   * 以前没人告诉模型这件事，于是 `baseIndex` 只剩“被裁掉的行数”这一个含义 ——
   * 面板左边那列跟着「向前加载」一路涨（每窗 +约 3 万），
   * 跟搜索结果里那个**文件真行号**对不上（实测：字节 321683181 是第 4515116 行，
   * 面板却显示 32792）。小文件看不见这个洞：整份装载时 `baseIndex` 恒为 0，恰好就是真行号。 */
  function lineBaseFromHeader(value) {
    const raw = String(value == null ? "" : value).trim();
    if (!raw) return null;
    const number = Number(raw);
    return Number.isFinite(number) && number >= 1 ? Math.floor(number) : null;
  }

  /* 上面那个数是**1 基行号**，而面板用的是 0 基的 `baseIndex`（行号 = baseIndex + 下标 + 1）。
   * ⚠️ 差一就是“面板第一行总比文件行号大 1”：我第一版就是这样漏了一步，
   *    真浏览器上量一下就看出来了（面板首行 4514679、真值 4514678）。
   */
  function lineBaseIndex(value) {
    const line = lineBaseFromHeader(value);
    return line === null ? null : line - 1;
  }

  /* 1 基证据行号 → 0 基 DOM 下标（`data-src` = “文件里第几行 − 1”，
   * `baseIndex` = 已装载那一段的**第一行**在文件里的 0 基行号）。
   *
   * ⚠️ **两个都要减**：证据行号是 1 基的，而 `baseIndex` 是 0 基的。
   *    下标 = line - 1 - baseIndex（用户口径 2026-10-13）。少减那个 1 就整体**差一行**：
   *    要定位第 1～13 行，实际圈的是第 2～14 行 —— 看起来就是“点定位停在启动日志首行”。
   * 下标 < 0 = 这一行还没装载 / 不在这一段里（调用方跳过它，不假装标上了）。 */
  function evidenceLineIndex(line, baseIndex) {
    const value = Number(line);
    if (!Number.isFinite(value)) return -1;
    return Math.floor(value) - 1 - (Number(baseIndex) || 0);
  }

  /* 事件号 → 行下标（纯函数，宿主回归直接驱它）。
   *
   * 派生视图（工具输出）的行**身份**就是它来自的那条 io 事件：`seqs` 与
   * `lines` 逐行对齐。找不到返回 **-1**（不返回 0 —— 那会假装第一条就是它）。 */
  function seqRowIndex(seqs, eventSeq) {
    const wanted = Math.floor(Number(eventSeq));
    if (!Array.isArray(seqs) || !Number.isFinite(wanted) || wanted < 1) return -1;
    for (let index = 0; index < seqs.length; index += 1) {
      if (Number(seqs[index]) === wanted) return index;
    }
    return -1;
  }

  /* 一条证据引用 → 定位计划（纯函数，宿主回归直接驱动它）：要轻微高亮的**区间**
   * 与要滚到窗口中间的**中心锚点**，两串都是 0 基下标。
   *
   * 锚点优先级（用户口径 2026-10-13）：
   *   ① 后端给的 `anchor_line`（决定性证据行，如 `@TASKS`）—— 但它必须**落在区间内**；
   *   ② 区间**末行** —— 旧记录没有 `anchor_line` 时的诚实回退（也是后端的默认值）；
   *   ③ 区间只有一行时 ② 就是那一行。
   * 坐标无效（行号不是数字 / 小于 1 / 末行在首行之前）返回 `null`：**什么也不做** ——
   * 不得把用户带到与这一段证据无关的位置。 */
  function locatePlan(ref, baseIndex) {
    const first = evidenceLineIndex(ref && ref.start_line, baseIndex);
    const last = evidenceLineIndex(ref && ref.end_line, baseIndex);
    if (first < 0 || last < first) return null;
    let anchor = evidenceLineIndex(ref && ref.anchor_line, baseIndex);
    if (!(anchor >= first && anchor <= last)) anchor = last;
    return { first, last, anchor };
  }

  global.PWModelLocate = Object.freeze({
    FALLBACK_TAIL_BYTES,
    byteText,
    mergeByteRanges,
    tailByteRange,
    previousByteRange,
    windowContract,
    parseContentRangeTotal,
    windowNote,
    prependPlan,
    olderMergePlan,
    lineBaseFromHeader,
    lineBaseIndex,
    evidenceLineIndex,
    seqRowIndex,
    locatePlan,
  });
})(globalThis);
