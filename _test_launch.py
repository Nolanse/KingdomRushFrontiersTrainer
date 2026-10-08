"""启动方式测试：验证方式一（读本地配置）与方式二（自动探测）的
优先级、参数解析与全部回退路径。

不启动游戏，只验证「决定用哪条路径、传什么参数」。
"""

import importlib.util
import json
import os
import sys
import tempfile
import tkinter as tk
from pathlib import Path

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="krft_launch_"))
os.environ["LOCALAPPDATA"] = str(tmp / "local")
os.environ["APPDATA"] = str(tmp / "roaming")
os.environ["USERPROFILE"] = str(tmp)

spec = importlib.util.spec_from_file_location("krf", HERE / "KingdomRushFrontiersTrainer.pyw")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

FAILED = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {extra}")
    if not cond:
        FAILED.append(label)


def write_cfg(**data):
    p = m.launch_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def clear_cfg():
    p = m.launch_config_path()
    if p.exists():
        p.unlink()


GAME = m.locate_default_game()
print(f"检测到的游戏：{GAME}")

print("\n--- 配置解析层 ---")

clear_cfg()
cfg, err = m.load_launch_config()
check("文件不存在 -> (None, 原因)", cfg is None and "不存在" in err, f"err={err}")

p = m.launch_config_path()
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("{ 不是合法 json", encoding="utf-8")
cfg, err = m.load_launch_config()
check("JSON 非法 -> 回退原因", cfg is None and "JSON" in err, f"err={err}")

p.write_text("[1,2,3]", encoding="utf-8")
cfg, err = m.load_launch_config()
check("顶层非对象 -> 回退原因", cfg is None and "对象" in err, f"err={err}")

p.write_text(json.dumps({"mode": "bogus"}), encoding="utf-8")
cfg, err = m.load_launch_config()
check("mode 非法 -> 回退原因", cfg is None and "mode" in err, f"err={err}")

p.write_text(json.dumps({"mode": "config", "steam_app_id": "abc"}), encoding="utf-8")
cfg, err = m.load_launch_config()
check("app_id 非整数 -> 回退原因", cfg is None and "整数" in err, f"err={err}")

p.write_text(json.dumps({"mode": "config", "extra_args": [1, 2]}), encoding="utf-8")
cfg, err = m.load_launch_config()
check("extra_args 非字符串数组 -> 回退原因", cfg is None and "字符串数组" in err, f"err={err}")

write_cfg(mode="config", game_path=str(GAME), steam_app_id="458710", extra_args=["-x", "-y"])
cfg, err = m.load_launch_config()
check("合法配置解析成功", cfg is not None and cfg["steam_app_id"] == 458710, f"err={err}")
check("extra_args 解析为列表", cfg["extra_args"] == ["-x", "-y"], f"={cfg['extra_args']}")

write_cfg(mode="config", game_path=str(GAME), extra_args="-single")
cfg, err = m.load_launch_config()
check("extra_args 字符串自动包成列表", cfg["extra_args"] == ["-single"], f"={cfg['extra_args']}")

print("\n--- 环境变量构造 ---")
env = m.build_launch_env(GAME, app_id=12345, use_steam=True)
check("显式 app_id 生效", env.get("SteamAppId") == "12345", f"={env.get('SteamAppId')}")
check("SteamGameId 同步", env.get("SteamGameId") == "12345")
env = m.build_launch_env(GAME, app_id=0, use_steam=True)
check("app_id=0 时自动识别", env.get("SteamAppId") == "458710", f"={env.get('SteamAppId')}")
env = m.build_launch_env(GAME, app_id=12345, use_steam=False)
check("use_steam=False 不注入", "SteamAppId" not in env)

print("\n--- 界面与优先级 ---")
root = tk.Tk()
root.withdraw()
app = m.TrainerApp(root)
root.update_idletasks()

# 1) 方式二
clear_cfg()
app.launch_mode_var.set(m.LAUNCH_MODE_AUTO)
plan = app.resolve_launch_plan()
check("方式二：走自动探测", plan["mode"] == m.LAUNCH_MODE_AUTO)
check("方式二：无回退原因", plan["fallback_reason"] == "", f"={plan['fallback_reason']}")
check("方式二：定位到真实 exe", plan["game"].is_file(), f"={plan['game']}")

# 2) 方式一 —— 配置可用
write_cfg(mode="config", game_path=str(GAME), steam_app_id="458710", extra_args=["-dx"])
app.launch_mode_var.set(m.LAUNCH_MODE_CONFIG)
plan = app.resolve_launch_plan()
check("方式一：读本地配置生效", plan["mode"] == m.LAUNCH_MODE_CONFIG)
check("方式一：使用配置路径", str(plan["game"]) == str(GAME), f"={plan['game']}")
check("方式一：带上传入参数", plan["extra_args"] == ["-dx"], f"={plan['extra_args']}")
check("方式一：使用配置 app_id", plan["app_id"] == 458710, f"={plan['app_id']}")
check("方式一：无回退", plan["fallback_reason"] == "")

