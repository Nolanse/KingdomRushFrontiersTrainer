"""用游戏自带的 lua51.dll（LuaJIT）加载桥接脚本做离线仿真测试。

不启动游戏、不碰存档：伪造 love.filesystem / signal / storage / main，
验证速度、金币、生命、无敌、伤害、兵营、存档、命令与恢复逻辑。

前置条件：本机已安装 Kingdom Rush Frontiers（会加载游戏目录下的 lua51.dll）。
游戏路径与桥接脚本均自动定位，无需手工修改。

用法：python _test_bridge.py
"""
import ctypes, os, sys, time, tempfile, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = str(HERE / "krft_bridge.lua")

# 复用主程序的 Steam 库扫描来定位游戏，避免硬编码路径
sys.path.insert(0, str(HERE))
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("krft_main", HERE / "KingdomRushFrontiersTrainer.pyw")
_main = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_main)

GAME_PATH = _main.locate_default_game()
GAME = str(GAME_PATH.parent)
LUA51 = os.path.join(GAME, "lua51.dll")

if not os.path.isfile(LUA51):
    print("[SKIP] 未找到游戏目录下的 lua51.dll，先安装 Kingdom Rush Frontiers 再运行本测试。")
    print(f"       当前查找位置：{GAME}")
    sys.exit(2)

work = Path(tempfile.mkdtemp(prefix="krft_test_"))
lua = ctypes.CDLL(LUA51)
lua.luaL_newstate.restype = ctypes.c_void_p
L = ctypes.c_void_p(lua.luaL_newstate())
lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
lua.luaL_openlibs(L)

def dofile(path):
    lua.luaL_loadfile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lua.luaL_loadfile.restype = ctypes.c_int
    lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lua.lua_pcall.restype = ctypes.c_int
    lua.lua_tolstring.argtypes=[ctypes.c_void_p, ctypes.c_int]
    lua.lua_tolstring.restype=ctypes.c_char_p
    if lua.luaL_loadfile(L, str(path).encode()) != 0:
        msg = ctypes.c_char_p(lua.lua_tolstring(L, -1)).value
        raise RuntimeError(f"load {path}: {msg}")
    if lua.lua_pcall(L, 0, 1, 0) != 0:
        msg = ctypes.c_char_p(lua.lua_tolstring(L, -1)).value
        raise RuntimeError(f"call {path}: {msg}")

def dostring(code):
    lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lua.luaL_loadstring.restype = ctypes.c_int
    lua.lua_tolstring.argtypes=[ctypes.c_void_p, ctypes.c_int]
    lua.lua_tolstring.restype=ctypes.c_char_p
    if lua.luaL_loadstring(L, code.encode()) != 0:
        msg = ctypes.c_char_p(lua.lua_tolstring(L, -1)).value
        raise RuntimeError(f"loadstring: {msg}")
    if lua.lua_pcall(L, 0, 0, 0) != 0:
        msg = ctypes.c_char_p(lua.lua_tolstring(L, -1)).value
        raise RuntimeError(f"exec: {msg}\n--- code ---\n{code}")

# ---- 伪造 love.filesystem（指向临时目录）----
fs_lua = f'''
local FS = {{ files = {{}}, handlers = {{}} }}
function FS.write(name, data)
  local p = [[{str(work).replace(chr(92), '/')}]] .. "/" .. name
  local f = io.open(p, "w"); if f then f:write(data); f:close() end
end
function FS.load(name)
  local p = [[{str(work).replace(chr(92), '/')}]] .. "/" .. name
  local f = io.open(p, "r")
  if not f then return nil, "cannot open " .. name end
  local s = f:read("*a"); f:close()
  local chunk, err = load(s, name)
  if not chunk then return nil, err end
  return chunk
end
love = {{ filesystem = FS }}
'''
dostring(fs_lua)

# ---- 伪造 hump.signal + storage + game_settings ----
dostring('''
package.preload["hump.signal"] = function()
  local M = { handlers = {} }
  function M.register(name, fn) M.handlers[name] = M.handlers[name] or {}; table.insert(M.handlers[name], fn); return true end
  function M.emit(name, ...)
    for _, fn in ipairs(M.handlers[name] or {}) do fn(...) end
  end
  signal = M   -- 游戏里 signal 是全局，桥接的 command_instant_win 直接用全局 signal
  return M
end
package.preload["storage"] = function()
  local M = { active_slot_idx = 1, slots = {
    [1] = { gems = 100, levels = { [1] = { stars = 1 } } }
  } }
  function M:load_slot(i) return self.slots[i] end
  function M:save_slot(slot, i, _) self.slots[i] = slot; return true end
  storage = M   -- 真实游戏里 storage 是全局
  return M
end
package.preload["game_settings"] = function() return { last_level = 22 } end
''')

