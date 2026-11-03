# -*- coding: utf-8 -*-
"""抓身份-PC.py —— 从 PC 微信内存里，把【当前活着的号】逐个抓成 PC 档身份文件

用法（建议【管理员】PowerShell）：
    python 抓身份-PC.py                  # 自动命名 身份-PC-1.txt / -2 / …
    python 抓身份-PC.py --name 招行号
    python 抓身份-PC.py --no-verify      # 跳过线上验证（不推荐）

它做四件事：
  ① 扫 PC 微信进程内存
  ② 把内存里【每一个号】分开认出来（按 cookieJar 段分组，不再混号）
  ③ 对每个号：用 PC(Windows) UA 真发一次 /chat/bag —— HTTP 200 才算这个号是活的
  ④ 活的存成 {"cookie": …, "ua": <Windows微信UA>}，直接给 AppleMonitorPro 用
"""
import ctypes, glob, io, json, os, re, sys, time

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
_ROOT = os.path.dirname(HERE)                      # 独立版根目录
_IDN = os.path.join(_ROOT, "身份")
IDENT_DIR = _IDN if os.path.isdir(_IDN) else HERE   # 身份文件目录
sys.path.insert(0, HERE)
import wx_cookie                                  # 复用它的进程枚举 / 内存读取 / 兜底

UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 "
         "Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows "
         "WindowsWechat/WMPF WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541d0c) XWEB/25510")
BAG = "https://web.mmap.apple.com/moe/p/wechat/chat/bag"
REF = "https://servicewechat.com/wxbc25a7df24ae0e6a/164/page-frame.html"
REAL_KEYS = ("as_dima", "as_pvi", "as_disa", "as_pcts", "as_loc", "rtsid", "dssid2", "dssf")

k32, MBI = wx_cookie.k32, wx_cookie.MBI
# 内存里 cookie 有两种存法，都要收：
#   A) cookie 块   ：as_dima=...; as_pvi=...; ...            ← 通常最完整（真正发出去的那个头）
#   B) JSON cookieJar：{"name":"as_pcts","value":"..."}      ← 有时缺 as_pcts
PAIR_RE = re.compile(rb'"name"\s*:\s*"([A-Za-z_][A-Za-z0-9_\-]{1,30})"\s*,\s*"value"\s*:\s*"([^"]{1,600})"')
BLOCK_RE = re.compile(rb"as_dima=[A-Za-z0-9+/=_.%|:@-]{20,400}"
                      rb"(?:;[ ]?[A-Za-z_][A-Za-z0-9_\-]{1,30}=[^;\x00\"\s]{0,600})*")
NOISE = ("Expires", "Path", "Domain", "Max-Age", "Secure", "HttpOnly", "SameSite", "Version")


def scan_mem():
    """返回 (cookie块列表, JSON对列表)"""
    blocks, pairs = [], []
    for pid in wx_cookie._pids():
        h = k32.OpenProcess(wx_cookie.PQI | wx_cookie.PVR, False, pid)
        if not h:
            continue
        addr, mbi = 0, MBI()
        try:
            while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                base, size = mbi.BaseAddress or 0, mbi.RegionSize
                if size <= 0:
                    break
                addr = base + size
                if mbi.State != wx_cookie.MEM_COMMIT or mbi.Protect in (0, wx_cookie.PAGE_NOACCESS) \
                        or (mbi.Protect & wx_cookie.PAGE_GUARD):
                    continue
                size = min(size, 8 * 1024 * 1024)
                buf = ctypes.create_string_buffer(size)
                got = ctypes.c_size_t(0)
                if not k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(got)):
                    continue
                d = buf.raw[:got.value]
                for m in BLOCK_RE.finditer(d):
                    blocks.append(m.group(0).decode("utf-8", "replace"))
                for m in PAIR_RE.finditer(d):
                    pairs.append((pid, base + m.start(), m.group(1).decode(),
                                  m.group(2).decode("utf-8", "replace")))
        finally:
            k32.CloseHandle(h)
    return blocks, pairs


def _jar_by_account(pairs, gap=6000):
    """JSON 对按"挨得近 = 同一个号"分组"""
    pairs = sorted(pairs, key=lambda x: (x[0], x[1]))
    groups, cur = [], []
    for p in pairs:
        if cur and (p[0] != cur[-1][0] or p[1] - cur[-1][1] > gap):
            groups.append(cur); cur = []
        cur.append(p)
    if cur:
        groups.append(cur)
    out = {}
    for g in groups:
        d = {}
        for _pid, _off, n, v in g:
            d[n] = v
        k = d.get("as_dima")
        if k and (k not in out or len(d) > len(out[k])):
            out[k] = d
    return out


