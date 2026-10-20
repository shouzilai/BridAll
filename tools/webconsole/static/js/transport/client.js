/* HTTP 与 SSE 的**唯一出口**（纯传输层：不碰 DOM、不读页面状态、不做业务判断）。
 *
 * 为什么单独一层：页面里散着 `fetch(` 与 `new EventSource` 时，"谁在联网"这件事
 * 只能靠通读全文回答；收成一处之后，超时、错误归一化、头部处理与流断开重连都有
 * 一个明确的落点，宿主回归也能直接喂它。
 *
 * 边界：
 *   - 只返回数据（对象 / 文本 / 字节 / 事件），不改界面；
 *   - 不缓存：静态资源与接口都带 `Cache-Control: no-store`，这里统一 `cache: "no-store"`；
 *   - 错误归一化成 `Error`，消息沿用各调用点原来的说法，不另造一套词。
 */
(function (global) {
  "use strict";

  async function jsonGet(url) {
    const response = await fetch(url, { cache: "no-store" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `${url} -> HTTP ${response.status}`);
    }
    return payload;
  }

  async function jsonPost(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  /* 二进制当文本取（`raw.bin` 可能不是合法 UTF-8：坏字节换成 U+FFFD，不抛异常）。 */
  async function textGet(url) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      return response.ok ? await response.text() : "";
    } catch (error) {
      return "";
    }
  }

  /* 区间/按行装载：把 `Response` 摊平成 `{ ok, status, buffer, header }`。
   * 需要头部（`Content-Range` / `X-Line-Base`）与字节原始长度，所以不在这里解码文本。 */
  async function rangeGet(url, headers, signal) {
    const response = await fetch(url, {
      cache: "no-store",
      headers,
      signal: signal || undefined,
    });
    const buffer = response.ok ? new Uint8Array(await response.arrayBuffer()) : new Uint8Array(0);
    return {
      ok: response.ok,
      status: response.status,
      buffer,
      header: (name) => response.headers.get(name),
    };
  }

  /* SSE：一个入口建连，`onError` 里由调用方决定要不要重连（重连策略属于页面）。 */
  function createStream(url, { onMessage, onError } = {}) {
    const source = new EventSource(url);
    if (onMessage) {
      source.onmessage = (message) => {
        try {
          onMessage(message);
        } catch (error) {
          /* 单帧坏掉不影响整条流 */
        }
      };
    }
    if (onError) source.onerror = () => onError(source);
    return source;
  }

  global.PWTransport = Object.freeze({
    jsonGet,
    jsonPost,
    textGet,
    rangeGet,
    createStream,
  });
})(globalThis);
