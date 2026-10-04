# 贡献指南 / Contributing

感谢关注。这个项目很小，规则也简单。

## 报告问题

开 Issue 时请附上：

| 信息 | 获取方式 |
|---|---|
| 修改器版本 | 界面标题栏的 `v1.0.2` |
| 桥接版本 | `%APPDATA%\kingdom_rush_frontiers\krft_status.txt` 的 `bridge_version` |
| 游戏版本 | 同上文件的 `timestamp` 附近，或 Steam 库属性 |
| 日志 | `%LOCALAPPDATA%\KingdomRushFrontiersTrainer\trainer.log` |

**请先删除日志中的个人路径再粘贴。**

## 提交代码前

```bash
python _test_bridge.py       # 必须全绿（44 项）
python _test_gui_smoke.py    # 必须全绿（23 项）
```

两个测试都需要本机已安装游戏。改动桥接逻辑时，请一并补上对应断言——
本项目有过两次「修一个功能、坏另一个功能」的回归，测试是唯一的防线。

## 代码风格

- Python：`PEP 8`，4 空格缩进
- Lua：`Allman` 风格（与原版一致），2 空格缩进
- 注释用中文，与现有代码保持一致
- 关键决策请在注释里写清**为什么**，而不只是**做了什么**

## 硬性约束

以下几条是这个项目不能碰的红线，PR 违反会被直接关闭：

1. **不得包含任何游戏美术、音频、字体或代码资源。** 这些版权归 Ironhide Game Studio 所有。
2. **不得引入网络请求。** 本项目的零网络是核心卖点，加了外联等于破坏用户信任。
3. **不得实现 DLL 注入或进程内存读写。** 现有方案（游戏自带 `-custom_script` 入口）是刻意的选择。
4. **不得修改游戏安装目录。** 运行时只读游戏 exe 做版本检查。
5. **所有功能必须有恢复逻辑。** 关闭开关 / 心跳超时 / 退出时都要还原为原值。

## 提交信息

用中文或英文均可，格式参考 [Conventional Commits](https://www.conventionalcommits.org/)：

```
fix(bridge): 修复关卡载入即自动通关
feat(ui): 增加兵营人数快捷输入
docs(readme): 补充 FAQ
test: 覆盖心跳超时恢复
```

## 许可证

贡献即表示你同意你的代码以 [MIT](LICENSE) 许可证发布。
