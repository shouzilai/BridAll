#!/usr/bin/env node
/*
 * 真实浏览器 E2E 的**驱动**（由 `tests/browser_e2e.py` 启动，不单独当测试用）。
 *
 * 为什么要单独有一个浏览器驱动：服务端的无硬件验收（`test_web_acceptance.py`）证不了
 * **页面在真浏览器里到底能不能用** —— 它只发 HTTP。而 `setViewportSize` 这种"窄屏"证据
 * 在 VS Code 的集成浏览器里会被忽略（上下文是 `viewport: null`），量出来的永远是 1024。
 * 所以窄屏这件事必须由**自己启动的浏览器**来量。
 *
 * 它做的事（全部在真 Chromium 里点真 DOM）：
 *
 * | 视口 | 验的东西 |
 * | --- | --- |
 * | 1280×800（桌面） | 视口真的生效 / 三个入口能切三种模式 / 嗅探出卡片 / 选中 / 连接 /
 * |  | 终端发一句并看到回显 / 断开 / 重连 / 控制台无报错 |
 * | 390×844（窄屏） | 视口真的生效 / 没有横向溢出 / 三个入口可见可点 / 设备面板可用 /
 * |  | 卡片占满一行（≤640px 的布局规则）/ 窄屏也能连上 |
 *
 * 用法：`node tests/browser_e2e.js <baseUrl> [截图目录]`
 * 环境变量：`PW_E2E_PORT` = 期望在卡片上看到的那个端口名（不设就不比）。
 * 退出码：0 = 全绿或 SKIP；1 = 有失败。找不到 playwright 时打 `SKIP:` 并以 0 退出
 * （由调用方决定怎么记这件事 —— 跳过不等于通过，见 `tests/browser_e2e.py`）。
 */
'use strict';

const fs = require('fs');
const path = require('path');

const url = process.argv[2];
const shotDir = process.argv[3] || '';
const expectedPort = process.env.PW_E2E_PORT || '';
const declaredPort = process.env.PW_E2E_DECLARED_PORT || '';

if (!url) {
  console.log('FAIL 用法：node browser_e2e.js <baseUrl> [截图目录]');
  process.exit(2);
}

let chromium = null;
try {
  ({ chromium } = require('playwright'));
} catch (error) {
  console.log('SKIP: 本机 node 找不到 playwright 模块（' + String(error.message).split('\n')[0] + '）');
  process.exit(0);
}

const failures = [];

function check(name, ok, detail) {
  const suffix = !ok && detail ? '：' + String(detail).slice(0, 200) : '';
  console.log((ok ? 'PASS ' : 'FAIL ') + name + suffix);
  if (!ok) failures.push(name);
}

function section(title) {
  console.log('\n[' + title + ']');
}

async function shot(page, name) {
  if (!shotDir) return;
  try {
    fs.mkdirSync(shotDir, { recursive: true });
    await page.screenshot({ path: path.join(shotDir, name) });
  } catch (error) {
    console.log('      （截图没存下：' + String(error.message).slice(0, 120) + '）');
  }
}

/** 按卡片上写的通信口找那张卡片（卡片顺序由服务端定，测试不猜序号）。 */
async function cardByPort(page, port) {
  const cards = page.locator('#sniffCards .sniff-card');
  const count = await cards.count();
  for (let index = 0; index < count; index += 1) {
    const label = String(await cards.nth(index).locator('.sniff-port').textContent() || '').trim();
    if (label === port) return cards.nth(index);
  }
  return null;
}

/** 选中一张卡片，并等到“连接”按钮可用。 */
async function pickCard(page, card, name) {
  await card.click();
  await page.waitForFunction(
    () => {
      const button = document.getElementById('observeConnect');
      return Boolean(button) && !button.disabled;
    },
    null, { timeout: 20000 },
  );
  check('选中卡片之后连接按钮可用：' + name, true);
}

/** 等某个元素的文字里出现 `needle`（页面自己的状态机说了算）。 */
async function waitForText(page, id, needle, timeout) {
  await page.waitForFunction(
    (args) => {
      const node = document.getElementById(args.id);
      return Boolean(node) && (node.textContent || '').includes(args.needle);
    },
    { id, needle },
    { timeout: timeout || 30000 },
  );
}

/** 失败时把“页面上写着什么 + 服务端此刻什么状态”一起打出来：
 * 只说一句“超时了”等于把定位工作丢给下一个人。 */
