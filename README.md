# Kingdom Rush Frontiers 专用修改器 / Trainer

![platform](https://img.shields.io/badge/platform-Windows%20x64-blue)
![version](https://img.shields.io/badge/version-1.0.2-green)
![license](https://img.shields.io/badge/license-MIT-yellow)
![network](https://img.shields.io/badge/network-none-brightgreen)

**《Kingdom Rush Frontiers》非官方单机修改器**

一个纯本地、零网络、完全离线的单机训练器。采用「外部界面 + 游戏自带脚本桥接」架构，
**不注入 DLL、不读写游戏进程内存、不修改游戏安装目录**。

- 适配对象：Kingdom Rush Frontiers **6.4.46** · Windows Steam · x64 · LÖVE + LuaJIT
- 架构沿用：Kingdom Rush Trainer（[gitee.com/zljgithub/KingdomRushTrainer](https://gitee.com/zljgithub/KingdomRushTrainer)，MIT，Gboxkit）的方案思路
- 验证状态：44 项桥接断言 + 23 项界面断言全部通过

> ⚠️ **免责声明**
> 本项目为个人学习与研究的非官方粉丝作品，与 Ironhide Game Studio 无任何关联。
> **不修改、不打包、不重新分发游戏本体文件与任何美术资源。**
> 请仅在单机体验中使用，勿用于排行榜、竞速提交或其他需要公平性的场景。
> 使用者需自行承担全部责任，并遵守当地法律与游戏用户协议。
>
> Unofficial fan-made trainer for personal/educational use only. Not affiliated with
> Ironhide Game Studio. Does not modify or redistribute any game files. Single-player use only.

---

## 目录

- [与 KR1 版修改器的差异](#与-kr1-版修改器的差异)
- [工作原理](#工作原理--how-it-works)
- [功能](#功能--features)
- [快速开始](#快速开始--quick-start)
- [环境依赖](#环境依赖--requirements)
- [使用示例](#使用示例--usage-examples)
- [使用（详细）](#使用--usage详细)
- [快捷键](#快捷键--hotkeys)
- [安全边界](#安全边界--safety)
- [开发与验证](#开发与验证--development)
- [兼容性](#兼容性--compatibility)
- [常见问题](#常见问题--faq)
- [许可证](#license)

## 与 KR1 版修改器的差异

原版修改器适配的是《Kingdom Rush》(KR1)。本作是 **KR2（Frontiers）**，代码库不同（`kr2/` 而非 `kr1/`），因此重新逆向了安装包并适配了以下差异：

| 项目              | KR1                          | **KR2（本项目）**                                    |
| --------------- | ---------------------------- | ----------------------------------------------- |
| 主程序             | `Kingdom Rush.exe`（359 MB）   | `Kingdom Rush Frontiers.exe`（455 MB）            |
| Steam AppID     | 246420                       | **458710**                                      |
| LÖVE 存档目录       | `%APPDATA%\kingdom_rush`     | **`%APPDATA%\kingdom_rush_frontiers`**          |
| bundle id       | `…kingdomrush.windows.steam` | **`com.ironhidegames.frontiers.windows.steam`** |
| 版本串             | `kr1-desktop-6.4.46`         | **`kr2-desktop-6.4.46`**                        |
| 主线关卡数           | 26                           | **22**（`kr2/data/levels/` 另有 level81/82/99 特殊关） |
| `game_settings` | `all/`                       | **`kr2/`**                                      |
| 桥接模块名           | `kr_trainer_bridge`          | **`krft_bridge`**                               |
| 状态/状态文件         | `kr_trainer_*`               | **`krft_*`**（与游戏原文件完全隔离）                        |

**运行时字段全部一致**（这是方案能直接复用的关键），已逐项在 KR2 字节码中确认：  
`DBG_TIME_MULT`、`store.player_gold`、`store.lives`、`store.paused`、`store.entities`、  
`store.send_next_wave`、`store.force_next_wave`、`store.game_outcome`、  
`health.hp/hp_max`、`tower.damage_factor`、`barrack.max_soldiers`、`health.dead_lifetime`、  
`attacks.range`、`signal "got-enemy-gold"`、`game-victory` / `game-victory-after`、  
`storage:load_slot/save_slot`、`slot.gems`、`slot.levels[i].stars`。

---

## 工作原理 / How it works

**不注入 DLL、不读写进程内存、不修改游戏安装目录中的任何文件。**

1. 界面把配置写成一个 Lua 表格文件 `%APPDATA%\kingdom_rush_frontiers\krft_state.lua`；
2. 用游戏自带的官方扩展入口 `-custom_script krft_bridge` 加载桥接脚本；
3. 桥接运行在游戏自己的 LuaJIT 环境里，通过游戏暴露的信号与回调调整运行时数值；
4. 界面每 0.8 秒刷新一次心跳；**心跳超时 6 秒、关闭开关、关闭修改器、游戏正常退出**时，桥接自动把所有改动按原值恢复。

```
┌────────────────────┐   写状态文件    ┌──────────────────┐   update(dt)    ┌────────────────────┐
│  修改器界面 (Tk)    │ ─────────────▶ │  krft_bridge.lua │ ─────────────▶ │  游戏 LuaJIT 运行时 │
│  心跳 + 功能开关    │                │  读配置 / 记原值  │                 │  store / entities   │
└────────────────────┘ ◀───────────── └──────────────────┘ ◀────────────── └────────────────────┘
              读状态文件（连接状态 / 版本 / 当前关卡）
```

---

## 功能 / Features

### 速度 Speed

- 预设 1x / 2x / 3x / 5x / 8x，自定义 1.0–16.0 倍（支持小数，用相邻整数帧交替实现平均速度）
- 暂停 / 恢复：仅暂停关卡仿真，界面与心跳保持工作
- **默认唯一启用项**，其它功能默认关闭

### 资源与生存 Resources & Survival

- 金币锁定（0–999,999,999）、宝石锁定（0–999,999,999）
- 基地生命锁定（1–9,999）
- 英雄与友军无敌
- **杀敌金币倍率（0.1–1000 倍）**：监听游戏自己的 `got-enemy-gold` 信号，在官方发放后补差额，2x/3x/5x/10x/20x 快捷按钮

### 战斗 Combat

- 英雄技能无冷却
- 友军伤害倍率（0.1–100）
- 防御塔攻速倍率（0.1–50）、射程倍率（0.1–20）
- **兵营增强**：出怪人数（1–10，原版 3）、刷新时间倍率（0.1–5.0，1.0 = 原版）

### 进度与波次 Progress & Waves

- 临时解锁全部关卡、临时全关三星
- 提前呼叫下一波、强制清场并推进、立即通关
- **立即通关会正常写入存档**：清空全部敌人并推进波次，由游戏自身的胜利判定收尾，
  因此星级、下一关解锁、`save_slot` 全部走官方流程（详见下方「实现说明」）
- 存档类功能**首次启用前自动备份**到 `KRFTBackups\时间戳-*`；关闭开关或退出时恢复原始存档字段
- 手动备份存档按钮

### 实现说明：为什么立即通关要「驱动」而不是「伪造」

游戏在 `all/systems.lua` 的关卡协程结尾按固定顺序收尾：

```
run_complete  →  写 slot.levels[idx].stars / already_won
             →  storage:save_slot()          ← 下一关就是在这里解锁并落盘
             →  emit "game-victory"           ← 之后才是胜利画面
             →  emit "game-victory-after"
```

**解锁下一关是协程正常跑到结尾时的副作用，不是胜利信号的附带结果。**
早期版本直接构造 `store.game_outcome` 并补发那两个信号，等于整段跳过协程收尾，
所以能弹出胜利画面却不写存档、也不解锁下一关。

现在改为制造官方判定所需的两个条件（无存活敌人 + 波次已打完），并分两帧推进，
让引擎跑一次正常的清理与波次检查，由 `systems.lua` 自己判定胜利。
桥接全程不构造 `game_outcome`、不自行 `emit` 胜利信号——测试里有专门的防回归断言守着这条线。

---

## 快捷键 / Hotkeys

| 功能                        | 快捷键                                  |
| ------------------------- | ------------------------------------ |
| 速度 1x / 2x / 3x / 5x / 8x | `Ctrl+Alt+1` / `2` / `3` / `5` / `8` |
| 暂停 / 恢复                   | `Ctrl+Alt+Space`                     |
| 金币 / 宝石 / 基地生命 / 杀敌金币     | `Ctrl+Alt+G` / `B` / `L` / `K`       |
| 兵营增强                      | `Ctrl+Alt+Y`                         |
| 友军无敌                      | `Ctrl+Alt+I`                         |
| 英雄无冷却                     | `Ctrl+Alt+C`                         |
| 伤害倍率                      | `Ctrl+Alt+D`                         |
| 防御塔攻速 / 射程                | `Ctrl+Alt+A` / `R`                   |
| 提前下一波                     | `Ctrl+Alt+W`                         |
| 立即通关                      | `Ctrl+Alt+V`                         |

界面底部点「查看 / 修改」可重新录制组合键（支持 Ctrl / Alt / Shift / Win + 字母、数字、F1–F12、方向键等）。

---

## 快速开始 / Quick Start

### 下载即用（推荐普通用户）

1. 前往仓库的 [Releases](../../releases/latest) 页面下载 `KingdomRushFrontiersTrainer.exe`（约 10 MB）
2. 双击运行 —— **无需安装 Python 或任何依赖**
3. 顶部「版本检查」变绿即表示已自动找到游戏
4. 点「启动 / 连接游戏」，等待显示「桥接状态：已连接」
5. 启用需要的功能

> Windows 可能提示「未知发布者」，这是未签名程序的正常提示，
> 选择「更多信息 → 仍要运行」即可。

### 从源码运行（开发者）

```bash
git clone https://github.com/<你的用户名>/KingdomRushFrontiersTrainer.git
cd KingdomRushFrontiersTrainer

# 运行界面（需标准版 Python，自带 tkinter）
python KingdomRushFrontiersTrainer.pyw

# 可选：安装打包依赖（仅在自行构建 exe 时需要）
python -m pip install -r requirements.txt
```

> 首次运行后，程序会自动把桥接脚本写入
> `%APPDATA%\kingdom_rush_frontiers`，无需手工复制。

---

## 环境依赖 / Requirements

### 运行修改器（必需）

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11 · **x64** |
| 游戏 | Kingdom Rush Frontiers（Steam 版），已安装 |
| 游戏版本 | 6.4.46（其他版本可能可用，但不保证） |
| 运行环境 | **无需 Python**（发布版已内置） |
| 网络 | **完全不需要** —— 本程序零网络请求 |

### 从源码运行 / 打包（开发者）

| 项目 | 要求 |
|---|---|
| Python | 3.9 – 3.12（**需带 tkinter**，标准版 CPython 自带） |
| PyInstaller | 6.x，仅打包时需要 |

> ⚠️ **注意**：部分精简版 Python（如 Windows Store 版、某些 conda 发行版）不带 `tkinter`，
> 运行界面会报 `ModuleNotFoundError: No module named 'tkinter'`。
> 请从 [python.org](https://www.python.org/downloads/windows/) 下载标准安装包。

### 运行测试

测试需要**本机已安装游戏**，因为它会加载游戏自带的 `lua51.dll`（LuaJIT）
来执行真实的桥接脚本。

```bash
python _test_bridge.py       # 桥接逻辑，55 项断言
python _test_gui_smoke.py    # 界面冒烟，29 项断言
python _test_launch.py       # 启动方式，39 项断言
```

---

## 使用示例 / Usage Examples

### 例 1：3 倍速速通

1. 启动修改器 → 点「启动 / 连接游戏」
2. 速度页点 `3x`
3. 进入关卡，敌人和波次按 3 倍速推进
4. 想停下点「暂停 / 恢复」（`Ctrl+Alt+Space`）

### 例 2：无限资源挑战

1. 资源页勾选「锁定金币」，值填 `99999`
2. 勾选「锁定基地生命」，值填 `20`
3. 战斗页勾选「友军无敌」
4. 关卡内数值会被持续锁定，放心操作

### 例 3：刷三星

1. 进度页勾选「临时全关三星」
2. **首次启用会自动备份存档**到 `KRFTBackups\时间戳-*`
3. 关掉开关或退出修改器即恢复原始存档

### 例 4：立即通关当前关

1. 进入关卡后，点进度页的「立即通关」
2. 程序会清空本关敌人并推进波次
3. **由游戏自身的胜利判定收尾**，因此星级与下一关解锁会正常写入存档

> 与旧版不同：v1.0.2 起「立即通关」只在**你点击按钮后**触发，
> 关卡载入时不会有任何自动行为。

### 例 5：兵营无限出兵

1. 战斗页勾选「兵营增强」
2. 「出怪人数」设为 `10`，「刷新时间倍率」设为 `0.5`
3. 兵营每波最多出 10 个兵，且阵亡后 0.5 倍时间即补员

### 例 6：命令行直接启动游戏

```bash
# 修改器会注入 SteamAppId / SteamGameId（AppID 458710），否则游戏会静默退出
set SteamAppId=458710
"E://SteamLibrary//steamapps//common//Kingdom Rush Frontiers//Kingdom Rush Frontiers.exe" -custom_script krft_bridge
```

---

### 两种启动方式

界面顶部有「启动方式」单选，两个入口随时可切换：

| | 方式一：本地配置 | 方式二：自动探测 |
|---|---|---|
| **入口** | 界面选「本地配置（方式一）」→ 点「启动 / 连接游戏」 | 界面选「自动探测（方式二）」→ 点「启动 / 连接游戏」 |
| **数据来源** | `%LOCALAPPDATA%\KingdomRushFrontiersTrainer\launch.json` | 界面的「游戏路径」输入框 + Steam 库扫描 |
| **游戏路径** | 取配置里的 `game_path` | 手动填写，或点「浏览」，或自动扫描 Steam 库 |
| **Steam AppID** | 取配置里的 `steam_app_id`；填 `0`/留空则自动识别 | 始终自动识别（458710） |
| **附加参数** | 取配置里的 `extra_args`，追加在 `-custom_script` 之后 | 无 |
| **适合场景** | 游戏装在 Steam 库扫描不到的非常规位置；需要固定 AppID 或额外启动参数；多机复用同一配置 | 绝大多数用户 |

#### 配置文件格式

点界面上的「导出配置模板」会在上述路径生成 `launch.json`：

```json
{
  "mode": "config",
  "game_path": "E:////SteamLibrary////steamapps////common////Kingdom Rush Frontiers////Kingdom Rush Frontiers.exe",
  "steam_app_id": "458710",
  "extra_args": [],
  "use_steam_env": true
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `mode` | string | `config` 用方式一；`auto` 用方式二（忽略本文件其余内容） |
| `game_path` | string | 游戏 exe 路径。**留空则回退到自动探测路径**，但仍按方式一处理附加参数 |
| `steam_app_id` | int | 留空或 `0` 时自动识别；非整数会导致整个配置被忽略并回退 |
| `extra_args` | string[] | 追加的自定义参数；写成单个字符串会自动包成数组 |
| `use_steam_env` | bool | 设为 `false` 则不注入 `SteamAppId`/`SteamGameId` |

#### 只读模式（不写游戏存档）

进度页有「只读模式」开关。开启后修改器**完全不读写游戏存档**（`slot_*.lua`），
宝石 / 解锁关卡 / 全关三星三项会自动禁用。

**什么时候用**

- 存档由其他工具管理，改动可能冲突
- 存档目录不可写（权限受限）
- 只想用运行时功能，不想动存档

**不受影响的功能**（它们本来就不写存档）

速度、暂停、锁定金币、锁定基地生命、杀敌金币倍率、友军无敌、英雄技能无冷却、
伤害倍率、防御塔攻速 / 射程、兵营增强、提前呼叫下一波。

**三层防护**

1. 桥接侧 `read_only_active()` 短路，`apply_slot_features()` 直接返回，
   `restore_slot_snapshot()` 永不调用 `save_slot`
2. 界面侧三个 Checkbutton 置 `disabled`，切换时保存原值、关闭时恢复
3. 状态文件写入前 `write_state()` 再次强制清零，快捷键旁路也会被拦

状态文件里的 `slot_writable=0` 可用于确认当前处于只读状态。

#### 优先级与回退

**优先级**：界面上的选择是唯一决定因素。

- 选中「方式二」→ **一律走自动探测，即使 `launch.json` 存在也不读**
- 选中「方式一」→ 读配置，下列任一情况**自动回退到方式二**并把原因写进日志：

| 异常情况 | 回退原因日志 |
|---|---|
| `launch.json` 不存在 | `配置不可用：配置文件不存在` |
| JSON 语法错误 | `配置不可用：配置文件不是合法 JSON：…` |
| 顶层不是对象 | `配置不可用：配置文件顶层必须是 JSON 对象` |
| `mode` 取值非法 | `配置不可用：mode 只能是 auto 或 config，收到：…` |
| `steam_app_id` 非整数 | `配置不可用：steam_app_id 必须是整数，收到：…` |
| `extra_args` 不是字符串数组 | `配置不可用：extra_args 必须是字符串数组` |
| `game_path` 指向的文件不存在 | `启动方式一不可用（配置中的 game_path 不存在：…）` |

此外还有**运行期二次回退**：若方式一的路径能通过存在性检查、但 `CreateProcess` 仍失败
（例如文件被占用、权限不足），程序会记录原因并用自动探测路径再试一次；
两次都失败才弹错误框。

设计原则：**任何配置问题都不该让用户启动不了游戏。**

界面下方那行说明会实时反映当前状态：配置就绪时显示目标路径与附加参数，
配置有问题时直接告诉你会回退。

## 使用 / Usage（详细）

### 方式 A：由修改器启动（推荐首次使用）

1. 关闭游戏，运行 `KingdomRushFrontiersTrainer.exe`
2. 顶部「版本检查」变绿即已自动找到游戏；否则点「浏览」选择 `Kingdom Rush Frontiers.exe`
3. 点「启动 / 连接游戏」，等待顶部显示「桥接状态：已连接」
4. 启用需要的功能。**关闭修改器时会先发送恢复指令再退出**

### 方式 B：从 Steam 直接启动（推荐日常）

1. 先运行一次修改器并点「启动 / 连接游戏」，让它把桥接脚本写入存档目录
2. Steam 库 → 右键《Kingdom Rush Frontiers》→ 属性 → 通用 → 启动选项，填：
   ```
   -custom_script krft_bridge
   ```
3. 从 Steam 正常启动游戏，修改器会自动识别并连接

> **为什么必须用本程序启动或加启动选项？** 因为 `steam_api.dll` 在缺少 Steam 运行上下文时初始化失败，  
> 直接 `CreateProcess` 启动会在 1–2 秒内静默退出。修改器会自动识别 AppID **458710** 并注入  
> `SteamAppId` / `SteamGameId` 环境变量解决这个问题。

---

## 安全边界 / Safety

- **不注入 DLL**、不调用 `WriteProcessMemory`、不扫描或写入进程内存、不挂系统计时器
- **不修改游戏安装目录**中的任何文件；全程只读游戏 exe（用于版本检查）
- 只往存档目录写 `krft_` 前缀的自有文件：`krft_bridge.lua` / `krft_state.lua` / `krft_status.txt`，  
  覆盖旧桥接脚本前会自动备份
- 所有可逆字段用弱引用快照记录原值；关闭开关 / 心跳超时 / 退出时按原值恢复
- 存档类功能（宝石 / 解锁 / 星级）以会话方式修改，关闭即恢复；启用前自动备份 `slot_*.lua`
- 崩溃或强关后，桥接检测到 6 秒心跳超时会自动恢复
- 各项功能独立开关，**默认只开启速度调节**

---

## 开发与验证 / Development

### 文件说明

| 文件                                | 说明                                      |
| --------------------------------- | --------------------------------------- |
| `KingdomRushFrontiersTrainer.pyw` | 界面主程序（源码）                               |
| `krft_bridge.lua`                 | 在游戏 LuaJIT 环境中运行的桥接脚本                   |
| `_test_bridge.py`                 | 桥接仿真测试（55 项断言，用游戏自带 `lua51.dll` 加载真实桥接） |
| `_test_gui_smoke.py`              | 界面冒烟测试（29 项断言，真实构建 Tk 控件并驱动回调）          |
| `_test_launch.py`                 | 启动方式测试（39 项断言，覆盖两种方式的优先级与全部回退路径）    |

### 跑测试

```bash
python _test_bridge.py      # 桥接逻辑：速度/资源/生存/战斗/兵营/存档/命令/恢复
python _test_gui_smoke.py   # 界面：控件、状态文件格式、回调、退出恢复
```

两个测试都用**游戏自带的 `lua51.dll`**（LuaJIT）加载真实桥接脚本，伪造 `love.filesystem` /  
`hump.signal` / `storage` / `game_settings`，因此验证的是真实执行路径，不需要启动游戏。

### 自行打包

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name KingdomRushFrontiersTrainer \
  --add-data "krft_bridge.lua;." \
  KingdomRushFrontiersTrainer.pyw
```

---

## 兼容性 / Compatibility

- 已验证：Kingdom Rush Frontiers **6.4.46** · Windows Steam · x64 · LÖVE + LuaJIT
- 版本检查会确认三件事：PE 是 x64、`version.lua` 里能读到版本号、**`main.lua` 里存在 `custom_script`**。  
  最后一项是整个方案的前提——若游戏后续版本移除了这个入口，修改器会明确报错而不是静默失效
- 不支持 32 位、非 Steam 构建
- 高倍速增加 CPU 占用，建议日常 2x–5x；8x 以上可能掉帧
- 强制清场可能跳过 Boss / 脚本事件，优先用「提前呼叫下一波」

---

## 常见问题 / FAQ

<details>
<summary><b>双击后没有反应 / 一闪而过</b></summary>

未签名程序会被 Windows SmartScreen 拦截。点「更多信息 → 仍要运行」。
若仍无法运行，可用命令行直接启动查看错误输出：

```bash
KingdomRushFrontiersTrainer.exe
```

日志位于 `%LOCALAPPDATA%\KingdomRushFrontiersTrainer\trainer.log`。
</details>

<details>
<summary><b>点「启动 / 连接游戏」后游戏秒退</b></summary>

游戏需要 Steam 运行上下文。本程序会自动识别 AppID **458710** 并注入环境变量。
若仍失败请确认：① Steam 客户端正在运行；② 游戏本体已完整下载。
</details>

<details>
<summary><b>顶部显示「游戏运行中 · 未加载桥接」</b></summary>

游戏不是通过本程序启动的。两种解决方式：

- 关闭游戏，改用本程序的「启动 / 连接游戏」
- 或在 Steam → 库 → 右键游戏 → 属性 → 通用 → 启动选项填：
  `-custom_script krft_bridge`
</details>

<details>
<summary><b>提示「No module named 'tkinter'」</b></summary>

当前 Python 不带 tkinter。从 [python.org](https://www.python.org/downloads/windows/)
安装标准版，或直接使用 Releases 里的打包版本。
</details>

<details>
<summary><b>修改器崩溃了，游戏里的数值会残留吗？</b></summary>

不会。桥接每 0.8 秒检测一次心跳，**6 秒内收不到心跳就自动恢复全部运行时数值**。
存档类功能（宝石 / 解锁 / 星级）也是会话式的，关闭即恢复。
</details>

<details>
<summary><b>会不会影响账号 / 封号？</b></summary>

本程序**零网络请求**，不与游戏服务器通信，不读取也不修改任何账号数据。
但游戏自身有反作弊机制与用户协议，请自行评估使用风险。
</details>

<details>
<summary><b>为什么仓库里没有游戏素材？</b></summary>

那些资源版权归 Ironhide Game Studio 所有，**不能也不应随本项目分发**。
本仓库只包含修改器自身的代码。
</details>

---

## License

[MIT](LICENSE) — 仅适用于本仓库中的修改器代码；游戏本体版权归 Ironhide Game Studio 所有。

架构设计参考了 [Kingdom Rush Trainer](https://gitee.com/zljgithub/KingdomRushTrainer)（MIT, Gboxkit）。
