/* **事件 → 窗口**的唯一映射（纯模型）。
 *
 * 用户口径（2026-10-17）：五个窗口的职责是**冻结**的，装配顺序必须是
 *
 *     SSE / EventBus → eventToSource() → 窗口 id → 那一窗的模型 → DOM
 *
 * 以前是 `switch (event.kind)` 里“认得的就画、认不得的 default: break”，而别处又会把
 * “没有来源的东西”当成工具输出 —— 设备正文（`msh >` / `trial` / `command not found`）
 * 于是从好几条路一起进了「工具输出」。现在只有这一张表说了算。
 *
 * ⚠️ “哪一**屏**”（自动验证 / 实时观察 / 离线诊断）是另一件事，由组合根的 `laneStore()` 回答。
 * ⚠️ `control` = 事件是真的、但它的正文不落任何窗（状态/消息/阶段卡由别的入口画）。
 *    它们**不是**未分类。
 *
 * 装载顺序：sources → … → stream → 本文件 → transport → app
 */
(function (global) {
  "use strict";

  const EVENT_WINDOW_BY_KIND = Object.freeze({
    // —— 落正文的五窗（注意 `term` 与 `raw` 同一窗：终端投影是「原始串口」的内容） ——
    tool: "tool",
    raw: "raw",
    term: "raw",
    decoded: "decoded",
    app_events: "events",
    test_step: "stage",
    test_step_refs: "stage",
    // —— 控制类（正文不落窗） ——
    run_started: "control",
    run_finished: "control",
    step: "control",
    live_test: "control",
    "live_test.device": "control",   // 设备侧执行事件：页面按 `/api/state` 画，不落窗
    replay: "control",
    panic: "control",
    assert: "control",
    info: "control",
    error: "control",
    observe_state: "control",
    request_rejected: "control",
  });

  /* 一条事件属于哪一窗（纯函数）：`{ source, reason }`。
   * `source === null` ⇒ 未分类（不进任何窗）；`reason` 是给人看的原因。 */
  function eventToSource(event) {
    const kind = String((event && event.kind) || "");
    if (!kind) return { source: null, reason: "事件没有 kind，无法判断属于哪个窗口" };
    if (Object.prototype.hasOwnProperty.call(EVENT_WINDOW_BY_KIND, kind)) {
      return { source: EVENT_WINDOW_BY_KIND[kind], reason: "" };
    }
    return {
      source: null,
      reason: `kind=${kind} 没有映射到任何窗口（未分类：不进任何窗，也不兜底到工具输出）`,
    };
  }

  /* 阶段事件该落到哪一屏（实时观察 vs 自动验证）—— 只看事件来源。 */
  function stageEventMode(event) {
    const origin = String((event && event.origin) || "run");
    return origin === "live" ? "live" : "auto";
  }

  global.PWModelEventRoute = Object.freeze({
    EVENT_WINDOW_BY_KIND,
    eventToSource,
    stageEventMode,
  });
})(globalThis);