async function diagnostics(page) {
  try {
    return await page.evaluate(async () => {
      const text = (id) => {
        const node = document.getElementById(id);
        return node ? (node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160) : '';
      };
      let state = {};
      try {
        state = await (await fetch('/api/state')).json();
      } catch (error) {
        state = { error: String(error) };
      }
      const observe = state.observe || {};
      const checked = document.querySelector('#sniffCards input[name="sniffDevice"]:checked');
      return {
        observeState: text('observeState'),
        observeMessage: text('observeMessage'),
        portStatus: text('portStatus'),
        targetNote: text('observeTargetNote'),
        selected: checked ? checked.value : '',
        apiObserveState: observe.state,
        apiDetail: String(observe.detail || '').slice(0, 160),
        apiTransparent: observe.transparent,
        apiIdentity: (observe.identity || {}).state,
        apiSerial: JSON.stringify((observe.serial || {}).effective || {}),
      };
    });
  } catch (error) {
    return { evaluate: String(error.message).slice(0, 160) };
  }
}

async function runDesktop(browser) {
  section('1. 桌面视口 1280×800：页面 / 三模式 / 嗅探 / 选择 / 连接 / 收发 / 断开重连');
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  page.setDefaultTimeout(60000);
  const problems = [];
  page.on('pageerror', (error) => problems.push('pageerror: ' + error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') problems.push('console: ' + message.text());
  });

  await page.goto(url, { waitUntil: 'load' });
  check('页面标题是工作台', String(await page.title()).includes('工作台'), await page.title());
  const width = await page.evaluate(() => window.innerWidth);
  check('视口真的是 1280（不是被忽略的默认值）', width === 1280, String(width));

  for (const [mode, pane] of [['auto', '#autoPanel'], ['live', '#liveStatus'], ['offline', '#offlinePanel']]) {
    await page.click('.mode-btn[data-mode="' + mode + '"]');
    check('入口能切到 ' + pane, await page.locator(pane).isVisible());
  }
  check('离线诊断不显示设备面板（那一路不碰串口）', !(await page.locator('#deviceBar').isVisible()));

  await page.click('.mode-btn[data-mode="live"]');
  check('实时观察显示设备面板', await page.locator('#deviceBar').isVisible());
  check('串口参数四个下拉都在', (await page.locator('#serialRow select').count()) === 4,
    String(await page.locator('#serialRow select').count()));
  await shot(page, 'desktop-01-idle.png');

  // ---- 嗅探
  await page.click('#sniffBtn');
  await page.waitForSelector('#sniffCards .sniff-card', { timeout: 90000 });
  const cardCount = await page.locator('#sniffCards .sniff-card').count();
  check('嗅探出卡片（不是 500、不是空表）', cardCount >= 1, String(cardCount));
  const ports = [];
  for (let index = 0; index < cardCount; index += 1) {
    ports.push(String(await page.locator('#sniffCards .sniff-card .sniff-port').nth(index).textContent() || '').trim());
  }
  if (expectedPort) check('系统枚举到的通用口在卡片里', ports.includes(expectedPort), JSON.stringify(ports));
  if (declaredPort) check('台架点名的口也在卡片里', ports.includes(declaredPort), JSON.stringify(ports));
  const stateText = String(await page.locator('#sniffCards .sniff-card .sniff-state').first().textContent() || '').trim();
  check('卡片上有身份状态那一行', stateText.length > 0, stateText);
  await shot(page, 'desktop-02-sniffed.png');

  // ---- 零配置那条路：**系统枚举到的通用口**（台架没点名）也要能选、能连
  const genericCard = expectedPort ? await cardByPort(page, expectedPort) : null;
  if (genericCard) {
    await pickCard(page, genericCard, '通用口');
    await page.click('#observeConnect');
    try {
      await waitForText(page, 'observeState', '已连接', 40000);
      check('零配置：通用口能连上', true);
    } catch (error) {
      check('零配置：通用口能连上', false, JSON.stringify(await diagnostics(page)));
      throw error;
    }
    await page.click('#observeDisconnect');
    await page.waitForFunction(
      () => !(document.getElementById('observeState') || {}).textContent.includes('已连接'),
      null, { timeout: 30000 },
    );
    check('零配置：通用口能断开', true);
  }
  // ---- 适配后的常规路：**台架点名的口**（终端收发走它）
  const declaredCard = declaredPort ? await cardByPort(page, declaredPort) : null;
  if (declaredCard) {
    await pickCard(page, declaredCard, '台架点名的口');
  } else if (!genericCard) {
    await page.locator('#sniffCards .sniff-card').first().click();
  }

  // ---- 连接
  try {
    await page.click('#observeConnect');
    await waitForText(page, 'observeState', '已连接', 40000);
    check('连接之后状态显示已连接', true);
  } catch (error) {
    check('连接之后状态显示已连接', false, JSON.stringify(await diagnostics(page)));
    throw error;
  }
  const baud = await page.inputValue('#serialBaud');
  check('串口参数行显示 115200', baud === '115200', baud);
  const serialState = await page.evaluate(() => {
    const row = document.getElementById('serialRow');
    return row ? row.textContent.replace(/\s+/g, ' ').trim().slice(0, 120) : '';
  });
  check('参数行渲染出了四个值', serialState.includes('115200'), serialState);
  await shot(page, 'desktop-03-connected.png');

  // ---- 终端收发（发一句，看到设备回显）
  // ⚠️ 走**页面真实存在的**输入路径，两条都算数（顺序即优先级）：
  //    ① 命令输入框（解码窗口底部的命令坞）；
  //    ② 日志窗口正文：点正文拿到键盘，敲的键直接变成字节（W6-4）。
  //    哪条生效会写进检查名 —— 跳过一个输入路径时也要看得出来。
  const token = 'e2e-' + Date.now().toString(36);
  let inputPath = '';
  if (await page.locator('#observeCmdInput').isVisible().catch(() => false)) {
    inputPath = '命令输入框';
    await page.fill('#observeCmdInput', token);
    await page.press('#observeCmdInput', 'Enter');
  } else if (await page.locator('#lines-raw').isVisible().catch(() => false)) {
    inputPath = '日志窗口正文敲键';
    await page.click('#lines-raw');
    await page.keyboard.type(token);
    await page.keyboard.press('Enter');
  }
  check('找得到一条能发字节的输入路径', Boolean(inputPath),
    JSON.stringify(await diagnostics(page)));
  let echoed = false;
  try {
    await page.waitForFunction(
      (needle) => document.body.textContent.includes(needle),
      '[I][echo] ' + token,
      { timeout: 40000 },
    );
    echoed = true;
  } catch (error) {
    echoed = false;
  }
  check('终端发出去的字节在页面上看到回显（' + (inputPath || '没有输入路径') + '）', echoed, token);
  await shot(page, 'desktop-04-terminal.png');

  // ---- 断开 / 重连
  await page.click('#observeDisconnect');
  await page.waitForFunction(
    () => !(document.getElementById('observeState') || {}).textContent.includes('已连接'),
    null, { timeout: 30000 },
  );
  check('断开之后不再是已连接', true);
  await page.click('#observeReconnect');
  await waitForText(page, 'observeState', '已连接', 40000);
  check('重连之后又是已连接', true);

  // ⚠️ 收尾必须**断开**：串口 owner 是**服务端**唯一的（一个进程只握着一条口），
  // 而窄屏那一段是**同一个服务**的另一个上下文 —— 桌面段不放手，窄屏段就连不上
  // （实测：窄屏那条一直等“连接按钮可用”，因为服务端此刻已经连着了）。
  await page.click('#observeDisconnect');
  await page.waitForFunction(
    () => !(document.getElementById('observeState') || {}).textContent.includes('已连接'),
    null, { timeout: 30000 },
  );
  check('桌面这一段结束后已断开（把串口让给下一段）', true);

  check('浏览器控制台没有报错（页面没有静默失败）', problems.length === 0, problems.slice(0, 3).join(' | '));
  await context.close();
}

