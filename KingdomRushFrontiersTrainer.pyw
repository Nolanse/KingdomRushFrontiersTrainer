"""Kingdom Rush Frontiers 专用修改器 —— 界面主程序。

设计沿用 Kingdom Rush Trainer 的成熟方案：
外部 Tk 界面 + 游戏自带 -custom_script 入口加载的 Lua 桥接脚本。
本程序不注入 DLL、不读写游戏进程内存、不修改游戏安装目录中的任何文件。

运行：python KingdomRushFrontiersTrainer.pyw
"""

from __future__ import annotations

import atexit
import ctypes
import functools
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Kingdom Rush Frontiers 专用修改器"
APP_VERSION = "1.3.2"
APP_BUILD_DATE = "2026-10-09"
APP_CREDITS = f"v{APP_VERSION} · {APP_BUILD_DATE}"

# ---- 目标游戏参数（Frontiers = KR2，2026-10 实机提取确认）----
TARGET_EXE_NAME = "Kingdom Rush Frontiers.exe"
TARGET_VERSION_PREFIX = "kr2-desktop-"
TARGET_BUNDLE_ID = "com.ironhidegames.frontiers.windows.steam"
# appmanifest 扫不到时的兜底 AppID。
# 游戏本体是带 steam_api.dll 的 Steam 构建、但目录不在 steamapps\common 下时
# （第三方渠道 / 绿色版 / 手动拷贝 / 库目录被移动），缺少 SteamAppId 会让
# steam_api.dll 初始化失败、进程 1-2 秒静默退出，因此按已知 AppID 兜底注入。
KNOWN_STEAM_APP_ID = 458710
# 非 Steam 库安装时的额外探测位置（相对 %USERPROFILE%），只扫两层
NON_STEAM_SEARCH_ROOTS = ("Desktop", "Downloads", "Documents")
NON_STEAM_SEARCH_DEPTH = 2
NON_STEAM_SEARCH_LIMIT = 800
# identity = kingdom_rush_frontiers -> LÖVE 存档目录名。
# 这是旧版的假定值，仅作兜底；真实 identity 要从游戏 exe 里读出来（见 love_identity_dirs），
# 因为新版可能改 bundle id / identity，届时 %APPDATA%\kingdom_rush_frontiers 就不再是存档目录。
SAVE_DIR_NAME = "kingdom_rush_frontiers"
# 历史 identity 候选：按可能性排序。新版换了 identity 时会同时命中多个，
# 打分环节会挑出真正含存档特征文件的那一个。
KNOWN_SAVE_DIR_NAMES = (
    SAVE_DIR_NAME,
    "kingdom_rush_frontiers_steam",
    "kingdomrush_frontiers",
    "com.ironhidegames.frontiers.windows.steam",
    "com.ironhidegames.frontiers",
    "frontiers",
)
# LÖVE 存档目录在 %APPDATA% 下可能多包一层 LOVE\（不同 identity 拼接规则）
SAVE_DIR_PARENTS = ("", "LOVE")

# 主线关卡 1..22（kr2/data/levels/ 下另有 level81/82/99 特殊关，不计入）
FALLBACK_LAST_LEVEL = 22

# 进程识别：① 精确同名 → ② 归一化同名（忽略空格/大小写/.exe）→ ③ 关键字兜底。
# ③ 用于适配 exe 被改名、或经启动器间接拉起的非官方构建；
# 命中 PROCESS_NAME_EXCLUDE 的候选（Steam / 启动器 / 安装器）一律排除，避免误连。
PROCESS_NAME_KEYWORD = "frontiers"
PROCESS_NAME_EXCLUDE = ("steam", "loader", "launcher", "setup", "install",
                        "unins", "crash", "report", "helper", "update",
                        "trainer")

BRIDGE_MODULE = "krft_bridge"
BRIDGE_FILE = "krft_bridge.lua"
STATE_FILE = "krft_state.lua"
STATUS_FILE = "krft_status.txt"
# 一次性命令的单调递增计数。桥接靠「本次值 > 上次值」判断是不是新请求，
# 因此这几个值绝不能跨会话持久化（详见 TrainerApp.load_config）。
COMMAND_NONCES = ("cmd_skip_wave", "cmd_force_wave", "cmd_win")
GAME_WINDOW_TITLE_HINT = "Frontiers"

CREATE_NEW_PROCESS_GROUP = 0x00000200
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

DEFAULT_HOTKEYS = {
    "speed_1": "Ctrl+Alt+1",
    "speed_2": "Ctrl+Alt+2",
    "speed_3": "Ctrl+Alt+3",
    "speed_5": "Ctrl+Alt+5",
    "speed_8": "Ctrl+Alt+8",
    "pause": "Ctrl+Alt+Space",
    "gold": "Ctrl+Alt+G",
    "gems": "Ctrl+Alt+B",
    "lives": "Ctrl+Alt+L",
    "kill_gold": "Ctrl+Alt+K",
    "barrack": "Ctrl+Alt+Y",
    "invincible": "Ctrl+Alt+I",
    "hero_no_cooldown": "Ctrl+Alt+C",
    "damage": "Ctrl+Alt+D",
    "tower_speed": "Ctrl+Alt+A",
    "tower_range": "Ctrl+Alt+R",
    "skip_wave": "Ctrl+Alt+W",
    "instant_win": "Ctrl+Alt+V",
}

HOTKEY_LABELS = {
    "speed_1": "速度 1x",
    "speed_2": "速度 2x",
    "speed_3": "速度 3x",
    "speed_5": "速度 5x",
    "speed_8": "速度 8x",
    "pause": "暂停 / 恢复",
    "gold": "锁定金币",
    "gems": "锁定宝石",
    "lives": "锁定基地生命",
    "kill_gold": "杀敌金币倍率",
    "barrack": "兵营增强",
    "invincible": "友军无敌",
    "hero_no_cooldown": "英雄无冷却",
    "damage": "伤害倍率",
    "tower_speed": "防御塔攻速",
    "tower_range": "防御塔射程",
    "skip_wave": "提前下一波",
    "instant_win": "立即通关",
}


# =====================================================================
# 基础工具
# =====================================================================

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def app_config_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return base / "KingdomRushFrontiersTrainer"


def log_message(message: str) -> None:
    """写运行日志。--windowed 模式没有控制台，排查问题全靠它。"""
    try:
        directory = app_config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with open(str(directory / "trainer.log"), "a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except OSError:
        pass


def guarded(func):
    """包裹 Tk 回调：异常写日志 + 弹窗，避免表现为「点了没反应」。"""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception:
            detail = traceback.format_exc()
            log_message(f"{func.__name__} 异常：\n{detail}")
            try:
                messagebox.showerror(APP_NAME, f"操作失败：{func.__name__}\n\n{detail}")
            except Exception:
                pass
    return wrapper


def candidate_save_dirs() -> list[Path]:
    """所有已知 identity 组合出来的存档目录，按可能性排序。"""
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    result: list[Path] = []
    for name in KNOWN_SAVE_DIR_NAMES:
        for parent in SAVE_DIR_PARENTS:
            result.append(appdata / parent / name if parent else appdata / name)
    return result


# 判定一个目录「像不像本作存档目录」的特征文件
SAVE_DIR_MARKERS = ("settings.lua", "global.lua", "krft_state.lua", "krft_status.txt")


def love_identity_dirs(blob: bytes) -> list[str]:
    """从游戏 exe 内嵌内容里提取 LÖVE identity 候选。

    LÖVE 在 love.filesystem.setIdentity(id) 之后，把存档目录定成
    %APPDATA%\\<id>\\save（新版则是 %APPDATA%\\<id>）。identity 写死在游戏代码里，
    新版一旦改 bundle id / identity，%APPDATA%\\kingdom_rush_frontiers 就不再是存档目录，
    桥接文件写进去游戏也读不到——表现为「module 'krft_bridge' not found」
    且 require 的候选路径里完全不出现存档目录。

    这里从 main.lua 的字节码常量表里扫出候选：与已知 identity 名单交叉，
    再补充任意形如 com.xxx 的 bundle id，最后交给打分环节裁决。
    """
    found: list[str] = []
    for name in KNOWN_SAVE_DIR_NAMES:
        if name.encode() in blob:
            found.append(name)
    # 补扫 bundle id 形态（com.ironhidegames.frontiers.windows.steam 等）
    for match in re.finditer(rb"com\.[a-z0-9_]+(?:\.[a-z0-9_]+)+", blob, re.I):
        name = match.group(0).decode("ascii", "ignore").lower()
        if name not in found:
            found.append(name)
    return found


def discover_save_dirs() -> list[Path]:
    """列出本作可能使用的全部存档目录，按可能性从高到低。

    除标准 identity 目录外，额外扫描 %APPDATA%\\LOVE\\* 与 %APPDATA%\\*kingdom* /
    *frontiers* / com.*：改了 bundle id / identity 的版本（含新版与第三方构建）
    会把存档落到别的目录名下，只认死 SAVE_DIR_NAME 会出现
    「桥接写了文件、游戏却找不到模块」的假死。
    """
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    found: list[Path] = []

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved not in found:
            found.append(resolved)

    for path in candidate_save_dirs():
        add(path)
    for pattern in ("LOVE/*", "*kingdom*", "*frontiers*", "*ironhide*", "com.*"):
        try:
            for child in sorted(appdata.glob(pattern)):
                if child.is_dir():
                    add(child)
        except OSError:
            continue
    return found


def save_dir_score(path: Path) -> int:
    """目录的「本作存档目录」可信度；-1 表示目录不存在。"""
    try:
        if not path.is_dir():
            return -1
    except OSError:
        return -1
    score = 0
    try:
        if any(path.glob("slot_*.lua")):
            score += 4
    except OSError:
        pass
    for name in SAVE_DIR_MARKERS:
        try:
            if (path / name).exists():
                score += 1
        except OSError:
            continue
    return score


def choose_save_dir(override: str | Path | None = None,
                    game_path: str | Path | None = None) -> Path:
    """选出本次会话使用的存档目录。

    优先级：override（launch.json 的 save_dir）→ 从游戏 exe 读出的 identity 目录
    → 特征打分最高者 → 标准 identity 目录。打分全为 0 时保持旧行为（优先存在的
    标准目录），绝不返回误导性的新目录。

    game_path 用于在新版换了 identity 的情况下定位真实存档目录：
    桥接文件必须落在游戏 setIdentity 指定的目录里，否则 require 不到模块。
    """
    if override:
        text = str(override).strip().strip('"')
        if text:
            return Path(text).expanduser()

    identities = identities_from_game(game_path)
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    for name in identities:
        for parent in SAVE_DIR_PARENTS:
            path = appdata / parent / name if parent else appdata / name
            if save_dir_score(path) > 0:
                return path
            # 目录还不存在时也优先采用：游戏刚装、还没跑过，首启就要写对位置
            try:
                if path.is_dir():
                    return path
            except OSError:
                continue

    for path in sorted(discover_save_dirs(), key=save_dir_score, reverse=True):
        if save_dir_score(path) > 0:
            return path
    for path in candidate_save_dirs():
        try:
            if path.is_dir():
                return path
        except OSError:
            continue
    return candidate_save_dirs()[0]


def identities_from_game(game_path: str | Path | None) -> list[str]:
    """读取游戏 exe 里的 LÖVE identity 候选；读不到就退回历史名单。"""
    if game_path:
        try:
            parsed = parse_game_binary(Path(game_path))
            found = parsed.get("identities")
            if isinstance(found, list) and found:
                return [str(item) for item in found]
        except OSError:
            pass
    return list(KNOWN_SAVE_DIR_NAMES)


def steam_library_dirs() -> list[Path]:
    """找出本机所有 Steam 库目录（steamapps）。

    依次尝试：注册表（SteamPath / InstallPath）、常见默认安装位置、各盘符下的常见库名，
    再解析每个库的 libraryfolders.vdf。
    """
    found: list[Path] = []
    if os.name != "nt":
        return found
    roots: list[Path] = []
    try:
        import winreg

        for hive, sub in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        ):
            for value in ("SteamPath", "InstallPath"):
                try:
                    with winreg.OpenKey(hive, sub) as key:
                        roots.append(Path(str(winreg.QueryValueEx(key, value)[0])))
                except OSError:
                    continue
    except ImportError:
        pass

    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "Steam")
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:\\")
        try:
            if not drive.exists():
                continue
        except OSError:
            continue
        roots.append(drive / "SteamLibrary")
        roots.append(drive / "Steam")
        roots.append(drive / "Program Files" / "Steam")
        roots.append(drive / "Program Files (x86)" / "Steam")

    def add(steamapps: Path) -> None:
        try:
            resolved = steamapps.resolve()
        except OSError:
            return
        if resolved.is_dir() and resolved not in found:
            found.append(resolved)

    for root in roots:
        steamapps = root / "steamapps"
        add(steamapps)
        vdf = steamapps / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            add(Path(match.group(1).replace("\\\\", "\\")) / "steamapps")
    return found