# ---- 加载桥接 ----
dofile(BRIDGE)
dostring("bridge = custom_script; assert(type(bridge)=='table', 'no bridge')")
print("[OK] 桥接脚本加载成功")

DEFAULTS = {
    "active": "true", "speed_enabled": "true", "speed": "1", "paused": "false",
    "gold_enabled": "false", "gold_value": "99999",
    "gems_enabled": "false", "gems_value": "99999",
    "lives_enabled": "false", "lives_value": "20",
    "kill_gold_enabled": "false", "kill_gold_multiplier": "2",
    "invincible": "false", "hero_no_cooldown": "false",
    "damage_enabled": "false", "damage_multiplier": "3",
    "tower_speed_enabled": "false", "tower_speed_multiplier": "3",
    "tower_range_enabled": "false", "tower_range_multiplier": "2",
    "barrack_enabled": "false", "barrack_soldiers": "3", "barrack_respawn_scale": "1.0",
    "unlock_levels": "false", "three_stars": "false",
    "cmd_skip_wave": "0", "cmd_force_wave": "0", "cmd_win": "0",
}

def write_state(**kw):
    """与 GUI 一致：每次写全量状态，格式为 return { ... } 表格构造。"""
    state = dict(DEFAULTS)
    state.update(kw)
    state.setdefault("heartbeat", str(int(time.time())))
    lines = [f"    {k} = {v}," for k, v in sorted(state.items())]
    (work / "krft_state.lua").write_text("return {\n" + "\n".join(lines) + "\n}\n", encoding="utf-8")

def setup_game():
    dostring('''
    _store = {
      player_gold = 500, lives = 20, paused = false,
      level_idx = 3, level_mode = 1, level_difficulty = 0,
      entities = {
        [1] = { tower = { damage_factor = 1.0, type = "archer" },
                health = { hp = 100, hp_max = 100 },
                attacks = { range = 5, list = { { cooldown = 2.0, damage_min = 10, damage_max = 20, range = 5 } } } },
        [2] = { hero = true, health = { hp = 50, hp_max = 50 },
                skills = { s1 = { cooldown = 30 } } },
        [3] = { tower = { damage_factor = 1.0 }, barrack = { max_soldiers = 3, soldiers = {
                { health = { hp = 10, hp_max = 10, dead_lifetime = 8.0 } } } } },
        [9] = { enemy = true, health = { hp = 100, hp_max = 100, dead = false } },
      },
      waves_finished = 3, wave_group_number = 1,
      saved_stars = 0, unlocked_next = false, save_slot_calls = 0,
    }    _item = { item_name = "game", store = _store, DBG_TIME_MULT = nil }
    game = _item
    main = { handler = { active_item = _item } }
    ''')

# 复刻 all/systems.lua 关卡协程的胜利判定与官方写档流程。
# 桥接只负责制造「无存活敌人 + 波次打完」这两个条件，
# 胜利判定、星级写入、下一关解锁、save_slot 全部由引擎侧完成——这正是修复的关键。
VICTORY_ENGINE = """
function engine_update(store)
  if store.game_outcome then return end
  if not store.force_next_wave and not store._waves_marked then return end
  store._waves_marked = true
  local alive = false
  for _, e in pairs(store.entities) do
    if type(e) == 'table' and e.enemy and type(e.health) == 'table' and not e.health.dead then
      alive = true break
    end
  end
  if alive then return end
  -- 官方流程：写 slot_level / stars / already_won
  store.saved_stars = 3
  storage.slots[1].levels[store.level_idx] = storage.slots[1].levels[store.level_idx] or {}
  storage.slots[1].levels[store.level_idx].stars = 3
  -- 解锁下一关
  storage.slots[1].levels[store.level_idx + 1] = storage.slots[1].levels[store.level_idx + 1] or {}
  storage.slots[1].levels[store.level_idx + 1].unlocked = true
  store.unlocked_next = true
  storage:save_slot(storage.slots[1], 1, true)
  store.save_slot_calls = (store.save_slot_calls or 0) + 1
  -- 最后才判定胜利并发信号
  store.game_outcome = {
    victory = true, lives_left = store.lives or 20,
    stars = 3, level_idx = store.level_idx,
    level_mode = store.level_mode, level_difficulty = store.level_difficulty,
  }
  signal.emit('game-victory', store)
  signal.emit('game-victory-after', store)
end
"""
dostring(VICTORY_ENGINE)