async function runNarrow(browser) {
  section('2. 窄屏视口 390×844：不溢出 / 入口可点 / 卡片满行 / 窄屏也能连上');
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  const page = await context.newPage();
  page.setDefaultTimeout(60000);
  const problems = [];
  page.on('pageerror', (error) => problems.push('pageerror: ' + error.message));

  await page.goto(url, { waitUntil: 'load' });
  const width = await page.evaluate(() => window.innerWidth);
  check('视口真的是 390（窄屏这件事真的量到了）', width === 390, String(width));
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check('页面没有横向溢出', overflow <= 2, String(overflow));

  for (const mode of ['auto', 'live', 'offline']) {
    const button = page.locator('.mode-btn[data-mode="' + mode + '"]');
    const visible = await button.isVisible();
    check('窄屏下入口可见：' + mode, visible);
    if (visible) {
      await button.click();
      check('窄屏下入口可点：' + mode, true);
    }
  }
  await page.click('.mode-btn[data-mode="live"]');
  check('窄屏下设备面板可见', await page.locator('#deviceBar').isVisible());
  check('窄屏下串口参数行可见', await page.locator('#serialRow').isVisible());
  await shot(page, 'narrow-01-live.png');

  await page.click('#sniffBtn');
  await page.waitForSelector('#sniffCards .sniff-card', { timeout: 90000 });
  const widths = await page.evaluate(() => {
    const host = document.getElementById('sniffCards');
    const card = host ? host.querySelector('.sniff-card') : null;
    return {
      host: host ? host.getBoundingClientRect().width : 0,
      card: card ? card.getBoundingClientRect().width : 0,
    };
  });
  check('窄屏下卡片占满一行（≤640px 的布局规则生效）',
    widths.host > 0 && widths.card > widths.host * 0.9, JSON.stringify(widths));
  const overflowAfter = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth);
  check('嗅探之后仍然没有横向溢出', overflowAfter <= 2, String(overflowAfter));
  await shot(page, 'narrow-02-sniffed.png');

  const narrowCard = (expectedPort ? await cardByPort(page, expectedPort) : null)
    || page.locator('#sniffCards .sniff-card').first();
  await narrowCard.click();
  try {
    await page.waitForFunction(
      () => {
        const button = document.getElementById('observeConnect');
        return Boolean(button) && !button.disabled;
      },
      null, { timeout: 20000 },
    );
    check('窄屏下选中卡片后连接按钮可用', true);
  } catch (error) {
    check('窄屏下选中卡片后连接按钮可用', false, JSON.stringify(await diagnostics(page)));
    throw error;
  }
  await page.click('#observeConnect');
  await waitForText(page, 'observeState', '已连接', 40000);
  check('窄屏下也能连上', true);
  const overflowConnected = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth);
  check('连上之后仍然没有横向溢出', overflowConnected <= 2, String(overflowConnected));
  await shot(page, 'narrow-03-connected.png');

  check('窄屏下浏览器没有报错', problems.length === 0, problems.slice(0, 3).join(' | '));
  await context.close();
}

