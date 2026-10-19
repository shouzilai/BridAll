/* 事件流的**降载记账**与**去重游标**（纯模型）。
 *
 * 实时观察是“一边收一边显示”：串口一秒钟几十条行事件，一行一次 DOM 写会把主线程拖死。
 * 所以高频行事件先排队、按批落 DOM；控制类事件（开始/结束/步骤/断言/崩溃）**立刻**生效。
 * 这里只算那本账（间隔、合并、丢弃、落后、p95），以及“这条事件该不该进模型”。
 *
 * ⚠️ 降载**只影响显示**：采集与证据落盘不经过这一层，`raw.bin` / `io_events.jsonl` 一字不少。
 *
 * 边界（架构约定）：不碰 DOM、不发请求、不读写 storage、不设定时器 ——
 * EventSource 与 setTimeout 的接线留在组合根。
 *
 * 装载顺序：sources → … → preferences → log-store → 本文件 → app
 */
(function (global) {
  "use strict";

  const SHED_FLUSH_MIN_MS = 16;      // 一帧
  const SHED_FLUSH_MAX_MS = 250;     // 再慢也不许把界面拖成“卡死”
  const SHED_BATCH_BUDGET_MS = 16;   // 需求 §6：主线程单批 p95 < 16 ms
  const SHED_QUEUE_MAX = 4000;       // 队列上限（到了丢最旧的，并记账）
  const SHED_SAMPLE_CAP = 64;        // 留最近多少批的耗时（用来算 p95）
  /* 这一页记住多少个 SSE 事件号（见 `seen` 集合）。到顶就整份清掉：
   * 清掉之后最坏只是“极老的重放还能再进来一次”，而内存有界。 */
  const EVENT_SEEN_MAX = 200000;
  /* 只有“高频行”事件才排队。控制类事件（开始/结束/步骤/断言/崩溃）必须**立刻**生效：
   * 批处理是为了让界面跟得上，不是为了让“点一下”等半秒。 */
  const SHED_QUEUED_KINDS = new Set(["tool", "raw", "decoded", "app_events", "term"]);

  function createShedState() {
    return {
      delayMs: SHED_FLUSH_MIN_MS,
      batches: 0,
      events: 0,
      coalesced: 0,
      slowBatches: 0,
      lastMs: 0,
      maxMs: 0,
      queued: 0,
      peakQueued: 0,
      overflow: 0,
      deferred: 0,
      metricsSkipped: 0,
      behind: 0,
      broadcastDropped: 0,
      // 最近几批的**实测耗时**（需求 §6 的预算是“单批 p95 < 16 ms”，那就算出 p95 来）。
      samples: [],
    };
  }

  /* 一批写完之后按**实测耗时**调整下一次的间隔（纯函数）。 */
  function shedAfterFlush(state, count, elapsedMs) {
    const next = Object.assign({}, state);
    const rows = Math.max(0, Number(count) || 0);
    const spent = Math.max(0, Number(elapsedMs) || 0);
    next.batches = state.batches + 1;
    next.events = state.events + rows;
    next.coalesced = state.coalesced + Math.max(0, rows - 1);
    next.lastMs = spent;
    next.maxMs = Math.max(state.maxMs, spent);
    next.queued = Math.max(0, state.queued - rows);
    next.samples = state.samples.concat([spent]).slice(-SHED_SAMPLE_CAP);
    if (spent > SHED_BATCH_BUDGET_MS) {
      // 超预算：下一批攒得更久一点（界面上跟着变慢，但采集与落盘一点不受影响）。
      next.slowBatches = state.slowBatches + 1;
      next.deferred = state.deferred + 1;
      next.delayMs = Math.min(
        SHED_FLUSH_MAX_MS, Math.max(SHED_FLUSH_MIN_MS * 2, state.delayMs * 2)
      );
    } else if (spent * 2 < SHED_BATCH_BUDGET_MS && state.delayMs > SHED_FLUSH_MIN_MS) {
      next.delayMs = Math.max(SHED_FLUSH_MIN_MS, Math.round(state.delayMs / 2));
    }
    return next;
  }

  /* 入队。到上限就丢**最旧的**并记账 —— 丢的是“还没写进 DOM 的事件”，
   * 证据文件在服务端，仍然完整；但“丢过”这件事必须在界面上说得出。 */
  function shedPush(queue, state, items) {
    const next = (queue || []).concat(items || []);
    const shed = Object.assign({}, state);
    if (next.length > SHED_QUEUE_MAX) {
      const overflow = next.length - SHED_QUEUE_MAX;
      next.splice(0, overflow);
      shed.overflow = state.overflow + overflow;
    }
    shed.queued = next.length;
    shed.peakQueued = Math.max(state.peakQueued, shed.queued);
    return { queue: next, shed };
  }

  function shedDrain(queue, state) {
    const items = queue || [];
    const shed = Object.assign({}, state);
    shed.queued = 0;
    return { items, queue: [], shed };
  }

  /* 最近几批耗时的分位数（默认 p95）。样本不够就返回 0 —— 不拿两三个数冒充分位数。 */
  function shedPercentile(state, quantile) {
    const samples = (state && state.samples) || [];
    if (samples.length < 8) return 0;
    const q = Math.min(0.999, Math.max(0, Number(quantile) || 0.95));
    const sorted = samples.slice().sort((left, right) => left - right);
    const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1));
    return sorted[index];
  }

  /* 状态行上那句话。**降载没有发生时不写**（空话会让人以为一直在降载）；
   * 一旦有丢弃或落后，就必须写出来（含“证据文件仍然完整”这句事实）。
   * ⚠️ 用户口径 2026-10-12：屏幕上只留**结果与“数据不完整”的警告**；
   *    批处理次数/耗时/队列水位这些实现记账进悬停（它们只影响显示）。 */
  function shedText(state, running) {
    const shed = state || {};
    const parts = [];
    if (shed.overflow) parts.push(`队列满丢弃 ${shed.overflow} 条（证据文件仍然完整）`);
    if (shed.behind) parts.push(`落后服务端 ${shed.behind} 条（正在补）`);
    if (shed.broadcastDropped) {
      parts.push(`服务端广播挤掉过 ${shed.broadcastDropped} 条（这一页看不到它们；证据文件仍然完整）`);
    }
    if (!parts.length) return "";
    return parts.join(" · ");
  }

  /* 降载的记账**只在实时观察那一屏**有意义（那是唯一“一边收一边显示”的场合）。
   * ⚠️ 唯一的例外必须留着：**真丢过东西**（队列满丢弃 / 服务端广播挤掉）时，哪一屏都说 ——
   *    那是“你看到的不是全部”的事实，不能因为切了屏就不提。 */
  function shedVisible(state, mode) {
    const shed = state || {};
    if (String(mode || "") === "live") return true;
    return Boolean(shed.overflow || shed.broadcastDropped);
  }

  /* 这条事件**该不该进模型**（纯函数，宿主回归直接喂事件号序列）。
   *
   * ⚠️ 同一条事件**只喂一次**（2026-10-18 用户实测）：SSE 一旦从 0 重连/重放，
   *    光靠游标挡不住 —— 它已经被归零了。重放的每一行都会被再追加一次，
   *    而且行号从 1 重新数 ⇒ 那一窗立刻被记为“行号有缺口”；之后串口的半行
   *    接不上自己的行号，**一字一行**。所以用集合去重，游标只管“消费到哪儿”。
   *
   * 返回 `{ accept, lastSeq }`：两件事别混在一起 ——
   * ① **游标** = 这一页消费到哪儿了（重放过来的也算消费过，否则状态行会一直说“落后”）；
   * ② **画过了** = 这一条真的进过模型（重复的不再进）。
   * 没有事件号（`seq` 不是正数）一律接受：控制类事件不带号也得生效。
   */

  global.PWModelStream = Object.freeze({
    SHED_FLUSH_MIN_MS, SHED_FLUSH_MAX_MS, SHED_BATCH_BUDGET_MS, SHED_QUEUE_MAX,
    SHED_SAMPLE_CAP, EVENT_SEEN_MAX, SHED_QUEUED_KINDS, createShedState, shedAfterFlush,
    shedPush, shedDrain, shedPercentile, shedText, shedVisible
  });
})(globalThis);
