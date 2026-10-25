/* 原始串口「搜索命中」的结果层纯函数（条件 → 行集合，行 → 证据行号/事件号）。
 *
 * 结果层不是第二份正文：每一行都是行模型里的第 N 行（同一份 `source.lines`）。
 *
 * ⚠️ 只有**搜索**进这一层：级别/模块/排除/只看错误/折叠重复这类**改行集合**的条件
 * 直接在终端正文里生效（`terminalRowPlan` 的 hidden 回调）—— 与别的窗口同一种 UI，
 * 不再另开一张“我藏了哪些行”的清单。搜索留在这里，是因为它回答的是“在哪里”
 * （可点着跳回正文），不是“藏了什么”。
 *
 * 不碰 DOM、不发请求、不碰定时器。
 *
 * 依赖：`log-filter.js`（`compileQuery` / `matchRanges` / `viewItems`）。
 * 装载顺序：sources → format → log-filter → … → 本文件 → … → app
 */
(function (global) {
  "use strict";

  const filter = global.PWModelLogFilter;
  if (!filter) {
    throw new Error("PWModelLogFilter 必须先于 PWModelRawResults 装载");
  }
  const { compileQuery, matchRanges, viewItems } = filter;

  /* 结果层最多列多少行（超出的只在 `total` 里计数，并在界面上说“还有多少条”）。 */
  const RAW_RESULT_MAX = 200;

  /* 结果层现在按哪些条件算（空表 = 这一层不出现）。纯函数：宿主回归直接喂 view。 */
  function resultCriteria(view) {
    if (!view) return [];
    const out = [];
    if (String(view.query || "").trim()) out.push(`搜索“${view.query}”`);
    return out;
  }

  /* 搜索结果的行（纯函数）：命中的行 + 命中区间 + 行号/事件号。
   * 返回 `{ rows, total, capped }` —— `total` 是真正命中的条数（可能大于列出来的）。 */
  function rawResultRows(source, view, limit = RAW_RESULT_MAX) {
    const out = { rows: [], total: 0, capped: false };
    if (!source || !view) return out;
    const { re, error } = compileQuery(view);
    if (!String(view.query || "").trim() || error || !re) return out;
    const seqs = source.seqs || null;
    for (const item of viewItems(source)) {
      const text = source.lines[item.index];
      const ranges = matchRanges(re, text);
      if (!ranges.length) continue;
      out.total += 1;
      if (out.rows.length >= limit) continue;
      const seq = seqs ? seqs[item.index] : null;
      out.rows.push({
        index: item.index,
        line: source.baseIndex + item.index + 1,
        text,
        ranges,
        seq: seq === null || seq === undefined ? null : seq,
      });
    }
    out.capped = out.total > out.rows.length;
    return out;
  }

  /* 结果层的签名：条件或内容变了才重建（这一层挂在 2 s 的刷新路径上）。 */
  function rawResultSignature(source, view) {
    if (!source || !view) return "";
    return [
      view.query, view.regex ? "re" : "lit", view.level, view.module,
      view.exclude,
      /* ⚠️ 排除的**匹配方式**必须进签名（用户实测：勾了「.*」以后结果层一条都没变）——
       * 签名不变宿主的重建就直接返回，列表还是旧的那份。同理 `excludeOff` 也要在。 */
      view.excludeRegex ? "re" : "lit",
      view.excludeOff ? "off" : "on", view.errorsOnly ? "E" : "", view.dedupe ? "D" : "",
      source.baseIndex, source.lines.length,
    ].join("|");
  }

  /* 结果层上的三句文案（标题、命中计数、每行的“跳到哪”）。 */
  function rawResultTitle(criteria) {
    const parts = criteria || [];
    return parts.length ? `结果 ${parts.join(" · ")}` : "";
  }

  function rawResultNote(result) {
    if (!result || !result.total) return "没有命中的行";
    return `${result.total} 条命中${result.capped ? `（只列前 ${RAW_RESULT_MAX} 条）` : ""}`;
  }

  function rawResultWhere(row) {
    if (!row) return "";
    return row.seq === null || row.seq === undefined
      ? `第 ${row.line} 行`
      : `#${row.seq} · 第 ${row.line} 行`;
  }

  global.PWModelRawResults = Object.freeze({
    RAW_RESULT_MAX, resultCriteria, rawResultRows, rawResultSignature,
    rawResultTitle, rawResultNote, rawResultWhere,
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
