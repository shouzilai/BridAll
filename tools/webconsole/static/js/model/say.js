/* 状态句的**档位机器**（纯模型）。
 *
 * 用户提的：“平台有很多调试语句，加个按钮能选择性地展示”。
 *
 * 这些“调试语句”其实是**状态句**：本平台把服务端给的事实逐条摊在一行里（设备自报的每个
 * 字段、字典来源与 ELF 指纹、会话与证据路径、不能做什么以及为什么…）。调试的时候每一条
 * 都值钱；平时盯着屏幕看的时候，一行太长就只看得见开头的两个字。
 *
 * 所以这里加的是**用户自己按的档位**，而不是替谁判断“哪一条不重要”：
 *   简洁 = 只留结论            （**屏幕上现在固定用这一档**，2026-10-12 起）
 *   标准 = 结论 + 关键项
 *   详细 = 全量原文            （自报的机器元数据、字典指纹…都摊开，就是改之前的写法）
 * ⚠️ 这三档仍由模型回归直接驱动（喂快照 + 档位名），不是只有“当前那一档”能测。
 *
 * ⚠️⚠️ 铁律（**不许因为藏了字就说出另一句话**）：
 *   ① **结论永远显示**：“身份：已回应”“拒绝解码，不猜”“这一路不做解码”…在任何档位都在；
 *      档位只能收起**支撑结论的原始字段/次级细节**，不能收起“行还是不行”这件事。
 *   ② 被收起的原文一定挂在该元素的 `title` 上（`sayTitle()`），悬停就看得到全量，
 *      而且 title 里明说“当前这一档收起了几项”（屏幕上那句不会被读成“全都在这里了”）。
 *   ③ 档位是**显示**偏好，不是事实：服务端说的那些字一个都没被改写，也没被重算。
 *
 * 这里只决定“铺开多少”，不含任何 DOM/存储/定时器；本机偏好（存储读写）留在组合根。
 *
 * 装载顺序：sources → format → … → observe → say → event-route → runs → transport → app
 */
(function (global) {
  "use strict";

  const SAY_LEVELS = [
    { id: "brief", label: "简洁", sub: "详情里只留结论那一句" },
    { id: "normal", label: "标准", sub: "详情给关键项" },
    { id: "full", label: "详细", sub: "详情给全量原文：原始字段与机器元数据都摊开" },
  ];
  const SAY_LEVEL_IDS = SAY_LEVELS.map((entry) => entry.id);
  /** 页面上**没有**档位控件（2026-10-12 起）：屏幕上固定用这一档。 */
  const SAY_DEFAULT = "brief";
  const SAY_RANK = { brief: 0, normal: 1, full: 2 };
  /* 一个片段要求的最低档位：结论 / 关键项 / 原始字段。 */
  const SAY_ALWAYS = 0;
  const SAY_KEY_ITEM = 1;
  const SAY_RAW = 2;

  /** 这一次用哪一档：给了合法的就用它，否则回到默认档（不读本机偏好）。 */


  /** 一个片段：`seg("文字")` = 结论（任何档位都显示）；`seg("文字", SAY_RAW)` = 只在详细档铺开。 */
  function seg(text, min = SAY_ALWAYS) {
    const value = text === null || text === undefined ? "" : String(text);
    return { text: value, min: Number(min) || 0 };
  }

  /* 按档位把一段段拼成一句。同时给出**没过筛的全文**与**被收起的条数**：
   * 调用方把 `full` 挂到 `title` 上 —— 档位只决定“要不要铺开”，决定不了
   * “这句话到底说了什么”（原文一个字都没改）。 */
  function say(level, segments) {
    const rows = [];
    for (const item of (segments || []).flat()) {
      if (!item) continue;
      const row = typeof item === "string" ? seg(item) : item;
      if (!row.text) continue;
      rows.push({ text: String(row.text), min: Number(row.min) || 0 });
    }
    const wanted = sayLevel(level);
    const limit = SAY_RANK[wanted];
    const shown = rows.filter((row) => row.min <= limit);
    return {
      text: shown.map((row) => row.text).join(" · "),
      full: rows.map((row) => row.text).join(" · "),
      hidden: rows.length - shown.length,
      level: wanted,
    };
  }

  /** 只要文字版（调用方不关心 title 时用它）。 */
  function sayText(level, segments) {
    return say(level, segments).text;
  }

  /* title：被档位收起来的东西在这里交代清楚（悬停即见全量原文）。
   * `note` 是这一格本来就在 title 里说的那句话（会话号 / 证据目录…），原样保留。
   *
   * ⚠️ 屏幕上那句永远不会被读成“全都在这里了”：只要这一档真收起了东西，title 里就先说
   *    一句“当前「简洁」档收起了 N 项细节”，再说全量原文。
   * ⚠️ 页面上**没有**档位控件（2026-10-12 用户口径：删掉「详情」选择栏、默认简洁）⇒ 这句只说
   *    “收起了几项 + 完整原文在下面”，不再叫人去选另一档。
   * ⚠️ 档位从 `said.level` 取（不从当前偏好）：模型回归可以喂任意一档，
   *    那时 title 里说的必须是被喂的那一档。 */
  function sayTitle(said, note = "") {
    const parts = [];
    if (note) parts.push(note);
    // 这一档没有收起任何东西（屏幕上那句就是全文）：不补一行重复同一句话的“完整原文”。
    if (!said || !said.full || said.text === said.full) return parts.join("\n");
    const folded = Number(said.hidden) || 0;
    parts.push(folded
      ? `收起了 ${folded} 项细节（完整原文就在下面）`
      : `细节收起来了（完整原文就在下面）`);
    parts.push(`完整原文：${said.full}`);
    return parts.join("\n");
  }

  /** 把几句拼成一块（`text` 用给的档位，`full` 是没过筛的全文，`title` 给全量 + 收起了几项）。 */
  function sayBlock(saids) {
    const rows = (saids || []).filter(Boolean);
    const text = rows.map((row) => row.text).filter(Boolean).join(" · ");
    const full = rows.map((row) => row.full).filter(Boolean).join(" · ");
    const hidden = rows.reduce((sum, row) => sum + (Number(row.hidden) || 0), 0);
    return { text, full, hidden, title: sayTitle({ text, full, hidden, level: sayLevel() }) };
  }

  global.PWModelSay = Object.freeze({
    SAY_LEVELS, SAY_LEVEL_IDS, SAY_DEFAULT, SAY_RANK,
    SAY_ALWAYS, SAY_KEY_ITEM, SAY_RAW,
    seg, say, sayText, sayTitle, sayBlock,
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