def _parse_block(b):
    d = {}
    for kv in b.split(";"):
        kv = kv.strip()
        if "=" in kv:
            n, v = kv.split("=", 1)
            n, v = n.strip(), v.strip()
            if n and v and n not in NOISE:
                d[n] = v
    return d


def accounts():
    """把内存里的号分开认出来：cookie块 + JSON对 都收，同号取并集（键多的那份优先）"""
    blocks, pairs = scan_mem()
    best = {}
    for b in blocks:
        d = _parse_block(b)
        k = d.get("as_dima")
        if not k:
            continue
        if k not in best or len(d) > len(best[k]):
            best[k] = dict(d)
    jars = _jar_by_account(pairs)
    for k, d in jars.items():
        if k not in best:
            best[k] = dict(d)
        else:
            for n, v in d.items():          # 并集：块里已有的为准
                best[k].setdefault(n, v)
    return list(best.values())


def build(d):
    """补全缺的键（复用 wx_cookie 的兜底逻辑），返回 cookie 串"""
    best = dict(d)
    cap_ck, file_ck = wx_cookie._fallbacks()
    for k in wx_cookie.USER_ORDER:
        if k in best:
            continue
        if k in cap_ck:
            best[k] = cap_ck[k]
        elif k in file_ck:
            best[k] = file_ck[k]
        elif k in wx_cookie.STATIC_EXTRA:
            best[k] = wx_cookie.STATIC_EXTRA[k]
    if "as_pvi" not in best:
        m = re.search(r"\|([0-9a-f]{32,64})\|", best.get("as_dima", ""))
        if m:
            best["as_pvi"] = m.group(1)
    if not (best.get("as_dima") and best.get("as_pcts")):
        return ""
    # ★ 全都要：原来只导出白名单里的 11 个键，会把登录态相关的 cookie（如 myacinfo/acn01）丢掉
    keys = [k for k in wx_cookie.USER_ORDER if k in best] + \
           [k for k in best if k not in wx_cookie.USER_ORDER]
    return "; ".join("%s=%s" % (k, best[k]) for k in keys) + ";"


def verify(ck, ua=UA_PC):
    try:
        from curl_cffi import requests as creq
    except Exception as e:
        return None, "没装 curl_cffi：%s" % e
    try:
        s = creq.Session(impersonate="chrome131")
        r = s.get(BAG, headers={"User-Agent": ua, "Accept": "*/*", "Referer": REF, "Cookie": ck}, timeout=20)
        return r.status_code, "%d 字节" % len(r.content)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def next_name():
    i = 1
    while os.path.exists(os.path.join(IDENT_DIR, "身份-PC-%d.txt" % i)):
        i += 1
    return "身份-PC-%d.txt" % i


