"""GUI 冒烟测试：真实构建 Tk 界面并驱动回调，不启动游戏。

用临时目录隔离 LOCALAPPDATA / APPDATA，不污染真实存档与配置。

用法：python _test_gui_smoke.py
"""
import importlib.util, os, sys, tempfile, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="krft_gui_"))
os.environ["LOCALAPPDATA"] = str(tmp / "local")
os.environ["APPDATA"] = str(tmp / "roaming")
os.environ["USERPROFILE"] = str(tmp)

import tkinter as tk
spec = importlib.util.spec_from_file_location(
    "krf", str(HERE / "KingdomRushFrontiersTrainer.pyw"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

F = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {extra}")
    if not cond: F.append(label)

root = tk.Tk(); root.withdraw()
app = m.TrainerApp(root)
root.update_idletasks()

check("窗口标题", "Frontiers" in root.title(), f"= {root.title()}")
check("自动定位游戏", app.game_path.name == m.TARGET_EXE_NAME, f"= {app.game_path.name}")
check("存档目录名", app.save_dir.name == m.SAVE_DIR_NAME, f"= {app.save_dir.name}")
check("版本检查通过", app.check_version()["valid"] is True)
check("桥接脚本可定位", m.resource_path(m.BRIDGE_FILE).is_file())
check("默认只开速度", app.state["speed_enabled"] is True and app.state["gold_enabled"] is False)
EXPECTED_TOGGLES = {"speed_enabled","gold_enabled","gems_enabled","lives_enabled","kill_gold_enabled",
    "invincible","hero_no_cooldown","damage_enabled","tower_speed_enabled","tower_range_enabled",
    "barrack_enabled","unlock_levels","three_stars"}
check("toggle 键齐全", set(app.toggles) == EXPECTED_TOGGLES,
      f"= {len(app.toggles)} 个, 缺 {EXPECTED_TOGGLES - set(app.toggles)}")
check("数值控件数量>=10", len(app.values) >= 10, f"= {len(app.values)}")

# 状态文件格式必须是 return {...}（prepare_bridge 会置 active=true 并写盘）
assert app.prepare_bridge(), "prepare_bridge 失败"
txt = (app.save_dir / m.STATE_FILE).read_text(encoding="utf-8")
check("状态文件 return 表格", txt.strip().startswith("return {") and txt.strip().endswith("}"))
check("状态含 heartbeat", "heartbeat =" in txt)
check("状态含全部功能键", all(k in txt for k in
      ("speed_enabled","gold_enabled","gems_enabled","lives_enabled","invincible",
       "hero_no_cooldown","damage_enabled","tower_speed_enabled","tower_range_enabled",
       "barrack_enabled","unlock_levels","three_stars","cmd_win")))

# Lua 侧能否真正解析这个状态文件（用游戏自带的 LuaJIT 验证格式正确性）
import ctypes
_lua51 = str(m.locate_default_game().parent / "lua51.dll")
if os.path.isfile(_lua51):
    lua = ctypes.CDLL(_lua51)
    lua.luaL_newstate.restype = ctypes.c_void_p
    L = ctypes.c_void_p(lua.luaL_newstate())
    lua.luaL_openlibs.argtypes = [ctypes.c_void_p]; lua.luaL_openlibs(L)
    lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lua.luaL_loadstring.restype = ctypes.c_int
    lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lua.lua_pcall.restype = ctypes.c_int
    lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lua.lua_tolstring.restype = ctypes.c_char_p

    def ex(c):
        if lua.luaL_loadstring(L, c.encode()) != 0:
            return "LOAD:" + str(lua.lua_tolstring(L, -1))
        if lua.lua_pcall(L, 0, 1, 0) != 0:
            return "EXEC:" + str(lua.lua_tolstring(L, -1))
        r = lua.lua_tolstring(L, -1)
        return r.decode() if r else None

    _w = str(app.save_dir).replace(chr(92), "/")
    ex(f'''local FS={{}}
    function FS.write(n,d) local f=io.open([[{_w}]].."/"..n,"w") if f then f:write(d) f:close() end end
    function FS.load(n) local f=io.open([[{_w}]].."/"..n,"r") if not f then return nil,"nf" end
      local s=f:read("*a") f:close() local c,e=load(s,n) if not c then return nil,e end return c end
    love={{filesystem=FS}}''')
    res = ex("local f=assert(love.filesystem.load('krft_state.lua')); local t=assert(f()); "
             "return type(t)..' active='..tostring(t.active)..' n='"
             "..tostring((function() local n=0 for _ in pairs(t) do n=n+1 end return n end)())")
    check("游戏 LuaJIT 可解析状态文件", bool(res) and res.startswith("table active=true"), f"= {res}")
else:
    print("[SKIP] 未找到 lua51.dll，跳过状态文件解析验证（需先安装游戏）")

# 回调驱动
app.set_speed(5);  check("set_speed(5)", float(app.state["speed"]) == 5.0)
app.set_kill_gold(10); check("set_kill_gold(10)", float(app.state["kill_gold_multiplier"]) == 10.0)
app.toggle_pause(); check("toggle_pause", app.state["paused"] is True)
app.toggle_pause(); check("toggle_pause 复位", app.state["paused"] is False)
import tkinter.messagebox as _mb
_mb.askyesno = lambda *a, **k: True   # 自动确认
app.state["cmd_win"] = 0; app.write_state(); app.cmd_instant_win()
check("立即通关命令自增", int(app.state["cmd_win"]) == 1)
app.cmd_skip_wave(); check("下一波命令自增", int(app.state["cmd_skip_wave"]) == 1)
app.cmd_force_wave(); check("强制清场命令自增", int(app.state["cmd_force_wave"]) == 1)
_mb.askyesno = _mb.askyesno

# 开关切换（含存档类备份路径）
app.toggles["invincible"].set(True); app.on_toggle()
check("无敌开关写状态", app.state["invincible"] is True)
app.toggles["gems_enabled"].set(True); app.on_toggle()
# 临时目录没有 slot_*.lua -> 自动备份无法完成 -> 按安全设计回滚该开关
check("无存档时宝石开关被回滚（安全）", app.state["gems_enabled"] is False)
check("回滚后不崩溃且日志有记录", "存档" in app.status_text.get("1.0", "end"))

# 退出恢复
app.shutdown()
txt2 = (app.save_dir / m.STATE_FILE).read_text(encoding="utf-8")
check("退出时 active=false", "active = false" in txt2)

app.poller.stop(); root.destroy()
shutil.rmtree(tmp, ignore_errors=True)
print("\n" + "="*50)
print("FAILED: "+str(F) if F else "ALL GUI SMOKE TESTS PASSED")
sys.exit(1 if F else 0)
