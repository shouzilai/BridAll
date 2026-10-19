/* 日志派生视图的纯模型（搜索、排除、过滤、折叠、命中）。
 *
 * 这一层的铁律：**只读模型**。搜索、过滤、折叠、高亮都只是“怎么显示”，
 * 不会改写 `source.lines`，更不会碰 runs/ 里的证据文件；源行号也不随过滤变化。
 *
 * 边界（架构约定）：不读 DOM、不发请求、不读写浏览器存储、不设定时器。
 * 依赖：`format.js` 必须先装载（`parseLine` / `isErrorLine` / `escapeRegExp`）。
 *
 * 装载顺序：`format.js` → 本文件 → `app.js`（`index.html` 与两处 Node 宿主回归一致）。
 */
(function (global) {
  "use strict";

  const format = global.PWModelFormat;
  if (!format) {
    throw new Error("PWModelFormat 必须先于 PWModelLogFilter 装载");
  }
  const { escapeRegExp, parseLine, isErrorLine } = format;

  /* 每个窗口一份视图状态：没有“全局搜索”，一个窗口的查询不会影响另三个。 */
  const MAX_QUERY = 200;
  const MAX_MARKS_PER_LINE = 200;

  /* 查询 → 正则。无效正则**当场**给出说明，而不是抛掉或悄悄当普通文本用：
   * 悄悄降级会让人以为“确实没有命中”。 */
  function compileQuery(view) {
    const key = `${view.regex ? "re" : "lit"}:${view.query}`;
    if (view.cache.key === key) return view.cache;
    const result = { key, re: null, error: "" };
    if (view.query) {
      if (view.query.length > MAX_QUERY) {
        result.error = `查询过长：${view.query.length} 字符（上限 ${MAX_QUERY}）`;
      } else {
        try {
          result.re = new RegExp(view.regex ? view.query : escapeRegExp(view.query), "g");
        } catch (error) {
          result.error = `正则无效：${error.message}`;
        }
      }
    }
    view.cache = result;
    return result;
  }

  /* 一行里的命中区间。每行设上限：一行里几千个命中只说明查询没意义，
   * 画出来只会把浏览器拖死（性能要求写在不卡死，而不是“能画出来”）。 */
  function matchRanges(re, text) {
    if (!re) return [];
    const ranges = [];
    re.lastIndex = 0;
    let match = re.exec(text);
    while (match) {
      if (match[0] === "") re.lastIndex += 1;
      else {
        ranges.push([match.index, match.index + match[0].length]);
        if (ranges.length >= MAX_MARKS_PER_LINE) break;
      }
      match = re.exec(text);
    }
    return ranges;
  }

  /* 排除条件 → 正则。规则与搜索**完全一致**（无效正则当场说明，不悄悄降级）：
   * 悄悄把正则当普通文本用，会让人以为“确实没有命中”。
   * 单独一个缓存槽：搜索与排除各改各的，不能互相把对方的编译结果顶掉。 */
  function compileExclude(view) {
    const key = `${view.excludeRegex ? "re" : "lit"}:${view.exclude}`;
    if (view.excache && view.excache.key === key) return view.excache;
    const result = { key, re: null, error: "" };
    if (view.exclude) {
      if (view.exclude.length > MAX_QUERY) {
        result.error = `排除条件过长：${view.exclude.length} 字符（上限 ${MAX_QUERY}）`;
      } else {
        try {
          result.re = new RegExp(view.excludeRegex ? view.exclude : escapeRegExp(view.exclude), "g");
        } catch (error) {
          result.error = `排除的正则无效：${error.message}`;
        }
      }
    }
    view.excache = result;
    return result;
  }

  /* 排除是否在生效：填了条件、没有一键停用、而且条件本身是有效的。
   * ⚠️ 正则无效时**不生效**（既不是“全部隐藏”，也不是“偷偷当普通文本”）： */
  function excludeActive(view) {
    if (!view.exclude || view.excludeOff) return false;
    return !compileExclude(view).error;
  }

  /* 这一行是不是被排除掉了（关键词或正则，规则与搜索一致）。 */
  function lineExcluded(view, text) {
    const { re } = compileExclude(view);
    if (!re) return false;
    re.lastIndex = 0;
    return re.test(String(text ?? ""));
  }

  /* 这一行为什么看不见：null = 显示；"filter" = 级别/模块/错误过滤；"exclude" = 文本排除。
   * 两种“看不见”**必须分开**：被排除的行要计数并说清楚（需求 §7），被过滤掉的行不用。 */
  function hiddenReason(view, text) {
    if (!linePasses(view, text)) return "filter";
    if (excludeActive(view) && lineExcluded(view, text)) return "exclude";
    return null;
  }

  /* 被排除掉的行数（显示用）。它是**算出来**的，不是一个只增不减的计数器：
   * 增量追加只扫新来的行，条件变了或线被裁剪过（baseIndex 变了）才重扫。
   * 一个累加计数器在裁剪之后会永远偏高 —— 而这个数是要给人看的。 */
  function excludedCount(source) {
    const view = source.view;
    const active = excludeActive(view);
    const signature = `${view.excludeRegex ? "re" : "lit"}:${view.exclude}`;
    const cache = view.excludeStat;
    const extend = Boolean(cache) && cache.baseIndex === source.baseIndex
      && cache.signature === signature && cache.active === active
      && cache.scanned <= source.lines.length;
    let count = extend && active ? cache.count : 0;
    let scanned = extend ? cache.scanned : 0;
    if (active) {
      for (let index = scanned; index < source.lines.length; index += 1) {
        if (lineExcluded(view, source.lines[index])) count += 1;
      }
      scanned = source.lines.length;
    } else {
      count = 0;
      scanned = 0;
    }
    view.excludeStat = { baseIndex: source.baseIndex, signature, active, count, scanned };
    return count;
  }

  function linePasses(view, text) {
    const value = String(text ?? "");
    if (view.errorsOnly && !isErrorLine(value)) return false;
    if (view.level !== "all" || view.module !== "all") {
      const { level, module } = parseLine(value);
      if (view.level !== "all" && level !== view.level) return false;
      if (view.module !== "all" && module !== view.module) return false;
    }
    return true;
  }

  /* 下一个可见项 —— 增量追加与整体重建**共用这一条规则**，所以两条路径不可能走散。
   * 返回 null = 这一行被过滤掉；merged = 并进了上一条折叠组（重复内容不再画）。
   *
   * 展开状态要**沿整组传递**：只把组首标成“摊开”不够，否则组的第 2 行会重新把
   * 第 3 行并进去 —— 看起来就像“只展开了第一行”。 */
  function visibleItem(source, index, tail) {
    const view = source.view;
    const text = source.lines[index];
    // 看不见的行一个节点也不建。**为什么**看不见由 `hiddenReason()` 说清楚。
    if (hiddenReason(view, text)) return null;
    const sameAsTail = Boolean(tail) && tail.text === text;
    const expanded = sameAsTail ? tail.expanded : view.expanded.has(index);
    if (view.dedupe && sameAsTail && !tail.expanded) {
      return { index, text, ranges: [], repeats: tail.repeats + 1, merged: true };
    }
    const { re } = compileQuery(view);
    return { index, text, ranges: matchRanges(re, text), repeats: 1, merged: false, expanded };
  }

  /* 整份派生视图（可见项清单）。复制与宿主回归都拿它当“视图应该长什么样”的参照。 */
  function viewItems(source) {
    const items = [];
    let tail = null;
    for (let index = 0; index < source.lines.length; index += 1) {
      const item = visibleItem(source, index, tail);
      if (!item) continue;
      if (item.merged) {
        tail.repeats = item.repeats;
        continue;
      }
      items.push(item);
      tail = item;
    }
    return items;
  }

  /* 复制/导出用的文本 = **看得见的那些行**：没有 UI 标记，也没有隐藏行。
   * 折叠组只留代表行（重复的那些行确实没显示出来）。 */
  function viewText(source) {
    return viewItems(source).map((item) => source.lines[item.index]).join("\n");
  }

  function totalHits(source) {
    return source.view.hits.reduce((sum, hit) => sum + hit.ranges.length, 0);
  }

  /* 扁平的第 n 处命中 → 哪一行、这一行的第几个。 */
  function locateHit(hits, cursor) {
    let remaining = cursor;
    for (const hit of hits) {
      if (remaining < hit.ranges.length) return { index: hit.index, slot: remaining };
      remaining -= hit.ranges.length;
    }
    return null;
  }

  global.PWModelLogFilter = Object.freeze({
    MAX_QUERY,
    MAX_MARKS_PER_LINE,
    compileQuery,
    matchRanges,
    compileExclude,
    excludeActive,
    lineExcluded,
    hiddenReason,
    excludedCount,
    linePasses,
    visibleItem,
    viewItems,
    viewText,
    totalHits,
    locateHit,
  });
})(globalThis);
