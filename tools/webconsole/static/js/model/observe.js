/* 实时观察那一屏的**纯选择器**（状态行文案、收发计数、观察组合、
 * 会话证据的下载目标、被跳过端口那句短话）。
 *
 * 这里只把服务端给的快照读成“该显示什么”，不碰 DOM、不发请求：
 *   · 端口/波特率/会话号/计数**一律照抄服务端**，页面不重算、不估算；
 *   · 身份匹配的结论（`card.match`）由服务端算好，页面只按它取目标，不自己比；
 *   · 下载目标看“这次会话里到底有没有那个文件”，不看“此刻在不在解码”。
 *
 * 依赖：`format.js`（时长/时刻/字节单位的写法）。
 *
 * 装载顺序：sources → format → … → stream → 本文件 → event-route → runs → transport → app
 */
(function (global) {
  "use strict";

  const format = global.PWModelFormat;
  if (!format) {
    throw new Error("PWModelFormat 必须先于 PWModelObserve 装载");
  }
  const { formatDuration, shortTime, byteUnits, beijingParts } = format;

  /* ============================ 设备嗅探的卡片与进度（Web 前端模块化） ============================
   * 目标适配 / D5、D10 里那几段**纯函数**：卡片的身份键、本次选中的卡片、逐固件兼容性、
   * “为什么不能跑”，以及进度那一行。入参一律显式（`sniff` 块 / 卡片 / 选中的 key），
   * **不读页面状态** —— 组合根保留同名转口，把 `app.sniff` / `app.selectedCard` 递进来。 */

  /* 卡片的身份：`本次序号|口`。序号只在这一次嗅探里有效（`scan_seq_persistent=false`），
   * 所以它不能跨轮当作设备的身份（xx：重新嗅探后旧序号不复用）。 */
  function sniffCardKey(card) {
    return `${Number(card && card.num) || 0}|${String((card && card.port) || "")}`;
  }

  /* 本次嗅探里选中的那一张卡片（2 s 轮询会把 `sniff` 整块换掉，所以每次都按 key 重找）。 */
  function selectedSniffCard() {
    const last = (app.sniff && app.sniff.last) || null;
    const cards = last && Array.isArray(last.cards) ? last.cards : [];
    const key = app.selectedCard ? app.selectedCard.key : "";
    return cards.find((card) => sniffCardKey(card) === key) || null;
  }

  /* 服务端为这张卡片算好的**逐固件兼容性结论**（`/api/state.sniff.cards[].firmwares`）。
   * ⚠️ 它是**对象列表**（`{firmware_id, allowed, reason, first_flash, target_id, project}`），
   *    **不是** id 列表：服务端把每一份固件都列出来并给出结论与理由。
   *    页面只取 `allowed === true` 的那几份 —— 不自己比名字/协议（那是服务端的唯一判断）。 */
  function sniffCardFirmwares(card) {
    const rows = card && Array.isArray(card.firmwares) ? card.firmwares : [];
    // 只留 id：这个函数的用处就是"这台设备**能跑**哪几份固件"（给固件下拉框筛）。
    return rows
      .filter((row) => row && row.allowed === true)
      .map((row) => String(row.firmware_id));
  }

  /* 这张卡片为什么不能跑：写**服务端给的理由**（不许自己编一个）。
   * 一份都不能用时，把第一条不适合的理由摆出来 —— 那是能行动的那一句
   * （“设备自报的项目是 rtthread，这份固件属于 logger”），“没有兼容固件”不是。 */
  function sniffCardFirmwareWhy(card) {
    const rows = card && Array.isArray(card.firmwares) ? card.firmwares : [];
    const ok = rows.filter((row) => row && row.allowed === true);
    if (ok.length) {
      const names = ok.slice(0, 2).map((row) => String(row.firmware_id));
      return `可用固件：${names.join("、")}${ok.length > 2 ? " …" : ""}`;
    }
    const first = rows.find((row) => row && row.reason);
    return first ? `不可用：${first.reason}` : "没有能用在它上面的固件";
  }

  /* 这次 run 点名的设备（`{scan_seq, port}`）—— 没选就返回 null（走老路径：按目标声明的口）。 */
  function runDeviceSelection() {
    const card = app.selectedCard;
    if (!card) return null;
    return { scan_seq: Number(card.scan_seq) || 0, port: String(card.port || "") };
  }

  /* 目标适配 / D10（需求 §12.3）：进度快照 → 页面上那一行。
   *
   * ⚠️ 四个状态与核心的 `SNIFF_STATES` 一一对应（idle / running / completed / failed）；
   *    `percent` 用**服务端派生的那个值**（唯一算法在服务端 `derived_percent()`），
   *    页面不另算一份当事实 —— 这里只把 `percentDerived` 一并给出来，供宿主判据
   *    钉住"两边算的是同一个数"。
   * ⚠️ 它只读进度快照，**不读 `last`**：上一轮的完整结果与"本轮走到哪"是两类事实。 */
  const SNIFF_PROGRESS_STATES = ["idle", "running", "completed", "failed"];

  function sniffProgressModel(sniff, busy) {
    const block = sniff || {};
    const row = block.progress || null;
    const state = SNIFF_PROGRESS_STATES.includes(String(row && row.state))
      ? String(row.state)
      : "idle";
    const total = Math.max(0, Number((row && row.total_ports) || 0));
    const done = Math.min(total, Math.max(0, Number((row && row.completed_ports) || 0)));
    const port = String((row && row.current_port) || "");
    const percent = Math.min(100, Math.max(0, Number((row && row.percent) || 0)));
    const reason = String((row && row.reason) || "");
    const percentDerived = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
    /* 点下去了、但服务端还没把那一轮摆到快照里（轮询 2 s 一拍）⇒ 先说“正在嗅探”，
     * 否则用户点一下看不到任何反应。⚠️ 这不是自己编进度：它只在**本轮还没有数字** 时说一句。 */
    const waiting = state !== "running" && Boolean(busy);
    /* 失败要有可操作的提示（不得永久停在“正在嗅探”，也不得默默回到“嗅探”）；
     * 跑完但那句话里有事（例：结果没能落盘）**照样说出来** —— 不静默降级。 */
    let text = "";
    let kind = "";
    if (state === "failed") {
      text = reason ? `嗅探失败：${reason}` : "嗅探失败：这一轮没有正常收口";
      kind = "fail";
    } else if (state === "completed" && reason) {
      text = `嗅探完成，但${reason}`;
      kind = "warn";
    } else if (state === "running") {
      text = port ? `正在嗅探 ${port} · ${done}/${total}` : `正在嗅探 · ${done}/${total}`;
    } else if (waiting) {
      text = "正在嗅探…";
    } else if (state === "completed") {
      /* 跑完留一条**能看出“哪一次”**的记录：完成数 + 上一次完成时刻（北京时间，与时区无关）。
       * ⚠️ 用户实测：连点两次嗅探，屏幕与上一轮逐字节相同 ⇒ “看起来没重新启动”。 */
      const parts = beijingParts(row && row.updated_utc);
      text = [`嗅探完成 · ${done}/${total}`, parts ? parts.clock : ""].filter(Boolean).join(" · ");
    }
    /* ⚠️ 等待新快照时**不沿用上一轮的数字**（用户实测：刚跑完 100%，再点时条子仍然满着，
     * 看着像“已经跑完了/没动”）。 */
    const shownPercent = waiting ? 0 : percent;
    const visible = state === "failed" || state === "running" || state === "completed" || waiting;
    return {
      state, total, done, port, percent: shownPercent, percentDerived,
      reason, visible, text, kind, waiting,
    };
  }

  /* 这一轮有没有“声明了但当前不存在”的口被跳过（用户口径 2026-10-10：不展示）。
   * 用服务端给的**结构化**字段（`last.skipped`）拼一句短话；完整那句话在证据与 `notes` 里。
   * ⚠️ 不靠解析 prose：那句长说明里有个括号与逗号，从里面抠端口号就是在自找麻烦。 */
  function sniffSkippedLine(last) {
    const rows = (last && Array.isArray(last.skipped)) ? last.skipped.map(String).filter(Boolean) : [];
    if (!rows.length) return "";
    return `另有 ${rows.length} 个登记的口当前不存在：${rows.join("、")}`;
  }

  /* 端口/波特率/状态/时长/RX/TX/最近一次收发 —— 全是服务端数出来的数，不在这里重算。
   * ⚠️ D8（2026-10-10）：这一行**不再是屏幕上那一行**（它太长，会把控制区撑成两行）；
   *    屏幕上显示的是 `observeStatCompact()` + `#observeCounters`，而这一行的全文改挂在
   *    `#observeStat` 的 `title` 上（悬停可看全：会话号 / 证据目录 / 最近收发 / io 事件）。
   *    它仍然是**唯一**那份实现（不另写一份“详细版”），宿主回归直接喂它。 */
  function observeStats(observe) {
    if (!observe) return "—";
    const parts = [];
    const session = observe.session || null;
    const port = session ? session.port : (observe.port || null);
    // 没连过任何设备时不要编一个端口出来（“— @ 115200”看着像已经选中了什么）。
    parts.push(port ? `${port} @ ${observe.baud}` : `波特率 ${observe.baud}`);
    parts.push(`状态 ${observe.state_label || observe.state}`);
    if (session) {
      parts.push(`会话 ${session.session_id}`);
      parts.push(`已连接 ${formatDuration(session.connected_ms)}`);
      // 会话时长与“本次连接时长”分开写：重连之后这两个数不一样（需求 §3.2 里的
      // “持续时间”说的是连接，而证据文件里那份是整次观察）。
      if (session.uptime_ms !== undefined && session.uptime_ms !== null
        && session.uptime_ms !== session.connected_ms) {
        parts.push(`会话时长 ${formatDuration(session.uptime_ms)}`);
      }
      parts.push(`RX ${session.rx_bytes} B${session.rx_chunks ? ` / ${session.rx_chunks} 次` : ""}`);
      parts.push(`TX ${session.tx_bytes} B`);
      parts.push(`io 事件 ${session.io_events} 条`);
      parts.push(`打开过 ${session.opens} 次`);
      parts.push(`证据 ${session.evidence_dir}/`);
      const last = session.last_rx_utc || session.last_tx_utc;
      parts.push(`最近收发 ${last ? shortTime(last) : "—"}`);
    } else if (observe.last) {
      parts.push(`上次会话 ${observe.last.session_id || "?"}`);
      parts.push(`RX ${observe.last.rx_bytes} B / TX ${observe.last.tx_bytes} B`);
      if (observe.last.evidence_dir) parts.push(`证据 ${observe.last.evidence_dir}/`);
    }
    if (observe.rx_error) parts.push(`读取出错 ${observe.rx_error}`);
    return parts.join(" · ");
  }

  /* 实时计数那一格：`TX 348 B | RX 12.6 KiB`（D8）。
   * ⚠️ 显示的是**换算过的单位**，而服务端的原始字节点数进 title（两个都要看得见）。 */
  function observeCounters(observe) {
    const session = (observe && observe.session) || null;
    if (!session) {
      return { tx: "TX —", rx: "RX —", title: "还没连过设备：没有计数" };
    }
    const rx = Number(session.rx_bytes) || 0;
    const tx = Number(session.tx_bytes) || 0;
    return {
      tx: `TX ${byteUnits(tx)}`,
      rx: `RX ${byteUnits(rx)}`,
      title: `本次会话：TX ${tx} B · RX ${rx} B`
        + (session.rx_chunks ? `（RX 分 ${session.rx_chunks} 次到达）` : ""),
    };
  }

  /* 实时观察的「目标」= **服务端算好的身份匹配结论**（目标适配 / D9，需求 §9.1）。
   *
   * 用户只选通信口：固件由服务端按这张卡片的**实测身份**唯一匹配 firmware profile，
   * 唯一命中就自动采用；零匹配 / 多匹配必须阻塞并说明原因。所以页面**不挑固件**：
   * 这里只把服务端的结论（`/api/state.sniff.last.cards[].match`）读出来，再去
   * `observe.targets` 里取那一份的标签 —— 一个字符串都不自己比。
   *
   * ⚠️ 卡片上的「可用固件 N 份」（`compatible_firmwares`）回答的是**另一个**问题：
   * “这一份能不能用在它上面”（含“覆盖同板另一份固件”这种**烧录**路径）。
   * 同一块板上跑着 RT-Thread 时，裸机那份也 `allowed=true`（一烧就把它换掉），
   * 但它**不是**“设备此刻跑的那一份” —— 观察组合、解码能力与受控用例目录都只能按
   * 真命中的那一份装配（ 就是拿错了这一份）。
   */
  function observeCombination(card, targets) {
    const rows = Array.isArray(targets) ? targets : [];
    if (!card) {
      return {
        state: "", firmware_id: "", rows: [], available: false, usable: false, note: "",
        blocked: "还没选设备：先选一个通信口（没嗅探过就点一次「嗅探」）",
      };
    }
    const match = card.match || null;
    if (!match || !match.state) {
      return {
        state: "", firmware_id: "", rows: [], available: false, usable: false, note: "",
        blocked: "还没拿到这台设备的身份结论：再点一次「嗅探」",
      };
    }
    const firmwareId = String(match.firmware_id || "");
    const row = rows.find((item) => String(item.firmware_id) === firmwareId) || null;
    const usable = Boolean(match.usable);
    const available = usable && Boolean(row);
    return {
      state: String(match.state),
      firmware_id: available ? firmwareId : "",
      rows: available ? [row] : [],
      available,
      // `usable` = 服务端的匹配结论本身（“这个身份能不能定到一份固件”）。
      // ⚠️ 自动验证那一屏靠它判断“能不能替你决定烧哪一份”（身份未知 + 多份候选 ⇒ 不替使用者挑）。
      usable,
      /* 唯一命中就什么都不说（用户口径 2026-10-12：这一项不显示、也不留句子）。
       * ⚠️ 用户口径（2026-10-13）：**身份未知 / 零匹配 / 多匹配不再拦连接** ⇒ 这里也不再
       *    摆“为什么连不了”的理由 —— 服务端那句原文里带着「多匹配 / 首次烧录声明」这类
       *    **平台内部术语**，而屏幕上要说的事已经有自己的说法（卡片写「没问到身份」，
       *    连上之后状态行说「身份未识别，已进入原始串口模式」）。原句仍在 API 里
       *    （`card.match.reason`），自动验证要用的也是它。
       * 唯一还留在屏幕上的情况是“身份匹配到了、本机却没有这一路的观察会话” —— 那是本机
       * 台架缺登记，跟身份没关系，得说出来。 */
      note: !usable || available
        ? ""
        : `服务端按这次实测身份匹配到 ${firmwareId}，而本机没有登记这一路的观察会话`,
    };
  }

  /* 会话里四个窗口各自对应哪个文件（tool / decoded 不是会话文件，见注释）。
   * ⚠️ 会话目录里没有 `tool.log`（主机侧说明不落盘），`decoded.log` 要等实时解码；
   * 所以这两个窗口指向最能解释自己的那一份，而不是留一个点不动的链接。 */
  const SESSION_FILE_BY_SOURCE = {
    tool: "manifest.json",
    raw: "raw.bin",
    events: "app_events.jsonl",
    decoded: "decoded.log",
  };

  /* 会话证据的下载目标（纯函数）。
   *
   * ⚠️ 判据是"**这个会话里到底有没有那个文件**"，不是"此刻在不在解码"：
   * 会话结束后解码已经停了（`decode.ready=false`），但文件就在那里 —— 拿运行状态去决定
   * 链接，会让刚采到的证据在断开那一刻变成"点不动"（真机验收当场遇到）。
   * 所以：正在观察时看 `decode.ready`，看历史会话时看会话自己的计数（`decoded_lines`）。
   *
   * 没解码时也不要指向一个不存在的文件：
   *  - `decoded.log` 不存在 ⇒ 不给下载，标题里说清原因（字典没命中 / 还没查身份）；
   *  - `app_events.jsonl` 不存在 ⇒ 「结构化事件」窗口改指 `io_events.jsonl`（那一定存在）。
   */
  function sessionEvidenceLink(sourceId, observe, session) {
    const decode = (observe || {}).decode || {};
    const current = (observe || {}).session || null;
    const live = Boolean(current && session && current.session_id === session.session_id);
    // 服务端没给原因时只能说**事实**（这次会话没有解码）：猜“没有匹配到字典”
    // 会在空闲/断开时变成一句误诊 —— 原因由服务端清空的那一刻，页面没资格补一个。
    const reason = decode.reason || "这次会话没有解码";
    const decodedLines = live
      ? (decode.ready ? 1 : 0) || (session && session.decoded_lines) || 0
      : (session && session.decoded_lines) || 0;
    const eventCount = live
      ? (decode.ready ? 1 : 0) || (session && session.app_events) || 0
      : (session && session.app_events) || 0;
    if (sourceId === "decoded" && !decodedLines) {
      return { file: "", title: `本次会话没有 decoded.log：${live ? reason : "这次会话没有解码"}` };
    }
    if (sourceId === "events" && !eventCount) {
      return {
        file: "io_events.jsonl",
        title: `本次会话没有解码，就没有 app_events.jsonl：这里下载 io 时间线（${live ? reason : "这次会话没有解码"}）`,
      };
    }
    const file = SESSION_FILE_BY_SOURCE[sourceId] || "";
    return {
      file,
      title: file
        ? `下载本会话的 ${file}（原始证据：不受视图截断、暂停或去重影响）`
        : "本次会话还没有这个文件",
    };
  }

  /* -------------------------------------------------- 状态行说明与命令框默认值 */

  /* 真正“出问题”的三个状态（连上/正常断开都不弹那一行；打不开 / 串口不在了 / 被别的模式占着才说）。 */
  const OBSERVE_TROUBLE_STATES = ["open_failed", "port_missing", "owned_elsewhere"];

  /* 实时观察那一行通知（用户口径 2026-10-12：**只在出问题时通报**）。
   *
   * 「身份未识别，已进入原始串口模式」这句话是**服务端**给的（`observe.transparent_reason`），
   * 页面只负责在有需要时说一遍；它说的是**眼前这件事**（连上了、按原始串口工作），
   * 不是“故障”，所以走 warn。位置只此一处：受控用例那一格屏幕上只说「不适用」。 */
  function observeNotice(observe) {
    if (!observe) return { text: "", kind: "" };
    const state = String(observe.state || "");
    if (state === "connected" && observe.transparent) {
      return {
        text: String(observe.transparent_reason || ""),
        kind: "warn",
      };
    }
    if (!OBSERVE_TROUBLE_STATES.includes(state)) return { text: "", kind: "" };
    const label = observe.state_label || state;
    const detail = String(observe.detail || "").trim();
    return {
      text: detail ? `实时观察：${label} —— ${detail}` : `实时观察：${label}`,
      kind: "warn",
    };
  }

  /* 换设备之后**不得沿用上一台设备的说法**。实时观察的身份/能力/计数说的
   * 都是**正握着串口的那一台**；用户刚在卡片里选了另一台时，界面必须把这件事说出来
   * （否则那些字会被读成“我刚选的这台已经连上了”）。
   * 连着的那一台与选中的那一台一致 ⇒ 不说；没在卡片里选设备 ⇒ 也不说（那是正常状态）。 */
  function observeSelectionNote(observe, selectedPort = (app.selectedCard && app.selectedCard.port) || null) {
    const connected = (observe && observe.device && observe.device.port) || null;
    const selected = selectedPort || null;
    if (!connected) return "";
    if (!selected) return `下面这些说的是${connected}（当前没在卡片里选设备）`;
    if (selected === connected) return "";
    return `连的是 ${connected}（不是你刚选的 ${selected}）：要换设备先断开`;
  }

  /* 设备/项目配置里的默认行结束符（需求 §8）：只在**换设备**时套用一次，
   * 不动用户当前的选择（他可能刚手动改成 LF）。
   * 返回 `null` = 这一次不该动；否则给 `{ deviceKey, value }` 让组合根写回。
   * ⚠️ “上一次套用的是哪台设备”仍归组合根（那是状态，不是选择器）。 */

  /* 观察目标下拉里的**那一个选项**。
   *
   * 名字后面只补“缺了哪一项”（无解码 / 无身份握手）—— 那是这一路能不能用的直接原因；
   * 完整事实（目标/设备/平台/项目包/四项能力）进悬停，屏幕上不铺开。
   * ⚠️ 能力三态：`undefined` 是**服务端没声明**，不能写成“不支持”（这两件事不一样）。 */

  /* 下拉框只在“这一路能连哪一份”真的变了时重建（每 2 s 轮询都会走到这里）。 */

  global.PWModelObserve = Object.freeze({
    SESSION_FILE_BY_SOURCE, sniffSkippedLine, observeStats, observeCounters,
    observeCombination, sessionEvidenceLink, OBSERVE_TROUBLE_STATES, observeNotice,
    observeSelectionNote,
    sniffCardKey,
    selectedSniffCard, sniffCardFirmwares, sniffCardFirmwareWhy, runDeviceSelection,
    SNIFF_PROGRESS_STATES, sniffProgressModel, /* 观察目标下拉的一个选项，以及“要不要重建这个下拉框”的签名。 */
  });
})(globalThis);
