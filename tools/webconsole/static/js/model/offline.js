/* 离线诊断 / 会话回看那一屏的**纯选择器**。
 *
 * 这一屏最容易出的错是“面板说的是 A，窗口里装的是 B”：
 *   · 已经装进四个窗口的那个会话，**优先于**下拉框此刻的值（装好了就是装好了）；
 *   · “当前会话”在没有 `session` 时回退到 `last`（刚采到的证据不该因为点了断开就下载不了），
 *     但**只回退到传进来的那一份快照**，不偷看全局 `app.observe` —— 否则判据会被
 *     “此刻全局恰好是什么”影响。
 *
 * 不碰 DOM、不发请求、不读存储。
 *
 * 装载顺序：sources → format → … → observe → 本文件 → say → … → app
 */
(function (global) {
  "use strict";

  /* 当前（或最近一次）会话：下载链接与窗口内容都靠它（纯函数：只认传进来那一份）。
   * 会话结束后回退到 `last`：刚采到的证据不应该因为点了“断开”就下载不了。 */

  /* 离线面板此刻指的是**哪一个**会话。
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

  /* 详情里“这几份证据太大没有随包带回”的窗口 → 那段内容去哪了。 */
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

  /* 诊断包按钮那句“这是谁的包”与“它不含什么”。 */
  function bundleLabel(target) {
    const id = target && target.sessionId;
    return id ? `导出诊断包 · ${id}` : "导出诊断包";
  }

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

  /* 离线面板上那几句话（状态行、一句说明、“装载”按钮的字、导入包那一句）。
   *
   * `review` = 已经装载的那份会话详情，`row` = 清单里选中的那一行，`sessionId` = 此刻指的会话号。
   * 装载与否只看**会话号对不对**（换了下拉框但没点装载，就不算已装载）。 */

  global.PWModelOffline = Object.freeze({
    offlineSelectedId, omittedWindows, bundleLabel, bundleScopeNote
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