def tick(n=1, dt=0.25):
    for _ in range(n):
        dostring("bridge:update(0.25)")
        dostring("if _store then engine_update(_store) end")

def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {extra}")
    if not cond: FAILURES.append(label)

FAILURES = []
# lua_pop / lua_getglobal 是宏，CDLL 不导出；用 lua_settop 清栈，表达式在 Lua 侧求值
lua.lua_settop.argtypes = [ctypes.c_void_p, ctypes.c_int]
lua.lua_tonumber.argtypes = [ctypes.c_void_p, ctypes.c_int]
lua.lua_tonumber.restype = ctypes.c_double
lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lua.luaL_loadstring.restype = ctypes.c_int

def qs(expr):
    """在 Lua 侧求值并以字符串取回（用于判断 nil 等非数值状态）。"""
    lua.lua_settop(L, 0)
    if lua.luaL_loadstring(L, f"return tostring({expr})".encode()) != 0:
        lua.lua_settop(L, 0)
        return "<load-error>"
    if lua.lua_pcall(L, 0, 1, 0) != 0:
        lua.lua_settop(L, 0)
        return "<exec-error>"
    raw = lua.lua_tolstring(L, -1)
    lua.lua_settop(L, 0)
    return raw.decode("utf-8", "replace") if raw else None


def q(expr):
    """在 Lua 侧求值表达式并取回数值（支持 a.b.c / a.b[1]）。"""
    lua.lua_settop(L, 0)
    if lua.luaL_loadstring(L, f"return tonumber({expr})".encode()) != 0:
        lua.lua_settop(L, 0)
        return float("nan")
    if lua.lua_pcall(L, 0, 1, 0) != 0:
        lua.lua_settop(L, 0)
        return float("nan")
    val = lua.lua_tonumber(L, -1)
    lua.lua_settop(L, 0)
    return float(val)

# 1) 初始：握手
dostring("bridge:init()")
write_state(active="true", speed_enabled="true", speed="3")
setup_game(); tick(3)
check("速度 3x", abs(q("game.DBG_TIME_MULT") - 3) < 1e-9, f"= {q('game.DBG_TIME_MULT')}")
check("状态文件已写", (work/"krft_status.txt").exists())

# 2) 小数倍速 2.5
write_state(active="true", speed_enabled="true", speed="2.5")
tick(4)
v = q("game.DBG_TIME_MULT")
check("小数倍速在 2/3 间交替", v in (2.0, 3.0), f"= {v}")

# 3) 金币锁定
write_state(active="true", speed_enabled="true", speed="1", gold_enabled="true", gold_value="88888")
tick(2)
check("金币锁定 88888", abs(q("_store.player_gold") - 88888) < 1e-9, f"= {q('_store.player_gold')}")

# 4) 生命锁定
write_state(active="true", speed_enabled="true", speed="1", lives_enabled="true", lives_value="50")
tick(2)
check("基地生命 50", abs(q("_store.lives") - 50) < 1e-9, f"= {q('_store.lives')}")

# 5) 无敌
write_state(active="true", speed_enabled="true", speed="1", invincible="true")
dostring("_store.entities[1].health.hp = 7")
tick(2)
check("友军无敌回满血", abs(q("_store.entities[1].health.hp") - 100) < 1e-9, f"= {q('_store.entities[1].health.hp')}")

# 6) 英雄无冷却
write_state(active="true", speed_enabled="true", speed="1", hero_no_cooldown="true")
tick(2)
check("英雄技能 CD = 0", abs(q("_store.entities[2].skills.s1.cooldown")) < 1e-9, f"= {q('_store.entities[2].skills.s1.cooldown')}")

# 7) 伤害倍率 3x
write_state(active="true", speed_enabled="true", speed="1", damage_enabled="true", damage_multiplier="3")
tick(2)
d1 = q("_store.entities[1].attacks.list[1].damage_min")
check("伤害 10 -> 30", abs(d1 - 30) < 1e-6, f"= {d1}")

