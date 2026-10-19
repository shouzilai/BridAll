/* 指标趋势的**纯计算**（纵轴范围/刻度取整、横轴滚动窗口与重启断点、
 * 增量帧的合并、逐序列的 min/max）。只读服务端给的数字，不碰 DOM、不发请求、不读时间。
 *
 * ⚠️ 这些数决定“图上的高低有没有参照”：
 *   · `full` 那两档口径都**不随窗口滚动而变**（轴要是每重画一次就换一次，高低就没了参照）；
 *   · 上界抬到 1/2/5 × 10^n（轴标签要读得出量级）；
 *   · 增量帧只并新点，同一帧重发不重复计点，这一帧没带的键要接着留着（否则曲线会缺一段）。
 *
 * 依赖：无（装载在 `preferences.js` 之后、`log-store.js` 之前）。
 */
(function (global) {
  "use strict";

  /* 这两条是“有天然满量程”的指标：它们的满量程 = 固件报的堆总量（不是整段最大值）。 */
  const HEAP_SCALE_KEYS = ["metric.sys.heap", "metric.sys.min_heap"];

  /* 窗格的数量与默认梯子、时间窗那几档都住在 `preferences.js`（它先于本文件装载）——
   * 这里只解构使用，不另写一份。 */
  const preferences = global.PWModelPreferences;
  if (!preferences) {
    throw new Error("PWModelPreferences 必须先于 PWModelMetrics 装载");
  }
  const { METRIC_PANE_COUNT, METRIC_PANE_DEFAULTS, METRIC_WINDOW_CHOICES } = preferences;

  /* 曲线上的文字（任务栈的峰值占用）也走同一个转义 —— 它来自固件报的字段。 */
  const format = global.PWModelFormat;
  if (!format) {
    throw new Error("PWModelFormat 必须先于 PWModelMetrics 装载");
  }
  const { escapeHtml } = format;

  /* 单位与曲线颜色的写法（只影响显示，不改变任何证据）。 */
  const UNIT_TEXT = { count: "次" };
  const METRIC_SERIES_COLORS = [
    "#0078d4", "#d83b01", "#107c10", "#8764b8", "#ca5010", "#038387", "#e3008c", "#4f6bed",
    "#498205", "#c239b3", "#8e562e", "#00b7c3", "#a4262c", "#5c2d91", "#69797e", "#ffb900",
  ];

  function valueBounds(points) {
    let min = null;
    let max = null;
    for (const point of points || []) {
      const value = point && point.value;
      if (typeof value !== "number") continue;
      if (min === null || value < min) min = value;
      if (max === null || value > max) max = value;
    }
    return { min, max };
  }

  function niceCeil(value) {
    if (!Number.isFinite(value) || value <= 0) return 1;
    const power = 10 ** Math.floor(Math.log10(value));
    const scaled = value / power;
    const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
    return step * power;
  }

  function heapTotalOf(metrics) {
    const entry = metrics && metrics.by_key ? metrics.by_key["metric.sys.heap_total"] : null;
    let top = 0;
    for (const point of (entry && entry.points) || []) if (point.value > top) top = point.value;
    return top;
  }

  function chartAxis(points, restarts, seconds) {
    const breaks = new Set((restarts || []).map((row) => row.seq));
    const rows = [];
    let offset = 0;
    let previous = null;
    for (const point of points) {
      if (previous !== null && point.t_s < previous) offset += previous;
      previous = point.t_s;
      rows.push({ point, x: offset + point.t_s });
    }
    // **滚动时间窗**：只画最近 `seconds` 秒（null = 全部）。
    // ⚠️ 先按累加后的横轴裁，再分段 —— 否则跨重启的断点会落在窗口外，画出一条不存在的连接。
    const span = Number.isFinite(seconds) ? seconds : null;
    const last = rows.length ? rows[rows.length - 1].x : 0;
    const kept = span === null ? rows : rows.filter((row) => row.x >= last - span);
    const segments = [[]];
    for (const row of kept) {
      const tail = segments[segments.length - 1];
      const uptimeWentBack = tail.length && row.point.t_s < tail[tail.length - 1].point.t_s;
      if ((breaks.has(row.point.seq) || uptimeWentBack) && tail.length) segments.push([]);
      segments[segments.length - 1].push(row);
    }
    return {
      rows: kept,
      segments: segments.filter((segment) => segment.length),
      hidden: rows.length - kept.length,
    };
  }

  function axisRange(entry, rows, scaleKey, heapTotal) {
    if (scaleKey === "full") {
      if (entry.unit === "percent") return { min: 0, max: 100, source: "0–100%" };
      const total = Number.isFinite(heapTotal) && heapTotal > 0 ? heapTotal : null;
      if (total !== null && HEAP_SCALE_KEYS.includes(entry.key)) {
        return { min: 0, max: total, source: "0–总量" };
      }
      // 没有"天然满量程"的指标：用**整段捕获**的最大值（不是窗口内的）向上取整。
      const top = Number.isFinite(entry.max) && entry.max > 0 ? entry.max : 1;
      return { min: 0, max: niceCeil(top), source: "0–上限" };
    }
    const values = rows.map((row) => row.point.value);
    let min = values.length ? Math.min(...values) : 0;
    let max = values.length ? Math.max(...values) : 1;
    if (min === max) {
      // A flat line still needs vertical room, otherwise it sits exactly on the axis
      // and reads as "no data".
      const span = Math.abs(min) || 1;
      min -= span * 0.05;
      max += span * 0.05;
    }
    return { min, max, source: "自动" };
  }

  function mergeMetrics(previous, incoming) {
    if (!incoming) return previous || null;
    if (!incoming.incremental) return incoming;
    const base = previous && previous.schema === incoming.schema ? previous : null;
    if (!base) return incoming;   // 没有底子：这一帧就是全部（图上从这一帧开始）
    const byKey = {};
    for (const [key, row] of Object.entries(incoming.by_key || {})) {
      const before = (base.by_key || {})[key];
      const points = before ? before.points.slice() : [];
      const seen = new Set(points.map((point) => `${point.seq}:${point.t_s}`));
      for (const point of row.points || []) {
        const id = `${point.seq}:${point.t_s}`;
        if (seen.has(id)) continue;
        seen.add(id);
        points.push(point);
      }
      byKey[key] = { ...row, points, count: points.length, ...valueBounds(points) };
    }
    // 这一帧没有新点的键要接着留着（服务端只带"有新点"的键），否则曲线会突然消失。
    for (const [key, row] of Object.entries(base.by_key || {})) {
      if (!(key in byKey)) byKey[key] = row;
    }
    const total = Object.values(byKey).reduce((sum, row) => sum + (row.count || 0), 0);
    return { ...incoming, by_key: byKey, point_count: total, merged: true };
  }

  /* 两个指标窗格的**选择器**（每窗哪个指标、旁边那个大号读数、
   * 时间窗换算、任务栈的峰值占用）。全部纯函数：喂它一份 `metrics` 载荷就行。 */
  function metricKeys(metrics) {
    return Object.keys((metrics && metrics.by_key) || {});
  }

  function lastMetricPoint(metrics, key) {
    const entry = metrics && metrics.by_key ? metrics.by_key[key] : null;
    const points = (entry && entry.points) || [];
    return points.length ? points[points.length - 1] : null;
  }

  function resolvePaneMetric(keys, preferred, current) {
    if (current && keys.includes(current)) return current;
    for (const key of preferred || []) {
      if (keys.includes(key)) return key;
    }
    return keys.length ? keys[0] : null;
  }

  function paneMetricChoices(keys, panes) {
    return Array.from({ length: METRIC_PANE_COUNT }, (_, index) =>
      resolvePaneMetric(keys, METRIC_PANE_DEFAULTS[index], panes ? panes[index] : null)
    );
  }

  function heapUsageReading(metrics) {
    const free = lastMetricPoint(metrics, "metric.sys.heap");
    const total = lastMetricPoint(metrics, "metric.sys.heap_total");
    if (!free || !total || !total.value) return null;
    const used = total.value - free.value;
    return { free: free.value, used, total: total.value, pct: (used / total.value) * 100, t_s: free.t_s };
  }

  function heapUsageText(reading) {
    if (!reading) return "";
    return `占用 ${reading.pct.toFixed(1)}%（= 1 − 空闲/总量；${reading.used} / ${reading.total} B，按最近一次的总量算）`;
  }

  function metricSeriesColor(index) {
    return METRIC_SERIES_COLORS[index % METRIC_SERIES_COLORS.length];
  }

  function taskStackDetail(point) {
    if (!point || !Number.isFinite(point.used_b) || !Number.isFinite(point.total_b)) return "";
    return `峰值占用 ${point.used_b} / ${point.total_b} B`;
  }

  function windowSeconds(key) {
    const row = METRIC_WINDOW_CHOICES.find((item) => item.key === key);
    return row ? row.seconds : null;
  }

  function nowText(entry, rows, reading) {
    if (reading) return `占用 ${reading.pct.toFixed(1)}%`;
    if (!rows.length) return "";
    const last = rows[rows.length - 1].point.value;
    return `${last} ${UNIT_TEXT[entry.unit] || entry.unit}`;
  }

  /* 方向词（“越大越安全”这类）：卡片标题与指标下拉的 title 都要用，**只此一处**。 */
  const DIRECTION_TEXT = { higher: "越大越安全", lower: "越小越好", neutral: "无方向" };

  /* 两张卡片的**正文标记**（纯字符串）。装配成节点、挂图例悬停是组合根的事
   * —— 这里只回答“这张卡上写什么、画什么”，于是不必开浏览器就能回归卡片文案。 */


  /* 两张图的 **SVG 正文**（纯字符串：给点、分段、轴范围，吐出标记）。
   * 这里不建节点、不量尺寸 —— 装配成 <svg> 是组合根的事。 */
  const PANE_CHART_SIZE = { width: 640, height: 240 };

  function drawMultiChart(entry, axes, size, range) {
    const width = (size && size.width) || PANE_CHART_SIZE.width;
    const height = (size && size.height) || PANE_CHART_SIZE.height;
    const pad = { top: 10, right: 12, bottom: 16, left: 52 };
    const all = axes.flatMap((axis) => axis.rows);
    if (!all.length) return `<text x="${pad.left}" y="${pad.top + 20}">这个时间窗里没有点</text>`;
    const xMin = Math.min(...all.map((row) => row.x));
    const xMax = Math.max(...all.map((row) => row.x));
    const usable = width - pad.left - pad.right;
    const x = (value) => pad.left + (xMax === xMin ? usable / 2 : ((value - xMin) / (xMax - xMin)) * usable);
    const y = (value) => pad.top + (1 - (value - range.min) / (range.max - range.min))
      * (height - pad.top - pad.bottom);
    const shapes = [0.25, 0.5, 0.75].map((fraction) => {
      const gy = pad.top + fraction * (height - pad.top - pad.bottom);
      return `<line class="grid" x1="${pad.left}" y1="${gy.toFixed(1)}" x2="${width - pad.right}" y2="${gy.toFixed(1)}"/>`;
    });
    axes.forEach((axis, index) => {
      for (const segment of axis.segments) {
        const d = segment.map((item, pointIndex) =>
          `${pointIndex ? "L" : "M"}${x(item.x).toFixed(1)},${y(item.point.value).toFixed(1)}`).join(" ");
        shapes.push(`<path class="line metric-series-line" data-series="${index}" style="--series-color:${metricSeriesColor(index)}" d="${d}"/>`);
      }
      const latest = axis.rows.length ? axis.rows[axis.rows.length - 1] : null;
      const detail = latest ? taskStackDetail(latest.point) : "";
      if (latest && detail) {
        const cy = y(latest.point.value);
        const labelY = Math.max(pad.top + 10, Math.min(height - pad.bottom - 4, cy + (cy < height / 2 ? 14 : -8)));
        shapes.push(`<text class="metric-series-detail" data-series="${index}" x="${Math.max(pad.left + 4, x(latest.x) - 132).toFixed(1)}" y="${labelY.toFixed(1)}" style="--series-color:${metricSeriesColor(index)}">${escapeHtml(detail)}</text>`);
      }
    });
    return `
      <line class="axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"/>
      <line class="axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"/>
      <text x="4" y="${pad.top + 8}">${range.max}</text>
      <text x="4" y="${height - pad.bottom}">${range.min}</text>
      <text x="${pad.left}" y="${height - 5}">${xMin}s</text>
      <text x="${width - pad.right - 24}" y="${height - 5}">${xMax}s</text>
      ${shapes.join("")}`;
  }

  function drawChart(entry, axis, size, range) {
    const width = (size && size.width) || PANE_CHART_SIZE.width;
    const height = (size && size.height) || PANE_CHART_SIZE.height;
    const pad = { top: 10, right: 12, bottom: 16, left: 52 };
    const { rows, segments } = axis;
    if (!rows.length) {
      // 窗口里一个点都没有 = **事实**，说清并给出下一步（不画一条空线冒充"数据到了"）。
      return `<text x="${pad.left}" y="${pad.top + 20}">这个时间窗里没有点（把时间窗调大，或选“全部”）</text>`;
    }
    const min = range.min;
    const max = range.max;
    const xMin = rows[0].x;
    const xMax = rows[rows.length - 1].x;
    const usable = width - pad.left - pad.right;
    const x = (value) => pad.left + (xMax === xMin ? usable / 2 : ((value - xMin) / (xMax - xMin)) * usable);
    const y = (value) =>
      pad.top + (1 - (value - min) / (max - min)) * (height - pad.top - pad.bottom);

    // 水平网格（任务管理器式的浅灰横线）：没有它，中间那段高度读不出量。
    const axisLine = (fraction) => {
      const gy = pad.top + fraction * (height - pad.top - pad.bottom);
      return `<line class="grid" x1="${pad.left}" y1="${gy.toFixed(1)}" `
        + `x2="${width - pad.right}" y2="${gy.toFixed(1)}"/>`;
    };
    const shapes = [0.25, 0.5, 0.75].map(axisLine);
    for (const segment of segments) {
      const d = segment
        .map((item, index) => `${index ? "L" : "M"}${x(item.x).toFixed(1)},${y(item.point.value).toFixed(1)}`)
        .join(" ");
      shapes.push(`<path class="line" d="${d}"/>`);
      if (segment.length === 1) {
        shapes.push(
          `<circle class="dot" cx="${x(segment[0].x).toFixed(1)}" cy="${y(segment[0].point.value).toFixed(1)}" r="2.5"/>`
        );
      }
    }
    for (let i = 1; i < segments.length; i += 1) {
      const previous = segments[i - 1][segments[i - 1].length - 1];
      const next = segments[i][0];
      const mid = (x(previous.x) + x(next.x)) / 2;
      shapes.push(
        `<line class="gap" x1="${mid.toFixed(1)}" y1="${pad.top}" x2="${mid.toFixed(1)}" y2="${(height - pad.bottom).toFixed(1)}"/>`
      );
    }

    return `
      <line class="axis" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"/>
      <line class="axis" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"/>
      <text x="4" y="${pad.top + 8}">${max}</text>
      <text x="4" y="${height - pad.bottom}">${min}</text>
      <text x="${pad.left}" y="${height - 5}">${xMin}s</text>
      <text x="${width - pad.right - 24}" y="${height - 5}">${xMax}s</text>
      ${shapes.join("")}`;
  }

  global.PWModelMetrics = Object.freeze({
    HEAP_SCALE_KEYS, valueBounds, niceCeil, heapTotalOf, chartAxis, axisRange, mergeMetrics,
    metricKeys, lastMetricPoint, resolvePaneMetric, paneMetricChoices, heapUsageReading,
    heapUsageText, metricSeriesColor, taskStackDetail, windowSeconds, nowText, UNIT_TEXT,
    METRIC_SERIES_COLORS, DIRECTION_TEXT, PANE_CHART_SIZE, drawChart, drawMultiChart
  });
})(globalThis);
