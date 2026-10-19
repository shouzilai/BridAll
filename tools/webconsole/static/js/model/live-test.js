/* 受控测试那一条路（受控用例 / L4b）的**纯文案**。
 *
 * 页面在这条路上只做三件事：把**服务端给的目录**画出来、把选中的用例交上去、显示
 * 这一批跑到哪了。能不能跑、为什么不能，全在服务端（`live_test_catalog` 的逐条 `reason`）
 * —— 页面照抄那句话，不自己判兼容性、不自己拼命令、也不自己估耗时（需求 §8）。
 * 刷新不重复执行：页面**从不**自动启动，状态全部来自 `/api/state`。
 *
 * 用户口径（2026-10-12 晚）：屏幕上**只留“跑着：…”**那一句（那件事需要人立刻知道）；
 * 收口之后不再在屏幕上写「已收口：PASS（…）」—— 它是**总结**，而“结果”已经在每张阶段卡上。
 * ⚠️ 所以 `text` 是“屏幕上那一句”，`full` 是“完整那一句”（含收口总结）——
 *    调用方不能拿 `full` 当屏幕文案。
 *
 * 不碰 DOM、不发请求。
 *
 * 装载顺序：sources → … → history → 本文件 → command → transport → app
 */
(function (global) {
  "use strict";

  /* 一条用例后面那点说明：约多久、要不要逐次确认。
   * ⚠️ 不可选的理由（服务端给的 `screen_reason`）不进屏幕文案（用户口径 2026-10-18：
   *    那是解释性内容，只挂在勾选框悬停里 —— 屏幕上只留“能不能点”本身）。 */
  function liveTestCaseMeta(row) {
    const bits = [];
    if (Number(row.expected_duration_s) > 0) bits.push(`约 ${Number(row.expected_duration_s)} s`);
    if (row.destructive) bits.push("破坏性：要服务端逐次确认");
    /* 用户口径（2026-10-13）：用例不可用时屏幕上只有一句人话「当前设备不支持此用例」——
     * 它由**服务端**给（`screen_reason`，能力类的原因统一收成那一句）；具体卡在哪条能力要求
     * （某个 `cap.`/`device.` 键）留在 API / 证据里，**页面一个都不认识**、也不许出现在这里。 */
    if (!row.selectable) bits.push(row.screen_reason || row.reason || "现在不能选");
    return bits.join(" · ");
  }

  /* 这一批跑到哪了（纯函数：模型回归直接喂它）。 */
  function liveTestStatusSay(snapshot) {
    if (!snapshot || !snapshot.batch) return { text: "", full: "" };
    const batch = snapshot.batch;
    const rows = Array.isArray(batch.executions) ? batch.executions : [];
    const running = rows.find((row) => row.status === "RUNNING");
    if (String(batch.status) === "RUNNING" && running) {
      const stage = (running.steps || []).find((step) => step.status === "running");
      const where = stage ? ` · ${stage.label}（${(Number(stage.duration_ms) / 1000).toFixed(1)} s）` : "";
      const text = `跑着：${running.case_id}${where}`;
      return { text, full: text };
    }
    const counts = (batch.summary && batch.summary.counts) || {};
    const said = Object.entries(counts).map(([key, value]) => `${key} ${value}`).join(" / ");
    const tail = [];
    if (said) tail.push(said);
    if (batch.reason) tail.push(batch.reason);
    return {
      text: "",
      full: `已收口：${batch.result || batch.status}${tail.length ? `（${tail.join(" · ")}）` : ""}`,
    };
  }

  /** 完整那一句（给悬停与证据口径用；**不再上屏幕**）。 */
  function liveTestStatusText(snapshot) {
    return liveTestStatusSay(snapshot).full;
  }

  /* 会话冻结块 → 阶段行（离线回放的**素材**），以及“这一阶段定位在哪”。 */
  function frozenLiveTestSteps(block) {
    if (!block || typeof block !== "object") return [];
    const rows = [];
    for (const execution of (Array.isArray(block.executions) ? block.executions : [])) {
      const steps = Array.isArray(execution && execution.steps) ? execution.steps : [];
      for (const step of steps) {
        if (step && step.case_id) rows.push(step);
      }
    }
    if (rows.length) return rows;
    for (const definition of (Array.isArray(block.definitions) ? block.definitions : [])) {
      const steps = Array.isArray(definition && definition.steps) ? definition.steps : [];
      for (const step of steps) {
        if (!step || !step.stage_id) continue;
        rows.push({
          kind: "test_step", case_id: step.stage_id, version: step.version,
          label: step.label, expected: step.expected, status: "pending",
        });
      }
    }
    return rows;
  }

  function describeRawRef(row) {
    const refs = row.evidence_refs || [];
    const raw = refs.find((item) => item.source === "raw");
    const others = refs.filter((item) => item.source !== "raw").length;
    if (!raw) {
      // 需求 §8.4：旧包/没补写的时候要说清“没有定位信息”，而不是留一个空位让人猜。
      return others ? "定位：这一阶段没有原始串口行号" : "定位：这份证据里没有定位信息";
    }
    return `定位：原始串口第 ${raw.start_line}–${raw.end_line} 行`;
  }

  /** 终态与状态词的唯一出处（与后台 `step_lifecycle.TERMINAL_STATUSES` 同一口径）。 */
  const STEP_STATUS_TEXT = {
    pending: "等待", running: "进行中", PASS: "通过", FAIL: "失败",
    ERROR: "出错", SKIPPED: "跳过", CANCELLED: "已取消",
  };

  /* **一张阶段卡上的每一句话**（状态词与耗时、非阻断、编号与版本、
   * 预期/现状/证据/定位、以及“结果关系”那条说明）。
   *
   * 入参显式：`selection` 是这一轮 run 勾选的用例 id（判“非阻断”要用），`nowMs` 是
   * 调用方读到的当前时刻（模型不读时钟）。返回**已经拼好的字符串**，页面只负责写进节点。 */

  /* 自动验证用例表里那三段文案。 */


  /* “确认覆盖”那一段：区域与服务端声明的一致；没声明就说没声明（不猜 0 地址）。 */

  global.PWModelLiveTest = Object.freeze({
    liveTestCaseMeta, liveTestStatusSay, liveTestStatusText, frozenLiveTestSteps,
    describeRawRef, STEP_STATUS_TEXT
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