def locate_default_game() -> Path:
    """自动定位 Kingdom Rush Frontiers.exe。"""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
    else:
        here = Path(__file__).resolve().parent
    candidates.extend([
        here / TARGET_EXE_NAME,
        here.parent / TARGET_EXE_NAME,
        Path.cwd() / TARGET_EXE_NAME,
    ])
    for steamapps in steam_library_dirs():
        try:
            for match in sorted((steamapps / "common").glob(f"*/{TARGET_EXE_NAME}")):
                candidates.append(match)
        except OSError:
            continue
    # Steam 库外还有一份副本时（第三方渠道 / 绿色版 / 手动拷贝），
    # 只在 Steam 库里找不到时才启用，避免抢掉正常安装的优先级
    if not any(p.is_file() for p in candidates):
        candidates.extend(scan_non_steam_roots())
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0] if candidates else Path(TARGET_EXE_NAME)


def scan_non_steam_roots() -> list[Path]:
    """在桌面 / 下载 / 文档下找不在 Steam 库里的安装副本。

    只按文件名匹配、只扫两层、命中数有上限，代价可控。
    找不到就返回空列表，绝不因为扫描失败影响正常启动。
    """
    found: list[Path] = []
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return found
    for name in NON_STEAM_SEARCH_ROOTS:
        root = home / name
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        for depth in range(1, NON_STEAM_SEARCH_DEPTH + 1):
            pattern = "/".join(["*"] * depth) + f"/{TARGET_EXE_NAME}"
            try:
                for match in sorted(root.glob(pattern)):
                    found.append(match)
                    if len(found) >= NON_STEAM_SEARCH_LIMIT:
                        return found
            except OSError:
                continue
    return found


def visible_window_titles(pid: int) -> list[str]:
    if os.name != "nt":
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    titles: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(handle, _lparam):
        if user32.IsWindowVisible(handle):
            length = user32.GetWindowTextLengthW(handle)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(handle, buffer, length + 1)
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
                if owner.value == pid:
                    titles.append(buffer.value)
        return True

    user32.EnumWindows(callback, 0)
    return titles


def list_processes() -> list[tuple[int, str]]:
    """枚举当前全部进程，返回 [(pid, exe 名)]。"""
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    result: list[tuple[int, str]] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return result
        while True:
            result.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def normalize_process_name(name: str) -> str:
    """归一化进程名：去 .exe，去空格 / 点 / 下划线 / 连字符，转小写。"""
    stem = str(name or "").strip().lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    return re.sub(r"[\s._\-]+", "", stem)


def process_match_tier(exe_name: str, target: str = TARGET_EXE_NAME) -> int:
    """进程名匹配分级：0=不匹配 1=关键字兜底 2=归一化同名 3=精确同名（越大越可信）。"""
    name = str(exe_name or "").strip()
    if not name:
        return 0
    if name.lower() == target.lower():
        return 3
    normalized = normalize_process_name(name)
    if normalized and normalized == normalize_process_name(target):
        return 2
    if any(word in normalized for word in PROCESS_NAME_EXCLUDE):
        return 0
    if PROCESS_NAME_KEYWORD and PROCESS_NAME_KEYWORD in normalized:
        return 1
    return 0


def find_game_processes(target: str = TARGET_EXE_NAME) -> list[tuple[int, str]]:
    """分层匹配游戏进程，返回 [(pid, exe 名)]。

    只返回最高命中的那一层：存在精确同名进程时，绝不把模糊命中的启动器一起混进来。
    同时排除当前修改器自身进程，避免单文件 exe 名字含 "Frontiers" 时把自己误判为游戏。
    """
    ranked: dict[int, list[tuple[int, str]]] = {}
    own_pid = os.getpid()
    for pid, name in list_processes():
        if pid == own_pid:
            continue
        tier = process_match_tier(name, target)
        if tier:
            ranked.setdefault(tier, []).append((pid, name))
    if not ranked:
        return []
    return ranked[max(ranked)]


def find_process_ids(exe_name: str = TARGET_EXE_NAME) -> list[int]:
    return [pid for pid, _ in find_game_processes(exe_name)]


def detect_steam_app_id(game_path: Path) -> int | None:
    """从 appmanifest 识别 Frontiers 的 AppID（用于注入 Steam 运行上下文）。

    优先按 installdir 指向的目录精确匹配，匹配不到再按 name 找 Frontiers。
    目录不在 Steam 库内时（第三方渠道 / 绿色版 / 手动拷贝 / 库目录被移动过），
    退化为「本体是否带 steam_api.dll」判断，命中则注入已知 AppID。
    """
    try:
        game_dir = game_path.resolve().parent
    except OSError:
        return None
    steamapps = None
    for candidate in (game_dir.parent, game_dir.parent.parent):
        if candidate.name.lower() == "steamapps":
            steamapps = candidate
            break
    if steamapps is None:
        # 只对本作主程序兜底，避免把 AppID 注入到名字碰巧含关键字的无关进程
        if PROCESS_NAME_KEYWORD not in normalize_process_name(game_path.name):
            return None
        return KNOWN_STEAM_APP_ID if looks_like_steam_build(game_dir) else None
    by_folder: int | None = None
    by_name: int | None = None
    for manifest in sorted(steamapps.glob("appmanifest_*.acf")):
        match = re.match(r"appmanifest_(\d+)\.acf", manifest.name)
        if not match:
            continue
        app_id = int(match.group(1))
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        folder_match = re.search(r'"installdir"\s+"([^"]+)"', text)
        if folder_match:
            folder = folder_match.group(1).replace("\\\\", "\\")
            try:
                if (steamapps / "common" / folder).resolve() == game_dir:
                    by_folder = app_id
            except OSError:
                pass
        if re.search(r'"name"\s+"Kingdom Rush Frontiers"', text):
            by_name = app_id
    if by_folder or by_name:
        return by_folder or by_name
    # 不在 Steam 库内：appmanifest 扫不到，回退到「本体是否带 steam_api.dll」这一客观事实。
    # 只对本作主程序生效，避免把 AppID 注入到名字碰巧含关键字的无关进程。
    if PROCESS_NAME_KEYWORD not in normalize_process_name(game_path.name):
        return None
    return KNOWN_STEAM_APP_ID if looks_like_steam_build(game_dir) else None


# Steam 运行上下文的客观标志：安装目录里有 steam_api.dll / steam_api64.dll
STEAM_API_DLLS = ("steam_api.dll", "steam_api64.dll")


def looks_like_steam_build(game_dir: Path) -> bool:
    """目录里带 steam_api*.dll → 该构建需要 Steam 运行上下文才能启动。"""
    try:
        return any((game_dir / dll).is_file() for dll in STEAM_API_DLLS)
    except OSError:
        return False


def steam_launch_env(game_path: Path) -> dict[str, str]:
    """给子进程注入 SteamAppId / SteamGameId。

    不注入时 steam_api.dll 初始化失败，进程会在 1-2 秒内静默退出（退出码 0）。
    """
    env = os.environ.copy()
    app_id = detect_steam_app_id(game_path)
    if app_id:
        env["SteamAppId"] = str(app_id)
        env["SteamGameId"] = str(app_id)
    try:
        game_dir = str(game_path.resolve().parent)
        if game_dir not in env.get("PATH", ""):
            env["PATH"] = game_dir + os.pathsep + env.get("PATH", "")
    except OSError:
        pass
    return env


def parse_game_binary(path: Path) -> dict[str, object]:
    """读取游戏 PE 与内嵌 ZIP，校验版本 / 架构 / 平台。

    Frontiers 的 ZIP 起点不能直接用 EOCD 算术值（PE 前缀 + 融合包有额外对齐），
    这里按「中央目录紧邻 EOCD 之前」定位起点，再回推出 prefix。
    """
    result: dict[str, object] = {
        "valid": False,
        "version": "未知",
        "architecture": "未知",
        "platform": "未知",
        "size": 0,
        "custom_script": False,
        "version_exact": False,
        "reason": "",
    }
    try:
        size = path.stat().st_size
        result["size"] = size
        with path.open("rb") as handle:
            head = handle.read(4096)
        if head[:2] != b"MZ":
            result["reason"] = "不是 Windows PE 可执行文件"
            return result
        pe_offset = struct.unpack_from("<I", head, 0x3C)[0]
        with path.open("rb") as handle:
            handle.seek(pe_offset)
            header = handle.read(24)
        machine = struct.unpack_from("<H", header, 4)[0]
        result["architecture"] = {0x8664: "x64", 0x14C: "x86"}.get(machine, hex(machine))
        result["architecture_ok"] = result["architecture"] == "x64"
        if not result["architecture_ok"]:
            result["reason"] = f"需要 x64，当前为 {result['architecture']}"
            return result

        # 定位 ZIP：EOCD -> 中央目录 -> 起点
        tail_len = min(size, 66 * 1024)
        with path.open("rb") as handle:
            handle.seek(size - tail_len)
            tail = handle.read(tail_len)
        idx = tail.rfind(b"PK\x05\x06")
        if idx < 0:
            result["reason"] = "未找到内嵌 ZIP（可能不是 LÖVE fused 构建）"
            return result
        eocd_abs = (size - tail_len) + idx
        (_sig, _d, _dc, _ed, ent_total, cd_size, cd_off, _cm) = struct.unpack(
            "<IHHHHIIH", tail[idx:idx + 22]
        )
        cd_abs = eocd_abs - cd_size
        with path.open("rb") as handle:
            handle.seek(cd_abs)
            if handle.read(4) != b"PK\x01\x02":
                result["reason"] = "ZIP 中央目录签名异常"
                return result
            zip_start = cd_abs - cd_off
            handle.seek(zip_start)
            payload = handle.read()

        archive = zipfile.ZipFile(io.BytesIO(payload))
        names = set(archive.namelist())
        version_blob = archive.read("version.lua") if "version.lua" in names else b""
        main_blob = archive.read("main.lua") if "main.lua" in names else b""
        archive.close()

        match = re.search((TARGET_VERSION_PREFIX + r"(\d+\.\d+\.\d+)").encode(), version_blob)
        if match:
            result["version"] = match.group(1).decode("ascii")
            result["version_exact"] = True
        elif b"kr2" in version_blob:
            # 改过 bundle id / 版本串前缀的非标准构建：退化匹配任意 x.y.z
            loose = re.search(rb"(\d+\.\d+\.\d+)", version_blob)
            if loose:
                result["version"] = loose.group(1).decode("ascii")
        result["platform"] = "Windows / Steam" if b"windows-steam" in version_blob else "Windows"
        result["bundle_ok"] = TARGET_BUNDLE_ID.encode() in version_blob
        # -custom_script 是整个方案的前提，必须确认
        result["custom_script"] = b"custom_script" in main_blob
        # 新版可能改 identity，存档目录随之改变；把候选带出去给存档定位用
        result["identities"] = love_identity_dirs(main_blob + version_blob)
        result["valid"] = bool(result["custom_script"]) and bool(result["version"] != "未知")
        if not result["custom_script"]:
            result["reason"] = "该版本未找到 -custom_script 入口，无法使用本修改器"
        elif result["version"] == "未知":
            result["reason"] = "无法读取游戏版本号"
    except Exception as exc:
        result["reason"] = str(exc)
    return result


