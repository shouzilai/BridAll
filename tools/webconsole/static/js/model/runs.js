/* 自动验证与历史会话的**纯判据**（“能不能启动”“这一轮选了哪些用例”
 * “这次会话算哪种状态”）。这里不碰 DOM：按钮灰不灰、提示写在哪，都由组合根决定。
 *
 * 装载顺序：sources → … → 本文件 → transport → app
 */
(function (global) {
  "use strict";

  /* “启动自动验证”现在能不能按、按不下去还差什么。
   *
   * 用户的现场原话是：点了“启动自动验证”**没有反应**。三个原因都真的存在过：
   *  1. 那个口正被实时观察拿着 —— 按钮本来是灰的，原因只在 tooltip 里；
   *  2. 所选固件会覆盖 Flash 而“逐次确认”没勾 —— 按钮**看着能按**，点完只往四个窗口
   *     下面的 `#runMessage` 写一行（那个位置在按钮上面一屏之外，屏幕上什么都没变）；
   *  3. 浏览器把 `confirm()` 吞掉了 —— 代码里 `if (!confirmed) return;`，一句都不说。
   *
   * 所以这里把“为什么”算成一份**可以显示**的东西：`blocked`（按钮灰掉）+ `hint`
   * （就在按钮旁边的一行话）+ `reason`（tooltip）。纯函数：宿主模型回归直接喂它组合。
   */
  function startReadiness(facts, options) {
    const spec = facts || {};
    const state = options || {};
    const missing = !spec.target || !spec.firmware || !spec.suite;
    if (state.running) {
      return { blocked: true, hint: "", reason: "已有 run 正在执行：等它结束或先取消" };
    }
    if (state.mode !== "auto") {
      return { blocked: true, hint: "", reason: "只有“自动验证”模式能启动 run：实时观察不编译、不烧录" };
    }
    if (state.portBlocked) {
      return {
        blocked: true,
        hint: "",
        reason: `串口被实时观察会话占用（${state.ownPort || "?"}）：先在实时观察里断开再启动`,
      };
    }
    if (missing) {
      /* 用户口径（2026-10-13）：身份未知、而这台设备上**可能烧录两份以上**固件时，
       * “烧哪一份”只能由人定（实时观察那条路不受影响 —— 它不烧录、也不靠身份进门）。
       * 那一栏在 `renderRunFirmwares()` 里已经留空（不替使用者挑），这里的提示就是
       * **为什么按钮按不下去**的那句话。 */
      if (!spec.firmware) {
        return {
          blocked: true,
          hint: "请选择要烧录的固件",
          reason: "请选择要烧录的固件：身份未识别时平台不替你决定烧哪一份",
        };
      }
      return { blocked: true, hint: "", reason: "先选好目标 / 固件 / 套件" };
    }
    if (spec.firmware.requires_flash_confirmation && !state.confirmFlash) {
      return {
        blocked: false,
        hint: "还差一步：勾选“我确认覆盖设备 Flash”（就在上面那个选项里）",
        reason: "所选固件会覆盖设备 Flash：每次运行都要重新确认",
      };
    }
    return { blocked: false, hint: "", reason: "" };
  }

  /* 这一轮 run 选了哪些受控测试（`case_id` 列表）。
   *
   * 读音要宽容：`manifest.tests.selection` 可能是 `[{case_id}, …]`，也可能是一串
   * 字面 id；认不出就是空表 —— **空表 = 没选**（于是阶段失败一律非阻断），
   * 这与“选择还在、只是形状没认出来”在屏幕上是一句话，所以宁可如实说没选。
   */

  /* 一次观察会话算哪种状态：`broken`（读不出来）/ `live`（还在写）/ `closed` / `open`（没有结束记录）。 */
  function sessionStateOf(row) {
    if (!row) return "broken";
    if (row.broken) return "broken";
    if (row.live) return "live";
    return row.complete ? "closed" : "open";
  }

  /* 会话持续时间 = manifest 记的开始/结束时刻之差；还在写的那次 = 开始到现在（`now`）。
   * ⚠️ 不拿 `totals` 编一个数（那份里根本没有时长）—— 时间戳本身就是记录下来的事实。 */
  function sessionDurationMs(row, now) {
    const started = Date.parse(String((row && row.opened_utc) || ""));
    if (!Number.isFinite(started)) return null;
    const closedRaw = row && row.closed_utc;
    const closed = closedRaw ? Date.parse(String(closedRaw)) : Number(now);
    if (!Number.isFinite(closed)) return null;
    return Math.max(0, closed - started);
  }

  /* 设备 / 通信口：先给设备（自报的标签），再给口与波特率。都没有就如实说“未知”。 */
  function sessionDeviceText(row) {
    const device = (row && (row.device_label || row.device)) || "";
    const port = (row && row.port) || "";
    const baud = row && row.baud ? ` @ ${row.baud}` : "";
    if (!device && !port) return "未知";
    return [device || "", port ? `${port}${baud}` : ""].filter(Boolean).join(" · ");
  }

  /* ------------------------------------------- 这一次 run 记下的设备（纯文案） */

  /* 与 core 的 `IDENTITY_STATES` 一一对应。措辞说的是**这一次问到的结果**，
   * 不是“这块设备坏了” —— “没问到”是一类正常设备（空板/旧固件），首烧路径要从这里过。 */
  const SNIFF_IDENTITY_LABEL = {
    confirmed: "已确认身份",
    unknown: "没问到身份",
    conflict: "与预期不符",
    unqueryable: "问不了身份",
  };

  /* 一句话：**回放只看它自己那份 manifest**。当前嗅探结果与当前台架配置一律不得参与改写 ——
   * 否则“上一次跑的是哪一块板”就永远说不清了（需求 §7）。
   *
   * 三种情形分开写，不混成一句：
   *  · 新 run（有 `device` 块）⇒ 报**当时记下的**序号与口；
   *  · 旧 run（没有）⇒ 如实说“按旧格式回放”，并把当时记下的 `target`/`port` 摆出来；
   *  · 两者都没有 ⇒ 说“这一趟没记下设备”，不编一个。 */
  function recordedDeviceText(recorded) {
    const row = recorded || {};
    const port = String(row.port || "");
    if (!port && !row.device_key && !row.target_id) return "设备：这一趟没有记下设备";
    if (row.legacy) {
      return `设备：按旧格式回放（${row.device_key || "?"}${port ? " · " + port : ""}）`
        + "—— 当时没记下设备序号与身份，不补";
    }
    const num = Number(row.scan_seq) ? `#${Number(row.scan_seq)} ` : "";
    const state = (row.identity_before || {}).state || "";
    const label = SNIFF_IDENTITY_LABEL[state] || state;
    return `设备：${num}${port || "?"}${label ? `（${label}）` : ""}`;
  }

  /* 历史表那一格里的小徽标：新 run 报“#3 COM3”，旧 run 报“旧格式”（悬停看详情）。 */
  function recordedDeviceBadge(recorded) {
    const row = recorded || {};
    if (row.legacy) return "旧格式";
    const num = Number(row.scan_seq) ? `#${Number(row.scan_seq)}` : "设备";
    return `${num}${row.port ? " " + row.port : ""}`;
  }

  /* ------------------------------------------- 启动前那一行摘要与设备行 */

  /* 「本次将执行：…」——只转述当前选择与采集时长/复位勾选，不触发任何动作。
   * `input` = `{ card, firmwareLabel, suite, seconds, reset }`；没选固件或套件就返回空串
   * （调用方自己说“先选好固件与套件”）。 */

  /* 这次 run 要动的设备，给人看的那一行（确认框与提示都用它）。
   * ⚠️ 没选卡片时**不说“未声明端口”**（那是把“没点名”说成了“设备没说”，会让人
   *    以为要去查设备声明）：而是明说这次按**本机登记**的端口走。 */
  function runDeviceLine() {
    const card = app.selectedCard;
    if (card) {
      const stateLabel = SNIFF_IDENTITY_LABEL[card.state] || card.state || "未知";
      return `设备：#${card.num} ${card.port}（${stateLabel}）`;
    }
    const fallback = (selectedRunFacts().target || {}).transport?.port || "";
    return `设备：未点名（按本机登记的端口 ${fallback || "未登记"} 走）`;
  }

  /* ------------------------------------------- 阶段卡里“失败”与“结果”的关系 */

  /* 阶段失败到底算不算这一轮的结果：只有它在这一轮 run 的受控测试选择里才算。
   * `selection` 是 `manifest.tests.selection` 读出来的用例 id 列表（`testSelectionIds`）。 */

  /* 这一屏的阶段卡里“失败”与“结果”的关系（验收 2026-10-17 第 2 条）。
   *
   * run 的结果来自 `report.assertions`；阶段卡来自受控测试（`test_step`），那是**另外**
   * 发起的一批，可以落在同一个会话里。两者可以同时为真，但屏幕上必须说清“这个失败算不算
   * 结果”——否则“PASS · 断言 5/5”与“某个阶段失败”并排出现，读者只能猜。
   * 判据只有一条：失败的那条**在不在这一轮 run 的受控测试选择里**。
   * 不在 ⇒ 它是诊断，不改结果，标“非阻断”；在 ⇒ 它就是判据的一部分。 */

  /* 为什么“阶段失败”与“结果 PASS”能同时成立 —— 这句话只进悬停，不占屏幕。 */

  /* 阶段/run 行上的两个纯计算 —— 证据引用的并集与“跑了多久”。
   * 引用顺序钉死（tool → raw → decoded → app_events），合并时**不许丢锚点**
   * （丢了就退回“区间末行”，而补写那一份正是带锚点的）。 */
  const EVIDENCE_REF_ORDER = ["tool", "raw", "decoded", "app_events"];

  function mergeEvidenceRefs(base, patch) {
    const bySource = new Map();
    for (const item of [...(base || []), ...(patch || [])]) {
      const source = String((item && item.source) || "");
      if (!EVIDENCE_REF_ORDER.includes(source)) continue;
      const anchor = Number(item.anchor_line);
      bySource.set(source, {
        source,
        start_line: Number(item.start_line),
        end_line: Number(item.end_line),
        /* ⚠️ 中心锚点**合并时不能丢**（用户口径 2026-10-13）：丢了就退回“区间末行”，
           而补写那一份正是带锚点的（`@TASKS` 这类决定性证据行）。旧记录本来就没有这个键
           ⇒ 这里也不写，前端自己按末行回退。 */
        ...(Number.isFinite(anchor) ? { anchor_line: anchor } : {}),
      });
    }
    return EVIDENCE_REF_ORDER.filter((name) => bySource.has(name)).map((name) => bySource.get(name));
  }

  function liveDurationMs(row, nowMs) {
    if (row.status !== "running" || !row.started_utc) return row.duration_ms || 0;
    const started = Date.parse(row.started_utc);
    if (Number.isNaN(started)) return row.duration_ms || 0;
    return Math.max(0, nowMs - started);
  }

  /* 断言按 domain 汇总（总数/通过数/失败的那几条 id）。 */
  function summarizeDomains(rows) {
    const buckets = new Map();
    for (const row of rows || []) {
      const name = String((row && row.domain) || "");
      if (!name) continue;
      const bucket = buckets.get(name) || { domain: name, total: 0, passed: 0, failed: [] };
      bucket.total += 1;
      if (row.status === "pass") bucket.passed += 1;
      else bucket.failed.push(row.id);
      buckets.set(name, bucket);
    }
    return Array.from(buckets.values());
  }

  global.PWModelRuns = Object.freeze({
    startReadiness,
    sessionStateOf,
    sessionDurationMs,
    sessionDeviceText,
    SNIFF_IDENTITY_LABEL,
    recordedDeviceText,
    recordedDeviceBadge,
    runDeviceLine,
    EVIDENCE_REF_ORDER, mergeEvidenceRefs, liveDurationMs,
    summarizeDomains,
  });
})(globalThis);
