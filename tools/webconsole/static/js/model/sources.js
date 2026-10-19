/* 日志工作台的**窗口清单**（纯数据，无 DOM、无请求）。
 *
 * 五个窗口是同一份工作区的五个来源，顺序就是屏幕上的先后顺序。ids 也是**所有**
 * 偏好的键（收起/宽度/折叠/筛选行的开合），所以这份清单必须是唯一出处 ——
 * 模型侧（偏好解码、布局规范化）与组合根都从 `PWModelSources` 取。
 *
 * ⚠️ 加窗口要一起改这里：少一处就会“分成 5 栏”却只画 4 栏，而界面上不会报错。
 */
(function (global) {
  "use strict";

  const SOURCES = [
    /* 工具输出：⚠️ 它是**派生视图**（`derived`）—— 内容是 `io_events.jsonl` 重画出来的，
     * 而 RX 事件根本不画（只落索引）⇒ 它的行号与文件行号不同源。所以它的每一行带的是
     * 它来自的那条 io 事件的**事件号**（`seq`），阶段卡要指到这一窗的某一行就用
     * `{ source: "tool", event_seq: N }` 而不是行区间。
     * 例外：自动验证 run 那一屏的「工具输出」看的是 tool.log 这份真文件 ⇒ 行号是真的，
     * 那种装载不带 `tool_seqs`，这一窗就不是派生视图（按行号定位照旧）。 */
    {
      id: "tool", label: "工具输出", hint: "构建、烧录与采集的输出", file: "tool.log",
      derived: true,
    },
    /* 阶段窗口（T6 迁入 + 2026-10-10 并排展示）：`file: ""` 是因为它**没有自己的证据文件**
     * —— 它的来源是 run 事件流里的 `test_step`/`test_step_refs`（见 renderStepCards）。
     * 没有证据文件 ⇒ 没有下载链接（不编一个指向别处的链接出来）。 */
    {
      id: "stage", label: "测试阶段", file: "", cards: true,
      hint: "这一轮的阶段事件（卡片）",
    },
    { id: "raw", label: "原始串口", hint: "设备发出的原始字节（非 UTF-8 字节按替换字符显示）", file: "raw.bin" },
    { id: "decoded", label: "解码日志", hint: "解码之后的日志", file: "decoded.log" },
    { id: "events", label: "结构化事件", hint: "结构化事件，一行一条", file: "app_events.jsonl" },
  ];
  const SOURCE_IDS = SOURCES.map((entry) => entry.id);
  const SOURCE_BY_ID = Object.fromEntries(SOURCES.map((entry) => [entry.id, entry]));
  const SOURCE_LABEL = Object.fromEntries(SOURCES.map((entry) => [entry.id, entry.label]));

  global.PWModelSources = Object.freeze({
    SOURCES,
    SOURCE_IDS,
    SOURCE_BY_ID,
    SOURCE_LABEL,
  });
})(globalThis);