def lua_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            value = 0.0
        # 不能用 ".6g"：它只有 6 位有效数字，会把 999999999 写成 1e+09（变成 10 亿）、
        # 123456789 写成 1.23457e+08（偏差 343 万），金币/宝石这类大数值会被直接改坏。
        # 整值按整数输出，其余用 repr 保证 round-trip 无损。
        if value == int(value) and abs(value) < 2 ** 53:
            return str(int(value))
        return repr(value)
    if value is None:
        return "nil"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def atomic_write_text(path: Path, content: str) -> None:
    """原子写文本。

    桥接每 0.2 秒读一次状态文件，os.replace 可能撞上句柄报 WinError 5，故重试后退化为直写。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(str(temp), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    for _ in range(8):
        try:
            os.replace(temp, path)
            return
        except OSError:
            time.sleep(0.05)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    try:
        temp.unlink()
    except OSError:
        pass


def parse_status(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


# =====================================================================
# 启动方式一：本地配置文件
# =====================================================================
# 允许用户在 %LOCALAPPDATA%\KingdomRushFrontiersTrainer\launch.json 里
# 预先写好游戏路径、Steam AppID 与附加启动参数。适合：
#   - 游戏装在 Steam 库扫描不到的非常规位置
#   - 需要固定 AppID 或额外启动参数
#   - 想在多台机器上复用同一份配置
LAUNCH_CONFIG_NAME = "launch.json"
# 允许的启动方式取值
LAUNCH_MODE_AUTO = "auto"        # 方式二：自动探测（默认）
LAUNCH_MODE_CONFIG = "config"    # 方式一：读本地配置文件
LAUNCH_MODE_CHOICES = (LAUNCH_MODE_AUTO, LAUNCH_MODE_CONFIG)


def launch_config_path() -> Path:
    return app_config_dir() / LAUNCH_CONFIG_NAME


def sample_launch_config() -> dict[str, object]:
    """生成一份配置模板，供「导出配置模板」使用。"""
    return {
        "mode": LAUNCH_MODE_CONFIG,
        "game_path": "",
        "steam_app_id": "",
        "extra_args": [],
        "use_steam_env": True,
        "save_dir": "",
        "note": "game_path 留空则回退到自动探测；填 Steam 库外的非常规路径时必填。"
                "save_dir 留空则自动识别存档目录；非 Steam 构建可显式指定。"
                "use_steam_env=false 时不注入 SteamAppId/SteamGameId。",
    }


def launch_config_save_dir() -> Path | None:
    """单独从 launch.json 取可选的 save_dir 覆盖，与 mode 是否生效无关。

    存档目录是「文件实际落在哪里」的客观事实，不该受启动方式影响；
    解析失败一律视为「未指定」，绝不因此阻断启动。
    """
    try:
        data = json.loads(launch_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw = str(data.get("save_dir", "")).strip().strip('"')
    return Path(raw).expanduser() if raw else None


def load_launch_config() -> tuple[dict[str, object] | None, str]:
    """读取本地启动配置。

    返回 (配置, 错误说明)。任一环节出问题都返回 (None, 原因)，
    调用方据此回退到自动探测，绝不因为配置问题让用户启动不了游戏。
    """
    path = launch_config_path()
    if not path.is_file():
        return None, "配置文件不存在"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"配置文件无法读取：{exc}"
    if not raw.strip():
        return None, "配置文件为空"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"配置文件不是合法 JSON：{exc}"
    if not isinstance(data, dict):
        return None, "配置文件顶层必须是 JSON 对象"

    mode = str(data.get("mode", LAUNCH_MODE_CONFIG)).strip().lower()
    if mode not in LAUNCH_MODE_CHOICES:
        return None, f"mode 只能是 {' 或 '.join(LAUNCH_MODE_CHOICES)}，收到：{mode}"

    extra = data.get("extra_args", [])
    if isinstance(extra, str):
        extra = [extra]
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        return None, "extra_args 必须是字符串数组"

    app_id_raw = data.get("steam_app_id", "")
    app_id = 0
    if str(app_id_raw).strip():
        try:
            app_id = int(str(app_id_raw).strip())
        except ValueError:
            return None, f"steam_app_id 必须是整数，收到：{app_id_raw!r}"

    game_raw = str(data.get("game_path", "")).strip().strip('"')
    game = Path(game_raw).expanduser() if game_raw else None
    save_raw = str(data.get("save_dir", "")).strip().strip('"')

    return {
        "mode": mode,
        "game": game,
        "steam_app_id": app_id,
        "extra_args": list(extra),
        "use_steam_env": coerce_bool(data.get("use_steam_env", True), True),
        # 区分「文件里显式写了」与「取默认值」：只有显式写了才覆盖界面开关
        "use_steam_env_set": "use_steam_env" in data,
        "save_dir": Path(save_raw).expanduser() if save_raw else None,
    }, ""


def build_launch_env(game_path: Path, app_id: int = 0, use_steam: bool = True) -> dict[str, str]:
    """构造子进程环境变量。

    app_id 显式给出时优先使用；否则按 Steam 库自动识别。
    use_steam=False 时不注入任何 Steam 变量（用于自定义启动场景）。
    """
    env = os.environ.copy()
    if use_steam:
        resolved = app_id if app_id > 0 else detect_steam_app_id(game_path)
        if resolved:
            env["SteamAppId"] = str(resolved)
            env["SteamGameId"] = str(resolved)
    try:
        game_dir = str(game_path.resolve().parent)
        if game_dir not in env.get("PATH", ""):
            env["PATH"] = game_dir + os.pathsep + env.get("PATH", "")
    except OSError:
        pass
    return env


def safe_int(value, fallback: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def coerce_bool(value, fallback: bool = True) -> bool:
    """宽松布尔解析：手写 JSON 里容易写成 "false" / "0" / "no"，不能当 True 用。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return fallback


def normalize_hotkey(text: str) -> str:
    parts = [p for p in re.split(r"\s*\+\s*", str(text or "").strip()) if p]
    mods: list[str] = []
    keys: list[str] = []
    for part in parts:
        low = part.lower()
        if low in ("ctrl", "control"):
            mods.append("Ctrl")
        elif low in ("alt",):
            mods.append("Alt")
        elif low in ("shift",):
            mods.append("Shift")
        elif low in ("win", "super", "meta"):
            mods.append("Win")
        else:
            keys.append(part)
    if not keys:
        return ""
    return "+".join(mods + keys)


MOD_VK = {"Ctrl": 0x11, "Alt": 0x12, "Shift": 0x10, "Win": 0x5B}


