/* 模式化历史列表（RUNTIME_MODES §12）的**纯文案与纯判据**。
 *
 * 三种模式共用同一套列表组件，但数据源、字段与证据语义必须分开：
 *   自动验证 → **历史运行**（runs/）；实时观察 → **历史观察**（会话清单）；
 *   离线诊断 → **诊断会话**（同一份清单，含导入的诊断包）。
 *
 * ⚠️ 观察会话**不包装成 run**：它没有 report.json / steps.jsonl，也不该被读成一次自动验证。
 * ⚠️ TX/RX **缺失就说“未知”**，不用 0、空白或 PASS 顶替（§12 明令）：`totals` 是会话结束时
 *    才写的，进行中那一次只有文件大小可看 —— 两种来源必须说得出区别。
 * ⚠️ 空目录 / 加载中 / 接口失败三种状态各有各的话，不冒充加载成功（§12.1 第 4 条）。
 *
 * 行构造（`runHistoryRows` / `sessionHistoryRows`）与 `historyModel()` 仍是**视图装配**，
 * 留在组合根等 M8；这里只给它们喂的文字与判据。不碰 DOM。
 *
 * 依赖：`format.js`（`escapeHtml`）。
 *
 * 装载顺序：sources → format → … → runs → 本文件 → command → transport → app
 */
