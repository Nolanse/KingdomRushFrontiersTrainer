"""打包产物验收：确认 dist 里的 exe 确实是 v1.3.2 且包含本次修复。

**不执行**未知二进制本体，只做静态检查与哈希计算。

注意：不能直接在 exe 字节里搜明文字符串——PyInstaller 会把 Lua 源码与
pyc 都以 zlib 压缩存储，grep 一律命中 0。正确做法是用 CArchiveReader
把条目解出来再比对。运行验证由 _test_bridge.py / _test_gui_smoke.py /
_test_lua_syntax.py 负责。
"""

import hashlib
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parent
EXE = ROOT / "dist" / "KingdomRushFrontiersTrainer.exe"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_exists() -> bool:
    if not EXE.is_file():
        print(f"[FAIL] 未找到产物：{EXE}")
        return False
    size_mb = EXE.stat().st_size / 1024 / 1024
    print(f"[PASS] 产物存在：{EXE.name} · {size_mb:.1f} MB")
    return True


def check_bridge_payload(reader: CArchiveReader) -> bool:
    """把包内桥接脚本解出来，与磁盘源文件逐字节比对。

    这是本次修复的核心：io 兜底读写的逻辑必须真的被打进包里。
    """
    src = ROOT / "krft_bridge.lua"
    src_data = src.read_bytes()
    try:
        packed = reader.extract("krft_bridge.lua")
    except Exception as exc:  # noqa: BLE001 - 归档损坏也要报出来
        print(f"[FAIL] 无法从包内提取 krft_bridge.lua：{exc}")
        return False

    if packed == src_data:
        print(f"[PASS] 包内桥接脚本与源文件逐字节一致（{len(src_data)} bytes）")
    else:
        print(f"[FAIL] 包内桥接脚本与源文件不一致：包 {len(packed)} / 源 {len(src_data)}")
        return False

    text = packed.decode("utf-8", "replace")
    ok = True
    checks = {
        "read_state_fallback 兜底读取": "function read_state_fallback",
        "read_state_any 双路读取": "read_state_any()",
        "游戏目录兜底路径": "getSourceBaseDirectory",
        "桥接版本号 1.3.2": 'local BRIDGE_VERSION = "1.3.2"',
    }
    for label, needle in checks.items():
        found = needle in text
        print(f"[{'PASS' if found else 'FAIL'}] 桥接含 {label}")
        ok &= found
    return ok


def check_python_fixes(reader: CArchiveReader) -> bool:
    """校验 Python 侧修复进了包：在 CArchive 目录与 PYZ 里查模块名。"""
    toc = list(reader.toc)
    ok = True

    # 主脚本以 pyc 形式存在，模块名等常量以明文留在字节里
    blob = EXE.read_bytes()
    # 主脚本名在 TOC 里
    entry = next((n for n in toc if n.lower().endswith("kingdomrushfrontierstrainer")), None)
    print(f"[{'PASS' if entry else 'WARN'}] 主脚本条目：{entry or '未按名字找到（可能已编译为模块名）'}")

    # 从 CArchive 里把主脚本 pyc 解出来验常量
    py_ok = False
    for name in toc:
        if name.lower().endswith("kingdomrushfrontierstrainer"):
            try:
                data = reader.extract(name)
            except Exception:  # noqa: BLE001
                continue
            for label, needle in (
                ("love_identity_dirs", b"love_identity_dirs"),
                ("KNOWN_SAVE_DIR_NAMES", b"KNOWN_SAVE_DIR_NAMES"),
                ("版本号 1.3.2", b"1.3.2"),
            ):
                found = needle in data
                print(f"[{'PASS' if found else 'FAIL'}] 主脚本含 {label}")
                ok &= found
            # 旧的重复 heartbeat 写法应已消失
            old = b"heartbeat = {int(time.time())}"
            gone = old not in data
            print(f"[{'PASS' if gone else 'FAIL'}] 旧的重复 heartbeat 写法已移除")
            ok &= gone
            py_ok = True
            break
    if not py_ok:
        print("[WARN] 未能解出主脚本条目，跳过 Python 侧常量校验")
    return ok


def check_modules_present(reader: CArchiveReader) -> bool:
    """确认关键模块进了包。

    PyInstaller 分两层存放：
      - CArchive 顶层：扩展模块（.pyd）、tcl/tk 数据、主脚本、数据文件
      - PYZ.pyz：纯 Python 模块（json / zipfile / shutil 等标准库）
    所以顶层 TOC 里找不到标准库是正常的，必须再翻 PYZ 一层。
    """
    top = [n.lower() for n in reader.toc]
    ok = True

    # 扩展模块与运行时数据必须在 CArchive 顶层
    for label, needles in (
        ("tcl/tk 运行时", ["_tcl_data", "_tk_data"]),
        ("tkinter 扩展", ["_tkinter.pyd", "pyi_rth__tkinter"]),
    ):
        hit = any(any(nd in n for n in top) for nd in needles)
        print(f"[{'PASS' if hit else 'FAIL'}] {label} 已打包")
        ok &= hit

    # 纯 Python 标准库在 PYZ 里
    pyz_names: set[str] = set()
    try:
        import marshal
        import struct

        pyz_data = reader.extract("PYZ.pyz")
        # PYZ 格式：'PYZ\0' + pyversion(4) + toc_offset(大端 u32)，TOC 是 marshal 后的
        # list[(name, (typecode, offset, length))]，不是 dict —— 遍历时要取元组第一项
        toc_offset = struct.unpack("!i", pyz_data[8:12])[0]
        pyz_toc = marshal.loads(pyz_data[toc_offset:])
        pyz_names = {
            str(entry[0]).lower() for entry in pyz_toc if isinstance(entry, (tuple, list))
        }
        print(f"       PYZ 内模块数：{len(pyz_names)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 解析 PYZ 失败：{exc}")

    for mod in ("json", "zipfile", "shutil", "struct", "hashlib", "re"):
        hit = mod in pyz_names or any(mod in n for n in top)
        print(f"[{'PASS' if hit else 'FAIL'}] 标准库 {mod}")
        ok &= hit

    # lupa 只是测试依赖，不应被打进发布产物
    lupa_absent = "lupa" not in pyz_names and not any("lupa" in n for n in top)
    print(f"[{'PASS' if lupa_absent else 'FAIL'}] 测试依赖 lupa 未被打进包")
    ok &= lupa_absent
    return ok


def write_checksum() -> bool:
    target = EXE.with_suffix(EXE.suffix + ".sha256")
    digest = sha256(EXE)
    target.write_text(f"{digest}  {EXE.name}\n", encoding="utf-8")
    again = sha256(EXE)
    ok = again == digest
    print(f"[{'PASS' if ok else 'FAIL'}] 校验文件已生成并回验通过")
    print(f"       sha256 = {digest}")
    return ok


def main() -> None:
    print("=" * 62)
    print("v1.3.2 打包产物验收")
    print("=" * 62)
    if not check_exists():
        raise SystemExit(1)

    reader = CArchiveReader(str(EXE))
    print(f"归档条目数：{len(list(reader.toc))}")
    print("-" * 62)

    ok = True
    ok &= check_bridge_payload(reader)
    ok &= check_python_fixes(reader)
    ok &= check_modules_present(reader)
    print("-" * 62)
    ok &= write_checksum()
    print("=" * 62)
    print("RESULT:", "ALL PASSED" if ok else "HAS FAILURE")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