# 8) 攻速倍率 2x
write_state(active="true", speed_enabled="true", speed="1", tower_speed_enabled="true", tower_speed_multiplier="2")
tick(2)
cs = q("_store.entities[1].attacks.list[1].cooldown")
check("塔攻速 CD 2.0 -> 1.0", abs(cs - 1.0) < 1e-6, f"= {cs}")

# 9) 射程倍率 2x
write_state(active="true", speed_enabled="true", speed="1", tower_range_enabled="true", tower_range_multiplier="2")
tick(2)
rg = q("_store.entities[1].attacks.range")
check("射程 5 -> 10", abs(rg - 10) < 1e-6, f"= {rg}")

# 10) 兵营
write_state(active="true", speed_enabled="true", speed="1", barrack_enabled="true",
            barrack_soldiers="5", barrack_respawn_scale="0.5")
tick(2)
ms = q("_store.entities[3].barrack.max_soldiers")
dl = q("_store.entities[3].barrack.soldiers[1].health.dead_lifetime")
check("兵营人数 3 -> 5", abs(ms - 5) < 1e-9, f"= {ms}")
check("刷新时间 8 -> 4", abs(dl - 4.0) < 1e-6, f"= {dl}")

# 11) 杀敌金币倍率 5x（got-enemy-gold 信号）
write_state(active="true", speed_enabled="true", speed="1", kill_gold_enabled="true", kill_gold_multiplier="5")
dostring("_store.player_gold = 0")
tick(2)
# 真实流程：systems.lua 先 store.player_gold = store.player_gold + gold，再 emit
# 桥接只在 emit 之后补 (倍率-1) 差额，因此这里先加 base 10
dostring("_store.player_gold = _store.player_gold + 10; signal.emit('got-enemy-gold', nil, 10)")
check("杀敌 10 金 x5 = 50", abs(q("_store.player_gold") - 50) < 1e-9, f"= {q('_store.player_gold')}")
dostring("_store.player_gold = 0")
dostring("_store.player_gold = _store.player_gold + 7; signal.emit('got-enemy-gold', nil, 7)")
check("杀敌 7 金 x5 = 35", abs(q("_store.player_gold") - 35) < 1e-9, f"= {q('_store.player_gold')}")

# 12) 宝石 + 全关三星 + 解锁
write_state(active="true", speed_enabled="true", speed="1", gems_enabled="true", gems_value="77777",
            unlock_levels="true", three_stars="true")
tick(2)
check("宝石 77777", abs(q("storage.slots[1].gems") - 77777) < 1e-9, f"= {q('storage.slots[1].gems')}")
check("第 22 关三星", abs(q("storage.slots[1].levels[22].stars") - 3) < 1e-9, f"= {q('storage.slots[1].levels[22].stars')}")

# 12.5) 防回归：关卡载入后绝不能自动通关（v1.0.2 修复的 bug）
# 背景：process_instant_win 里曾在「换关时」把 win_stage 置 0，
# 导致任何关卡一载入就立刻清场并判定通关。必须验证未点击按钮时完全不动作。
setup_game()
dostring("_store.level_idx = 5")
write_state(active="true", speed_enabled="true", speed="1", cmd_win="0")
tick(8)   # 连续跑 many 帧，模拟关卡正常载入 + 玩家操作
check("载入关卡不自动清场", qs("_store.entities[9].health.dead") == "false",
      f"dead={qs('_store.entities[9].health.dead')}")
check("载入关卡敌人血量不变", float(q("_store.entities[9].health.hp")) == 100.0,
      f"hp={q('_store.entities[9].health.hp')}")
check("载入关卡不自动标记波次", qs("_store._waves_marked") == "nil",
      f"_waves_marked={qs('_store._waves_marked')}")
check("载入关卡不判定通关", qs("_store.game_outcome") == "nil",
      f"outcome={qs('_store.game_outcome')}")
check("载入关卡不写存档", float(q("_store.save_slot_calls")) == 0.0)
check("载入后速度功能仍正常", qs("game.DBG_TIME_MULT") == "1", f"= {qs('game.DBG_TIME_MULT')}")

# 其他按钮不受影响：提前下一波应照常工作
write_state(active="true", speed_enabled="true", speed="1", cmd_win="0", cmd_skip_wave="50")
tick(1)
check("提前下一波仍可用", qs("_store.send_next_wave") == "true")
check("提前下一波不触发通关", qs("_store.game_outcome") == "nil")