(function (global) {
  "use strict";

  const format = global.PWModelFormat;
  if (!format) {
    throw new Error("PWModelFormat 必须先于 PWModelHistory 装载");
  }
  const { escapeHtml, formatDuration, shortTime } = format;
  const runs = global.PWModelRuns;
  if (!runs) {
    throw new Error("PWModelRuns 必须先于 PWModelHistory 装载");
  }
  const { sessionStateOf, sessionDurationMs, sessionDeviceText, recordedDeviceText, recordedDeviceBadge } = runs;

  /* 会话的“身份”说法（决策 chip 与回看总结共用同一套词）住在 `identity.js`（它先于本文件装载）。 */
  const identity = global.PWModelIdentity;
  if (!identity) {
    throw new Error("PWModelIdentity 必须先于 PWModelHistory 装载");
  }
  const { IDENTITY_TEXT } = identity;

  /* 一次 run 的证据清单：**必产物**永远列（缺了就是缺了，链接会 404 得明明白白），
   * 受控测试的产物只在真有的时候才列。 */
  const MANDATORY = [
    ["manifest.json", "manifest"],
    ["raw.bin", "raw"],
    ["decoded.log", "decoded"],
    ["report.json", "report"],
    ["app_events.jsonl", "events"],
    ["metrics.jsonl", "metrics"],
  ];
  const TEST_EVIDENCE = [
    ["test_results.jsonl", "tests"],
    ["test_input.bin", "input"],
    ["test_readback.bin", "readback"],
  ];

  /* 会话状态：**只说这一份记录的状态**，不用 PASS/FAIL 那套 run 语义（会话说的是观察，
   * 不是“测试通过”）。 */
  const HISTORY_SESSION_STATES = {
    live: { text: "观察中", kind: "live" },
    closed: { text: "已结束", kind: "closed" },
    open: { text: "没有结束记录", kind: "open" },
    broken: { text: "读不出来", kind: "broken" },
  };

  const MODE_IDS = ["auto", "live", "offline"];

  const HISTORY_TITLES = { auto: "历史运行", live: "历史观察", offline: "诊断会话" };
  const HISTORY_HINTS = {
    auto: "最近 10 次运行（自己滚）",
    live: "点一行去离线诊断回看这次会话",
    offline: "本机会话与导入的诊断包；点一行回看",
  };
  const HISTORY_COLUMNS = {
    auto: ["run", "结果", "目标", "发起者", "耗时", "开始时间（北京时间）", "证据"],
    live: ["会话", "状态", "设备/通信口", "持续时间", "开始时间（北京时间）", "TX/RX", "结束原因", "证据"],
    offline: ["会话", "状态", "设备/通信口", "持续时间", "开始时间（北京时间）", "TX/RX", "结束原因", "证据"],
  };
  const SESSION_EVIDENCE_LINKS = [
    ["manifest.json", "manifest"],
    ["raw.bin", "raw"],
    ["io_events.jsonl", "events"],
    ["decoded.log", "decoded"],
    ["app_events.jsonl", "app_events"],
  ];

  function historyTitle(mode) { return HISTORY_TITLES[mode] || HISTORY_TITLES.auto; }
  function historyHint(mode) { return HISTORY_HINTS[mode] || HISTORY_HINTS.auto; }
  function historyColumns(mode) { return (HISTORY_COLUMNS[mode] || HISTORY_COLUMNS.auto).slice(); }

  /* 会话的收发字节数：**先看 totals（会话结束时写的），没有就退到文件大小**。
   * 两种来源必须说得出区别：它们不是同一个数，也不该被说成一个数。 */
  function sessionRowCounts(row) {
    const totals = (row && row.totals) || {};
    const sizes = (row && row.counts) || {};
    const hasTotals = totals.rx_bytes !== undefined && totals.rx_bytes !== null;
    const value = (fromTotals, fromFiles) => (fromTotals !== undefined && fromTotals !== null
      ? fromTotals
      : (fromFiles === undefined ? null : fromFiles));
    return {
      fromTotals: hasTotals,
      rx: value(totals.rx_bytes, sizes.raw_bytes),
      rxChunks: hasTotals ? (totals.rx_chunks ?? null) : null,
      tx: hasTotals ? (totals.tx_bytes ?? 0) : null,
      decodedLines: hasTotals ? (totals.decoded_lines ?? 0) : null,
    };
  }

  function sessionTrafficText(row) {
    const counts = sessionRowCounts(row);
    const rx = counts.rx === null ? "未知" : `${counts.rx} B${counts.fromTotals ? "" : "（文件）"}`;
    const tx = counts.tx === null ? "未知" : `${counts.tx} B`;
    return `RX ${rx} · TX ${tx}`;
  }

  function sessionEndReasonText(row) {
    if (!row || row.broken) return "未知";
    if (row.live) return "不适用（进行中）";
    const reason = row.end_reason;
    if (reason) return String(reason);
    return row.complete ? "未知" : "没有收尾记录";
  }

  function sessionOriginBadge(row) {
    return row && row.origin === "import" ? ' <span class="muted">（导入）</span>' : "";
  }

  function sessionEvidenceHtml(row) {
    const files = new Set((row && row.files) || []);
    const links = SESSION_EVIDENCE_LINKS
      .filter(([name]) => files.has(name))
      .map(([name, label]) => `<a href="/api/observe/sessions/${encodeURIComponent(row.session_id)}`
      + `/evidence/${name}?download=1">${label}</a>`);
    return links.length ? links.join("") : '<span class="muted">—</span>';
  }

  /* 清单里的一行（纯函数：宿主回归直接驱动文案）。
   * ⚠️ 导入来的会话必须一眼就看出来：“别人给的包”与“自己在现场采的”不能长得一样。 */
  function sessionOptionLabel(row) {
    const counts = sessionRowCounts(row);
    const parts = [row.session_id + (row.origin === "import" ? "（导入）" : "")];
    if (row.port) parts.push(row.port);
    if (counts.rx !== null) {
      parts.push(`RX ${counts.rx} B${counts.fromTotals ? "" : "（文件）"}`);
    }
    if (counts.tx) parts.push(`TX ${counts.tx} B`);
    if (counts.decodedLines === null) parts.push("还没结束");
    else parts.push(counts.decodedLines ? `解码 ${counts.decodedLines} 行` : "没有解码");
    if (row.live) parts.push("进行中");
    else if (!row.complete) parts.push("没有结束记录");
    return parts.join(" · ");
  }

  /* 导入会话的核对结果（纯函数）：**核对过**与**没得核对**是两件事。 */
  function importNote(row) {
    const record = row && row.import;
    if (!record) return "";
    const parts = [
      `${record.imported_utc ? shortTime(record.imported_utc) : "时间未记录"} 从 `
        + `${record.imported_from || "未知文件"} 导入`,
    ];
    if (record.verified) parts.push(`包内声明的 sha256 逐份核对通过（${(record.checks || []).length} 份）`);
    else parts.push("包里没有可核对的 sha256：只能确认它是个结构合法的包");
    if ((record.warnings || []).length) parts.push(`注意：${(record.warnings || []).join("；")}`);
    return parts.join(" · ");
  }

  /* 空 / 加载中 / 接口失败：三种状态各有各的话，**不冒充加载成功**（§12.1 第 4 条）。 */  function historyNote(mode, state, error, count) {
    if (state === "error") return `读不到${mode === "auto" ? " run 列表" : "会话清单"}：${error || "接口失败"}`;
    if (state === "loading") return "加载中…";
    if (!count) {
      return mode === "auto"
        ? "还没有任何 run 目录"
        : "本机还没有会话：先做一次实时观察，或导入一个诊断包";
    }
    return "";
  }

  /* ---------------------------------------------------------------- 行构造 */

  /* 一次 run 的行：run / 结果 / 目标 / 发起者 / 耗时 / 开始 / 证据。
   * ⚠️ 被记下的设备用 `recordedDeviceBadge()` 报“#3 COM3 / 旧格式”，悬停给全文 ——
   *    回放只看它自己那份 manifest，当前嗅探结果不许参与。 */
  function runHistoryRows(runs) {
    return (runs || []).map((run) => {
      const available = new Set(run.artifact_names || []);
      const evidenceLinks = MANDATORY.concat(
        TEST_EVIDENCE.filter(([name]) => available.has(name))
      );
      const links = evidenceLinks.map(
        ([name, label]) =>
          `<a href="/api/runs/${encodeURIComponent(run.run_id)}/evidence/${name}?download=1">${label}</a>`
      ).join("");
      const recorded = run.device
        ? ` <span class="lock" title="${escapeHtml(recordedDeviceText(run.device))}">${escapeHtml(recordedDeviceBadge(run.device))}</span>`
        : "";
      return {
        action: { kind: "run", id: run.run_id },
        cells: [
          `<span class="run">${escapeHtml(run.run_id)}</span>`
            + ` <a href="#run=${encodeURIComponent(run.run_id)}" title="回看本次证据">回看</a>`,
          `<span class="badge" data-result="${escapeHtml(run.result || "unknown")}">${escapeHtml(run.result || "unknown")}</span>`,
          `${escapeHtml(run.target || "—")} · ${escapeHtml(run.port || "—")}${run.suite ? " · " + escapeHtml(run.suite) : ""}${recorded}`,
          escapeHtml(run.client_label || "—"),
          escapeHtml(formatDuration(run.duration_ms)),
          escapeHtml(shortTime(run.started_utc)),
          `<div class="links">${links}</div>`,
        ],
      };
    });
  }

  /* 一次观察会话的行。⚠️ 观察会话**不包装成 run**：点一行走的是现有的离线装载链。 */
  function sessionHistoryRows(sessions, now) {
    return (sessions || []).map((row) => {
      const state = HISTORY_SESSION_STATES[sessionStateOf(row)];
      return {
        action: { kind: "session", id: row.session_id },
        cells: [
          `<span class="run">${escapeHtml(row.session_id)}</span>${sessionOriginBadge(row)}`
            + ` <a href="#" data-history-action="session" data-history-id="${escapeHtml(row.session_id)}"`
            + ' title="回看这次会话">回看</a>',
          `<span class="badge" data-state="${state.kind}">${state.text}</span>`,
          escapeHtml(sessionDeviceText(row)),
          escapeHtml(formatDuration(sessionDurationMs(row, now))),
          escapeHtml(shortTime(row.opened_utc)),
          escapeHtml(sessionTrafficText(row)),
          escapeHtml(sessionEndReasonText(row)),
          `<div class="links">${sessionEvidenceHtml(row)}</div>`,
        ],
      };
    });
  }

  /* 三种模式共用的一份列表模型（纯函数）：给定模式与数据，说出标题、提示、列、行和状态行。 */
  function historyModel(mode, view) {
    const spec = view || {};
    const kind = MODE_IDS.includes(mode) ? mode : "auto";
    const columns = historyColumns(kind);
    const rows = kind === "auto"
      ? runHistoryRows(spec.runs || [])
      : sessionHistoryRows(spec.sessions || [], spec.now);
    const state = spec.state || "ready";
    return {
      mode: kind,
      title: historyTitle(kind),
      hint: historyHint(kind),
      columns,
      rows,
      state,
      note: historyNote(kind, state, spec.error, rows.length),
    };
  }


  /* 会话清单的一行、回看面板那一句，以及“哪些产物是派生的”。 */
  const DERIVED_ARTIFACTS = ["decoded.log", "app_events.jsonl"];

  function sessionRowText(row) {
    if (!row) return "还没有选择会话";
    const counts = sessionRowCounts(row);
    const missing = row.missing || [];
    const required = missing.filter((name) => !DERIVED_ARTIFACTS.includes(name));
    const derived = missing.filter((name) => DERIVED_ARTIFACTS.includes(name));
    const parts = [`会话 ${row.session_id}`];
    if (row.broken) return `${parts[0]}：${row.broken}`;
    if (row.origin === "import") {
      // 导入的：先把“它是别人给的”放在最前面，再说内容。
      const note = importNote(row);
      parts.push(`**导入的包**${note ? `（${note}）` : ""}`);
    }
    if (row.port) parts.push(`${row.port}${row.baud ? ` @ ${row.baud}` : ""}`);
    if (counts.rx !== null) {
      parts.push(`RX ${counts.rx} B${counts.rxChunks ? ` / ${counts.rxChunks} 次` : ""}${counts.fromTotals ? "" : "（按文件大小算，会话还没结束）"}`);
    }
    if (counts.tx) parts.push(`TX ${counts.tx} B`);
    if (required.length) parts.push(`⚠ 缺少 ${required.join("、")}：这次会话不完整`);
    else if (derived.length) parts.push(`没有 ${derived.join("、")}（这次会话没有解码）`);
    if (row.live) parts.push("正在写（回看会拿到当下的快照）");
    else if (row.complete) parts.push(`结束 ${shortTime(row.closed_utc)}`);
    else parts.push("没有结束记录：这次会话没有正常收尾，但已经落盘的字节仍然可回看");
    return parts.join(" · ");
  }

  function sessionRowLine(row) {
    if (!row) return "还没有选择会话";
    if (row.broken) return `会话 ${row.session_id}：${row.broken}`;
    const counts = sessionRowCounts(row);
    const missing = (row.missing || []).filter((name) => !DERIVED_ARTIFACTS.includes(name));
    const derived = (row.missing || []).filter((name) => DERIVED_ARTIFACTS.includes(name));
    const parts = [`会话 ${row.session_id}`];
    if (row.origin === "import") parts.push("**导入的包**");
    if (counts.rx !== null) {
      // “按文件大小算”是**这个数的性质**（清单里没有写侧的总数）：不能收了它，否则
      // 会被读成精确值。
      parts.push(`RX ${counts.rx} B${counts.fromTotals ? "" : "（按文件大小算）"}`);
    }
    if (counts.tx) parts.push(`TX ${counts.tx} B`);
    if (missing.length) parts.push(`⚠ 缺少 ${missing.join("、")}`);
    else if (derived.length) parts.push("没有解码");
    if (row.live) parts.push("正在写");
    else if (row.complete) parts.push(`结束 ${shortTime(row.closed_utc)}`);
    else parts.push("没有结束记录");
    return parts.join(" · ");
  }

  function sessionReviewText(review) {
    if (!review) return "还没有装载任何会话";
    const counts = review.counts || {};
    const manifest = review.manifest || {};
    const totals = review.manifest_totals || {};
    const identity = manifest.identity || {};
    const parts = [
      `会话 ${review.session_id}`,
      `RX ${counts.raw_bytes || 0} B（${counts.raw_lines || 0} 行）`,
      `TX ${counts.tx_events || 0} 条`,
      `io 事件 ${counts.io_events || 0} 条`,
    ];
    if (review.origin === "import") {
      // 屏幕上一句**结论**（这是导入的包）；来源（什么时候、从哪个 zip、核对了几份哈希）
      // 是“当时怎么来的” ⇒ 后者归悬停（`#sessionStat.title`，用户口径 2026-10-11）。
      parts.unshift("**导入的包**");
    }
    if (counts.decoded_lines || counts.app_events) {
      parts.push(`解码 ${counts.decoded_lines || 0} 行 · 结构化事件 ${counts.app_events || 0} 条`);
    } else {
      parts.push("没有 decoded.log（这次会话没有解码）");
    }
    parts.push(
      `身份 ${IDENTITY_TEXT[identity.state] || identity.state || "未查询"}`
      + (identity.source && identity.source !== "none" ? `（${identity.source}）` : "")
    );
    if (manifest.closed_utc) {
      parts.push(`结束 ${shortTime(manifest.closed_utc)} · ${manifest.end_reason || "未记录原因"}`);
    } else {
      parts.push("没有结束记录");
    }
    // 写侧记的数（manifest.totals）与读侧数出来的数对不上时**说出来**，不挑一个信。
    if (totals.rx_bytes !== undefined && totals.rx_bytes !== null && totals.rx_bytes !== counts.raw_bytes) {
      parts.push(`⚠ manifest 记的 RX ${totals.rx_bytes} B 与文件里数出来的 ${counts.raw_bytes} B 不一致`);
    }
    return parts.join(" · ");
  }

  /* 回看面板那个大号行（短句）；长句在上面 `sessionReviewText`。 */
  function sessionReviewLine(review) {
    if (!review) return "还没有装载任何会话";
    const counts = review.counts || {};
    const manifest = review.manifest || {};
    const identity = manifest.identity || {};
    const parts = [`会话 ${review.session_id}`];
    if (review.origin === "import") parts.push("**导入的包**");
    parts.push(`RX ${counts.raw_bytes || 0} B`);
    if (counts.tx_events) parts.push(`TX ${counts.tx_events} 条`);
    parts.push(counts.decoded_lines || counts.app_events
      ? `解码 ${counts.decoded_lines || 0} 行`
      : "没有解码");
    parts.push(`身份 ${IDENTITY_TEXT[identity.state] || identity.state || "未查询"}`);
    parts.push(manifest.closed_utc ? `结束 ${shortTime(manifest.closed_utc)}` : "没有结束记录");
    return parts.join(" · ");
  }

  global.PWModelHistory = Object.freeze({
    HISTORY_TITLES, HISTORY_HINTS, HISTORY_COLUMNS, SESSION_EVIDENCE_LINKS,
    MANDATORY, TEST_EVIDENCE, HISTORY_SESSION_STATES,
    historyTitle, historyHint, historyColumns,
    sessionRowCounts, sessionTrafficText, sessionEndReasonText,
    sessionOriginBadge, sessionEvidenceHtml, historyNote,
    sessionOptionLabel, importNote,
    runHistoryRows, sessionHistoryRows, historyModel,
    escapeHtml,
    DERIVED_ARTIFACTS,
    sessionRowText, sessionRowLine, sessionReviewText,
    sessionReviewLine,
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
