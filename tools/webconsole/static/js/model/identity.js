/* 身份那一块与“这一路还没做什么”的**纯文案**。
 *
 * 身份：**设备自报**与**主机匹配**必须分开说（需求 §6.2）。没回应的字段写"未提供"，
 * 绝不为了好看编一个值。三件事分开讲，因为它们是三个不同的事实：
 *   ① 设备自报身份查到没有（服务端的状态词，页面不推断）；
 *   ② **主机**能不能拿这个 build 找到字典（命中 / 不匹配 / 连可查的号都没有）；
 *   ③ 这一刻在不在解码。
 * ⚠️ ②是“主机匹配”的结论，不是设备的能力：写“设备不支持字典”会把责任安错人。
 *
 * 档位（用户口径 2026-10-10）：结论（“身份：已回应” / “拒绝解码，不猜” / “实时解码 N 行”）
 * 在任何档都显示；设备自报的字段属于“关键项”；`schema` / `protocols` 与字典的 ELF 指纹
 * 属于机器对机器的字段，只在详细档铺开。
 *
 * 屏幕上那几枚 chip **只允许**是短结论：不带 build 号、不带字典路径、不带可用字典清单。
 *
 * 依赖：`say.js`（`say`/`seg`/`sayBlock` 与 `SAY_KEY_ITEM`/`SAY_RAW`）。
 * 不碰 DOM、不发请求。
 *
 * 装载顺序：sources → … → observe → offline → **say** → identity → event-route → …
 */