# 无敌等战斗开关也不应触发通关
setup_game()
write_state(active="true", speed_enabled="true", speed="1", cmd_win="0", invincible="true")
tick(5)
check("开启无敌不触发通关", qs("_store.game_outcome") == "nil")
check("开启无敌敌人未被清场", qs("_store.entities[9].health.dead") == "false")

# 13) 下一波 / 立即通关
write_state(active="true", speed_enabled="true", speed="1", cmd_skip_wave="500", cmd_win="200")
tick(1)
check("send_next_wave 置位", qs("_store.send_next_wave") == "true", f"= {qs('_store.send_next_wave')}")

# 立即通关：桥接不应自己造 game_outcome，而应清场 + 标记波次完成，
# 由引擎（仿真桩）判定胜利后走官方写档流程 -> 解锁下一关。
check("立即通关：敌人被清场", qs("_store.entities[9].health.dead") == "true")
check("立即通关：血量归零", float(q("_store.entities[9].health.hp")) == 0.0)
tick(1)
check("立即通关：波次已推进", float(q("_store.waves_finished")) > 3,
      f"= {q('_store.waves_finished')}")
check("引擎判定出胜利", qs("_store.game_outcome.victory") == "true", f"= {qs('_store.game_outcome.victory')}")
check("官方流程写入本关星级", float(q("_store.saved_stars")) >= 1, f"= {q('_store.saved_stars')}")
check("官方流程解锁下一关 (idx+1)", qs("_store.unlocked_next") == "true", f"= {qs('_store.unlocked_next')}")
check("已调用 save_slot", float(q("_store.save_slot_calls")) >= 1)

# 防回归：桥接绝不能自己构造 game_outcome 或自行发胜利信号，
# 否则又会绕过 systems.lua 的写档流程（那正是「不解锁下一关」的根因）。
setup_game()
dostring("_store.entities[9] = nil")   # 无敌人
dostring("_emitted = {}")
dostring("signal.emit = function(name, ...) _emitted[#_emitted+1] = name end")
write_state(active="true", speed_enabled="true", speed="1", cmd_win="300")
tick(1)
check("桥接不自建 game_outcome（第一阶段）", qs("_store.game_outcome") == "nil",
      f"= {qs('_store.game_outcome')}")
check("桥接不自行 emit 胜利信号", (qs("(function() return #_emitted end)()") or "0") == "0",
      f"emitted={qs('table.concat(_emitted, \'|\')')}")
tick(2)
# 波次已标记，但敌人已移除 -> 引擎仍应判定胜利并写档
check("引擎最终判定胜利", qs("_store.game_outcome.victory") == "true")
check("胜利后仍写入存档", float(q("_store.save_slot_calls")) >= 1)

# 14) 全部关闭 -> 恢复原值
setup_game()
write_state(active="true", speed_enabled="true", speed="1")
tick(3)
check("关闭后金币恢复 500", abs(q("_store.player_gold") - 500) < 1e-9, f"= {q('_store.player_gold')}")
check("关闭后无敌回退（血量不再被改）", q("_store.entities[1].health.hp_max") == 100)

# 15) 心跳超时 -> 自动恢复
dostring("_store.player_gold = 777")
write_state(active="true", speed_enabled="true", speed="4", gold_enabled="true", gold_value="1")
tick(2)
check("心跳有效时金币=1", abs(q("_store.player_gold") - 1) < 1e-9)
setup_game()
write_state(active="true", speed_enabled="true", speed="4", heartbeat="1")  # 过期心跳
tick(3)
check("心跳超时自动恢复金币 500", abs(q("_store.player_gold") - 500) < 1e-9, f"= {q('_store.player_gold')}")
check("心跳超时速度复位", qs("game.DBG_TIME_MULT") == "nil", f"= {qs('game.DBG_TIME_MULT')}")

# 4) 补：单独验证「有效心跳下速度保持」
write_state(active="true", speed_enabled="true", speed="4")
tick(2)
check("有效心跳速度保持 4x", qs("game.DBG_TIME_MULT") == "4", f"= {qs('game.DBG_TIME_MULT')}")

print("\n" + "="*50)
if FAILURES:
    print(f"FAILED {len(FAILURES)}: {FAILURES}")
else:
    print("ALL TESTS PASSED")
shutil.rmtree(work, ignore_errors=True)
sys.exit(1 if FAILURES else 0)
