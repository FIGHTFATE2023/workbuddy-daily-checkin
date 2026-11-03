# WorkBuddy 每日积分自动签到

> **版本适配：本发行版适配 WorkBuddy 桌面客户端 5.9.3（Windows）。**
> 发行标签 `v5.9.3` 表示适配的客户端版本；其他客户端版本未验证，升级后可能需要重新适配。
>
> 下载：[适配 5.9.3 的发行版](https://github.com/FIGHTFATE2023/workbuddy-daily-checkin/releases/tag/v5.9.3)，选择附件 `workbuddy-daily-checkin-5.9.3.zip`，解压后双击 `install.cmd`。

一个无人值守的 Windows 小工具：每天早上 8 点自动领取 WorkBuddy「Buddy 加油站」的每日积分，防止断签（断签清零，第 7 天是 1000 分）。幂等设计——已领过就跳过，重复执行不会多领、不会扣分。

纯 Python 标准库，**零第三方依赖**；凭据只存本机，不上传任何数据。

## 功能

- 每日 08:00 自动查询签到状态，未领则领取，领完复核累计积分
- 网络未就绪自动重试（最多 5 次 × 60 秒）；另有 08:05 / 08:20 两次补跑触发
- access token 过期自动用 refresh token 走官方接口刷新，刷新后自动轮换写回
- 凭据全部失效时，可从**运行中的 WorkBuddy 客户端**取回令牌（凭据桥）
- 彻底失败时弹 Windows 系统通知，提醒你手动领取
- 日志落盘 `checkin.log`，支持 `--json` 输出

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10/11（用到 Win32 API 与 PowerShell 通知） |
| Python | 3.9+，无任何第三方包 |
| WorkBuddy 桌面客户端 | **5.9.3**；首次取凭据或凭据桥兜底时需要它在运行并已登录 |

## 快速开始

1. 解压本目录到任意位置
2. 双击 `install.cmd`（或在 PowerShell 中执行）：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
   ```

3. 安装器会自动：定位 Python → 复制脚本到 `%USERPROFILE%\.workbuddy\wb-checkin` → 做一次状态自检 → 注册每天 08:00/08:05/08:20 的计划任务

**首次取凭据**：保持 WorkBuddy 客户端在运行，手动跑一次：

```
python "%USERPROFILE%\.workbuddy\wb-checkin\checkin.py" --json
```

脚本会通过凭据桥自动取到令牌并落盘，之后长期无需再管（令牌有效期一般到年底，过期会自动刷新/重取）。

## 工作原理

```
08:00 计划任务触发
   ↓
读取凭据（按优先级）：
  1. 命令行参数 / 环境变量 WB_ACCESS_TOKEN / WB_REFRESH_TOKEN
  2. credentials.json（脚本自维护，自动轮换）
  3. 客户端 auth 目录中的明文 .info 登录态备份（旧版本客户端）
  4. 凭据桥：从运行中的客户端进程内存提取令牌（bridge.py）
   ↓
查今日状态 → 已领则跳过（幂等）｜未领则领取 → 复核累计
   ↓
401 时：refresh token 刷新 → 仍失败则凭据桥重取 → 重试
   ↓
彻底失败：弹系统通知提醒手动领
```

**凭据桥（bridge.py）**：客户端新版把登录态加密存盘，磁盘上还原不了；但客户端运行时内存中必然持有明文令牌。桥只读进程内存、只提取本机登录的 JWT、验证可用后写入 `credentials.json`（0600 权限），不写注册表、不上传、不修改客户端。

## 常用命令

```bash
# 只查状态，不领取
python checkin.py --status --json

# 手动领取一次（幂等）
python checkin.py --json

# 失败时弹系统通知（计划任务用的就是这个参数）
python checkin.py --notify

# 指定 refresh token（适用于无法运行客户端的服务器场景）
python checkin.py --refresh-token <你的refresh_token> --json

# 禁用凭据桥（更保守，仅用本地/明文凭据）
python checkin.py --no-bridge --json
```

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 / 今天已领取 |
| 2 | 无可用凭据（客户端未运行且本地无凭据） |
| 3 | 接口或网络失败 |
| 4 | 凭据已失效且无法自动恢复，需重新登录客户端 |

## 文件说明

| 文件 | 说明 |
|---|---|
| `checkin.py` | 签到器主程序 |
| `bridge.py` | 凭据桥（从客户端内存取令牌） |
| `install.ps1` / `install.cmd` | 一键安装（复制脚本 + 注册计划任务） |
| `uninstall.ps1` | 卸载计划任务（`-Purge` 连本地文件一起删） |
| `credentials.json` | 安装后生成：本机令牌（0600 权限，**不要外传**） |
| `checkin.log` | 安装后生成：运行日志 |

## 排障

- **看日志**：`%USERPROFILE%\.workbuddy\wb-checkin\checkin.log`
- **提示无凭据**：启动 WorkBuddy 客户端并登录，再手动跑一次脚本即可
- **杀毒软件拦截**：凭据桥会读客户端进程内存，个别杀软可能告警；可将 `wb-checkin` 目录加入白名单，或改用 `--no-bridge` + 手动提供 refresh token
- **连续多天失败**：客户端大版本更新可能改变令牌形态或接口路径，需要按日志重新适配

## 隐私与安全

- 令牌只写入本机 `credentials.json`（仅当前用户可读），日志中令牌一律脱敏
- 所有请求只发往 `www.workbuddy.cn`，不经过任何第三方
- 卸载时建议用 `uninstall.ps1 -Purge` 一并删除凭据文件

## 已知限制与免责声明

- 接口路径与凭据形态来自对当前客户端版本的观察，**客户端大版本更新后可能失效**
- 电脑需在触发时段处于开机状态（错过会在开机后补跑；全天不开机则无法执行）
- 本工具仅供个人自动化使用，请遵守 WorkBuddy 的服务条款；使用风险自负