# 3) 方式一 —— 配置不存在 -> 回退
clear_cfg()
app.launch_mode_var.set(m.LAUNCH_MODE_CONFIG)
plan = app.resolve_launch_plan()
check("方式一：配置缺失触发回退", plan["mode"] == m.LAUNCH_MODE_AUTO)
check("方式一：回退原因已记录", "配置不可用" in plan["fallback_reason"], f"={plan['fallback_reason']}")

# 4) 方式一 —— 路径不存在 -> 回退
write_cfg(mode="config", game_path=str(tmp / "no_such_game.exe"))
app.launch_mode_var.set(m.LAUNCH_MODE_CONFIG)
plan = app.resolve_launch_plan()
check("方式一：路径无效触发回退", plan["mode"] == m.LAUNCH_MODE_AUTO)
check("方式一：回退原因指明不存在", "不存在" in plan["fallback_reason"], f"={plan['fallback_reason']}")

# 5) 方式一 —— 配置未指定 game_path -> 用探测路径但保留方式一
write_cfg(mode="config", steam_app_id="458710", extra_args=["-z"])
app.launch_mode_var.set(m.LAUNCH_MODE_CONFIG)
plan = app.resolve_launch_plan()
check("方式一：留空 game_path 仍走方式一", plan["mode"] == m.LAUNCH_MODE_CONFIG)
check("方式一：留空时定位到真实 exe", plan["game"].is_file(), f"={plan['game']}")
check("方式一：留空时保留附加参数", plan["extra_args"] == ["-z"])
check("方式一：留空时给出说明", "未指定" in plan["fallback_reason"], f"={plan['fallback_reason']}")

# 6) 方式一 —— JSON 损坏 -> 回退
m.launch_config_path().write_text("{坏 json", encoding="utf-8")
app.launch_mode_var.set(m.LAUNCH_MODE_CONFIG)
plan = app.resolve_launch_plan()
check("方式一：JSON 损坏触发回退", plan["mode"] == m.LAUNCH_MODE_AUTO)
check("方式一：损坏原因已记录", "JSON" in plan["fallback_reason"], f"={plan['fallback_reason']}")

# 7) 界面选方式二时，即使配置存在也不读
write_cfg(mode="config", game_path=str(GAME))
app.launch_mode_var.set(m.LAUNCH_MODE_AUTO)
plan = app.resolve_launch_plan()
check("方式二优先：选中即忽略配置", plan["mode"] == m.LAUNCH_MODE_AUTO)

# 8) 界面提示随状态更新
clear_cfg()
app.launch_mode_var.set(m.LAUNCH_MODE_CONFIG)
app.refresh_mode_hint()
hint = app.mode_hint.cget("text")
check("提示：缺配置时明确说明回退", "回退" in hint and "未找到" in hint, f"={hint[:50]}")
write_cfg(mode="config", game_path=str(GAME))
app.refresh_mode_hint()
hint = app.mode_hint.cget("text")
check("提示：配置就绪时显示目标", str(GAME) in hint, f"={hint[:50]}")
app.launch_mode_var.set(m.LAUNCH_MODE_AUTO)
app.refresh_mode_hint()
check("提示：方式二说明自动识别", "458710" in app.mode_hint.cget("text"))

# 9) 模式选择持久化
app.launch_mode_var.set(m.LAUNCH_MODE_CONFIG)
app.save_config()
saved = json.loads((Path(os.environ["LOCALAPPDATA"]) / "KingdomRushFrontiersTrainer" / "config.json").read_text(encoding="utf-8"))
check("启动方式已持久化", saved.get("launch_mode") == m.LAUNCH_MODE_CONFIG, f"={saved.get('launch_mode')}")

# 10) 模式默认值随配置推断
clear_cfg()
app._saved_launch_mode = None
check("无配置时默认方式二", app.detect_launch_mode() == m.LAUNCH_MODE_AUTO)
write_cfg(mode="config", game_path=str(GAME))
check("有 config 配置时默认方式一", app.detect_launch_mode() == m.LAUNCH_MODE_CONFIG)
app._saved_launch_mode = m.LAUNCH_MODE_AUTO
check("用户上次选择优先于配置", app.detect_launch_mode() == m.LAUNCH_MODE_AUTO)

app.poller.stop()
root.destroy()

print("\n" + "=" * 52)
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("ALL LAUNCH-PLAN TESTS PASSED")