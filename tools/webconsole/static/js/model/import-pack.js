/* 诊断包导入的**纯文案**（进度那一行、阶段自述、成功总结、失败/取消）。
 *
 * 这里只把服务端给的数读成一句人话，不碰 DOM、不发请求、不自己算进度：
 *   · `percent`：**总量未知就是 null**（不许画一个假的百分比）；
 *   · 上传还在飞的时候用**浏览器**报的已发/总量（服务端的 `received_bytes` 一定落后，
 *     拿它当进度会让进度条**往回退**）；请求体交出去之后才换成服务端的阶段与进度；
 *   · “用户取消了”与“服务端拒了”是两件事：前者是**用户意图的完成**（warn），后者才是 fail。
 *
 * 依赖：`locate.js`（`byteText` 的写法）。
 *
 * 装载顺序：… → locate → 本文件 → search → …
 */
(function (global) {
  "use strict";

  const locate = global.PWModelLocate;
  if (!locate) {
    throw new Error("PWModelLocate 必须先于 PWModelImportPack 装载");
  }
  const { byteText } = locate;


  function importFailureText(error) {
    const code = error && error.code ? String(error.code) : "";
    // 两种“取消”都要认：服务端把结论写成了 409 `import_cancelled`，或者上传是被本地中断的。
    const cancelled = code === "import_cancelled" || Boolean(error && error.cancelled);
    if (cancelled) {
      return {
        text: "这次导入已取消（没收完的临时包已删掉）",
        kind: "warn",
        cancelled: true,
        code: code || "import_cancelled",
      };
    }
    return {
      text: `导入被拒：${error && error.message ? error.message : error}`,
      kind: "fail",
      cancelled: false,
      code: code,
    };
  }

  function importProgressModel(state) {
    const sent = Math.max(0, Number(state && state.sent) || 0);
    const size = Number(state && state.size) || 0;
    const task = (state && state.task) || null;
    const known = size > 0;
    const handedOver = Boolean(state && state.uploaded);
    const serverish = Boolean(task)
      && (handedOver || task.stage !== "upload" || task.state !== "running");
    if (serverish) {
      const share = task.percent === null || task.percent === undefined
        ? null : Number(task.percent);
      return {
        percent: Number.isFinite(share) ? share : null,
        text: importStageText(task),
        canCancel: task.state === "running",
        finished: task.state !== "running",
        from: "server",
      };
    }
    return {
      percent: known ? Math.min(1, sent / size) : null,
      text: known
        ? `正在上传 ${byteText(sent)} / ${byteText(size)}`
        : `正在上传 ${byteText(sent)}（大小未知）`,
      canCancel: true,
      finished: false,
      from: "browser",
    };
  }

  function importStageText(task) {
    if (!task) return "";
    const label = task.stage_label || task.stage || "";
    const parts = [label];
    if (task.state === "done" && task.result && task.result.session_id) {
      parts.push(`已导入 ${task.result.session_id}`);
    }
    if (task.state === "failed" && task.error) parts.push(`失败：${task.error_why || task.error}`);
    if (task.state === "cancelled") parts.push("已取消");
    return parts.filter(Boolean).join(" · ");
  }

  function importResultText(imported) {
    const row = imported || {};
    const check = row.verified
      ? `已逐份核对（${(row.checks || []).length} 份）`
      : "包里没有核对信息，只确认了结构合法";
    return `已导入 ${row.session_id}（原包里的会话号 ${row.origin_session_id}；${check}）`;
  }
  global.PWModelImportPack = Object.freeze({
    importProgressModel,
    importStageText,
    importResultText,
    importFailureText,
  });
})(globalThis);
