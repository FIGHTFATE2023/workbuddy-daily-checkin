#!/usr/bin/env python3
"""WorkBuddy 每日积分签到器（幂等、无第三方依赖）。

流程：解析凭据 -> (必要时) 刷新 access token -> 查今日状态 -> 未领则领取 -> 复核

凭据优先级：
  1. --access-token / 环境变量 WB_ACCESS_TOKEN
  2. --refresh-token / 环境变量 WB_REFRESH_TOKEN  (走官方刷新接口换新)
  3. <脚本目录>/credentials.json      (脚本自维护，刷新后自动轮换写回)
  4. 客户端 auth 目录下的「明文」.info 备份 (旧版本或历史备份)

退出码：0 成功/已领 | 2 无可用凭据 | 3 接口或网络失败 | 4 凭据已失效

安全约定：不打印任何凭据明文；credentials.json 以 0600 权限写入，仅存 refresh/access token。
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CRED_PATH = os.path.join(HERE, "credentials.json")
LOG_PATH = os.path.join(HERE, "checkin.log")

STATUS_EP = "/v2/billing/meter/checkin-activity-status"
CHECKIN_EP = "/v2/billing/meter/daily-checkin"
REFRESH_EP = "/v2/plugin/auth/token/refresh"      # 客户端自用，prefixPath=/plugin
AUTH_DIR = os.path.expandvars(r"%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth")
DEFAULT_DOMAIN = "www.workbuddy.cn"


# ---------------------------------------------------------------- 基础设施
def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def mask(tok: str) -> str:
    return f"{tok[:6]}…{tok[-4:]} (len={len(tok)})" if tok and len(tok) > 20 else "***"


def post(url: str, headers: dict, timeout: int = 25) -> tuple[int | None, dict]:
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except Exception as e:  # 网络层
        return None, {"_error": f"{type(e).__name__}: {e}"}


def _parse(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text[:300]}


def dig(obj, *keys):
    """在嵌套 dict 里递归找第一个非空值。"""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if v not in (None, "", [], {}):
                return v
        for v in obj.values():
            r = dig(v, *keys)
            if r not in (None, "", [], {}):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = dig(v, *keys)
            if r not in (None, "", [], {}):
                return r
    return None


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 凭据
def load_plaintext_info():
    """从客户端 auth 目录里找「未加密」的 .info（含旧备份），返回 (access, refresh, domain, uid)。"""
    for path in sorted(glob.glob(os.path.join(AUTH_DIR, "*.info")), reverse=True):
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        auth, acc = d.get("auth") or {}, d.get("account") or {}
        at, rt = auth.get("accessToken"), auth.get("refreshToken")
        # 当前版本这两个字段是 {"$wbEncrypted":1,"envelope":...} 的字典，跳过
        if not isinstance(at, str) and not isinstance(rt, str):
            continue
        return (at if isinstance(at, str) else "",
                rt if isinstance(rt, str) else "",
                str(auth.get("domain") or DEFAULT_DOMAIN),
                str(acc.get("uid") or ""),
                os.path.basename(path))
    return None


def read_meta():
    """当前登录态里的 domain / uid 是明文的，可直接读。"""
    domain, uid = DEFAULT_DOMAIN, ""
    try:
        d = json.load(open(os.path.join(AUTH_DIR, "workbuddy-desktop.info"), encoding="utf-8"))
        domain = str((d.get("auth") or {}).get("domain") or DEFAULT_DOMAIN).strip() or DEFAULT_DOMAIN
        uid = str((d.get("account") or {}).get("uid") or "")
    except Exception:
        pass
    return domain, uid


def resolve_credential(args):
    """返回 dict(access, refresh, domain, uid, source)。无可用凭据返回 None。"""
    domain, uid = read_meta()

    if args.access_token or os.environ.get("WB_ACCESS_TOKEN"):
        return {"access": args.access_token or os.environ["WB_ACCESS_TOKEN"], "refresh": "",
                "domain": domain, "uid": uid, "source": "命令行/环境变量"}

    if args.refresh_token or os.environ.get("WB_REFRESH_TOKEN"):
        return {"access": "", "refresh": args.refresh_token or os.environ["WB_REFRESH_TOKEN"],
                "domain": domain, "uid": uid, "source": "命令行/环境变量"}

    if os.path.isfile(CRED_PATH):
        try:
            c = json.load(open(CRED_PATH, encoding="utf-8"))
            if c.get("accessToken") or c.get("refreshToken"):
                return {"access": c.get("accessToken", ""), "refresh": c.get("refreshToken", ""),
                        "domain": c.get("domain") or domain, "uid": c.get("uid") or uid,
                        "source": "本地凭据库"}
        except Exception:
            pass

    pt = load_plaintext_info()
    if pt:
        at, rt, d2, u2, name = pt
        return {"access": at, "refresh": rt, "domain": d2, "uid": u2 or uid,
                "source": f"明文登录态备份({name})"}
    return None


def save_credential(cred: dict) -> None:
    try:
        with open(CRED_PATH, "w", encoding="utf-8") as f:
            json.dump({k: cred.get(k, "") for k in ("accessToken", "refreshToken", "expiresAt",
                                                   "refreshExpiresAt", "domain", "uid")},
                      f, ensure_ascii=False, indent=2)
        os.chmod(CRED_PATH, 0o600)
    except OSError as e:
        log(f"⚠ 写入本地凭据库失败：{e}")


def refresh_access(cred: dict):
    """用 refresh token 换新的 access token。成功返回 True 并就地更新 cred。"""
    if not cred.get("refresh"):
        return False
    domain = cred.get("domain") or DEFAULT_DOMAIN
    log(f"→ 正在刷新 access token（{domain}）")
    http, obj = post(f"https://{domain}{REFRESH_EP}", {
        "X-Domain": domain,
        "X-Refresh-Token": cred["refresh"],
        "X-Auth-Refresh-Source": "plugin",
    })
    if http != 200:
        msg = dig(obj, "msg", "message", "error_msg") or str(obj)[:160]
        log(f"✗ 刷新失败 HTTP {http}：{msg}")
        return False
    data = dig(obj, "accessToken") or {}
    if isinstance(data, str) and data:
        # 极端情况：data 直接是 token 字符串
        cred["access"] = data
    else:
        inner = (obj.get("data") or {})
        inner = inner.get("data") if isinstance(inner.get("data"), dict) else inner
        if not isinstance(inner, dict):
            log("✗ 刷新响应结构异常"); return False
        cred["access"] = str(inner.get("accessToken") or inner.get("access_token") or "")
        if inner.get("refreshToken"):
            cred["refresh"] = str(inner["refreshToken"])
        exp = as_int(inner.get("expiresIn"))
        cred["expiresAt"] = int(time.time() * 1000) + (exp or 0) * 1000 if exp else ""
    if not cred["access"]:
        log("✗ 刷新响应中没有 accessToken"); return False
    log(f"✓ access token 已刷新：{mask(cred['access'])}")
    save_credential(cred)
    return True


# ---------------------------------------------------------------- 业务
def run_bridge() -> bool:
    """调用 bridge.py 从运行中的客户端内存取一次令牌；成功返回 True。"""
    script = os.path.join(HERE, "bridge.py")
    if not os.path.isfile(script):
        log("✗ 未找到 bridge.py")
        return False
    try:
        r = subprocess.run([sys.executable, script], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        for line in (r.stdout or "").strip().splitlines():
            log(f"  [bridge] {line}")
        return r.returncode == 0
    except Exception as e:
        log(f"✗ 调用桥接程序失败：{e}")
        return False


def api_headers(cred: dict) -> dict:
    return {"Authorization": f"Bearer {cred['access']}",
            "X-User-Id": cred.get("uid") or "",
            "X-Domain": cred.get("domain") or DEFAULT_DOMAIN}


def summarize(obj: dict) -> dict:
    return {
        "checked_in": bool(dig(obj, "today_checked_in", "todayCheckedIn") or False),
        "code": dig(obj, "code"),
        "msg": str(dig(obj, "msg", "message") or ""),
        "today_credit": as_int(dig(obj, "today_credit", "todayCredit")),
        "streak_days": as_int(dig(obj, "streak_days", "streakDays", "continuousDays")),
        "total_credits": as_int(dig(obj, "total_credits", "totalCredits")),
        "activity": str(dig(obj, "activity_name", "activityName") or ""),
    }


def notify_fail(reason: str) -> None:
    """失败时弹系统通知，避免断签。"""
    title, body = "WorkBuddy 积分未领取", f"{reason}\n请打开 WorkBuddy → 左下角头像 → Buddy 加油站 领取今日礼包"
    ps = f"""
