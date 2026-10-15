# 平台架构与边界

> 权威来源是代码里的 `tools/webconsole/platform_boundary.py`（它同时是**机器检查**的数据源）。
> 本文只解释它，不复制它的表。

## 1. 三层

```text
core            平台核心：契约、编排、检查、脚手架、假工具、台架、体检模型、边界自述
target-*        目标适配：这块板 / 这套工具链的知识（工具、命令、判据、故障、身份协议）
```

依赖方向只能是 **core ← target**。core 里出现目标知识就是边界破了，而这条**有机器判据**：

| 判据 | 挡的是什么 |
| --- | --- |
| `module_layer_problems()` | 新模块没在分层表里登记（"没人知道它属于谁"） |
| `scope_problems()` | core 模块 import 了目标层模块（唯一口子：`COMPOSITION_MODULES`） |
| `token_hit_problems()` | core 的**代码**里出现目标层字面量（工具名 / 芯片族名） |

跑一次就看到结论：

```powershell
python tools\webconsole\platform_boundary.py
python tools\hwops.py check          # `Core 泄漏` 那一项就是上面三条
```

## 2. core 里有什么

| 模块 | 职责 |
| --- | --- |
| `hwops_contract` | **接入契约的形状与纯函数**：步骤、判据、故障、能力四态、身份核对。只有形状，不碰机器 |
| `hwops_runner` | **统一操作服务**：什么时候做、把结果记成证据、失败时说什么。一切注入 |
| `hwops_check` | **机器检查**：契约完整性、旁路命令、判据来源、跨 profile 串用、Core 泄漏、开源边界 |
| `hwops_fake` | **假工具与假串口** + 一份中性参考契约（契约测试与脚手架冒烟共用） |
| `hwops_scaffold` | **脚手架**：生成声明骨架 + 契约测试 + 接入清单 |
| `bench_config` | **本机台架**：读文件、按声明校验、产出"带来源的值"。库代码自己从不读文件 |
| `bench_setup` | **装配处**：把目标的声明接进框架（唯一允许 import 目标的地方之一） |
| `target_adapters` | **受控命令的形状**：`CommandSpec`（argv 由代码生成，配置里进不来命令） |
| `core_primitives` / `console_input` / `unicode_console` | 时间戳与行切分 / 输入字节的唯一转换 / 控制台编码降级 |
| `platform_boundary` | 上面那张表的唯一来源 |
| `doctor_model` / `doctor_platform` / `doctor_target` | 体检的契约模型 / 平台层检查 / 目标扩展接口 |
| `hwops` / `doctor` | 两个**组合入口**（命令行外壳） |

## 3. 一次操作的形状

```text
PREFLIGHT ──┬─ 契约自洽？步骤覆盖全？输入在？工具解析得出来？台架配了？
            └─ 任一不成立 ⇒ 拒绝开始（不部分采用）

BUILD ──> FLASH ──> VERIFY_IDENTITY ──> DEBUG_CHECK ──> PASS
           │            │
           │            └─ 四态：pass / mismatch / 未核验 / 核验不了
           └─ 安全闸门：没有两条显式同意 ⇒ refused，**一个字节都不写**

失败 ⇒ 稳定故障码 + 处置建议 ⇒ 同一故障码连续两次之后，第三次在调工具前被拒（熔断）
```

每一步都落一份只追加的证据 JSON（`artifacts/hwops/`，不进仓库）：步骤时间、工具 argv、
耗时、产物摘要（大小 + SHA-256）、结果码、故障码、输出尾部。

## 4. "期望值从哪来"比"比了什么"重要

烧录成功要同时满足两件事：**厂商工具的判据** 与 **设备的实际身份核验**。
而"这次烧的就是这次编的"要求**期望值来自本次构建的产物**（`IdentitySource`），
不能是写死的常量 —— 写死的只能证明"设备报得出某个值"。

读不到那份产物时，该字段记成 **未比**（`unresolved`）并出现在结论里，**不是默默放过**。

## 5. 为什么判据是数据

因为"按步骤名挑判据"（`if step == "build": 用另一套口径`）会在同一个闭环里长出两套工作方式。
所以：

- 判据写在声明里（`SuccessCriterion`），只有 `hwops_contract.judge()` 一处判；
- `hwops_check.criterion_source_problems()` 扫源码，发现"按步骤名分支 + 在那个分支里挑判据"就红。

## 6. 熔断为什么按故障码

硬件卡住时报错**每次措辞都不一样**，而处置**只有一条**。挂在措辞上等于没有熔断；
挂在"适配器 + 故障码"上，才能真正做到"两次之后停下来让人处理"。
