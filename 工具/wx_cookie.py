# wx_cookie.py - 取 web.mmap.apple.com 的活 cookie
# 优先从「微信 app 自己的 cookieJar」（JS 状态：{"name":"as_pcts","value":"..."}）取权威值，
# 取不到再退回宽松匹配（内存 name=value）+ 抓包/文件兜底。
import ctypes, ctypes.wintypes as wt, json, os, re, subprocess, sys

USER_ORDER = ["as_dima", "as_pvi", "as_disa", "rdcStatus", "as_loc", "as_pcts", "rtsid", "as_ltn_wechat", "dssid2", "dssf", "as_dc"]
NEED = ("as_dima", "as_pcts")     # ★ 实测：真实请求头里从来没有 as_ltn_wechat
                                  #   （对照：真实 /chat/bag 只带 as_dima/as_pvi/as_disa/as_pcts/as_loc/rtsid/dssid2/dssf）
STATIC_EXTRA = {"rdcStatus": "1", "dssf": "1", "as_dc": "ucp4"}
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(HERE)                      # 独立版根目录
_IDN = os.path.join(_ROOT, "身份")
IDENT_DIR = _IDN if os.path.isdir(_IDN) else HERE   # 身份文件目录
FALLBACK_FILE = os.path.join(HERE, "moe_cookie.txt")
CAPTURE = os.path.join(HERE, "capture_out", "capture.jsonl")

JAR_RE = re.compile(rb'"name"\s*:\s*"(as_pcts|as_dima|as_disa|as_ltn_wechat|dssid2|rtsid|as_loc|as_pvi)"\s*,\s*"value"\s*:\s*"([^"]{4,400})"')
LOOSE_RE = re.compile(rb"(as_dima|as_pvi|as_disa|as_pcts|as_ltn_wechat|rtsid|dssid2|dssf|as_dc|as_loc|rdcStatus)=([A-Za-z0-9+/=_.%|:@-]{4,400})")

k32 = ctypes.windll.kernel32
PQI, PVR = 0x0400, 0x0010
MEM_COMMIT, PAGE_NOACCESS, PAGE_GUARD = 0x1000, 0x01, 0x100


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


k32.OpenProcess.restype = wt.HANDLE
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


def _pids():
    out = []
    for exe in ("WeChatAppEx.exe", "Weixin.exe"):
        try:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + exe, "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=20)
            for line in r.stdout.splitlines():
                p = [x.strip('"') for x in line.split('","')]
                if len(p) >= 2 and p[1].strip().isdigit():
                    out.append(int(p[1].strip()))
        except Exception:
            pass
    return out


def _scan(regexes, need_marker=(b"as_", )):
    """扫内存：返回 {name: value}（同名后见覆盖 = 取较新的那份）"""
    out = {}
    for pid in _pids():
        h = k32.OpenProcess(PQI | PVR, False, pid)
        if not h:
            continue
        addr, mbi = 0, MBI()
        try:
            while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                base, size = mbi.BaseAddress or 0, mbi.RegionSize
                if size <= 0:
                    break
                addr = base + size
                if mbi.State != MEM_COMMIT or mbi.Protect in (0, PAGE_NOACCESS) or (mbi.Protect & PAGE_GUARD):
                    continue
                size = min(size, 8 * 1024 * 1024)
                buf = ctypes.create_string_buffer(size)
                got = ctypes.c_size_t(0)
                if not k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(got)):
                    continue
                d = buf.raw[:got.value]
                if not any(m in d for m in need_marker):
                    continue
                for rx in regexes:
                    for m in rx.finditer(d):
                        name = m.group(1).decode()
                        val = m.group(2).decode("utf-8", "replace").rstrip(";, \x00\"")
                        if len(val) >= 4:
                            out[name] = val
        finally:
            k32.CloseHandle(h)
    return out


def _parse_cookie_header(txt, out):
    for pair in (txt or "").split("; "):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() and k.strip() not in out:
                out[k.strip()] = v.strip()


