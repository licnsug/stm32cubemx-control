# STM32CubeMX Control

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)

**English** | [中文](#中文)

Drive STM32CubeMX from the command line, or let an AI agent drive it for you:
create a project from nothing but a part number, reconfigure an existing one,
regenerate the code headlessly, and check the result with a real Keil build.

---

## What it is

A Codex plugin plus a standalone Python toolkit that controls STM32CubeMX
without the GUI.

* `create` a project from an MCU name alone — no template project needed
* `new` clone an existing project when you want a known-good starting point
* change any peripheral or middleware setting (`set mode`, `set pin`,
  `set ip parameters`, `clock resolution`)
* find a free pin for a signal CubeMX could not place (`pin-plan`)
* regenerate code, then build it with Keil MDK and read the errors back
* see exactly what CubeMX accepted: every edit is validated by CubeMX itself and
  reported as a per-key diff
* roll back: every generation is snapshotted first

## Why it works this way

CubeMX has no public API, but it does have an official command console
(`STM32CubeMX.exe -q <script>`) and it ships a device database describing every
legal mode and parameter. This project drives those two things and nothing else:

* CubeMX is **not** patched, modified or bundled. You install it from ST as usual.
* All state lives in the plain-text `.ioc` file, so every operation is
  inspectable and reversible.
* CubeMX stays the authority. If an edit is not legal, CubeMX drops it on the
  next load, and the toolkit shows you exactly which key disappeared.

## Features

| Command | What it does |
| --- | --- |
| `create` | build a project from an MCU name alone, then optionally generate the code |
| `new` | clone a complete reference project, including its peripheral configuration |
| `show` / `verify` | summarise a project; flag duplicate pins, unlisted pins, missing signals |
| `script` | run raw console commands: `set mode`, `set pin`, `set ip parameters`, `clock resolution`, `project generate` |
| `pin-map` / `pin-plan` | ask the device database which pins can carry a signal, then apply one |
| `mcu` / `ip-modes` / `ip-params` | query what an MCU has and what a peripheral accepts |
| `generate` | regenerate code headlessly, with a backup and a change report |
| `build` | build with Keil UV4 and parse errors and warnings |
| `backups` / `restore` | list snapshots and roll one back |
| `merge` | transplant a configuration block from another project |

The plugin also exposes the same engine as **14 MCP tools** (`cubemx_*`) so an
agent can call them directly.

## Requirements

| | |
| --- | --- |
| STM32CubeMX | 6.x — install it yourself from ST; it is not bundled here |
| Python | 3.8 or newer |
| Codex | the desktop app, for the skill and the MCP tools |
| Keil MDK | optional, only for `build` |
| OS | Windows for the installer and `build`; the toolkit itself is portable Python |

## Install

### With the installer (recommended)

`install.ps1` copies the plugin to a location you choose, registers it with
Codex, installs it, and verifies that it runs.

~~~powershell
git clone https://github.com/licnsug/stm32cubemx-control.git
cd stm32cubemx-control
powershell -ExecutionPolicy Bypass -File .\install.ps1
~~~

It prompts for the install location, offering
`%USERPROFILE%\plugins\stm32cubemx-control` as the default — press Enter to
accept. For a silent install:

~~~powershell
.\install.ps1 -InstallDir D:\tools\stm32cubemx-control -Force
~~~

### Manually

~~~powershell
codex plugin marketplace add https://github.com/licnsug/stm32cubemx-control.git
codex plugin add stm32cubemx-control`stm32cubemx-control
~~~

That installs the skill. To get the MCP tools as well, add this to
`~/.codex/config.toml`, pointing at wherever you cloned the repository:

~~~toml
[mcp_servers.stm32cubemx-control]
command = 'python'
args = ['C:/path/to/stm32cubemx-control/plugins/stm32cubemx-control/scripts/cubemx_mcp.py']
enabled = true
~~~

Then **restart the Codex app** and start a new conversation.

## Quick start

Point it at a project — the argument can be the project folder or the `.ioc`
file itself.

~~~bash
python plugins/stm32cubemx-control/scripts/cubemx.py env
python plugins/stm32cubemx-control/scripts/cubemx.py show  E:\work\MyBoard
python plugins/stm32cubemx-control/scripts/cubemx.py verify E:\work\MyBoard --expect TIM1_CH3N
~~~

**Create a project from nothing but a part number:**

~~~bash
cubemx.py create STM32G431CBUx MyBoard E:\work --generate
~~~

CubeMX opens the MCU, names the project, picks the toolchain and writes the
`.ioc`; the toolkit then generates a complete project tree including the Keil
project. On a real run this produced 1173 files, and CubeMX filled in the order
code and database version itself.

**Reconfigure an existing project:**

~~~bash
cubemx.py script E:\work\MyBoard --file cmds.txt
~~~

where `cmds.txt` holds one console command per line:

~~~text
set mode TIM1 "PWM Generation1 CH1 CH1N"
set mode TIM1 "PWM Generation2 CH2 CH2N"
set mode TIM1 "PWM Generation3 CH3 CH3N"
set pin PB1 TIM1_CH3N
set ip parameters TIM1 Period 8499
set ip parameters TIM1 DeadTime 100
clock resolution
project generate
~~~

Batch commands into one call: each invocation boots a whole CubeMX session, so
extra commands in the same run cost almost nothing, while separate runs cost
about 20-40 seconds each.

**Regenerate and verify:**

~~~bash
cubemx.py generate E:\work\MyBoard
cubemx.py build    E:\work\MyBoard
cubemx.py backups
cubemx.py restore  E:\work\MyBoard MyBoard-20260929-193000
~~~

## How it works

1. **You describe or the agent decides** what the project needs.
2. **The toolkit writes console commands**, not configuration files — CubeMX
   itself enables the peripheral and assigns the pins.
3. **CubeMX loads and re-saves the `.ioc`.** Whatever it does not accept simply
   disappears, and the toolkit reports the difference key by key.
4. **Code is generated and built**, and the compiler output is parsed back.

Two habits make this reliable: close the CubeMX GUI before generating (a window
holding the project in memory will overwrite your changes when it saves), and
treat a non-empty `dropped` list as a failure rather than a cosmetic change.

## Known limitations

* **Close the CubeMX GUI before generating.** An open window holds the project in
  memory and rewrites the `.ioc` when anyone saves it. The `generate` command
  detects this and refuses instead of racing.
* **Each operation boots a whole CubeMX session**, about 20-40 seconds. Batch
  your commands; do not issue one command per run.
* **STM32H7 stalls** in CubeMX's headless mode on a power-supply dialog it cannot
  display. F4/F7/G4/L4/G0 are fine. The runner detects the stall and stops
  instead of waiting out the timeout.
* **Parameter values are not validated by CubeMX.** Always take them from
  `ip-params`; a wrong string silently produces broken code.
* **A new project needs a whole project.** CubeMX rejects partial `.ioc` files,
  so `create` starts from an MCU name and `new` copies a complete project.
* **Keil build verification is Windows-only.** The rest is portable Python.
* This tool configures and checks; it does not write your application logic.

## Related projects

Other open-source work in the same space, and worth a look:

* [h666zhang/vscode-cube-mcp](https://github.com/h666zhang/vscode-cube-mcp) — MCP
  server over the CubeMX console, with a template library
* [Wafleem/stm32cubemx_mcp](https://github.com/Wafleem/stm32cubemx_mcp) — MCP
  server with plan-then-apply and hash-checked rollback
* [Boughzala-ayoub/cubemx-tools](https://github.com/Boughzala-ayoub/cubemx-tools) —
  Java CLI and MCP server; its console command reference was a useful reference

STM32CubeMX is proprietary software by STMicroelectronics; Keil MDK is
proprietary software by Keil. Neither is bundled here, and this project is not
affiliated with or endorsed by either company.

## License

MIT — see [LICENSE](LICENSE).

---

## 中文

**这是什么**：一个 Codex 插件，同时也是一个独立的 Python 工具包，用来在**不开图形界面**的情况下控制 STM32CubeMX——从一颗芯片型号从零建工程、改现有工程的任意配置、无头生成代码，再用 Keil 真编译一遍验证。

### 能做什么

| 命令 | 作用 |
| --- | --- |
| `create` | 只给一个芯片型号就建出工程，可直接连生成代码 |
| `new` | 克隆一个完整工程（连同外设配置），适合要一个已知可用的起点 |
| `show` / `verify` | 汇总工程；检查重复引脚、未登记引脚、缺失信号 |
| `script` | 执行 CubeMX 控制台原始命令：`set mode`、`set pin`、`set ip parameters`、`clock resolution`、`project generate` |
| `pin-map` / `pin-plan` | 查设备库问"这个信号能落在哪些脚上"，然后自动挑一个空闲的用上 |
| `mcu` / `ip-modes` / `ip-params` | 查某颗芯片有什么外设、某个外设接受哪些模式和参数取值 |
| `generate` | 无头生成代码，生成前自动备份并给出改动清单 |
| `build` | 调 Keil UV4 编译并解析报错 |
| `backups` / `restore` | 列出快照并回滚 |
| `merge` | 从另一个工程移植一整块配置 |

插件同时把这套能力暴露为 **14 个 MCP 工具**（`cubemx_*`），AI 可以直接调用。

### 为什么这样做

CubeMX 没有公开 API，但它有官方的命令控制台（`STM32CubeMX.exe -q <脚本>`），并且自带一份描述所有合法模式和参数的设备数据库。本项目只驱动这两样东西：

* **不修改、不打包 CubeMX**，你照常从 ST 官网安装。
* 所有状态都在纯文本 `.ioc` 里，每一步都可检查、可回滚。
* **CubeMX 始终是权威**。改得不合法，它在下次加载时就丢掉，工具会把丢掉的是哪个键逐条告诉你。

### 环境要求

STM32CubeMX 6.x（自行安装，本项目不打包）、Python 3.8+、Codex 桌面应用（用于技能和 MCP 工具）。Keil MDK 仅在用 `build` 时需要。安装脚本和 `build` 是 Windows 专用，工具本体是跨平台 Python。

### 安装

推荐用安装脚本，它会像普通软件一样**询问你装到哪**（默认 `%USERPROFILE%\plugins\stm32cubemx-control`，直接回车即可）：

~~~powershell
git clone https://github.com/licnsug/stm32cubemx-control.git
cd stm32cubemx-control
powershell -ExecutionPolicy Bypass -File .\install.ps1
~~~

静默安装：`.\install.ps1 -InstallDir D:\tools\stm32cubemx-control -Force`

手动安装则是 `codex plugin marketplace add` 加 `codex plugin add`（见英文部分）。手动安装只得到技能；要一并拿到 MCP 工具，需要在 `~/.codex/config.toml` 里加一段 `[mcp_servers.stm32cubemx-control]`，指向你克隆下来的脚本路径。装完**重启 Codex 应用**并新开一个对话。

### 快速上手

~~~bash
# 从零建工程（只给芯片型号）
cubemx.py create STM32G431CBUx MyBoard E:\work --generate

# 改现有工程：把命令写进文件，一次调用批量执行
cubemx.py script E:\work\MyBoard --file cmds.txt

# 生成、编译、回滚
cubemx.py generate E:\work\MyBoard
cubemx.py build    E:\work\MyBoard
cubemx.py backups
cubemx.py restore  E:\work\MyBoard MyBoard-20260929-193000
~~~

`cmds.txt` 里一行一条控制台命令，例如：

~~~text
set mode TIM1 "PWM Generation1 CH1 CH1N"
set pin PB1 TIM1_CH3N
set ip parameters TIM1 Period 8499
clock resolution
project generate
~~~

**一条重要习惯**：命令要批量塞进一次调用。每次调用都要启动一整个 CubeMX 会话（约 20–40 秒），同一次里多跑几条几乎不额外花时间，拆成多次则是成倍开销。

### 已知限制

* **生成期间必须先关掉 CubeMX 图形界面**。开着的窗口把工程存在内存里，一保存就把你的改动盖回去；`generate` 检测到这种情况会直接拒绝而不是去抢。
* **每次操作启动一整个 CubeMX 会话**，约 20–40 秒，所以务必批量。
* **STM32H7 在无头模式下会卡死**（要弹一个显示不出来的电源对话框）。F4/F7/G4/L4/G0 正常。工具会检测到卡死并终止，不会干等超时。
* **参数取值 CubeMX 不校验**，必须从 `ip-params` 里取准确字符串，写错会静默生成错代码。
* **新工程需要一份完整工程**：CubeMX 不接受残缺的 `.ioc`，所以 `create` 从芯片型号出发，`new` 复制完整工程。
* Keil 编译验证仅 Windows 可用；工具其余部分是跨平台 Python。
* 这个工具负责配置和检查，不代写你的业务逻辑。

### 相关项目

同一领域的其他开源工作，值得一看：[h666zhang/vscode-cube-mcp](https://github.com/h666zhang/vscode-cube-mcp)（带模板库的 MCP 服务器）、[Wafleem/stm32cubemx_mcp](https://github.com/Wafleem/stm32cubemx_mcp)（先出计划再执行、带哈希校验回滚）、[Boughzala-ayoub/cubemx-tools](https://github.com/Boughzala-ayoub/cubemx-tools)（Java 版 CLI 与 MCP 服务器，它的控制台命令参考很有价值）。

STM32CubeMX 是 STMicroelectronics 的专有软件，Keil MDK 是 Keil 的专有软件；本项目不打包它们，也与这两家公司没有隶属或背书关系。

### 许可

MIT，见 [LICENSE](LICENSE)。
