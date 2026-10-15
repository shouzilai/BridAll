# 版本与发布信息

> 版本号：见仓库根目录 `VERSION`。本文件回答"这一版是怎么生成的、验过什么、有什么已知限制"。

## 1. 这一版是什么

首次发布版本（`0.1.0`）：一套与芯片、工具链无关的**硬件接入框架**（`tools/`）；
参考适配随后逐笔加进 `targets/`。

不含：任何私有产品工程、私有固件、真实运行证据、厂商受限二进制、本机绝对路径或串口名，
以及内部的开发过程文档。

## 2. 怎么生成的

1. 建立纳入 / 排除清单（哪些代码、文档、示例可以公开）；
2. 对**当前工作树**与**全部 Git 历史**做敏感信息扫描（凭据、本机路径、串口名、大文件、
   受限资产），只从纳入清单里取内容；
3. 对纳入内容做脱敏：本机绝对路径改成中性说法，移除指向仓库外文档的链接；
   内部目标/固件 ID 示例 → 中性示例；
4. 许可证与第三方审计（见 `THIRD_PARTY_NOTICES.md`）；
5. 在**没有本机台架配置、没有私有工程**的条件下跑通：`doctor.py`、
   `tests/run_all.py`、`create-adapter` 生成物自检；
6. 把内容按三笔提交组织进一个**全新初始化的**本地仓库（不沿用既有仓库的历史）。

## 3. 干净环境验证（本版实测）

在仓库根目录、**没有 `bench.local.json`、没有安装 pyserial** 的条件下：

| 命令 | 期望 |
| --- | --- |
| `python tools\doctor.py` | 无 `FAIL`，退出码 0（缺工具只给 `WARN`） |
| `python tests\run_all.py` | 全部通过，退出码 0 |
| `python tools\hwops.py check` | `通过`（没有 `problems`），退出码 0 |
| `python tools\hwops.py opensource-audit --scope all` | 0 处本机事实 |
| `python tools\hwops.py create-adapter --out <临时目录> --id demo` | 生成物自检 **12/12** 通过 |

`WARN` 的含义：这台机器还没有告诉框架"工具装在哪、工程在哪"，属于**待配置**，
不是缺陷（`bench.example.json` 就是给你抄的）。

⚠️ 上表是在**还没有** `bench.local.json` 的前提下测的：一旦按 `INSTALL.md` 建好并填上台架文件，
`opensource-audit --scope all` 会把里面的真实路径照实报出来（`.gitignore` 挡的是**提交**，不是扫描），
`run_all.py` 随之变红 —— 那说明扫描在工作，不是回归。

## 4. 已知限制

- 参考适配声明的是**调用形状与判据**，不是"保证在你的工具链版本上原样跑通"：
  参数名、输出标记都可能随厂商工具版本变化，接自己的硬件时请按实际输出核对判据；
- 串口实现假定"打开前先设 DTR/RTS"就能避免复位；不同平台的驱动行为可能不同，
  接真机时请先确认设备没有被复位；
- `doctor.py` 的端口占用检查在非 Windows 平台依赖 `lsof`；
- 各条参考适配自己的限制写在 `targets/README-*.md` 里（例如"哪一项期望值还没接上构建产物"）；
- 本版**不做**：远程设备农场、云端编译、通用 IDE、任意 shell；
- `create-adapter` 的 `--id` 写**点分**形式会推导出带连字符的模块名，生成物里
  `from demo-target import ...` 直接 `SyntaxError`：请用连字符（`my-board`）；
- 骨架**不生成** `bench_declaration()`（台架键的消费方，见
  [docs/adapter-guide.md](adapter-guide.md) §3），要用台架键得自己补。

## 5. 校验和

生成物（本地仓库）的提交 ID 与完整清单见发布时随附的验收记录；
需要校验内容时用 `git rev-parse HEAD` 取提交，`git archive` 打包后用 `Get-FileHash` 比对。