def hotkey_vk(text: str) -> tuple[set[str], int]:
    """把 'Ctrl+Alt+1' 解析成 (修饰键集合, 主键 VK)。"""
    norm = normalize_hotkey(text)
    if not norm:
        return set(), 0
    parts = norm.split("+")
    mods = {p for p in parts if p in MOD_VK}
    key = parts[-1]
    special = {
        "Space": 0x20, "Enter": 0x0D, "Esc": 0x1B, "Tab": 0x09, "Backspace": 0x08,
        "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
        "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    }
    if key in special:
        return mods, special[key]
    if key.startswith("F") and key[1:].isdigit():
        num = int(key[1:])
        if 1 <= num <= 12:
            return mods, 0x70 + num - 1
    if len(key) == 1:
        return mods, ord(key.upper())
    return mods, 0


# 缓存 user32 句柄：HotkeyPoller 每秒要调用上千次 GetAsyncKeyState，
# 每次新建 WinDLL 会重复解析 DLL 并覆盖 use_last_error 的 TLS 状态。
_USER32 = None


def _user32():
    global _USER32
    if _USER32 is None:
        _USER32 = ctypes.WinDLL("user32", use_last_error=True)
    return _USER32


class HotkeyPoller(threading.Thread):
    """后台轮询全局快捷键。

    注意：本线程只负责「检测」，不直接调用回调。
    回调会操作 Tk 变量与弹出模态框，必须经 root.after 投递回主线程执行，
    否则 Tk 会抛 "main thread is not in main loop"（实测可复现）。
    """

    def __init__(self, root: tk.Tk, callback, mapping: dict[str, str]):
        super().__init__(daemon=True)
        self._root = root
        self.callback = callback
        self.mapping = mapping
        self._stop = threading.Event()
        self._down: set[tuple] = set()

    def stop(self) -> None:
        self._stop.set()

    def _pressed(self, vk: int) -> bool:
        if os.name != "nt":
            return False
        return bool(_user32().GetAsyncKeyState(vk) & 0x8000)

    def _invoke(self, action: str) -> None:
        """在主线程执行回调（由 root.after 调用）。"""
        try:
            self.callback(action)
        except Exception:
            log_message("快捷键处理异常：\n" + traceback.format_exc())

    def _dispatch(self, action: str) -> None:
        """把回调投递到主线程。窗口已销毁时静默丢弃。"""
        try:
            self._root.after(0, lambda a=action: self._invoke(a))
        except Exception:
            # TclError：主循环已退出（窗口关闭），忽略即可
            pass

    def run(self) -> None:
        while not self._stop.is_set():
            if os.name == "nt":
                for action, combo in list(self.mapping.items()):
                    mods, key_vk = hotkey_vk(combo)
                    if not key_vk:
                        continue
                    if not all(self._pressed(MOD_VK[m]) for m in mods):
                        continue
                    token = (action, tuple(sorted(mods)), key_vk)
                    if self._pressed(key_vk):
                        if token not in self._down:
                            self._down.add(token)
                            self._dispatch(action)
                    else:
                        self._down.discard(token)
            time.sleep(0.05)


# =====================================================================
# 主界面
# =====================================================================

class TrainerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.state: dict[str, object] = {
            "active": False,
            "revision": 0,
            "heartbeat": 0,
            "speed_enabled": True,
            "speed": 1.0,
            "paused": False,
            "gold_enabled": False,
            "gold_value": 99999,
            "gems_enabled": False,
            "gems_value": 99999,
            "lives_enabled": False,
            "lives_value": 20,
            "kill_gold_enabled": False,
            "kill_gold_multiplier": 2.0,
            "invincible": False,
            "hero_no_cooldown": False,
            "damage_enabled": False,
            "damage_multiplier": 3.0,
            "tower_speed_enabled": False,
            "tower_speed_multiplier": 3.0,
            "tower_range_enabled": False,
            "tower_range_multiplier": 2.0,
            "barrack_enabled": False,
            "barrack_soldiers": 3,
            "barrack_respawn_scale": 1.0,
            "unlock_levels": False,
            "three_stars": False,
            # 只读模式：不写游戏存档（存档由第三方工具管理 / 目录不可写时使用）
            "read_only": False,
            "cmd_skip_wave": 0,
            "cmd_force_wave": 0,
            "cmd_win": 0,
        }
        self.toggles: dict[str, tk.BooleanVar] = {}
        self.values: dict[str, tk.DoubleVar] = {}
        # 每个开关对应的 Checkbutton 控件（只读模式需要禁用）
        self._toggle_widgets: dict[str, ttk.Checkbutton] = {}
        self._slot_widgets: dict[str, ttk.Checkbutton] = {}
        # 只读模式切换前的开关原值（None = 当前未处于备份态）
        self._slot_backup: dict[str, bool | None] = {k: None for k in self.SLOT_FEATURES}
        self.hotkeys: dict[str, str] = dict(DEFAULT_HOTKEYS)

        # 配置必须先读：存档目录覆盖与两个兼容性开关都来自配置文件。
        # 先定位游戏再定存档目录——新版换了 identity 时必须按游戏 exe 里读出的身份选。
        self.load_config()
        self.game_path = locate_default_game()
        self.save_dir = choose_save_dir(getattr(self, "_saved_save_dir", None), self.game_path)
        self.pid: int | None = None
        self.prepared = False
        self.backup_done = False
        self.connected = False
        self.version_cache: dict[str, object] | None = None
        self.version_cache_key = None
        self.last_status: dict[str, str] = {}
        self.suppress_events = False
        self.last_heartbeat = 0.0

        self.game_path_var = tk.StringVar(value=str(self.game_path))
        self.save_dir_var = tk.StringVar(value=str(self.save_dir))
        self.connection_var = tk.StringVar(value="未连接")
        self.bridge_var = tk.StringVar(value="桥接状态：未就绪")
        self.backup_label = tk.StringVar(value="尚未备份存档")
        self.status_text: tk.Text | None = None
        # 兼容性开关：只决定「是否尝试启动」，不写入游戏状态文件
        self.force_launch_var = tk.BooleanVar(value=bool(getattr(self, "_saved_force_launch", False)))
        self.use_steam_env_var = tk.BooleanVar(
            value=bool(getattr(self, "_saved_use_steam_env", True)))

        self.configure_style()
        self.build_ui()

        self.poller = HotkeyPoller(root, self.on_hotkey, self.hotkeys)
        self.poller.start()
        atexit.register(self.shutdown)

        self.root.after(300, self._poll_loop)
        self.root.after(200, self.check_version)
        self.log(f"存档目录：{self.save_dir}")
        log_message(f"启动 {APP_NAME} {APP_VERSION}")

    # ---------- 样式 ----------
    def configure_style(self) -> None:
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("740x700")
        self.root.minsize(700, 620)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f5f6f8")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("TLabel", background="#f5f6f8", foreground="#1f2430", font=("Microsoft YaHei UI", 9))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#111827", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Muted.TLabel", background="#f5f6f8", foreground="#6b7280", font=("Microsoft YaHei UI", 8))
        style.configure("Good.TLabel", background="#f5f6f8", foreground="#15803d", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Bad.TLabel", background="#f5f6f8", foreground="#b91c1c", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Warn.TLabel", background="#f5f6f8", foreground="#b45309", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Status.TLabel", background="#f5f6f8", foreground="#1d4ed8", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TCheckbutton", background="#f5f6f8", foreground="#1f2430", font=("Microsoft YaHei UI", 9))
        style.configure("Hotkey.TButton", font=("Microsoft YaHei UI", 8))

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        if self.status_text is not None:
            self.status_text.insert("end", f"[{stamp}] {message}\n")
            self.status_text.see("end")
        log_message(message)

    # ---------- 界面 ----------
    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # 顶部：游戏路径 + 版本
        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text="游戏路径").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.game_path_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="浏览", command=self.browse_game).pack(side="left")
        self.launch_button = ttk.Button(top, text="启动 / 连接游戏", command=self.launch_game)
        self.launch_button.pack(side="left", padx=(6, 0))

        # 启动方式：与「游戏路径」并列，两个入口各自独立可用
        mode_row = ttk.Frame(outer)
        mode_row.pack(fill="x", pady=(6, 0))
        ttk.Label(mode_row, text="启动方式").pack(side="left")
        self.launch_mode_var = tk.StringVar(value=self.detect_launch_mode())
        auto_radio = ttk.Radiobutton(
            mode_row, text="自动探测（方式二）", variable=self.launch_mode_var,
            value=LAUNCH_MODE_AUTO, command=self.on_launch_mode_changed)
        auto_radio.pack(side="left", padx=(10, 0))
        config_radio = ttk.Radiobutton(
            mode_row, text="本地配置（方式一）", variable=self.launch_mode_var,
            value=LAUNCH_MODE_CONFIG, command=self.on_launch_mode_changed)
        config_radio.pack(side="left", padx=(10, 0))
        ttk.Button(mode_row, text="打开配置目录", style="Hotkey.TButton",
                   command=self.open_launch_config).pack(side="left", padx=(10, 0))
        ttk.Button(mode_row, text="导出配置模板", style="Hotkey.TButton",
                   command=self.export_launch_template).pack(side="left", padx=(4, 0))
        self.mode_hint = ttk.Label(outer, text="", style="Muted.TLabel")
        self.mode_hint.pack(anchor="w", pady=(2, 0))
        self.refresh_mode_hint()

        self.version_label = ttk.Label(outer, text="版本检查：检查中…")
        self.version_label.pack(anchor="w", pady=(4, 0))

        # 兼容性开关：非 Steam / 非常规构建的两个逃生口
        compat = ttk.Frame(outer)
        compat.pack(fill="x", pady=(3, 0))
        ttk.Checkbutton(compat, text="忽略兼容性检查（非 Steam / 非常规构建）",
                        variable=self.force_launch_var,
                        command=self.on_compat_changed).pack(side="left")
        ttk.Checkbutton(compat, text="注入 Steam 运行环境变量",
                        variable=self.use_steam_env_var,
                        command=self.on_compat_changed).pack(side="left", padx=(18, 0))

        # 存档目录：桥接文件落点，非标准 identity 构建需要能看到并重检
        save_row = ttk.Frame(outer)
        save_row.pack(fill="x", pady=(3, 0))
        ttk.Label(save_row, text="存档目录").pack(side="left")
        ttk.Entry(save_row, textvariable=self.save_dir_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(save_row, text="重新检测", style="Hotkey.TButton",
                   command=self.redetect_save_dir).pack(side="left")
        ttk.Button(save_row, text="打开", style="Hotkey.TButton",
                   command=self.open_save_dir).pack(side="left", padx=(4, 0))

        conn = ttk.Frame(outer)
        conn.pack(fill="x", pady=(4, 8))
        ttk.Label(conn, textvariable=self.connection_var, style="Status.TLabel").pack(side="left")
        ttk.Label(conn, textvariable=self.bridge_var, style="Muted.TLabel").pack(side="left", padx=(16, 0))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        self._build_speed_tab(notebook)
        self._build_resource_tab(notebook)
        self._build_combat_tab(notebook)
        self._build_progress_tab(notebook)
        self._build_log_tab(outer)

    def _card(self, parent, title: str):
        card = ttk.Frame(parent, style="Card.TFrame", padding=10)
        card.pack(fill="x", pady=(0, 8))
        ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 6))
        return card

    def add_toggle(self, parent, row: int, key: str, label: str, help_text: str = "") -> tk.BooleanVar:
        var = tk.BooleanVar(value=bool(self.state.get(key)))
        self.toggles[key] = var
        box = ttk.Checkbutton(parent, text=label, variable=var, command=self.on_toggle)
        box.grid(row=row, column=0, sticky="w", pady=2)
        self._toggle_widgets[key] = box
        if key in self.SLOT_FEATURES:
            self._slot_widgets[key] = box
        if help_text:
            ttk.Label(parent, text=help_text, style="Muted.TLabel").grid(row=row, column=1, sticky="w", padx=(12, 0))
        return var

    def add_toggle_value(self, parent, row: int, key: str, value_key: str, label: str,
                         minimum: float, maximum: float, default: float, help_text: str = ""):
        var = self.add_toggle(parent, row, key, label, help_text)
        num = tk.DoubleVar(value=float(self.state.get(value_key, default)))
        self.values[value_key] = num
        spin = ttk.Spinbox(parent, from_=minimum, to=maximum, increment=0.1, width=9,
                           textvariable=num, command=self.on_controls_changed)
        spin.grid(row=row, column=2, sticky="e", padx=(12, 0))
        spin.bind("<KeyRelease>", lambda _e: self.on_controls_changed())
        spin.bind("<FocusOut>", lambda _e: self.on_controls_changed())
        return var

    def _build_speed_tab(self, notebook) -> None:
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="速度")
        card = self._card(tab, "游戏速度")
        presets = ttk.Frame(card)
        presets.pack(fill="x", pady=(0, 6))
        for value in (1, 2, 3, 5, 8):
            ttk.Button(presets, text=f"{value}x", width=6,
                       command=lambda v=value: self.set_speed(v)).pack(side="left", padx=(0, 6))
        ttk.Button(presets, text="暂停 / 恢复", command=self.toggle_pause).pack(side="left", padx=(6, 0))

        row = ttk.Frame(card)
        row.pack(fill="x")
        self.add_toggle(row, 0, "speed_enabled", "启用速度调节", "默认唯一启用项")
        speed_var = tk.DoubleVar(value=float(self.state.get("speed", 1.0)))
        self.values["speed"] = speed_var
        ttk.Spinbox(row, from_=1.0, to=16.0, increment=0.5, width=9,
                    textvariable=speed_var, command=self.on_controls_changed).grid(
            row=0, column=2, sticky="e", padx=(12, 0))
        row.grid_columnconfigure(1, weight=1)

        ttk.Label(tab, text="提示：8x 以上可能掉帧，日常建议 2x–5x。小数倍率通过相邻整数帧交替实现。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(4, 0))

    def _build_resource_tab(self, notebook) -> None:
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="资源 / 生存")

        card = self._card(tab, "资源")
        grid = ttk.Frame(card)
        grid.pack(fill="x")
        grid.grid_columnconfigure(1, weight=1)
        self.add_toggle_value(grid, 0, "gold_enabled", "gold_value", "锁定金币",
                              0, 999999999, 99999, "0 – 999,999,999")
        self.add_toggle_value(grid, 1, "gems_enabled", "gems_value", "锁定宝石",
                              0, 999999999, 99999, "首次启用自动备份存档")
        self.add_toggle_value(grid, 2, "kill_gold_enabled", "kill_gold_multiplier", "杀敌金币倍率",
                              0.1, 1000, 2.0, "0.1 – 1000 倍，1.0 = 无加成")
        quick = ttk.Frame(grid)
        quick.grid(row=2, column=2, sticky="e", padx=(12, 0))
        for mult in (2, 3, 5, 10, 20):
            ttk.Button(quick, text=f"{mult}x", width=5,
                       command=lambda m=mult: self.set_kill_gold(m)).pack(side="left", padx=(2, 0))

        card2 = self._card(tab, "生存")
        grid2 = ttk.Frame(card2)
        grid2.pack(fill="x")
        grid2.grid_columnconfigure(1, weight=1)
        self.add_toggle_value(grid2, 0, "lives_enabled", "lives_value", "锁定基地生命",
                              1, 9999, 20, "1 – 9,999")
        self.add_toggle(grid2, 1, "invincible", "英雄与友军无敌")

    def _build_combat_tab(self, notebook) -> None:
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="战斗")

        card = self._card(tab, "友军增强")
        grid = ttk.Frame(card)
        grid.pack(fill="x")
        grid.grid_columnconfigure(1, weight=1)
        self.add_toggle(grid, 0, "hero_no_cooldown", "英雄技能无冷却")
        self.add_toggle_value(grid, 1, "damage_enabled", "damage_multiplier", "友军伤害倍率",
                              0.1, 100, 3.0, "0.1 – 100 倍")

        card2 = self._card(tab, "防御塔")
        grid2 = ttk.Frame(card2)
        grid2.pack(fill="x")
        grid2.grid_columnconfigure(1, weight=1)
        self.add_toggle_value(grid2, 0, "tower_speed_enabled", "tower_speed_multiplier", "攻速倍率",
                              0.1, 50, 3.0, "0.1 – 50 倍")
        self.add_toggle_value(grid2, 1, "tower_range_enabled", "tower_range_multiplier", "射程倍率",
                              0.1, 20, 2.0, "0.1 – 20 倍")

        card3 = self._card(tab, "兵营（防御塔）")
        grid3 = ttk.Frame(card3)
        grid3.pack(fill="x")
        grid3.grid_columnconfigure(1, weight=1)
        self.add_toggle_value(grid3, 0, "barrack_enabled", "barrack_soldiers", "出怪人数",
                              1, 10, 3, "原版 3")
        self.add_toggle_value(grid3, 1, "barrack_enabled", "barrack_respawn_scale", "刷新时间倍率",
                              0.1, 5.0, 1.0, "1.0 = 原版")
        ttk.Label(card3, text="注：出怪人数与刷新时间共用「兵营增强」开关。",
                  style="Muted.TLabel").pack(anchor="w")

    def _build_progress_tab(self, notebook) -> None:
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="进度 / 波次")

        card = self._card(tab, "进度（临时，关闭开关或退出即恢复）")
        grid = ttk.Frame(card)
        grid.pack(fill="x")
        grid.grid_columnconfigure(1, weight=1)
        self.add_toggle(grid, 0, "unlock_levels", "临时解锁全部关卡", f"1 – {FALLBACK_LAST_LEVEL}")
        self.add_toggle(grid, 1, "three_stars", "临时全关三星")
        self.add_toggle(grid, 2, "gems_enabled", "锁定宝石", "首次启用自动备份存档")
        self.slot_hint = ttk.Label(card, text="启用前会自动备份存档。", style="Muted.TLabel")
        self.slot_hint.pack(anchor="w")

        # 只读模式：完全不动游戏存档
        ro = self._card(tab, "只读模式")
        ro_grid = ttk.Frame(ro)
        ro_grid.pack(fill="x")
        ro_grid.grid_columnconfigure(1, weight=1)
        self.add_toggle(ro_grid, 0, "read_only", "只读模式（不写游戏存档）",
                        "开启后宝石 / 解锁 / 三星自动禁用")
        self.read_only_hint = ttk.Label(
            ro, text="", style="Muted.TLabel", wraplength=640, justify="left")
        self.read_only_hint.pack(anchor="w", pady=(4, 0))
        self.refresh_read_only_ui()

        card2 = self._card(tab, "波次")
        grid2 = ttk.Frame(card2)
        grid2.pack(fill="x")
        grid2.grid_columnconfigure(1, weight=1)
        ttk.Button(grid2, text="提前呼叫下一波", command=self.cmd_skip_wave).grid(row=0, column=0, sticky="w", pady=3)
        ttk.Button(grid2, text="强制清场并推进", command=self.cmd_force_wave).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Button(grid2, text="立即通关", command=self.cmd_instant_win).grid(row=2, column=0, sticky="w", pady=3)
        ttk.Label(grid2, text="推荐优先用「提前呼叫下一波」；强制清场可能跳过 Boss 脚本事件。\n"
                             "「立即通关」会清空敌人并推进波次，由游戏自身判定胜利，"
                             "因此星级与下一关解锁会正常写入存档。",
                  style="Muted.TLabel", wraplength=520, justify="left").grid(
            row=0, column=1, rowspan=3, sticky="w", padx=(12, 0))

        card3 = self._card(tab, "存档")
        ttk.Button(card3, text="手动备份存档", command=lambda: self.backup_saves("manual")).pack(anchor="w")
        ttk.Label(card3, textvariable=self.backup_label, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

    def _build_log_tab(self, outer) -> None:
        card = self._card(outer, "运行日志")
        holder = ttk.Frame(card)
        holder.pack(fill="both", expand=True)
        self.status_text = tk.Text(holder, height=6, font=("Consolas", 9), wrap="none",
                                   bg="#ffffff", fg="#1f2430", relief="solid", borderwidth=1)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.status_text.yview)
        self.status_text.configure(yscrollcommand=scroll.set)
        self.status_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        hot = ttk.Frame(card)
        hot.pack(fill="x", pady=(6, 0))
        ttk.Label(hot, text="快捷键：", style="Muted.TLabel").pack(side="left")
        ttk.Button(hot, text="查看 / 修改", style="Hotkey.TButton", command=self.show_hotkeys).pack(side="left")
        ttk.Label(hot, text=f"  {APP_CREDITS}", style="Muted.TLabel").pack(side="right")

    # ---------- 配置 ----------
    def config_file(self) -> Path:
        return app_config_dir() / "config.json"

    def load_config(self) -> None:
        try:
            data = json.loads(self.config_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data.get("state"), dict):
            for key, value in data["state"].items():
                if key in self.state:
                    self.state[key] = value
        if isinstance(data.get("hotkeys"), dict):
            for key, value in data["hotkeys"].items():
                if key in DEFAULT_HOTKEYS and isinstance(value, str):
                    self.hotkeys[key] = normalize_hotkey(value) or DEFAULT_HOTKEYS[key]
        # 一次性命令的 nonce 必须归零，绝不能跨会话恢复：
        # 桥接侧 last_* 在新游戏进程里从 0 开始，若这里把上次点击的计数读回来，
        # 会被当成新请求，玩家一进关卡就自动「提前呼叫下一波 / 立即通关」。
        for key in COMMAND_NONCES:
            self.state[key] = 0
        # 恢复启动方式；缺省时依据 launch.json 是否存在自动判断
        self._saved_launch_mode = data.get("launch_mode")
        if self._saved_launch_mode not in LAUNCH_MODE_CHOICES:
            self._saved_launch_mode = None
        # 两个兼容性开关
        self._saved_force_launch = coerce_bool(data.get("force_launch"), False)
        self._saved_use_steam_env = coerce_bool(data.get("use_steam_env"), True)
        # 存档目录覆盖只认 launch.json：把自己的探测结果回写成"硬绑定"会让目录一旦变动就失效
        self._saved_save_dir = launch_config_save_dir()

    def save_config(self) -> None:
        try:
            path = self.config_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = self.launch_mode_var.get() if hasattr(self, "launch_mode_var") else LAUNCH_MODE_AUTO
            payload: dict[str, object] = {
                "state": self.state,
                "hotkeys": self.hotkeys,
                "launch_mode": mode,
            }
            if hasattr(self, "force_launch_var"):
                payload["force_launch"] = bool(self.force_launch_var.get())
            if hasattr(self, "use_steam_env_var"):
                payload["use_steam_env"] = bool(self.use_steam_env_var.get())
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError:
            pass

    # ---------- 状态写入 ----------
    def collect_state(self) -> None:
        if self.suppress_events:
            return
        for key, var in self.toggles.items():
            self.state[key] = bool(var.get())
        for key, var in self.values.items():
            self.state[key] = var.get()

    @staticmethod
    def clamp_float(value, minimum: float, maximum: float, fallback: float) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return fallback
        if not math.isfinite(result):
            return fallback
        return max(minimum, min(maximum, result))

    @staticmethod
    def clamp_int(value, minimum: int, maximum: int, fallback: int) -> int:
        try:
            return max(minimum, min(maximum, int(round(float(value)))))
        except (TypeError, ValueError):
            return fallback

    def state_text(self) -> str:
        """状态文件必须是 `return { ... }`。

        桥接用 love.filesystem.load() 加载后 pcall，裸赋值语句的 chunk 返回 nil，
        只有表格构造才会把配置表交回给桥接。

        heartbeat 由这里统一覆盖写入：state 字典里也存着同名字段，若两处都输出，
        生成的文件会出现重复键。Lua 构造式里重复键不报错但会静默取最后一个，
        且新版 LuaJIT 对表构造的重复键检测更严格——统一在此处落笔，避免歧义。
        """
        lines = []
        now = int(time.time())
        for key in sorted(self.state):
            if key == "heartbeat":
                continue
            lines.append(f"    {key} = {lua_literal(self.state[key])},")
        lines.append(f"    heartbeat = {now},")
        self.state["heartbeat"] = now
        return "return {\n" + "\n".join(lines) + "\n}\n"

    # 所有可写数值参数的合法区间。与 Lua 侧 clamp_number 的范围保持一致，
    # 避免「GUI 越界值 / 手改状态文件」把非法值写进桥接。
    CLAMP_RULES: dict[str, tuple[float, float]] = {
        "speed": (1.0, 16.0),
        "gold_value": (0.0, 999999999.0),
        "gems_value": (0.0, 999999999.0),
        "lives_value": (1.0, 9999.0),
        "kill_gold_multiplier": (0.1, 1000.0),
        "damage_multiplier": (0.1, 100.0),
        "tower_speed_multiplier": (0.1, 50.0),
        "tower_range_multiplier": (0.1, 20.0),
        "barrack_soldiers": (1.0, 10.0),
        "barrack_respawn_scale": (0.1, 5.0),
    }

    def write_state(self, force: bool = False) -> None:
        try:
            self.collect_state()
            for key, (low, high) in self.CLAMP_RULES.items():
                if key in self.state:
                    self.state[key] = self.clamp_float(self.state[key], low, high, low)
            # 只读模式：存档类三项绝不外发（快捷键等旁路也要拦住）
            if self.state.get("read_only"):
                for key in self.SLOT_FEATURES:
                    self.state[key] = False
            self.state["revision"] = int(self.state.get("revision", 0)) + 1
            self.state["heartbeat"] = int(time.time())
            payload = self.state_text()
            # 桥接脚本可能在存档目录，也可能在游戏目录兜底；两处都写，
            # 否则身份判定一旦偏差，功能开关全部哑火但界面毫无察觉。
            errors: list[str] = []
            for target in self.state_targets():
                try:
                    atomic_write_text(target, payload)
                except OSError as exc:
                    errors.append(f"{target}：{exc}")
            self.last_heartbeat = time.time()
            if errors and not force:
                self.log("部分状态写入位置失败：" + "；".join(errors))
        except OSError as exc:
            self.log(f"写入控制状态失败：{exc}")

    def state_targets(self) -> list[Path]:
        """状态文件的写入位置，与桥接脚本的落点保持一致。"""
        targets = [self.save_dir / STATE_FILE]
        try:
            game_dir = Path(self.game_path_var.get().strip()).resolve().parent
            game_target = game_dir / STATE_FILE
            if game_target not in targets:
                targets.append(game_target)
        except (OSError, ValueError):
            pass
        return targets

    # ---------- 桥接准备 ----------
    def bridge_targets(self) -> list[Path]:
        """桥接脚本需要落到哪些目录。

        首选是存档目录——LÖVE 把它算进 package.path，require 能找到。
        但新版游戏若改了 identity、或存档目录尚未被创建，存档目录这条路会失效，
        此时游戏目录本身也在 package.path 里（require 的候选里有 <游戏目录>\\krft_bridge.lua），
        因此额外写一份到游戏目录兜底。

        游戏目录通常需要写权限（Program Files 下会失败），失败只记日志不中断流程。
        """
        targets = [self.save_dir / BRIDGE_FILE]
        try:
            game_dir = Path(self.game_path_var.get().strip()).resolve().parent
            game_target = game_dir / BRIDGE_FILE
            if game_target not in targets:
                targets.append(game_target)
        except (OSError, ValueError):
            pass
        return targets

    def prepare_bridge(self) -> bool:
        source = resource_path(BRIDGE_FILE)
        if not source.is_file():
            messagebox.showerror(APP_NAME, f"缺少桥接脚本：\n{source}")
            return False
        try:
            source_data = source.read_bytes()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法读取桥接脚本：\n{exc}")
            return False

        written = 0
        errors: list[str] = []
        for target in self.bridge_targets():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() != source_data:
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-bridge")
                    backup_dir = target.parent / "KRFTBackups" / stamp
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup_dir / target.name)
                    self.log(f"已备份旧桥接脚本：{backup_dir}")
                target.write_bytes(source_data)
                self.log(f"已写入桥接脚本：{target}")
                written += 1
            except OSError as exc:
                # 游戏目录可能无写权限；存档目录写成功就够了，不因此中断
                errors.append(f"{target}：{exc}")

        if written == 0:
            messagebox.showerror(
                APP_NAME,
                "无法准备桥接文件（所有候选目录都写入失败）：\n\n" + "\n".join(errors))
            return False
        for line in errors:
            self.log(f"桥接脚本跳过该位置（不影响使用）：{line}")

        self.prepared = True
        self.state["active"] = True
        self.write_state(force=True)
        return True

    def backup_saves(self, reason: str) -> Path | None:
        try:
            files = sorted(self.save_dir.glob("slot_*.lua"))[:10]
            if not files:
                self.log("未找到 slot_*.lua；当前可能尚未创建存档槽。")
                return None
            stamp = datetime.now().strftime(f"%Y%m%d-%H%M%S-{reason}")
            backup_dir = self.save_dir / "KRFTBackups" / stamp
            # 目录名只精确到秒，同一秒内再备份一次（例如连点「手动备份存档」）
            # 会让 mkdir 抛 FileExistsError，被当成「备份失败并取消修改」，属于误报。
            # 冲突时追加序号，保证每次备份都能成功。
            suffix = 0
            while True:
                candidate = backup_dir if suffix == 0 else backup_dir.with_name(f"{stamp}-{suffix}")
                try:
                    candidate.mkdir(parents=True, exist_ok=False)
                    backup_dir = candidate
                    break
                except FileExistsError:
                    suffix += 1
                    if suffix > 99:
                        raise
            for source in files:
                shutil.copy2(source, backup_dir / source.name)
            atomic_write_text(backup_dir / "backup_manifest.json", json.dumps({
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
                "files": [str(p) for p in files],
                "trainer_version": APP_VERSION,
            }, ensure_ascii=False, indent=2))
            self.backup_done = True
            self.log(f"存档备份完成：{backup_dir}")
            self.backup_label.set(f"最近备份：{backup_dir}")
            return backup_dir
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"存档备份失败：\n{exc}")
            return None

    SLOT_FEATURES = ("gems_enabled", "unlock_levels", "three_stars")

    def refresh_read_only_ui(self) -> None:
        """只读模式下禁用三个存档类开关并给出说明。

        原值只在**首次进入只读**时记录一次（_slot_backup 为空哨兵 None），
        之后反复切换不会把已清零的值当成"用户原意"覆盖掉。
        """
        ro = bool(self.state.get("read_only"))
        for key in self.SLOT_FEATURES:
            var = self.toggles.get(key)
            if var is None:
                continue
            if ro:
                # 仅在首次进入时备份；已备份过就保留最初的用户设置
                if self._slot_backup.get(key) is None:
                    self._slot_backup[key] = bool(var.get())
                var.set(False)
            else:
                saved = self._slot_backup.get(key)
                var.set(bool(saved) if saved is not None else False)
                # 恢复后清掉备份，下次进入只读时重新记录
                self._slot_backup[key] = None
            self._slot_widgets[key].configure(state="disabled" if ro else "normal")
        if ro:
            self.read_only_hint.configure(
                text="已启用：修改器不会读写游戏存档（slot_*..lua）。\n"
                     "宝石 / 解锁关卡 / 全关三星已禁用；"
                     "速度、伤害、射程、兵营、波次等运行时功能不受影响。",
                style="Good.TLabel")
            self.slot_hint.configure(text="只读模式下不可用。", style="Bad.TLabel")
        else:
            self.read_only_hint.configure(
                text="关闭时若存档目录不可写或存档由其他工具管理，开启此模式可避免冲突。",
                style="Muted.TLabel")
            self.slot_hint.configure(text="启用前会自动备份存档。", style="Muted.TLabel")

    def ensure_backup(self) -> bool:
        """存档类功能首次启用前自动备份。只读模式下无需备份。"""
        if bool(self.state.get("read_only")):
            return True
        if self.backup_done:
            return True
        return self.backup_saves("auto") is not None

    # ---------- 游戏操作 ----------
    @guarded
    def browse_game(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择游戏主程序（Kingdom Rush Frontiers.exe）",
            filetypes=[("Kingdom Rush Frontiers", TARGET_EXE_NAME), ("可执行文件", "*.exe")])
        if selected:
            self.game_path = Path(selected)
            self.game_path_var.set(str(self.game_path))
            self.save_config()
            self.check_version()

    @guarded
    def check_version(self) -> dict[str, object]:
        path_text = self.game_path_var.get().strip()
        self.game_path = Path(path_text).expanduser() if path_text else locate_default_game()
        try:
            stat = self.game_path.stat()
            cache_key = (str(self.game_path.resolve()), stat.st_size, stat.st_mtime_ns)
        except OSError:
            cache_key = (str(self.game_path), 0, 0)
        if cache_key == self.version_cache_key and self.version_cache is not None:
            result = self.version_cache
        else:
            result = parse_game_binary(self.game_path)
            self.version_cache_key = cache_key
            self.version_cache = result
        if result["valid"]:
            extra = "" if result.get("version_exact") else " · 版本串非标准（非 Steam 构建）"
            self.version_label.configure(
                text=f"版本检查：{result['version']} · {result['platform']} · {result['architecture']}"
                     f" · -custom_script 可用{extra}",
                style="Good.TLabel")
        else:
            exists = self.game_path.is_file() if self.game_path else False
            forced = bool(self.force_launch_var.get()) if hasattr(self, "force_launch_var") else False
            if not exists:
                note = "版本检查：未找到游戏，请点击「浏览」选择游戏主程序"
            elif forced:
                note = f"版本检查：未通过（{result['reason']}）· 已勾选忽略，将直接尝试"
            else:
                note = f"版本检查：不兼容或无法识别（{result['reason']}）"
            self.version_label.configure(
                text=note, style="Warn.TLabel" if (exists and forced) else "Bad.TLabel")
        return result

    @guarded
    def on_compat_changed(self) -> None:
        """兼容性开关变化：立刻刷新版本显示与启动方式说明。"""
        self.check_version()
        self.refresh_mode_hint()
        self.save_config()
        self.log("兼容性设置已更新：忽略检查={}，注入 Steam 环境={}".format(
            "是" if self.force_launch_var.get() else "否",
            "是" if self.use_steam_env_var.get() else "否"))

    @guarded
    def redetect_save_dir(self) -> None:
        """重新扫描存档目录。

        launch.json 里的 save_dir 优先级最高；没有覆盖时才按存档特征自动选择。
        """
        override = launch_config_save_dir()
        previous = self.save_dir
        # 重新检测时一并带上游戏路径：换了游戏版本（可能换 identity）后存档目录会变
        game_path = self.game_path_var.get().strip() if self.game_path_var else None
        self.save_dir = choose_save_dir(override, game_path)
        self.save_dir_var.set(str(self.save_dir))
        self.save_config()
        if override:
            self.log(f"已按 launch.json 的 save_dir 指定存档目录：{self.save_dir}")
        if str(previous) == str(self.save_dir):
            self.log(f"存档目录保持不变：{self.save_dir}")
        else:
            self.log(f"存档目录已更新：{previous} -> {self.save_dir}")
            if self.prepared:
                self.prepare_bridge()

    @guarded
    def open_save_dir(self) -> None:
        try:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(self.save_dir))  # noqa: S606  (Windows 打开资源管理器)
        except (OSError, AttributeError) as exc:
            messagebox.showinfo(APP_NAME, f"存档目录：\n{self.save_dir}\n\n（无法自动打开：{exc}）")

    def detect_launch_mode(self) -> str:
        """默认选中的启动方式：优先用户上次的选择，其次看 launch.json 是否存在。"""
        saved = getattr(self, "_saved_launch_mode", None)
        if saved in LAUNCH_MODE_CHOICES:
            return str(saved)
        config, _ = load_launch_config()
        if config and config.get("mode") in LAUNCH_MODE_CHOICES:
            return str(config["mode"])
        return LAUNCH_MODE_AUTO

    def refresh_mode_hint(self) -> None:
        """刷新启动方式说明标签。"""
        path = launch_config_path()
        mode = self.launch_mode_var.get()
        inject_ui = bool(self.use_steam_env_var.get()) if hasattr(self, "use_steam_env_var") else True
        if mode == LAUNCH_MODE_CONFIG:
            if path.is_file():
                config, err = load_launch_config()
                if config:
                    game = config["game"]
                    where = str(game) if game else "（未指定，将回退自动探测）"
                    inject = (bool(config["use_steam_env"])
                              if config.get("use_steam_env_set") else inject_ui)
                    self.mode_hint.configure(
                        text=f"方式一：将读取 {path.name} —— 目标 {where}；"
                             f"附加参数 {config['extra_args'] or '无'}；"
                             f"注入 Steam 环境 {'是' if inject else '否'}",
                        style="Good.TLabel")
                else:
                    self.mode_hint.configure(
                        text=f"方式一：配置无效（{err}），启动时将自动回退到方式二", style="Bad.TLabel")
            else:
                self.mode_hint.configure(
                    text=f"方式一：未找到 {path.name}，可点「导出配置模板」生成；启动时将自动回退到方式二",
                    style="Bad.TLabel")
        else:
            self.mode_hint.configure(
                text="方式二：使用上方「游戏路径」；Steam 运行环境变量"
                     f"（AppID 458710）注入：{'是' if inject_ui else '否'}",
                style="Muted.TLabel")

    @guarded
    def on_launch_mode_changed(self) -> None:
        self.refresh_mode_hint()
        self.save_config()

    @guarded
    def open_launch_config(self) -> None:
        """打开配置所在目录；文件不存在时先导出模板。"""
        path = launch_config_path()
        if not path.is_file():
            self.export_launch_template()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path.parent))  # noqa: S606  (Windows 打开资源管理器)
        except (OSError, AttributeError) as exc:
            messagebox.showinfo(APP_NAME, f"配置目录：\n{path.parent}\n\n（无法自动打开：{exc}）")

    @guarded
    def export_launch_template(self) -> None:
        """导出 launch.json 模板，已存在时不覆盖。"""
        path = launch_config_path()
        if path.is_file():
            if not messagebox.askyesno(APP_NAME, f"{path.name} 已存在，是否覆盖？"):
                return
        template = sample_launch_config()
        detected = locate_default_game()
        if detected is not None and detected.name == TARGET_EXE_NAME:
            template["game_path"] = str(detected)
            template["steam_app_id"] = str(detect_steam_app_id(detected) or "")
        try:
            atomic_write_text(path, json.dumps(template, ensure_ascii=False, indent=2))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"导出配置模板失败：\n{exc}")
            return
        self.log(f"已导出配置模板：{path}")
        self.refresh_mode_hint()
        messagebox.showinfo(
            APP_NAME,
            f"已导出配置模板：\n{path}\n\n"
            "可直接编辑 game_path / steam_app_id / extra_args。\n"
            "mode 填 config 时生效，填 auto 则忽略本文件。")

    def resolve_launch_plan(self) -> dict[str, object]:
        """决定本次启动走哪条路径。

        优先级：
          1. 界面选中「方式一」且 launch.json 配置可用  -> 读本地文件启动
          2. 其余情况                                   -> 自动探测（方式二）

        回退：方式一因「文件不存在 / JSON 非法 / 路径无效 / mode 不符」不可用时，
        一律记录原因并回退方式二，绝不让配置问题挡住启动。
        """
        wanted = self.launch_mode_var.get() if hasattr(self, "launch_mode_var") else LAUNCH_MODE_AUTO

        config, err = load_launch_config()
        if wanted == LAUNCH_MODE_CONFIG:
            if not config:
                reason = f"配置不可用：{err or '未知原因'}"
                self.log(f"启动方式一不可用（{reason}），回退到自动探测。")
                return self._auto_launch_plan(reason)
            game: Path | None = config["game"]
            if game is not None and not game.is_file():
                reason = f"配置中的 game_path 不存在：{game}"
                self.log(f"启动方式一不可用（{reason}），回退到自动探测。")
                return self._auto_launch_plan(reason)
            # game_path 留空是允许的：此时直接用自动探测路径，但保持方式一的参数
            target = game if (game is not None and game.is_file()) else Path(
                self.game_path_var.get().strip() or str(locate_default_game()))
            if not target.is_file():
                reason = f"无法定位可执行文件：{target}"
                self.log(f"启动方式一不可用（{reason}），回退到自动探测。")
                return self._auto_launch_plan(reason)
            steam_on = (bool(config["use_steam_env"]) if config.get("use_steam_env_set")
                        else bool(self.use_steam_env_var.get()))
            return {
                "mode": LAUNCH_MODE_CONFIG,
                "game": target,
                "app_id": int(config["steam_app_id"]) or detect_steam_app_id(target),
                "extra_args": list(config["extra_args"]),
                "use_steam": steam_on,
                "fallback_reason": "" if game is not None else "配置未指定 game_path，已定位自动探测路径",
            }

        # 界面明确选了方式二：即使存在配置也不读
        return self._auto_launch_plan("")

    def _auto_launch_plan(self, fallback_reason: str) -> dict[str, object]:
        """方式二：沿用界面上的游戏路径 + Steam 自动识别。"""
        path_text = self.game_path_var.get().strip()
        game = Path(path_text).expanduser() if path_text else locate_default_game()
        return {
            "mode": LAUNCH_MODE_AUTO,
            "game": game,
            "app_id": 0,  # 由 build_launch_env 自动识别
            "extra_args": [],
            "use_steam": (bool(self.use_steam_env_var.get())
                          if hasattr(self, "use_steam_env_var") else True),
            "fallback_reason": fallback_reason,
        }

    @guarded
    def launch_game(self) -> None:
        plan = self.resolve_launch_plan()
        game_path: Path = plan["game"]
        mode = plan["mode"]

        if mode == LAUNCH_MODE_AUTO:
            self.game_path = game_path
            self.game_path_var.set(str(game_path))
            result = self.check_version()
            if not result["valid"]:
                if self.force_launch_var.get():
                    self.log(f"已勾选「忽略兼容性检查」，跳过确认：{result['reason']}")
                elif not messagebox.askyesno(
                        APP_NAME,
                        f"当前游戏文件未通过兼容性检查：\n{result['reason']}\n\n"
                        "仍要准备桥接文件并尝试启动吗？\n"
                        "（非 Steam / 非常规构建可先勾选「忽略兼容性检查」）"):
                    return
        else:
            self.game_path = game_path
            self.game_path_var.set(str(game_path))
            self.log(f"启动方式一：使用本地配置的游戏路径 {game_path}")
            result = self.check_version()
            if not result["valid"]:
                self.log(f"配置路径未通过兼容性检查（{result['reason']}），继续尝试启动。")

        if not self.prepare_bridge():
            return
        self.save_config()
        env = build_launch_env(game_path, int(plan["app_id"]), bool(plan["use_steam"]))
        resolved_app_id = env.get("SteamAppId")
        self.log(f"存档目录：{self.save_dir}；注入 Steam 环境：{'是' if plan['use_steam'] else '否'}"
                 + (f"（AppID {resolved_app_id}）" if resolved_app_id else "（未能识别 AppID）"))

        existing = find_game_processes()
        if existing:
            pid, name = existing[0]
            self.log(f"检测到游戏进程：{name}（PID {pid}，匹配层级 {process_match_tier(name)}）")
            status = self.read_bridge_status()
            if status.get("bridge") == "1" and self.status_fresh(status):
                self.pid = pid
                self.log(f"已连接游戏进程 PID {pid}。")
                self.connection_var.set(f"已连接（PID {pid}）")
                return
            self.log(f"游戏进程 PID {pid} 未就绪：krft_status.txt 不存在、bridge≠1 或状态超时")
            messagebox.showwarning(
                APP_NAME,
                f"检测到游戏进程已在运行（PID {pid}，{name}），但桥接未就绪。\n\n"
                "常见原因：\n"
                "1. 游戏不是由本修改器以 -custom_script krft_bridge 启动的；\n"
                "2. 存档目录不一致，桥接文件未被游戏加载；\n"
                "3. 当前游戏版本不支持 -custom_script 入口。\n\n"
                "请完全退出游戏后重试。若使用第三方启动器，建议改用方式 B：\n"
                "手动给游戏添加启动参数 -custom_script krft_bridge，再由修改器自动连接。")
            return

        args = [str(game_path), "-custom_script", BRIDGE_MODULE]
        args.extend(plan["extra_args"])
        label = "方式一（本地配置）" if mode == LAUNCH_MODE_CONFIG else "方式二（自动探测）"
        if plan["fallback_reason"]:
            self.log(f"本次以{label}启动；回退原因：{plan['fallback_reason']}")
        else:
            self.log(f"本次以{label}启动：{' '.join(args)}")
        try:
            process = subprocess.Popen(
                args, env=env, cwd=str(game_path.parent),
                creationflags=CREATE_NEW_PROCESS_GROUP)
        except OSError as exc:
            # 方式一失败时，若与方式二路径不同，尝试用自动探测再试一次
            if mode == LAUNCH_MODE_CONFIG and not plan["fallback_reason"]:
                auto_game = locate_default_game()
                if auto_game and auto_game.resolve() != game_path.resolve():
                    self.log(f"配置路径启动失败（{exc}），改用自动探测路径重试：{auto_game}")
                    try:
                        process = subprocess.Popen(
                            [str(auto_game), "-custom_script", BRIDGE_MODULE],
                            env=build_launch_env(auto_game), cwd=str(auto_game.parent),
                            creationflags=CREATE_NEW_PROCESS_GROUP)
                        self.game_path = auto_game
                        self.game_path_var.set(str(auto_game))
                    except OSError as exc2:
                        messagebox.showerror(APP_NAME, f"两种启动方式均失败：\n{exc}\n\n自动探测重试：{exc2}")
                        return
                else:
                    messagebox.showerror(APP_NAME, f"启动失败：\n{exc}")
                    return
            else:
                messagebox.showerror(APP_NAME, f"启动失败：\n{exc}")
                return
        self.pid = process.pid
        self.log(f"游戏进程已创建 PID {self.pid}，等待桥接握手…")
        self.root.after(500, lambda: self.verify_launch(process.pid))

    def verify_launch(self, pid: int) -> None:
        """等待桥接握手。

        用 root.after 分帧轮询，而不是 while + time.sleep 阻塞主线程——
        后者在桥接无响应时会冻结界面 20 秒（实测 21.5s），期间无法关闭窗口。
        """
        self._verify_deadline = time.time() + 20.0
        self._verify_pid = pid
        self._verify_tick()

    def _verify_tick(self) -> None:
        pid = getattr(self, "_verify_pid", 0)
        if time.time() > getattr(self, "_verify_deadline", 0.0):
            self.connection_var.set("未连接")
            self.bridge_var.set("桥接状态：握手超时")
            messagebox.showwarning(
                APP_NAME,
                "等待桥接握手超时。\n\n请确认：\n"
                "1. 游戏窗口已正常打开\n"
                f"2. 存档目录可写（{self.save_dir}）\n"
                "3. 当前游戏版本包含 -custom_script 入口")
            return
        if not find_process_ids():
            self.connection_var.set("未连接")
            self.bridge_var.set("桥接状态：游戏已退出")
            messagebox.showwarning(
                APP_NAME,
                "游戏进程已退出或未能识别。\n\n常见原因：\n"
                "1. Steam 运行环境缺失（非 Steam 构建请关闭「注入 Steam 运行环境变量」）\n"
                "2. 游戏已在另一个 Steam 客户端实例中运行\n"
                "3. 当前版本缺少 -custom_script 入口\n"
                "4. 进程名与预期差异较大，可在下方日志查看识别结果")
            return
        status = self.read_bridge_status()
        if status.get("bridge") == "1" and self.status_fresh(status):
            self.connection_var.set(f"已连接（PID {pid}）")
            self.bridge_var.set(f"桥接状态：已连接 v{status.get('bridge_version', '?')}")
            self.log("桥接握手成功。")
            return
        # 让出主线程，保持界面可响应
        self.root.after(400, self._verify_tick)

    def status_targets(self) -> list[Path]:
        """状态回报的可能位置：存档目录优先，游戏目录兜底（identity 变更场景）。"""
        targets = [self.save_dir / STATUS_FILE]
        try:
            game_dir = Path(self.game_path_var.get().strip()).resolve().parent
            game_target = game_dir / STATUS_FILE
            if game_target not in targets:
                targets.append(game_target)
        except (OSError, ValueError):
            pass
        return targets

    def read_bridge_status(self) -> dict[str, str]:
        """读桥接上报，优先存档目录，取不到再试游戏目录那份。

        identity 变更后 GUI 可能把存档目录指向游戏目录旁边，此时只有那一份是新的。
        按 mtime 挑最新的一份，避免读到旧文件误判为「未加载桥接」。
        """
        best: dict[str, str] = {}
        best_mtime = -1.0
        for target in self.status_targets():
            try:
                if not target.is_file():
                    continue
                parsed = parse_status(target)
                if not parsed:
                    continue
                mtime = target.stat().st_mtime
                if mtime > best_mtime:
                    best_mtime = mtime
                    best = parsed
            except OSError:
                continue
        return best

    def status_fresh(self, status: dict[str, str]) -> bool:
        return (time.time() - safe_int(status.get("timestamp"), 0)) < 8

    # ---------- 回调 ----------
    @guarded
    def on_toggle(self) -> None:
        # 只读模式开关变化时，先做联动禁用/恢复
        if self.toggles.get("read_only") is not None:
            before = bool(self.state.get("read_only"))
            self.state["read_only"] = bool(self.toggles["read_only"].get())
            if before != self.state["read_only"]:
                self.refresh_read_only_ui()
        self.collect_state()
        # 只读模式下强制三项存档功能为关闭，防止被绕过界面写入
        if self.state.get("read_only"):
            for key in self.SLOT_FEATURES:
                self.state[key] = False
                if key in self.toggles:
                    self.toggles[key].set(False)
            self.write_state()
            self.save_config()
            return
        if any(self.state.get(key) for key in self.SLOT_FEATURES):
            if not self.ensure_backup():
                # 备份失败则回滚这三个开关，避免改动存档
                for key in self.SLOT_FEATURES:
                    self.state[key] = False
                    if key in self.toggles:
                        self.toggles[key].set(False)
                self.log("存档备份失败，已取消存档类功能。")
        self.write_state()
        self.save_config()

    @guarded
    def on_controls_changed(self) -> None:
        self.collect_state()
        self.write_state()
        self.save_config()

    def set_speed(self, value: float) -> None:
        self.values["speed"].set(float(value))
        self.state["speed_enabled"] = True
        if "speed_enabled" in self.toggles:
            self.toggles["speed_enabled"].set(True)
        self.on_controls_changed()

    def toggle_pause(self) -> None:
        new_value = not bool(self.state.get("paused"))
        self.state["paused"] = new_value
        self.write_state()
        self.log("已暂停关卡仿真。" if new_value else "已恢复关卡仿真。")

    def set_kill_gold(self, value: float) -> None:
        self.values["kill_gold_multiplier"].set(float(value))
        self.state["kill_gold_enabled"] = True
        if "kill_gold_enabled" in self.toggles:
            self.toggles["kill_gold_enabled"].set(True)
        self.on_controls_changed()

    def _fire(self, key: str, label: str, confirm: str | None = None) -> None:
        if confirm and not messagebox.askyesno(APP_NAME, confirm):
            return
        self.state[key] = int(self.state.get(key, 0)) + 1
        self.write_state()
        self.log(label)

    def cmd_skip_wave(self) -> None:
        self._fire("cmd_skip_wave", "已请求：提前呼叫下一波。")

    def cmd_force_wave(self) -> None:
        self._fire("cmd_force_wave", "已请求：强制清场并推进。",
                   "强制清场可能跳过 Boss 与脚本事件，确定继续？")

    def cmd_instant_win(self) -> None:
        self._fire("cmd_win", "已请求：立即通关。",
                   "将清空本关全部敌人并推进波次，由游戏自身判定胜利。\n\n"
                   "胜利后会正常写入存档（星级与下一关解锁）。确定继续？")

    def on_hotkey(self, action: str) -> None:
        try:
            if action.startswith("speed_"):
                self.set_speed(int(action.split("_")[1]))
            elif action == "pause":
                self.toggle_pause()
            elif action == "gold":
                self._toggle_from_hotkey("gold_enabled")
            elif action == "gems":
                self._toggle_from_hotkey("gems_enabled")
            elif action == "lives":
                self._toggle_from_hotkey("lives_enabled")
            elif action == "kill_gold":
                self._cycle_kill_gold()
            elif action == "barrack":
                self._toggle_from_hotkey("barrack_enabled")
            elif action == "invincible":
                self._toggle_from_hotkey("invincible")
            elif action == "hero_no_cooldown":
                self._toggle_from_hotkey("hero_no_cooldown")
            elif action == "damage":
                self._toggle_from_hotkey("damage_enabled")
            elif action == "tower_speed":
                self._toggle_from_hotkey("tower_speed_enabled")
            elif action == "tower_range":
                self._toggle_from_hotkey("tower_range_enabled")
            elif action == "skip_wave":
                self.cmd_skip_wave()
            elif action == "instant_win":
                self.cmd_instant_win()
        except Exception:
            log_message("快捷键处理异常：\n" + traceback.format_exc())

    def _toggle_from_hotkey(self, key: str) -> None:
        # 只读模式下存档类功能不可用，快捷键直接拒绝
        if key in self.SLOT_FEATURES and bool(self.state.get("read_only")):
            self.log(f"只读模式已启用，{HOTKEY_LABELS.get(key, key)} 不可用。")
            return
        if key in self.SLOT_FEATURES and not bool(self.state.get(key)):
            if not self.ensure_backup():
                return
        new_value = not bool(self.state.get(key))
        self.state[key] = new_value
        if key in self.toggles:
            self.toggles[key].set(new_value)
        self.write_state()
        self.log(f"{'启用' if new_value else '关闭'}：{HOTKEY_LABELS.get(key, key)}")

    def _cycle_kill_gold(self) -> None:
        steps = [1.0, 2.0, 3.0, 5.0, 10.0, 20.0]
        current = self.clamp_float(self.state.get("kill_gold_multiplier", 2.0), 0.1, 1000, 2.0)
        idx = 0
        for i, step in enumerate(steps):
            if abs(step - current) < 0.01:
                idx = (i + 1) % len(steps)
                break
        self.set_kill_gold(steps[idx])

    # ---------- 快捷键设置窗口 ----------
    def show_hotkeys(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("快捷键设置")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="点击「录制」后直接按下组合键。", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 6))
        vars_map: dict[str, tk.StringVar] = {}
        for row, (action, default) in enumerate(DEFAULT_HOTKEYS.items(), start=1):
            ttk.Label(win, text=HOTKEY_LABELS.get(action, action)).grid(
                row=row, column=0, sticky="w", padx=10, pady=2)
            var = tk.StringVar(value=self.hotkeys.get(action, default))
            vars_map[action] = var
            ttk.Entry(win, textvariable=var, width=20, state="readonly").grid(
                row=row, column=1, sticky="w", padx=6)
            ttk.Button(win, text="录制", width=6,
                       command=lambda a=action, v=var: self._record_hotkey(win, a, v)).grid(
                row=row, column=2, padx=(0, 10))
        ttk.Button(win, text="恢复默认", command=lambda: [v.set(d) for v, d in
                                                          zip(vars_map.values(), DEFAULT_HOTKEYS.values())]).grid(
            row=len(DEFAULT_HOTKEYS) + 1, column=1, sticky="w", padx=6, pady=10)

        def apply_close() -> None:
            for action, var in vars_map.items():
                combo = normalize_hotkey(var.get())
                if combo:
                    self.hotkeys[action] = combo
            self.save_config()
            self.log("快捷键已保存。")
            win.destroy()

        ttk.Button(win, text="保存", command=apply_close).grid(
            row=len(DEFAULT_HOTKEYS) + 1, column=2, padx=(0, 10), pady=10)

    def _record_hotkey(self, win, action: str, var: tk.StringVar) -> None:
        win.focus_force()
        var.set("按下组合键…")
        win.update()
        pressed: set[str] = []
        main = {"vk": 0, "name": ""}

        def on_key(event):
            key = event.keysym
            if key in ("Control_L", "Control_R"):
                pressed.add("Ctrl")
            elif key in ("Alt_L", "Alt_R"):
                pressed.add("Alt")
            elif key in ("Shift_L", "Shift_R"):
                pressed.add("Shift")
            else:
                if key == "space":
                    main["name"] = "Space"
                elif key.startswith("F") and key[1:].isdigit():
                    main["name"] = key.upper()
                elif len(key) == 1:
                    main["name"] = key.upper()
                else:
                    main["name"] = key
            combo = "+".join(
                [m for m in ("Ctrl", "Alt", "Shift", "Win") if m in pressed]
                + ([main["name"]] if main["name"] else [])
            )
            if main["name"]:
                var.set(combo)
                win.unbind("<Key>")
                win.bind("<Key>", on_key, add="+")
                return "break"
            return None

        win.bind("<Key>", on_key)

    # ---------- 轮询与退出 ----------
    def _poll_loop(self) -> None:
        try:
            now = time.time()
            pids = find_process_ids()
            if pids:
                if self.pid not in pids:
                    self.pid = pids[0]
                status = self.read_bridge_status()
                if status.get("bridge") == "1":
                    if self.status_fresh(status):
                        self.connected = True
                        self.connection_var.set(f"已连接（PID {self.pid}）")
                        scene = status.get("scene", "")
                        level = safe_int(status.get("level"), 0)
                        self.bridge_var.set(
                            f"桥接状态：v{status.get('bridge_version', '?')} · "
                            f"{'关卡中' if scene == 'game' else '主菜单'}"
                            + (f" · 第 {level} 关" if level else ""))
                    else:
                        self.connected = False
                        self.connection_var.set("已连接（桥接无响应）")
                else:
                    self.connected = False
                    self.connection_var.set(f"游戏运行中（PID {self.pid}）· 未加载桥接")
                    self.bridge_var.set(
                        "桥接状态：请用本程序启动游戏，或给游戏加 -custom_script krft_bridge 启动参数")
            else:
                if self.pid is not None:
                    self.log("检测到游戏进程已退出。")
                self.connected = False
                self.pid = None
                self.connection_var.set("未连接")
                self.bridge_var.set("桥接状态：未就绪")

            if self.pid is not None and now - self.last_heartbeat >= 0.8:
                self.write_state()
        except Exception:
            log_message("轮询异常：\n" + traceback.format_exc())
        self.root.after(700, self._poll_loop)

    def shutdown(self) -> None:
        """退出前写 active=false，让桥接立刻恢复所有运行时数值。"""
        try:
            if self.prepared:
                self.suppress_events = True
                self.state["active"] = False
                self.state["revision"] = int(self.state.get("revision", 0)) + 1
                self.state["heartbeat"] = int(time.time())
                atomic_write_text(self.save_dir / STATE_FILE, self.state_text())
                log_message("已发送恢复指令（active=false）")
        except Exception:
            pass
        try:
            self.poller.stop()
        except Exception:
            pass


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("clam")
    except tk.TclError:
        pass
    TrainerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
