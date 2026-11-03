#!/usr/bin/env python3
"""凭据桥：从运行中的 WorkBuddy 客户端进程内存里取出一个可用的 access token。

背景：客户端 5.9.3 起把登录态做了 at-rest 加密（keyblob + 构建期注入的 protector secret），
      磁盘上无法还原明文。但客户端运行时必然持有明文令牌，本程序只读进程内存，
      不写文件、不上传、不修改客户端。

产出：把可用令牌写入同目录 credentials.json（0600），供 checkin.py 使用。

退出码：0 拿到可用令牌 | 2 客户端未运行 | 3 扫描到令牌但均不可用 | 4 未扫描到令牌
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CRED_PATH = os.path.join(HERE, "credentials.json")
AUTH_INFO = os.path.expandvars(r"%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info")
STATUS_EP = "/v2/billing/meter/checkin-activity-status"

PROCESS_NAMES = ("WorkBuddy", "workbuddy")
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}   # READONLY/RW/WRITECOPY/EXEC_READ/EXEC_RW/EXEC_WRITECOPY
CHUNK = 8 * 1024 * 1024
OVERLAP = 8192
MAX_REGION = 256 * 1024 * 1024

JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{8,}")
# V8 可能用两字节字符串存令牌，需同时匹配 UTF-16LE 形态
JWT_UTF16 = b"e\x00y\x00J\x00h\x00b\x00G\x00c\x00i\x00O\x00i\x00"
JWT_HEAD = b"eyJhbGciOi"
TOKEN_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
RT_RE = re.compile(rb"refreshToken\\?\"?\s*[:=]\s*\\?\"([A-Za-z0-9_.\-]{40,})")


def extract_jwts(chunk: bytes, out: set) -> None:
    """从一段内存里提取 JWT，兼容单字节与 UTF-16LE 两种存储形态。"""
    for head, is16 in ((JWT_HEAD, False), (JWT_UTF16, True)):
        start = 0
        while True:
            i = chunk.find(head, start)
            if i < 0:
                break
            seg = chunk[i:i + 2600]
            txt = seg.decode("utf-16-le", "ignore") if is16 else seg.decode("latin-1", "ignore")
            m = TOKEN_SHAPE.match(txt)
            if m:
                out.add(m.group(0))
            start = i + 1

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.VirtualQueryEx.restype = ctypes.c_size_t


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD), ("__a1", wt.DWORD),
                ("RegionSize", ctypes.c_ulonglong), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD), ("__a2", wt.DWORD)]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def read_meta():
    domain, uid = "www.workbuddy.cn", ""
    try:
        d = json.load(open(AUTH_INFO, encoding="utf-8"))
        domain = str((d.get("auth") or {}).get("domain") or domain).strip() or domain
        uid = str((d.get("account") or {}).get("uid") or "")
    except Exception:
        pass
    return domain, uid


def pids_of(names) -> list[int]:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -in @(%s) } "
             "| Select-Object -ExpandProperty Id" % ",".join(f"'{n}'" for n in names)],
            capture_output=True, text=True, timeout=30).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def scan_pid(pid: int, found_jwt: set, found_rt: set) -> int:
    """扫描单个进程，返回读到的字节数；-1 表示无法打开进程。"""
    h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return -1
    scanned = 0
    try:
        addr, mbi = 0, MBI()
        while kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            base, size = mbi.BaseAddress, mbi.RegionSize
            if not size:
                break
            if (mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in READABLE
                    and size <= MAX_REGION):
                off = 0
                while off < size:
                    n = min(CHUNK, size - off)
                    buf = ctypes.create_string_buffer(n)
                    got = ctypes.c_size_t(0)
                    ok = kernel32.ReadProcessMemory(
                        h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got))
                    if ok and got.value:
                        chunk = buf.raw[:got.value]
                        extract_jwts(chunk, found_jwt)
                        for m in RT_RE.finditer(chunk):
                            found_rt.add(m.group(1).decode("ascii", "ignore"))
                        scanned += got.value
                    off += n - OVERLAP if n == CHUNK else n
            addr = base + size
    finally:
        kernel32.CloseHandle(h)
    return scanned


def claims_ok(tok: str):
    """解 payload 看是否过期，返回 (uid/sub, exp) 或 None。"""
    try:
        p = tok.split(".")[1]
        obj = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return None
    exp = obj.get("exp")
    if isinstance(exp, (int, float)) and exp < datetime.now().timestamp():
        return None
    return obj


def probe(tok: str, domain: str, uid: str) -> bool:
    req = urllib.request.Request(
        f"https://{domain}{STATUS_EP}", data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {tok}", "X-User-Id": uid, "X-Domain": domain,
                 "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status == 200 and '"code":401' not in body.replace(" ", "")
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def main() -> int:
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.pop(k, None)

    domain, uid = read_meta()
    pids = pids_of(PROCESS_NAMES)
    if not pids:
        log("✗ 未发现运行中的 WorkBuddy 客户端，请先启动客户端后重试")
        return 2
    log(f"发现 {len(pids)} 个客户端进程：{pids}（domain={domain}）")

    jwts, rts = set(), set()
    for pid in pids:
        before = len(jwts)
        n = scan_pid(pid, jwts, rts)
        if n < 0:
            log(f"  pid {pid}: 无法打开（权限不足，跳过）")
        else:
            log(f"  pid {pid}: 读取 {n/1048576:.1f} MB，新增 {len(jwts) - before} 个候选，累计 {len(jwts)}")

    if not jwts:
        log("✗ 内存中未扫描到 JWT 令牌（客户端可能刚启动，页面尚未加载登录态）")
        return 4

    # 先按长度降序（WorkBuddy 登录令牌约 1200~1400 字符，网关令牌通常 <400），再过滤过期
    cands = []
    for t in sorted(jwts, key=len, reverse=True):
        c = claims_ok(t)
        if c:
            cands.append((t, c))
    log(f"未过期候选：{len(cands)} / {len(jwts)}（按长度降序尝试）")

    for tok, cl in cands:
        iss = str(cl.get("iss", ""))
        log(f"  试一个候选：iss={iss[:40]} len={len(tok)}")
        if probe(tok, domain, uid):
            data = {"accessToken": tok, "refreshToken": "", "domain": domain, "uid": uid,
                    "expiresAt": int(float(cl.get("exp", 0)) * 1000)}
            with open(CRED_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.chmod(CRED_PATH, 0o600)
            log(f"✓ 已取到可用令牌并写入 credentials.json（iss={iss[:40]}，exp={datetime.fromtimestamp(float(cl.get('exp', 0))):%Y-%m-%d}）")
            if rts:
                log(f"  附带发现 {len(rts)} 个疑似 refreshToken（暂不使用）")
            return 0

    log("✗ 扫描到的令牌全部不可用（可能属于其他服务）")
    return 3


if __name__ == "__main__":
    sys.exit(main())