def roster():
    """列出已抓到的所有 PC 档身份（按 as_dima 去重），并逐个真发一次验证"""
    files = sorted(glob.glob(os.path.join(IDENT_DIR, "身份-PC*.txt")), key=os.path.getmtime)
    if not files:
        print("还没有任何 身份-PC*.txt"); return 0
    rows, seen = [], {}
    for f in files:
        try:
            j = json.loads(io.open(f, encoding="utf-8").read())
        except Exception:
            continue
        ck = j.get("cookie") or ""
        if "as_dima=" not in ck:
            continue
        fp = ck.split("as_dima=")[1].split(";")[0][:16]
        rows.append((f, fp, ck, j.get("ua") or "", j.get("note") or ""))
    print("=" * 92)
    print("%-20s %-18s %-6s %-10s %s" % ("文件", "指纹(as_dima)", "键数", "验证", "备注"))
    uniq = {}
    for f, fp, ck, ua, note in rows:
        keys = [x.split("=")[0] for x in ck.split("; ") if "=" in x]
        st, _info = ("未验证", "") if not note else (note.split("验证=")[1].split("｜")[0] if "验证=" in note else "未验证"), ""
        if note and "验证=" not in note:
            st = "未验证"
        mark = ""
        if fp in uniq:
            mark = "  ← 重复（和 %s 同一个号）" % os.path.basename(uniq[fp])
        else:
            uniq[fp] = f
        print("%-20s %-18s %-6d %-10s %s%s" % (os.path.basename(f), fp + "…", len(keys), st, note[:44], mark))
    print("-" * 92)
    print("不同号个数: %d" % len(uniq))
    for fp, f in uniq.items():
        print("   %s…  →  %s" % (fp, os.path.basename(f)))
    return len(uniq)


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__); return 0
    if "--list" in sys.argv:
        return roster()
    do_verify = "--no-verify" not in sys.argv
    base_name = sys.argv[sys.argv.index("--name") + 1] if "--name" in sys.argv else ""
    if base_name and not base_name.lower().endswith(".txt"):
        base_name += ".txt"

    print("=" * 78)
    print("① 找 PC 微信进程…")
    pids = wx_cookie._pids()
    print("   找到 %d 个进程: %s" % (len(pids), pids))
    if not pids:
        print("   ✗ 没找到 Weixin.exe / WeChatAppEx.exe —— 微信 PC 端没开")
        print("   → 打开微信 PC 端登录要抓的号，并在里面打开一次苹果小程序（逛到购物袋）")
        return 2

    print("② 把内存里的号分开认…")
    accs = accounts()
    print("   认出 %d 个号" % len(accs))
    if not accs:
        print("   ✗ 内存里没有 cookieJar —— 小程序没打开过")
        print("   → 在【PC 微信】里打开苹果小程序，逛到购物袋页，再重跑")
        return 4
    for d in accs:
        print("      as_dima=%s…  键 %d 个 %s" % (d["as_dima"][:16], len(d),
                                                [k for k in REAL_KEYS if k in d]))

    ok_list, bad_list = [], []
    for i, d in enumerate(accs, 1):
        ck = build(d)
        if not ck:
            print("   #%d 键不齐（缺 as_dima/as_pcts）→ 跳过" % i); continue
        print("-" * 78)
        print("③ 号 #%d  as_dima=%s…" % (i, d["as_dima"][:16]))
        if not do_verify:
            ok_list.append((d, ck, "未验证")); continue
        print("④ 线上验证（PC 微信 UA 请求 /chat/bag）…")
        st, info = verify(ck)
        print("   → HTTP %s（%s）" % (st, info))
        if st == 200:
            print("   ✓ 活的：这份 cookie + PC 档 UA 通")
            ok_list.append((d, ck, "HTTP 200"))
        else:
            print("   ✗ 没过（%s）→ 这个号现在不能用/不是当前登录的号" % st)
            bad_list.append((d, st))

    if not ok_list:
        print("=" * 78)
        print("✗ 没有一个号通过验证。常见原因：")
        print("   · 541 = Apple 风控（等一会儿再试）")
        print("   · 403 = 没在小程序里真的打开过页面（去逛一次购物袋）")
        return 6

    # ★ 去重：同一个号（as_dima 前 16 位）已经存过就不再存，免得你以为有几个号
    existing = {}
    for f in glob.glob(os.path.join(IDENT_DIR, "身份-PC*.txt")):
        try:
            c = json.loads(io.open(f, encoding="utf-8").read()).get("cookie") or ""
            if "as_dima=" in c:
                existing[c.split("as_dima=")[1].split(";")[0][:16]] = os.path.basename(f)
        except Exception:
            pass

    print("=" * 78)
    for n, (d, ck, v) in enumerate(ok_list):
        fp = d["as_dima"][:16]
        if fp in existing and "--force" not in sys.argv:
            print("· 跳过 %s…：这个号已经存过了 -> %s" % (fp, existing[fp]))
            continue
        if base_name:
            nm = base_name if len(ok_list) == 1 else base_name.replace(".txt", "-%d.txt" % (n + 1))
        else:
            nm = "身份-PC-%s.txt" % d["as_dima"][:8]      # ★ 带指纹命名，永不撞名
        out = os.path.join(IDENT_DIR, nm)
        have = [x.split("=", 1)[0] for x in ck.split("; ") if "=" in x]
        miss = [k for k in REAL_KEYS if k not in have]
        j = {"cookie": ck, "ua": UA_PC,
             "note": "PC档（Windows微信）｜抓于 %s｜验证=%s｜缺键=%s"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), v, "、".join(miss) or "无")}
        io.open(out, "w", encoding="utf-8").write(json.dumps(j, ensure_ascii=False, indent=1))
        print("✓ 已存 %s   （as_dima=%s…  验证 %s  缺键 %s）"
              % (os.path.basename(out), d["as_dima"][:16], v, "、".join(miss) or "无"))
    print("→ 在 AppleMonitorPro「抢购任务」里，身份文件选这几份；卡片标题会显示「PC 档（Windows 微信）」")
    if bad_list:
        print("（另有 %d 个号没过验证，没存：%s）"
              % (len(bad_list), "、".join("%s…→HTTP %s" % (d["as_dima"][:12], st) for d, st in bad_list)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
