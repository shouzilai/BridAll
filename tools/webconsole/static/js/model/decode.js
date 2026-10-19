/* 解码窗为什么空着 / 为什么不该有内容 —— **纯文案**。
 *
 * 用户问过："原始串口在输出，解码日志完全没动静，是应该有前置条件吗？"
 * 是有的，而且服务端把它们逐条写在 `decode.reason` 里：平台要声明 decode=supported、
 * 项目要有 token_domain、装配处要交进字典库与解码器，最后还要**精确命中字典**。
 * 这里只转述服务端说的原因（+当前能不能触发），不自己编一套说法。
 *
 * 两个状态词不能混（用户口径 2026-10-12：“将长篇前置条件压缩为「未就绪」或「不适用」”）：
 *   「不适用」= **这条路本来就不做**；「未就绪」= **还没到位**。
 *   混了就会把“不做”读成“等等就好”（ 那一族错的老毛病）。
 *
 * ⚠️：**先问“这一屏看的对象”要不要解码，再谈“为什么没内容”**。实测
 *    （2026-10-08）自动验证屏上写着“主机没有 build 0x93dafbcb 对应的字典”—— 那是**实时观察**
 *    那次会话的设备，末尾还跟着“（先连接设备）”，而这一屏根本没有这个动作。
 *    所以固定入参收敛成 `{ mode, capability, observe, level }`：非实时观察屏只说自己这一屏的事
 *    （`capability` 由组合根按当前屏的来源算好递进来），拿不到自己那份原因就不说。
 *
 * 不碰 DOM、不发请求；读 `app.mode` 的那一层留在组合根。
 *
 * 装载顺序：sources → … → say → identity → 本文件 → event-route → …
 */
(function (global) {
  "use strict";

  const sayMod = global.PWModelSay;
  if (!sayMod) {
    throw new Error("PWModelSay 必须先于 PWModelDecode 装载");
  }
  const { resolveLevel } = sayMod;

  const DECODE_NOT_APPLICABLE = "解码：不适用";
  const DECODE_NOT_READY = "解码：未就绪";

  function decodeNotApplicable(why, reason, brief) {
    if (brief) return DECODE_NOT_APPLICABLE;
    return `${DECODE_NOT_APPLICABLE}（${why}）${reason ? `：${reason}` : ""}`;
  }

  /* 解码窗口为什么空着（空串 = 这一屏此刻不该有这句话）。 */
  function decodeWhyText(observe, level) {
    if (!observe) return "";
    const decode = observe.decode || {};
    if (decode.ready) return "";   // 正在解码：这行本来就该消失
    // 简洁档（用户口径 2026-10-10）：只留“空的 + 服务端给的原因”，那些“你可以怎么做”
    // 的引导语属于读过一次就知道的东西，不必每时每刻占着屏。
    const brief = sayLevel(level) === "brief";
    // ⚠️ 服务端没给原因时**不能自己编一条**：`|| "没有匹配到字典"` 是猜的 ——
    //    会话一空闲/断开，服务端就把原因清空（`observe.py` 里那位），于是这句凭空
    //    断定了“没命中字典”（实测：空闲态 `reason=""`，页面却这么说）。空就只说空着。
    const reason = decode.reason || "";
    /* ：**先问“这一屏看的对象”要不要解码，再谈“为什么没内容”**。
     * ⚠️ 实测（2026-10-08）：自动验证屏上写着“主机没有 build 0x93dafbcb 对应的字典” —— 那是
     *    **实时观察**那次会话的设备，末尾还跟着“（先连接设备）”，而这一屏根本没有这个动作
     *    ⇒ 不是措辞问题，是把别的对象的原因贴在了这一屏上。
     * 修法：非实时观察屏**只说自己这一屏的事**；拿不到自己那份原因就不说，不借用别人的。 */
    if (app.mode !== "live") {
      if (screenDecodeCapability(observe) === false) {
        // 需求 §2 点名的措词就是“当前固件未启用 Token Log”；
        // ⚠️ 用户口径 2026-10-12：屏幕上只说「不适用」，这句完整的话与服务端给的原因进悬停。
        return decodeNotApplicable(`当前固件未启用 Token Log`, reason, brief);
      }
      return "";
    }
    const caps = observe.capabilities || {};
    /* 用户口径（2026-10-13）：身份未识别 ⇒ 这一条会话按**原始串口模式**工作，屏幕上说
     * 「不适用」（服务端给的那句完整话在悬停里）。
     * ⚠️ 只说在**还没在解码**的时候：万一连上之后身份查询回来了、字典也精确命中，
     *    解码会自己跑起来 —— 那时 `decode.ready` 为真，上面那一行早把这句说明收掉了。 */
    if (observe.transparent && !decode.ready) {
      return decodeNotApplicable(
        String(observe.transparent_reason || "身份未识别"), reason, brief);
    }
    if (!caps.decode) {
      return decodeNotApplicable("这一路不做解码", reason, brief);
    }
    // 能力上支持解码、但字典还没到位：屏幕上只说「未就绪」（原因在悬停里）。
    const ready = "解码：未就绪";
    if (brief) return ready;
    return `${ready}${reason ? `：${reason}` : ""}`
      + (observe.state === "connected"
        ? "（连接后会再查一次身份；命中字典之后才开始逐行解码）"
        : "（先连接设备）");
  }

  /* 解码窗底那一句的**两个版本**：屏幕上按当前档位，title 里给全量。
   * ⚠️ 两个版本走**同一个实现**（只是档位不同），不另写一份“详细版”文案。 */
  function decodeWhySay(observe, level) {
    const text = decodeWhyText(observe, level);
    const full = decodeWhyText(observe, "full");
    return { text, full, level: sayLevel(level), hidden: text && full && text !== full ? 1 : 0 };
  }

  /* PWTOKEN-001：解码窗**默认收成标签**，只有“这一路支持解码 + 本地确实有字典”才自动展开。
   * 判据只有服务端说的两件事：`capabilities.decode`（支持吗）与 `decode.ready`（正在解码吗）。
   * ⚠️ 页面**不自己推断**有没有字典。 */
  function decodeReadyToShow(observe) {
    if (!observe) return false;
    return (observe.capabilities || {}).decode === true
      && (observe.decode || {}).ready === true;
  }

  global.PWModelDecode = Object.freeze({
    DECODE_NOT_APPLICABLE, DECODE_NOT_READY, decodeNotApplicable, decodeWhyText, decodeWhySay,
    decodeReadyToShow
  });
})(typeof globalThis !== "undefined" ? globalThis : this);