(function (global) {
  "use strict";

  const sayMod = global.PWModelSay;
  if (!sayMod) {
    throw new Error("PWModelSay 必须先于 PWModelIdentity 装载");
  }
  const { say, seg, sayBlock, SAY_KEY_ITEM, SAY_RAW } = sayMod;

  /* 服务端说的那几个身份状态词（页面只转述，不推断）。 */
  const IDENTITY_TEXT = {
    unknown: "未查询",
    querying: "查询中",
    ok: "已回应",
    timeout: "无响应",
    unsupported: "设备不支持",
  };
  /* 设备自报的**机器对机器**字段（只在详细档铺开）。 */
  const SAY_SELF_METADATA_FIELDS = ["schema", "protocols"];

  function observeIdentitySay(observe, level) {
    if (!observe) return say(level, []);
    const identity = observe.identity || { state: "unknown", fields: {} };
    const state = IDENTITY_TEXT[identity.state] || identity.state || "未知";
    const rows = Object.entries(identity.fields || {})
      .filter(([, value]) => value !== null && value !== undefined && value !== "");
    const show = (pairs) => pairs
      .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`);
    const mine = show(rows.filter(([key]) => !SAY_SELF_METADATA_FIELDS.includes(key)));
    const meta = show(rows.filter(([key]) => SAY_SELF_METADATA_FIELDS.includes(key)));
    const parts = [seg(`身份：${state}`)];
    if (!rows.length) parts.push(seg("设备没有自报字段"));
    if (mine.length) parts.push(seg(`设备自报 ${mine.join(" ")}`, SAY_KEY_ITEM));
    if (meta.length) parts.push(seg(`握手元数据 ${meta.join(" ")}`, SAY_RAW));
    const archive = identity.archive || null;
    if (archive && archive.path) {
      const hash = (archive.elf_sha256 || "").slice(0, 12);
      // 命中的是哪份字典（名字/来源与条数）是结论；那串 ELF 指纹是核对用的原始字段。
      parts.push(seg(
        `主机匹配 字典 ${archive.origin || archive.path}（${archive.entries} 条）`,
        SAY_KEY_ITEM));
      if (hash) parts.push(seg(`字典指纹 ELF ${hash}`, SAY_RAW));
    } else if (identity.build_id32 !== null && identity.build_id32 !== undefined) {
      // 有 build id 但没命中：这是一个**结论**，不是"还没做"。
      // ⚠️ 简洁档也要留住“拒绝解码”这四个字（那是结论）；具体是哪个号只在标准及以上铺开。
      const hex = `0x${Number(identity.build_id32).toString(16).padStart(8, "0")}`;
      parts.push(seg(sayLevel(level) === "brief"
        ? "主机匹配 没有对应的字典（拒绝解码，不猜）"
        : `主机匹配 没有 build ${hex} 对应的字典（拒绝解码，不猜）`));
    } else if (identity.source !== "none") {
      // ⚠️ 不能把这一种写成"没有对应字典"：我们连**可查的号**都没有，
      // 那是两件不同的事实（一个说查过了没命中，一个说没得可查）。
      parts.push(seg("主机匹配 设备没有报告 build id，没有可查的目标"));
    }
    const decode = observe.decode || {};
    if (decode.ready) parts.push(seg(`实时解码 ${decode.lines} 行`));
    // ⚠️ 服务端没给原因时不能自己编一条：只说“未解码”（简洁档连这一句也收短）。
    else if (decode.reason) {
      parts.push(seg(sayLevel(level) === "brief" ? "未解码" : `未解码：${decode.reason}`));
    }
    return say(level, parts);
  }

  function observeIdentityLine(observe, level) {
    return observeIdentitySay(observe, level).text;
  }

  /* 身份**结论**（屏幕上那几枚 chip 的文字）—— 用户口径 2026-10-11 验收 P1：
   * 「主界面仅保留身份结论，例如“身份已确认 / 字典不匹配”；完整原因和字典清单移入悬停或详情入口」。
   *
   * 所以这个函数**只允许**返回短结论：它不能带 build 号、不能带字典路径、不能带可用字典清单。
   * （判据里就把这条钉死了：`JSON.stringify(summary)` 里不许出现那些东西。） */
  function identitySummary(observe) {
    const identity = (observe && observe.identity) || {};
    const archive = identity.archive || null;
    const hasBuild = identity.build_id32 !== null && identity.build_id32 !== undefined;
    let label = "身份未查询";
    let kind = "";
    if (identity.state === "ok") {
      label = "身份已确认";
      kind = "ok";
    } else if (identity.state === "querying") {
      label = "身份查询中";
    } else if (identity.state === "timeout") {
      label = "身份无响应";
      kind = "fail";
    } else if (identity.state === "unsupported") {
      label = "设备不支持身份查询";
      kind = "warn";
    }
    let dictLabel = "";
    let dictKind = "";
    if (identity.state === "ok") {
      if (archive) {
        dictLabel = "字典已命中";
        dictKind = "ok";
      } else if (hasBuild) {
        dictLabel = "字典不匹配";
        dictKind = "fail";
      } else {
        dictLabel = "没有可查的 build id";
        dictKind = "warn";
      }
    }
    const decode = (observe && observe.decode) || {};
    let decodeLabel = "";
    let decodeKind = "";
    if (decode.ready) {
      decodeLabel = `解码 ${decode.lines} 行`;
      decodeKind = "ok";
    } else if (decode.reason) {
      // “未解码”是结论；**为什么**（含手上有哪些字典）在详情与悬停里。
      decodeLabel = "未解码";
      decodeKind = "warn";
    }
    return { label, kind, dictLabel, dictKind, decodeLabel, decodeKind };
  }

  /* 这一步还**没有**的能力，直接由服务端的能力声明派生（不是这里硬编码的承诺）。 */
  function observeLimitsSay(observe, level) {
    if (!observe) return say(level, []);
    const caps = observe.capabilities || {};
    // 每一条都是“结论（任何档都在）+ 为什么/在你手上看到的样子（标准及以上）”。
    const notes = [];
    if (!caps.evidence) {
      notes.push(seg("本切片只统计收发字节"));
      notes.push(seg("尚未写成会话证据（raw.bin / io_events.jsonl 在下一步）", SAY_KEY_ITEM));
    }
    // ⚠️ 这句话过时了（实时解码早就实现了）：`decode=false` 现在**只有**一种含义 ——
    //    这一路按能力声明就不做解码（PWTOKEN-001 的关闭 Token Log 构型）。说成"尚未实现"
    //    是拿旧的进度当真话讲，与"不猜不编"冲突。
    if (!caps.decode) {
      notes.push(seg("这一路没有解码能力"));
      notes.push(seg("会话里只有原始字节与事件记录", SAY_KEY_ITEM));
    }
    if (!caps.send) {
      notes.push(seg("命令框与终端直输尚未实现"));
      notes.push(seg("现在只收不发", SAY_KEY_ITEM));
    }
    if (!caps.identity) {
      notes.push(seg("身份握手尚未实现"));
      notes.push(seg("不会主动去问设备型号", SAY_KEY_ITEM));
    }
    return say(level, notes);
  }

  function observeLimits(observe, level) {
    return observeLimitsSay(observe, level).text;
  }

  /* 「详情」里那一整段（也是 `#identChip` 悬停里那一份）：自报字段 + 主机匹配 + 能力提示。
   * 档位只管**这里**铺开多少（屏幕上的 chip 永远只有结论）。返回 `sayBlock()` 的形状：
   * `text` = 当前档位下展开的那一段，`title` = 全量原文 + “这一档收起了几项”。 */
  function identityDetailSay(observe, level) {
    return sayBlock([
      observeIdentitySay(observe, level),
      observeLimitsSay(observe, level),
    ]);
  }

  /* 悬停里那一份：**永远**给全量原文（结论 chip 很小，悬停是它的详情入口之一；
   * 另一个是「详情」按钮 —— 两个入口给的是**同一句话**，逐字一致）。 */
  function identityHoverTitle(detail) {
    if (!detail || !detail.full) return "";
    const parts = [];
    if (detail.text !== detail.full) {
      // 页面上没有档位控件（2026-10-12）：只说“屏幕上那句是简化过的、全文就在下面”。
      parts.push("屏幕上那句是简化过的（完整原文就在下面）");
    }
    parts.push(`完整原文：${detail.full}`);
    return parts.join("\n");
  }

  global.PWModelIdentity = Object.freeze({
    IDENTITY_TEXT, SAY_SELF_METADATA_FIELDS,
    observeIdentitySay, observeIdentityLine, identitySummary,
    observeLimitsSay, observeLimits, identityDetailSay, identityHoverTitle,
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
