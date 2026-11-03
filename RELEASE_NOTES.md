# WorkBuddy 每日积分自动签到 — 适配 5.9.3

本发行版适配 **WorkBuddy Windows 桌面客户端 5.9.3**。标签 `v5.9.3` 对应适配的客户端版本，其他客户端版本未验证。

## 下载与安装

1. 下载发行附件 **`workbuddy-daily-checkin-5.9.3.zip`** 并解压。
2. 确保 Windows 10/11 已安装 Python 3.9+，并启动 WorkBuddy **5.9.3**、登录自己的账号。
3. 双击 `install.cmd`，或执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`。
4. 安装器将脚本复制到 `%USERPROFILE%\.workbuddy\wb-checkin`，并注册每天 08:00、08:05、08:20 执行的计划任务。

## 包含功能

- 自动查询并领取每日积分，已领取时跳过。
- 网络失败重试、令牌刷新和失败通知。
- 针对 5.9.3 的加密登录态，通过凭据桥从本机运行中的客户端读取令牌。
- 纯 Python 标准库，无第三方 Python 包依赖。

ZIP 包含 `checkin.py`、`bridge.py`、`install.cmd`、`install.ps1`、`uninstall.ps1` 和 `README.md`；不包含用户凭据或运行日志。

客户端升级后可能需要重新适配。首次获取凭据及凭据桥恢复时，需要客户端保持运行并已登录。使用方法与卸载方式见 README。

发布前检查：Python 源码语法、命令行帮助、PowerShell 脚本语法和 ZIP 完整性；未在发布过程中执行实际签到或安装计划任务。
