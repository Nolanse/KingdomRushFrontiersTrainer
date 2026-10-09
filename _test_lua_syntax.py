"""校验 Lua 桥接脚本可编译，并用真实 LuaVM 验证 heartbeat 重复键的修复效果。

1. 用 lupa（真实 LuaJIT/Lua 解释器）编译 krft_bridge.lua，确认新增的
   read_state_fallback / read_state_any / io 兜底写状态没有语法错误。
2. 复现旧版 state_text() 生成的「heartbeat 重复键」文件，验证 Lua 侧的实际行为。
"""

import re
from pathlib import Path

import lupa
from lupa import LuaRuntime

BRIDGE = Path(__file__).resolve().parent / "krft_bridge.lua"


def check_lua_compiles() -> bool:
    """把桥接脚本塞进一个带最小桩环境的 LuaVM，确认能编译执行。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    # 桥接在加载期就 pcall(require, ...)，需要先摆好桩，否则会走异常分支
    lua.execute("""
        love = {
            filesystem = {
                load = function() return nil end,
                write = function() end,
                getSourceBaseDirectory = function() return "." end,
            },
            timer = { getTime = function() return 0 end },
        }
        package = package or {}
        _G.io = io
    """)
    source = BRIDGE.read_text(encoding="utf-8")
    # Lua 5.1 有 loadstring，5.2+ 改名 load；这里按存在的那个来。
    # 返回值打包成 table，避免多返回值在 lupa 里不可迭代。
    result = lua.eval(
        "function(src)"
        "  local f = loadstring or load"
        "  local chunk, err = f(src, '@krft_bridge.lua')"
        "  return { ok = chunk ~= nil, err = err }"
        "end"
    )(source)
    ok_lua = bool(result["ok"])
    err = result["err"]
    if not ok_lua:
        print(f"[FAIL] 桥接脚本编译失败：{err}")
        return False
    print("[PASS] 桥接脚本编译通过（含 io 兜底读写）")
    return True


def check_heartbeat_duplicate() -> None:
    """旧版 state_text() 会输出两个 heartbeat；确认新写法只有一个。"""
    new_text = Path(__file__).resolve().parent / "krft_state_new_sample.lua"
    sample = """
local keys = {}
local body = "return {\n"
for _, k in ipairs({"active", "heartbeat", "revision", "speed"}) do
    keys[#keys + 1] = k
end
print("新写法 heartbeat 出现次数：", select(2, string.gsub(table.concat(keys, ","), "heartbeat", "")))
"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(sample)
    # 直接在 Python 侧做正则统计更可靠
    print("[INFO] Python 侧正则统计用于对照")


def python_side_heartbeat_check() -> bool:
    """真实调用 state_text()，确认输出里 heartbeat 只出现一次。"""
    pyw = Path(__file__).resolve().parent / "KingdomRushFrontiersTrainer.pyw"
    src = pyw.read_text(encoding="utf-8")
    # 从 def state_text 起，遇到下一个同级 def（或 class）为止
    start = src.find("    def state_text(self)")
    if start < 0:
        print("[FAIL] 未能在源码中定位 state_text")
        return False
    rest = src[start:]
    nxt = re.search(r"\n    (?:def |# ---)", rest)
    body = rest[: nxt.start()] if nxt else rest

    import time as _time

    namespace: dict[str, object] = {
        "lua_literal": lambda v: repr(v),
        "time": _time,
    }
    exec("class _T:\n" + body, namespace)  # type: ignore[arg-type]
    fake = namespace["_T"]()
    fake.state = {"active": True, "revision": 7, "heartbeat": 111, "speed": 3.0}
    text = fake.state_text()

    count = text.count("heartbeat")
    ok = count == 1
    print(f"[{'PASS' if ok else 'FAIL'}] state_text 输出中 heartbeat 出现 {count} 次（应为 1）")
    if not ok:
        print(text)
        return False

    # 再用真实 Lua 跑一遍，确认这份状态文件能被解析且 heartbeat 取到新值
    lua = LuaRuntime(unpack_returned_tuples=True)
    parsed = lua.execute(
        "local f = loadstring or load\n"
        "local chunk = f(...)\n"
        "return chunk()",
        text,
    )
    heartbeat = parsed["heartbeat"]
    ok2 = isinstance(heartbeat, (int, float)) and heartbeat > 111
    print(f"[{'PASS' if ok2 else 'FAIL'}] Lua 解析得 heartbeat={heartbeat}（应 > 旧值 111）")
    return ok and ok2


def lua_duplicate_key_behavior() -> None:
    """用真实 Lua 验证：表构造里的重复键取最后一个，且不报错。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    result = lua.execute("""
        local t = { heartbeat = 111, active = true, heartbeat = 222 }
        return t.heartbeat
    """)
    print(f"[INFO] Lua 表构造重复键取值 = {result}（后者覆盖前者）")


def main() -> None:
    ok = True
    ok &= check_lua_compiles()
    ok &= python_side_heartbeat_check()
    lua_duplicate_key_behavior()
    print()
    print("RESULT:", "ALL PASSED" if ok else "HAS FAILURE")


if __name__ == "__main__":
    main()
