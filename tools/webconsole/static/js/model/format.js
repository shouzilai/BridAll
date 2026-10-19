/* 纯格式化模型（北京时间、行时间、级别/模块、字节与时长）。
 *
 * 边界（架构约定）：
 *   - 不读 DOM、不发请求、不读写浏览器存储、不设定时器；
 *   - 只接收值、返回值。`globalThis` 只用来登记命名空间，不读写页面状态。
 *
 * 装载顺序：本文件必须在 `log-filter.js` 与 `app.js` 之前执行（`index.html` 里
 * `<script>` 的先后顺序是唯一出处）。
 */
(function (global) {
  "use strict";

  /* 时间戳一律按 **UTC+8** 渲染，与浏览器所在时区无关：
   * 现场是北京时间，而“页面显示的时间”和“手机上的时间”对不上会直接毁掉联调的效率。
   *
   * ⚠️ 实现上先把毫秒加 8 小时，再用 `getUTC*` 读字段 —— 这样**不看本机时区**：
   * 用 `getHours()` 的话，换一台 UTC 的机器（或 CI）就显示成另一个时间。
   *
   * 精度：服务端的 `utc_now()` 是毫秒（`…SS.mmmZ`），事件流每条都带它 ⇒ 毫秒级。 */
  const BEIJING_OFFSET_MS = 8 * 3600 * 1000;

  function toEpochMs(value) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
    const text = String(value ?? "").trim();
    if (!text) return null;
    const ms = Date.parse(text);
    return Number.isFinite(ms) ? ms : null;
  }

  function beijingParts(value) {
    const ms = toEpochMs(value);
    if (ms === null) return null;
    const shifted = new Date(ms + BEIJING_OFFSET_MS);
    const pad = (number, width = 2) => String(number).padStart(width, "0");
    return {
      date: `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`,
      clock: `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:${pad(shifted.getUTCSeconds())}`,
      ms: pad(shifted.getUTCMilliseconds(), 3),
    };
  }

  /* 每行前缀用：`HH:MM:SS.mmm`（只有时间，够短；完整日期在元数据那一层）。 */
  function beijingClock(value) {
    const parts = beijingParts(value);
    return parts ? `${parts.clock}.${parts.ms}` : "";
  }

  /* 元数据用：`YYYY-MM-DD HH:MM:SS.mmm`。取不到就返回空串（调用方负责写“不知道”）。 */
  function beijingStamp(value) {
    const parts = beijingParts(value);
    return parts ? `${parts.date} ${parts.clock}.${parts.ms}` : "";
  }

  const escapeRegExp = (text) => String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  /* HTML 转义：历史/证据那些行列是拼字符串拼出来的，服务端给的设备名与路径必须转义。
   * 纯函数，住在模型里；组合根直接解构用。 */
  const escapeHtml = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  /* 设备日志的级别/模块写在行里，例如 "[I][dht] cycle …"。
   * 工具输出与裸串口没有这种标记，就如实返回 null —— 猜一个级别比不显示更糟。 */
  const LEVEL_MODULE_RE = /(?:^|\s)\[([EWIDV])\](?:\[([A-Za-z0-9_.\-]+)\])?/;

  function parseLine(text) {
    const match = LEVEL_MODULE_RE.exec(String(text ?? ""));
    if (!match) return { level: null, module: null };
    return { level: match[1], module: match[2] || null };
  }

  /* “错误”= 设备标了 [E]，或者这条记录压根没被解析器认识（A17 会因为后者直接判 FAIL）。
   * 只看错误必须把两种都留下，否则“整洁的错误列表”会恰好漏掉最该看的那种。 */
  function isErrorLine(text) {
    const value = String(text ?? "");
    return parseLine(value).level === "E" || value.includes("record.unparsed");
  }

  /* 行上的时间只取证据里**真实存在**的字段（设备 uptime）。
   * 写成 `+12.345s` 的样子：它一眼能看出是**设备自报的用时**，不是墙钟 ——
   * 主机时刻（上面那套北京时间）用 `HH:MM:SS.mmm` 的样子，两者不混。 */
  const TIME_RE = /\bup_ms=(\d+)|\buptime_s=(\d+)/;

  function lineTime(text) {
    const match = TIME_RE.exec(String(text ?? ""));
    if (!match) return "";
    if (match[1] !== undefined) return `+${(Number(match[1]) / 1000).toFixed(3)}s`;
    return `+${match[2]}s`;
  }

  function formatDuration(ms) {
    if (ms === null || ms === undefined) return "—";
    if (ms < 1000) return `${ms} ms`;
    const seconds = ms / 1000;
    if (seconds < 60) return `${seconds.toFixed(1)} s`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes} m ${(seconds - minutes * 60).toFixed(0)} s`;
  }

  /* 字节数写成“人读得动”的紧凑形式：`B` / `KiB` / `MiB` / `GiB`。
   * ⚠️ 只换单位、**不重算**（数值仍是服务端数出来的那些）；1024 进位、最多一位小数 ⇒
   *    位数稳定，换单位时读数不会左右跳（D8 要的“稳定宽度”靠这个 + CSS 的等宽数字）。 */
  function byteUnits(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return "—";
    if (value < 1024) return `${Math.round(value)} B`;
    const units = ["KiB", "MiB", "GiB", "TiB"];
    let scaled = value / 1024;
    let index = 0;
    while (scaled >= 1024 && index < units.length - 1) {
      scaled /= 1024;
      index += 1;
    }
    return `${scaled.toFixed(1)} ${units[index]}`;
  }

  /* 持续时间的紧凑写法：`分:秒`、`时:分:秒`、`1天 02:03:04`。
   * ⚠️ 与 `formatDuration()` 分工不同（那个是给步骤耗时看的 `10.1 s` / `1 m 5 s`，判据钉着它）：
   *    这里要的是“一眼看出连了多久”，所以按时长自动选刻度，不写单位也能读懂。 */
  function compactDuration(ms) {
    const total = Math.max(0, Math.round(Number(ms) || 0) / 1000);
    const seconds = Math.floor(total % 60);
    const minutes = Math.floor(total / 60) % 60;
    const hours = Math.floor(total / 3600) % 24;
    const days = Math.floor(total / 86400);
    const two = (value) => String(value).padStart(2, "0");
    if (days > 0) return `${days}天 ${two(hours)}:${two(minutes)}:${two(seconds)}`;
    if (total >= 3600) return `${hours}:${two(minutes)}:${two(seconds)}`;
    return `${minutes}:${two(seconds)}`;
  }

  /* 元数据里的时间一律**北京时间**（毫秒）。
   * ⚠️ 以前是把 ISO 的 `T`/`Z` 去掉直接显示 —— 那是 **UTC**，比现场时间早 8 小时，
   * 于是“设备卡上的最近活动”“run 开始时刻”全都对不上电脑上的表（真机验收提出）。
   * 认不出来的值写“—”（不知道就说不知道，不显示一个看着像时间的字符串）。 */
  function shortTime(iso) {
    const stamp = beijingStamp(iso);
    return stamp || "—";
  }

  function shortSha(sha) {
    return sha ? String(sha).slice(0, 10) : "—";
  }

  global.PWModelFormat = Object.freeze({
    BEIJING_OFFSET_MS,
    toEpochMs,
    beijingParts,
    beijingClock,
    beijingStamp,
    escapeRegExp,
    escapeHtml,
    LEVEL_MODULE_RE,
    parseLine,
    isErrorLine,
    TIME_RE,
    lineTime,
    formatDuration,
    byteUnits,
    compactDuration,
    shortTime,
    shortSha,
  });
})(globalThis);