$ErrorActionPreference='Stop'
try {{
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
  $tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $t = $tpl.GetElementsByTagName('text')
  $t.Item(0).AppendChild($tpl.CreateTextNode({json.dumps(title, ensure_ascii=False)})) | Out-Null
  $t.Item(1).AppendChild($tpl.CreateTextNode({json.dumps(body, ensure_ascii=False)})) | Out-Null
  $toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('WorkBuddy 签到').Show($toast)
}} catch {{ }}
"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=25)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy 每日积分签到（幂等）")
    ap.add_argument("--status", action="store_true", help="只查状态，不领取")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--notify", action="store_true", help="失败时弹系统通知")
    ap.add_argument("--no-bridge", action="store_true", help="禁用从客户端内存取令牌")
    ap.add_argument("--access-token", default="", help="直接指定 access token")
    ap.add_argument("--refresh-token", default="", help="直接指定 refresh token")
    args = ap.parse_args()

    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ.pop(k, None)

    res = {"ok": False, "action": "none", "message": "", "today_credit": None,
           "streak_days": None, "total_credits": None, "source": "",
           "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    def done(code: int) -> int:
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
        if code != 0 and args.notify:
            notify_fail(res["message"] or "自动领取失败")
        return code

    cred = resolve_credential(args)
    if not cred and not args.no_bridge:
        log("本地没有可用凭据，尝试从运行中的客户端取一次…")
        if run_bridge():
            cred = resolve_credential(args)
    if not cred:
        res["message"] = ("无可用凭据，且无法从客户端取到（客户端是否在运行？）；"
                          "请在客户端手动领取")
        log(f"✗ {res['message']}")
        return done(2)

    res["source"] = cred["source"]
    log(f"凭据来源：{cred['source']}｜domain={cred.get('domain')}｜uid={cred.get('uid') or '?'}")

    domain = cred.get("domain") or DEFAULT_DOMAIN

    # 1. 先试当前 access token；不可用则刷新，再不行就从客户端重新取
    # 清晨网络可能未就绪（Wi-Fi/VPN 还没连上）：网络层失败时最多再等 5 次、每次 60 秒
    for attempt in range(6):
        http, st = post(f"https://{domain}{STATUS_EP}", api_headers(cred))
        if http is not None:
            break
        if attempt < 5:
            log(f"→ 网络未就绪（{str(st.get('_error', ''))[:120]}），60 秒后重试（{attempt + 1}/5）")
            time.sleep(60)
    if http in (401, 403) or (http == 200 and str(dig(st, "code")) in ("401", "10002")):
        log("→ access token 不可用，尝试用 refresh token 刷新")
        if refresh_access(cred):
            http, st = post(f"https://{domain}{STATUS_EP}", api_headers(cred))
        if http in (401, 403) and not args.no_bridge:
            log("→ 刷新未成功，改从客户端内存重新取一次")
            if run_bridge():
                cred = resolve_credential(args)
                domain = cred.get("domain") or DEFAULT_DOMAIN
                res["source"] = cred["source"]
                http, st = post(f"https://{domain}{STATUS_EP}", api_headers(cred))
        if http in (401, 403):
            res["message"] = "令牌失效且无法自动恢复，请重新登录 WorkBuddy"
            log(f"✗ {res['message']}")
            return done(4)

    if http is None:
        res["message"] = f"网络请求失败：{st.get('_error')}"
        log(f"✗ {res['message']}")
        return done(3)
    if http != 200:
        res["message"] = f"状态查询失败 HTTP {http}：{dig(st,'msg','error_msg') or str(st)[:120]}"
        log(f"✗ {res['message']}")
        return done(3)

    info = summarize(st)
    res["streak_days"] = info["streak_days"]
    res["total_credits"] = info["total_credits"]
    log(f"活动：{info['activity'] or 'Buddy加油站'}｜连续 {info['streak_days']} 天｜累计 {info['total_credits']} 积分")

    if info["checked_in"]:
        res.update(ok=True, action="skip", message="今天已领取，跳过",
                   today_credit=info["today_credit"])
        log("→ 今天已领取，跳过")
        return done(0)

    if args.status:
        res.update(ok=True, action="status-only", message="今天还未领取")
        return done(0)

    # 2. 领取
    http, ck = post(f"https://{domain}{CHECKIN_EP}", api_headers(cred))
    ck_info = summarize(ck) if isinstance(ck, dict) else {}
    if http in (401, 403):
        res["message"] = "领取时鉴权失败，请重新登录 WorkBuddy"
        log(f"✗ {res['message']}")
        return done(4)
    if str(ck_info.get("code")) == "10001" or "已签到" in ck_info.get("msg", ""):
        res.update(ok=True, action="skip", message="今天已领取（接口幂等）")
        log(f"→ {ck_info.get('msg') or '今天已签到'}")
        return done(0)
    if http != 200 or str(ck_info.get("code")) not in ("0", "None"):
        res["message"] = f"领取失败 HTTP {http}：{ck_info.get('msg') or str(ck)[:120]}"
        log(f"✗ {res['message']}")
        return done(3)

    gained = as_int(dig(ck, "today_credit", "credit", "points", "reward"))
    res.update(ok=True, action="checkin", message="领取成功",
               today_credit=gained if gained is not None else info["today_credit"],
               streak_days=ck_info.get("streak_days") or info["streak_days"])
    log(f"✓ 领取成功：+{res['today_credit']} 积分（连续 {res['streak_days']} 天）")

    # 3. 复核
    try:
        time.sleep(1)
        h2, st2 = post(f"https://{domain}{STATUS_EP}", api_headers(cred))
        i2 = summarize(st2) if h2 == 200 else {}
        if i2.get("checked_in"):
            res["total_credits"] = i2.get("total_credits")
            log(f"  复核通过，累计 {res['total_credits']} 积分")
    except Exception:
        pass

    return done(0)


if __name__ == "__main__":
    sys.exit(main())