/** 找一个**真的能起来**的浏览器：自带 Chromium → Edge → Chrome → 环境变量指定的可执行文件。
 *
 * ⚠️ 为什么不直接 `chromium.launch()`：Playwright 与它自带的浏览器是**成对发布**的
 * （版本号要对上）。本机常见的情形是"装了 playwright 1.62，浏览器缓存里还是旧 revision"，
 * 那时 `launch()` 只会说 "Executable doesn't exist"。而 Edge/Chrome 几乎一定在（`channel` 走它）。
 * 证据里必须写出**实际用的是哪一个** —— "跑过浏览器 E2E" 与 "到底在哪个浏览器里跑过" 不是一件事。
 */
async function launchBrowser() {
  const attempts = [
    ['playwright 自带 Chromium', {}],
    ['Edge（channel=msedge）', { channel: 'msedge' }],
    ['Chrome（channel=chrome）', { channel: 'chrome' }],
  ];
  if (process.env.PW_E2E_EXECUTABLE) {
    attempts.push(['环境变量指定的可执行文件', { executablePath: process.env.PW_E2E_EXECUTABLE }]);
  }
  const errors = [];
  for (const [label, options] of attempts) {
    try {
      const browser = await chromium.launch(Object.assign({ headless: true }, options));
      console.log('      浏览器：' + label);
      return browser;
    } catch (error) {
      errors.push(label + '：' + String(error.message).split('\n')[0].slice(0, 140));
    }
  }
  console.log('SKIP: 本机没有能起来的 Chromium / Edge / Chrome');
  for (const line of errors) console.log('      ' + line);
  return null;
}

(async () => {
  const browser = await launchBrowser();
  if (!browser) {
    console.log('RESULT: 跳过（没有可用的浏览器，这一项**没有**验证）');
    process.exit(0);
  }
  try {
    await runDesktop(browser);
    await runNarrow(browser);
  } finally {
    await browser.close();
  }
  console.log('');
  if (failures.length) {
    console.log('RESULT: ' + failures.length + ' 条失败 —— ' + failures.join('；'));
    process.exit(1);
  }
  console.log('RESULT: 全部通过（桌面 1280×800 + 窄屏 390×844：嗅探 / 选择 / 连接 / 终端收发 / '
    + '断开重连 / 三种模式 / 布局不溢出）');
  process.exit(0);
})().catch((error) => {
  console.log('FAIL 浏览器 E2E 抛出异常：' + (error && error.message));
  console.log('RESULT: 1 条失败 —— 浏览器 E2E 抛出异常');
  process.exit(1);
});