def _fallbacks():
    cap_ck, file_ck = {}, {}
    try:
        if os.path.exists(CAPTURE):
            with open(CAPTURE, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "as_ltn_wechat=" not in line or '"cookie"' not in line.lower():
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    for k, v in (r.get("req_headers") or {}).items():
                        if k.lower() == "cookie" and "as_ltn_wechat=" in v:
                            cap_ck = {}
                            _parse_cookie_header(v, cap_ck)
    except Exception:
        pass
    try:
        if os.path.exists(FALLBACK_FILE):
            _parse_cookie_header(open(FALLBACK_FILE, encoding="utf-8").read(), file_ck)
    except Exception:
        pass
    return cap_ck, file_ck


def get_live_cookie(verbose=False):
    jar = _scan([JAR_RE])          # 权威：app 的 cookieJar
    src = "jar"
    best = dict(jar)
    if not all(k in best for k in NEED):
        loose = _scan([LOOSE_RE])
        for k, v in loose.items():
            if k not in best or (k != "as_dima" and len(v) > len(best[k])):
                best[k] = v
        src = "jar+loose" if jar else "loose"
    cap_ck, file_ck = _fallbacks()
    for k in USER_ORDER:
        if k in best:
            continue
        if k in cap_ck:
            best[k] = cap_ck[k]
        elif k in file_ck:
            best[k] = file_ck[k]
        elif k in STATIC_EXTRA:
            best[k] = STATIC_EXTRA[k]
    if "as_pvi" not in best:
        m = re.search(r"\|([0-9a-f]{32,64})\|", best.get("as_dima", ""))
        if m:
            best["as_pvi"] = m.group(1)
    if verbose:
        print("[wx_cookie] 来源=%s  键=%s" % (src, ",".join("%s(%d)" % (k, len(best[k])) for k in USER_ORDER if k in best)))
    if not all(k in best for k in NEED):
        return None
    # ★ 全都要：别只导出白名单，登录态相关的 cookie 也要带上
    keys = [k for k in USER_ORDER if k in best] + [k for k in best if k not in USER_ORDER]
    return "; ".join("%s=%s" % (k, best[k]) for k in keys) + ";"


if __name__ == "__main__":
    c = get_live_cookie(verbose=True)
    print(("OK %d 字节" % len(c)) if c else "FAILED")
    if c:
        print(",".join(p.split("=", 1)[0] for p in c.split("; ") if "=" in p))


CK_BLOCK_RE = re.compile(r"(as_dima=[^;]+(?:; [A-Za-z_][A-Za-z0-9_\-]{1,30}=[^;]*)*)")
STK_BLOCK_RE = re.compile(r"[xX][-_]aos[-_]stk[^A-Za-z0-9]{0,12}([A-Za-z0-9_\-]{20,40})")


def get_exact_request():
    """从 app 最近那条请求的头块里，取【cookie 原文 + 配对 stk】（照抄 = 与 app 完全一致）"""
    out = []
    for pid in _pids():
        h = k32.OpenProcess(PQI | PVR, False, pid)
        if not h:
            continue
        addr, mbi = 0, MBI()
        try:
            while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                base, size = mbi.BaseAddress or 0, mbi.RegionSize
                if size <= 0:
                    break
                addr = base + size
                if mbi.State != MEM_COMMIT or mbi.Protect in (0, PAGE_NOACCESS) or (mbi.Protect & PAGE_GUARD):
                    continue
                size = min(size, 8 * 1024 * 1024)
                buf = ctypes.create_string_buffer(size)
                got = ctypes.c_size_t(0)
                if not k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(got)):
                    continue
                d = buf.raw[:got.value]
                if b"as_pcts=" not in d or b"aos-stk" not in d:
                    continue
                txt = d.decode("utf-8", "replace")
                for m in CK_BLOCK_RE.finditer(txt):
                    ck = m.group(1)
                    if "as_ltn_wechat=" not in ck or "as_pcts=" not in ck:
                        continue
                    seg = txt[max(0, m.start() - 2000):m.end() + 2000]
                    for sm in STK_BLOCK_RE.finditer(seg):
                        out.append((ck, sm.group(1)))
        finally:
            k32.CloseHandle(h)
    return out
