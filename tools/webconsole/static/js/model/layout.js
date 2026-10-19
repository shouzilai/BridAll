/* 工作台的**几何与滚轮边界**（纯计算）。
 *
 * 这些数决定“点一下阅读位到底能不能对齐、滚轮到边会不会带走整页”：
 *   · `readingPlan()` 只认**一行**布局（多行下把正文拉成一屏高，整块工作区就成了 2～3 屏，
 *     一屏里只看得到第一行）—— 多行一律诚实降级为“只对齐顶部、正文高度不动”；
 *   · `sanitizeHeadHeight()` 挡住后台标签页量出来的垃圾高度（`innerWidth/innerHeight` 为 0）；
 *   · `wheelEdge()` 用 1 px 容差：子像素滚动位置不会差一点就穿过去。
 *
 * 不碰 DOM：几何由组合根量好递进来，这里只算。
 */
(function (global) {
  "use strict";


  const READING_MIN_BODY = 240;
  const READING_BOTTOM_GAP = 4;

  function sanitizeHeadHeight(measured, innerHeight) {
    const value = Math.max(0, Math.round(Number(measured) || 0));
    const cap = Math.max(40, Math.round((Number(innerHeight) || 0) * 0.25));
    return Math.min(value, cap);
  }

  function layoutIsSane(width, height) {
    return Number(width) >= 200 && Number(height) >= 200;
  }

  function readingPlan({
    wsTop, paneOffset = 0, paneChrome = 0, innerHeight, maxScroll, rows = 1,
    minBody = READING_MIN_BODY, bottomGap = READING_BOTTOM_GAP,
  }) {
    const limit = Math.max(0, Math.round(maxScroll));
    const wanted = Math.round(wsTop);
    const target = Math.max(0, Math.min(limit, wanted));
    const topAligned = Math.abs(target - wanted) < 1;        // 顶部真的贴住了吗
    // 滚到 target 之后，当前窗格顶边在视口里的位置：
    const paneTop = paneOffset + (wanted - target);
    const available = Math.round(innerHeight - paneTop - bottomGap);
    const body = Math.round(available - paneChrome);
    /* ⚠️ 用户实测（2026-10-17）：**多行**布局（≤1044 px 的 2×2+1、≤640 px 的单列）下，把一个窗格
     * 的正文拉到"一屏高"，整块工作区就成了 2～3 屏 —— 一屏里永远只看得到第一行，其余各行
     * 被压到屏幕外面（提示却还写着"已对齐"）。所以这里只认**一行**：多行一律走③诚实降级
     * （只对齐顶部，正文高度不动，让页面照常往下滚）。 */
    const rowCount = Math.max(1, Math.round(Number(rows) || 1));
    const fit = rowCount === 1 && Number.isFinite(body) && body >= minBody;
    return { target, topAligned, available, body, fit, rows: rowCount };
  }

  function paneChromeGap(parts) {
    let total = 0;
    for (const part of parts || []) {
      if (!part || part.hidden) continue;
      const height = Number(part.height);
      if (Number.isFinite(height) && height > 0) total += height;
    }
    return Math.round(total);
  }

  function wheelEdge(state, deltaY) {
    if (!deltaY) return null;
    const max = state.scrollHeight - state.clientHeight;
    if (max <= 1) return deltaY > 0 ? 1 : -1;      // 根本滚不动，也算在边界上
    if (deltaY < 0) return state.scrollTop <= 0 ? -1 : null;
    return state.scrollTop >= max - 1 ? 1 : null;  // 容 1 px：子像素滚动位置不会差一点就穿过去
  }

  global.PWModelLayout = Object.freeze({
    READING_MIN_BODY,
    READING_BOTTOM_GAP,
    sanitizeHeadHeight,
    layoutIsSane,
    readingPlan,
    paneChromeGap,
    wheelEdge,
  });
})(globalThis);
