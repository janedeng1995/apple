# -*- coding: utf-8 -*-
"""qrpay.py —— 成单后凭「订单链接」抓微信支付二维码 + 轮询支付状态。

移植自 二维码支付demo/qrpay_demo.py，改动：
  · print → log 回调；time.sleep → 受 stop_evt 控制的可中断等待
  · 可选走代理（proxy_provider，每次请求现取，和主软件一致）
  · 可选带一份 PC 浏览器 cookie（更稳，避开 541；不给也能跑，订单链接本身是访客授权）
  · 二维码 PNG 存盘 + 自动打开；轮询状态通过 on_status 回调抛出
注意：这套走的是 www.apple.com.cn / secureN（PC 浏览器域），和主软件下单用的
      微信小程序域（web.mmap.apple.com）cookie 不通用，所以这里独立建会话。
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import time
import urllib.parse
from pathlib import Path

try:
    from curl_cffi import requests as creq
except ImportError:  # 让主程序能 import，但真用时会报错
    creq = None

IMPERSONATE = "chrome150"
UA_DEFAULT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

GUEST_URL_RE = re.compile(
    r"(https://[A-Za-z0-9.-]*apple\.com\.cn)/wechat/shop/order/guest/([^/?#]+)/([^/?#]+)")

PAID_HINTS = ["支付成功", "已支付", "已完成付款", "付款成功", "感谢你购买", "感谢您的购买",
              "感谢您购买", "订单已完成", "payment received", "payment complete",
              "thank you for your order"]
EXPIRED_HINTS = ["二维码已过期", "二维码过期", "二维码已失效", "二维码失效", "已过期",
                 "已失效", "expired", "重新获取", "请刷新"]

INFO_KEYS = ["orderNumber", "orderPlacedDate", "orderStatus", "statusDescription",
             "paymentStatus", "paymentType", "paymentMethodPaymentTypeName",
             "totalAmount", "payNowText", "paymentTimeExpiry",
             "paymentTimeToExpiryEpoch", "isWeChatOrder"]

RETRY_STATUS = (403, 541)


def _ui_fetch_id() -> str:
    abc = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choices(abc, k=10)) + "-" + "".join(random.choices(abc, k=8))
def _harvest(obj, keys, found=None, depth=0):
    if found is None:
        found = {}
    if depth > 10:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and k not in found and isinstance(v, (str, int, float, bool)):
                found[k] = v
            else:
                _harvest(v, keys, found, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _harvest(v, keys, found, depth + 1)
    return found


def _fmt_epoch(v):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(v)))
    except Exception:
        return str(v)


def _open_file(p: str):
    try:
        if os.name == "nt":
            os.startfile(p)
        elif sys.platform == "darwin":
            os.system('open "%s"' % p)
        else:
            os.system('xdg-open "%s"' % p)
    except Exception:
        pass


import sys  # noqa: E402


class GuestPay:
    """访客支付会话：维护 cookie 罐 + x-aos-stk（stk 每步都会变，必须跟着刷新）。"""

    def __init__(self, cookie="", ua=UA_DEFAULT, stk="", debug=False, outdir=".",
                 log=None, stop_evt=None, proxy_provider=None):
        if creq is None:
            raise RuntimeError("没装 curl_cffi：pip install curl_cffi")
        self.cookies = {}
        for part in (cookie or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    self.cookies[k] = v
        self.ua = ua or UA_DEFAULT
        self.stk = stk or ""
        self.debug = debug
        self.outdir = Path(outdir)
        self.n = 0
        self.log = log or (lambda m: None)
        self.stop_evt = stop_evt
        self.proxy_provider = proxy_provider
        self.s = creq.Session(impersonate=IMPERSONATE)

    # ---------------- cookie / stk ----------------
    def cookie_header(self) -> str:
        return "; ".join("%s=%s" % (k, v) for k, v in self.cookies.items())

    def _absorb(self, r):
        try:
            for sc in (r.headers.get_list("set-cookie") or []):
                kv = sc.split(";", 1)[0]
                if "=" not in kv:
                    continue
                k, v = kv.split("=", 1)
                k, v = k.strip(), v.strip()
                low = sc.lower()
                if (not v) or ("max-age=0" in low) or ("max-age=-1" in low):
                    self.cookies.pop(k, None)
                else:
                    self.cookies[k] = v
        except Exception:
            pass
        j = None
        try:
            j = r.json()
        except Exception:
            j = None
        if isinstance(j, dict):
            meta = (j.get("body") or {}).get("meta") or {}
            stk = ((meta.get("h") or {}).get("x-aos-stk")
                   or (meta.get("mandatoryAdditionalHeaders") or {}).get("x-aos-stk"))
            if stk:
                self.stk = stk
        if not self.stk:
            # ★★ graviton 的 HTML 页面里也嶝贴着 x-aos-stk（浏览器就是从这里取初始值的）。
            #   只解析 JSON 响应会把它漏掉 → 空 stk 打 payNow 必 403/541。
            #   （2026-09-23 实测：日志里 `当前 stk=-` 就是这个漏派的蹱彩）
            try:
                _m = re.search(r'"x-aos-stk"\s*:\s*"([A-Za-z0-9_\-]{10,})"',
                               getattr(r, "text", "") or "")
                if _m:
                    self.stk = _m.group(1)
            except Exception:
                pass
        return j

    def _px(self):
        if self.proxy_provider is None:
            return None
        try:
            return self.proxy_provider()
        except Exception:
            return None

    def _base_headers(self, referer=""):
        h = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "sec-ch-ua": '"Microsoft Edge";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "syntax": "graviton",
            "modelversion": "v2",
            "user-agent": self.ua,
            "x-requested-with": "Fetch",
            "x-aos-ui-fetch-call-1": _ui_fetch_id(),
            "Cookie": self.cookie_header(),
        }
        if referer:
            h["referer"] = referer
            m = re.match(r"(https://[^/]+)", referer)
            if m:
                h["origin"] = m.group(1)
        if self.stk:
            h["x-aos-stk"] = self.stk
        return h

    def _dump(self, method, url, r):
        if not self.debug:
            return
        d = self.outdir / "debug"
        d.mkdir(parents=True, exist_ok=True)
        name = "%02d_%s.txt" % (self.n, re.sub(r"[^A-Za-z0-9]+", "_", url)[:60])
        try:
            body = r.text
        except Exception:
            body = "<binary>"
        (d / name).write_text("URL: %s %s\nHTTP: %s\nSTK: %s\n\n%s"
                              % (method, url, r.status_code, self.stk, body),
                             encoding="utf-8", errors="replace")

    def _sleep(self, secs):
        if self.stop_evt is not None:
            return self.stop_evt.wait(secs)     # True=被叫停
        time.sleep(secs)
        return False

    def _retry_wait(self, i):
        wait = 4.0 + 2.0 * i + random.random() * 2.0
        self.log("   · 被苹果风控/限流 → %.1f 秒后重试" % wait)
        return self._sleep(wait)

    def post(self, url, model_page, referer="", data=None, timeout=30, tries=4):
        h = self._base_headers(referer)
        if model_page:
            h["x-aos-model-page"] = model_page
        h["content-type"] = "application/x-www-form-urlencoded"
        px = self._px()
        kw = {"proxies": {"http": px, "https": px}} if px else {}
        r = j = None
        for i in range(max(1, tries)):
            r = self.s.request("POST", url, headers=h,
                               data=(data if data is not None else ""),
                               timeout=timeout, allow_redirects=False, **kw)
            self.n += 1
            j = self._absorb(r)
            self._dump("POST", url, r)
            if r.status_code not in RETRY_STATUS or i == tries - 1:
                break
            if self._retry_wait(i):
                break
        return r, j

    def get(self, url, browser=False, timeout=30, tries=3):
        if browser:
            h = {
                "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                           "image/avif,image/webp,*/*;q=0.8"),
                "accept-language": "zh-CN,zh;q=0.9",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
                "user-agent": self.ua,
                "Cookie": self.cookie_header(),
            }
        else:
            h = self._base_headers(referer=url)
        px = self._px()
        kw = {"proxies": {"http": px, "https": px}} if px else {}
        r = j = None
        for i in range(max(1, tries)):
            r = self.s.request("GET", url, headers=h, timeout=timeout,
                               allow_redirects=True, **kw)
            self.n += 1
            j = self._absorb(r)
            self._dump("GET", url, r)
            if r.status_code not in RETRY_STATUS or i == tries - 1:
                break
            if self._retry_wait(i):
                break
        return r, j


def resolve_link(gp: GuestPay, link: str):
    link = (link or "").strip()
    m = GUEST_URL_RE.search(link)
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(0)
    if not link:
        raise RuntimeError("没有订单链接")
    gp.log("① 打开订单链接，等它跳到访客订单页 …")
    r, _ = gp.get(link, browser=True)
    if r.status_code in RETRY_STATUS:
        raise RuntimeError("打开订单链接被苹果风控（HTTP %s）。等几十秒重试，"
                           "或自己在浏览器打开链接、把地址栏的订单页链接贴进来。" % r.status_code)
    candidates = [getattr(r, "url", "")] + [getattr(h, "url", "") for h in (getattr(r, "history", None) or [])]
    for u in candidates:
        m = GUEST_URL_RE.search(u or "")
        if m:
            gp.log("   [OK] 落到访客订单页：%s" % u)
            return m.group(1), m.group(2), m.group(3), u
    m = GUEST_URL_RE.search(getattr(r, "text", "") or "")
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(0)
    raise RuntimeError("没能解析到访客订单页（最终URL: %s）。请直接把浏览器地址栏的订单页链接贴进来。"
                       % getattr(r, "url", "?"))


def step_fetch_order(gp: GuestPay, base: str, order: str, token: str):
    ref = "%s/wechat/shop/order/guest/%s/%s?e=true" % (base, order, token)
    url = "%s/wechat/shop/orderx/guestx/%s/%s?e=true&_a=fetchOrder&_m=guestOrderSpinner" % (base, order, token)
    r, j = gp.post(url, "OrderStatusGuestSummary", referer=ref)
    info = _harvest((j or {}).get("body") or {}, INFO_KEYS)
    return r, j, info


def step_pay_now(gp: GuestPay, base: str, order: str, token: str):
    ref = "%s/wechat/shop/order/guest/%s/%s?e=true" % (base, order, token)
    url = ("%s/wechat/shop/order/guestx/%s/%s?e=true&_a=payNow"
           "&_m=orderDetail.orderHeader.payNow" % (base, order, token))
    r, j = gp.post(url, "OrderStatusGuestSummary", referer=ref)
    head = (j or {}).get("head") or {}
    # ★ status 可能是 int 302 也可能是字符串 "302"（session._next_url 早就用 str() 防过）：
    #   严格 == 302 会把一个完全成功、带着 url+sk 的响应当成失败扔掉。
    if str(head.get("status")) in ("301", "302", "303", "307") and isinstance(head.get("data"), dict):
        d = head["data"]
        args = dict(d.get("args") or {})
        # ★★ 关键：payNow 的 302 里下发的 _st 就是下一页（qrcodepay / checkstatus）
        #   要用的 x-aos-stk。不接上，后面取码/轮询带的是上一页的老 stk 甚至空 stk → 541。
        #   （2026-09-23 真机 curl 实证：checkstatus 的 x-aos-stk 与 payNow 302 的 _st 一致）
        _st = args.get("_st") or ""
        if _st:
            gp.stk = _st
        gp.log("③ payNow → %s ｜ ordernumber=%s ｜ sk=%s… ｜ stk=%s…"
               % (d.get("url") or "?", str(args.get("ordernumber") or "?")[:14],
                  str(args.get("sk") or "?")[:16], (gp.stk or "-")[:12]))
        return (d.get("url") or ""), args, j
    # 没拿到跳转 → 把服务端原话带回去，别只说"被限流"让人猜
    _dbg = ""
    try:
        _dbg = json.dumps(j, ensure_ascii=False)[:600] if j is not None else ""
    except Exception:
        _dbg = ""
    gp.log("③ payNow 没拿到跳转 ｜ HTTP %s ｜ 原文：%s"
           % (getattr(r, "status_code", "?"),
              _dbg or "(非 JSON 响应；完整响应已落盘到 支付二维码\\debug\\)"))
    return "", {}, j


def _qr_png_from_json(j):
    d = (((j or {}).get("body") or {}).get("qrCodePayNow") or {}).get("d") or {}
    src = ((d.get("qrCodePaymentImageData") or {}).get("src")) or ""
    if "base64," in src:
        try:
            return base64.b64decode(src.split("base64,", 1)[1]), d
        except Exception:
            return None, d
    return None, d


def _qr_png_from_text(text):
    m = re.search(r"data:image/png;base64,([A-Za-z0-9+/=\s]+)", text or "")
    if m:
        try:
            return base64.b64decode(re.sub(r"\s+", "", m.group(1)))
        except Exception:
            return None
    return None


def _poll_url_from_json(j, base, order, sk):
    try:
        u = j["body"]["qrCodePaymentStatusPolling"]["a"]["checkstatus"]["url"]
        if u:
            return u.replace("\\/", "/").replace("&amp;", "&")
    except Exception:
        pass
    return ("%s/wechat/shop/paynow/qrcodepayx/qrcodepaystatus?ordernumber=%s&sk=%s"
            "&_a=checkstatus&_m=qrCodePayNow.qrCodePaymentStatusPolling"
            % (base, urllib.parse.quote(order, safe=""),
               urllib.parse.quote(sk or "", safe="/")))


def _next_url(r, txt: str):
    """服务端的“跳转”两种：HTTP 302 头；或 body 里 head.data.{status:302,url}。
    返回 (url, 是否跳转)。"""
    try:
        loc = (r.headers.get("Location") or r.headers.get("location") or "") if r is not None else ""
    except Exception:
        loc = ""
    if loc:
        return loc, True
    try:
        _j = json.loads(txt or "{}")
        _head = (_j.get("head") or {})
        _d = (_head.get("data") or {})
        # ★ 两种子都支撑：真机里是 head.status=302 + head.data.url；
        #   也有接口把 status 塞进 head.data（session._next_url 就是按后者写的）。
        _st = _head.get("status", _d.get("status"))
        if str(_st) in ("301", "302", "303", "307") and _d.get("url"):
            return _d["url"], True
    except Exception:
        pass
    return "", False


def _dump_resp(r, tag: str, gp) -> str:
    """把一发响应的全文写盘，返回文件路径。
    抱不到二维码时这个文件就是唯一能说清楚服务端到底回了什么的话格。"""
    try:
        d = os.path.join(str(gp.outdir), "debug")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%02d_%s.txt" % (gp.n, tag))
        body = getattr(r, "text", "") or ""
        with open(p, "w", encoding="utf-8", errors="replace") as f:
            f.write("HTTP: %s\nSTK: %s\nURL: %s\n\n%s"
                    % (getattr(r, "status_code", "?"), gp.stk, tag, body))
        return p
    except Exception:
        return ""


def _diag(r, tag: str, gp) -> None:
    """抱不到图时把这一发的真实返回打出来，别让人拿着空洞猜。"""
    t = getattr(r, "text", "") or ""
    brief = ""
    try:
        j = json.loads(t)
        brief = json.dumps({"head": j.get("head"), "body_keys": list((j.get("body") or {}).keys())},
                           ensure_ascii=False)[:220]
    except Exception:
        brief = t[:200].replace("\n", " ").replace("\r", " ")
    gp.log("   ✗ %s 没拿到图 │ HTTP %s │ %d 字节 │ %s"
           % (tag, getattr(r, "status_code", "?"), len(t), brief or "(空)"))
    p = _dump_resp(r, tag, gp)
    if p:
        gp.log("     → 完整响应已存盘：%s" % p)


def step_get_qr(gp: GuestPay, pay_url: str, args: dict, base: str, order: str):
    """④ 拿二维码。

    ★★ 2026-09-23 真机复测：
      1) payNow 给出 url=…/paynow/qrcodepay + args{_st, ordernumber, sk}
      2) 先 POST qrcodepay（把 args 当表单体）—— 这一发把“支付页”在服务端建起来。
         直接跳过它打 checkstatus，服务端只回一个 1013 字节的
         {"status":200} 空壳（没二维码）——因为它认为你还没打开过这个页。
      3) 然后再打 checkstatus（带 sk + x-aos-stk=_st）→ 返回二维码 base64
      4) 之后就用同一个 checkstatus 轮询支付状态
    """
    sk = (args or {}).get("sk") or ""
    poll = _poll_url_from_json({}, base, order, sk)

    # ① 先 POST qrcodepay：把支付页在服务端建起来（真机就是这么跳的）
    gp.log("④ POST qrcodepay 先把支付页建起来（args 当表单体）…")
    r, j = gp.post(pay_url, "QRCodePayNowNP", referer=pay_url, data=(args or None))
    png, d = _qr_png_from_json(j)
    if png is None:
        png = _qr_png_from_text(getattr(r, "text", "") or "")
    _u, _isr = _next_url(r, getattr(r, "text", "") or "")
    if png is None and _isr and _u:
        if _u.startswith("/"):
            _m = re.match(r"(https://[^/]+)", pay_url)
            _u = (_m.group(1) + _u) if _m else _u
        gp.log("   qrcodepay 返回跳转 → 跟过去：%s" % _u[:100])
        r, j = gp.get(_u)
        png, d = _qr_png_from_json(j)
        if png is None:
            png = _qr_png_from_text(getattr(r, "text", "") or "")
    if png is not None:
        return png, d, poll
    _diag(r, "qrcodepay", gp)

    # ② 再打 checkstatus：这一发才返回二维码
    gp.log("④ 再打 checkstatus 要二维码…")
    r, j = gp.post(poll, "QRCodePayNowNP", referer=pay_url)
    png, d = _qr_png_from_json(j)
    if png is None:
        png = _qr_png_from_text(getattr(r, "text", "") or "")
    _u, _isr = _next_url(r, getattr(r, "text", "") or "")
    if png is None and _isr and _u:
        if _u.startswith("/"):
            _m = re.match(r"(https://[^/]+)", poll)
            _u = (_m.group(1) + _u) if _m else _u
        gp.log("   checkstatus 返回跳转 → 跟过去：%s" % _u[:100])
        r, j = gp.get(_u)
        png, d = _qr_png_from_json(j)
        if png is None:
            png = _qr_png_from_text(getattr(r, "text", "") or "")
    if png is not None:
        return png, d, poll
    _diag(r, "checkstatus", gp)

    # ③ 最后一招：把 args 当查了参 GET qrcodepay（有时服务端只认这种）
    gp.log("   → 最后试一下 GET qrcodepay（args 当查不参数）")
    _g = pay_url + ("&" if "?" in pay_url else "?") + urllib.parse.urlencode(args or {})
    r, j = gp.get(_g)
    png, d = _qr_png_from_json(j)
    if png is None:
        png = _qr_png_from_text(getattr(r, "text", "") or "")
    poll2 = _poll_url_from_json(j, base, order, sk)
    if png is None:
        _diag(r, "qrcodepay-GET", gp)
    return png, d, (poll2 or poll)
def poll_payment(gp: GuestPay, poll_url, pay_url, png0, out_png,
                 poll_sec=3.0, max_wait=1800.0, refresh=None, on_status=None):
    """轮询支付状态；on_status(state, text) 回调给界面。返回 paid/expired/timeout/stopped。"""
    def _st(state, text=""):
        if on_status:
            try:
                on_status(state, text)
            except Exception:
                pass
    gp.log("⑤ 开始轮询支付状态（每 %.0f 秒一次）…" % poll_sec)
    _st("waiting", "等待扫码支付…")
    t0 = time.time()
    last_png = png0
    n = 0
    refreshed = 0
    while True:
        if gp.stop_evt is not None and gp.stop_evt.is_set():
            _st("stopped", "已停止轮询")
            return "stopped"
        n += 1
        try:
            r, j = gp.post(poll_url, "QRCodePayNowNP", referer=pay_url, tries=2)
        except Exception as e:
            gp.log("   轮询出错：%s" % e)
            if gp._sleep(poll_sec):
                _st("stopped", "已停止轮询")
                return "stopped"
            continue
        text = getattr(r, "text", "") or ""
        head = (j or {}).get("head") or {}
        stamp = time.strftime("%H:%M:%S")

        if head.get("status") == 302 or r.status_code in (301, 302):
            url = (head.get("data") or {}).get("url") or r.headers.get("location") or ""
            gp.log("[%s] ★ 服务端返回 302 → %s" % (stamp, url))
            if url:
                try:
                    r2, _ = gp.get(url)
                    t2 = getattr(r2, "text", "") or ""
                    if any(h in t2 for h in PAID_HINTS):
                        gp.log("★ 支付成功！（跳转页已出现成功文案）")
                except Exception as e:
                    gp.log("   跟跳失败：%s" % e)
            _st("paid", "已支付成功")
            return "paid"

        png, d = _qr_png_from_json(j)
        if png is None:
            png = _qr_png_from_text(text)
        if png and png != last_png:
            last_png = png
            try:
                Path(out_png).write_bytes(png)
                gp.log("[%s] 二维码已更新，重新打开图片" % stamp)
                _open_file(out_png)
            except Exception as e:
                gp.log("   写图片失败：%s" % e)

        if any(h in text for h in EXPIRED_HINTS):
            gp.log("[%s] [!] 二维码已过期/失效" % stamp)
            if refresh and refreshed < 5:
                refreshed += 1
                got = refresh()
                if got:
                    pay_url = got["pay_url"]
                    poll_url = got["poll_url"]
                    last_png = got.get("png") or last_png
                    gp.log("   已重新发起支付，换成新二维码，继续轮询")
                    _st("waiting", "二维码已过期→已自动换新，继续等待扫码…")
                    continue
            _st("expired", "二维码过期")
            return "expired"
        if not png and any(h in text for h in PAID_HINTS):
            gp.log("[%s] ★ 响应里出现支付成功文案（二维码已下线）" % stamp)
            _st("paid", "已支付成功")
            return "paid"

        brief = {k: v for k, v in d.items() if isinstance(v, (str, int, float, bool))}
        gp.log("[%s] 轮询#%d · head=%s · %s"
               % (stamp, n, head.get("status"), json.dumps(brief, ensure_ascii=False)[:150]))
        _st("waiting", "等待扫码支付…（第 %d 次）" % n)
        if max_wait and (time.time() - t0) > max_wait:
            gp.log("已等待 %.0f 秒，退出轮询" % max_wait)
            _st("timeout", "轮询超时（二维码可能仍有效）")
            return "timeout"
        if gp._sleep(poll_sec):
            _st("stopped", "已停止轮询")
            return "stopped"


def fetch_qr(link, log=None, stop_evt=None, out_dir=".", cookie="", ua="", stk="",
             proxy_provider=None, poll=True, poll_sec=3.0, max_wait=1800.0,
             on_status=None, open_image=True, debug=False):
    """一条龙：解析链接→fetchOrder→payNow→拿二维码→(可选)轮询支付。
    返回 dict：{ok, order, png_path, poll_url, pay_url, info, result}"""
    log = log or (lambda m: None)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gp = GuestPay(cookie=cookie, ua=ua, stk=stk, debug=debug, outdir=str(out_dir),
                  log=log, stop_evt=stop_evt, proxy_provider=proxy_provider)

    def _stopped():
        return stop_evt is not None and stop_evt.is_set()

    def _bail():
        if on_status:
            try:
                on_status("stopped", "已停止")
            except Exception:
                pass
        return {"ok": False, "order": "", "result": "stopped",
                "png_path": "", "poll_url": "", "pay_url": "", "info": {}}
    base, order, token, final_url = resolve_link(gp, link)
    log("① 订单页 → 域名 %s ｜ 订单号 %s" % (base, order))
    if _stopped():
        return _bail()
    r2, j2, info = step_fetch_order(gp, base, order, token)
    log("② 订单详情：%s" % json.dumps(info, ensure_ascii=False)[:300])
    # ★ stk 是 541 的头号嫌疑人，每一步都打出来（demo 原本就打，移植时丢了）
    log("   当前 stk=%s…（空 = 下一发 payNow 基本一定 403/541）"
        % ((gp.stk or "-")[:12]))
    if _stopped():
        return _bail()
    pay_url, pay_args, j3 = step_pay_now(gp, base, order, token)
    if not pay_url:
        raise RuntimeError("③ payNow 没拿到跳转地址：多是被苹果 403/541 限流。"
                           "可试：关掉「取码走代理」改直连 │ 换一份 PC 浏览器 Cookie │ "
                           "稍等半分钟再试（也可能订单已支付）")
    if _stopped():
        return _bail()
    png, d, poll_url = step_get_qr(gp, pay_url, pay_args, base, order)
    if not png:
        raise RuntimeError("④ 没抱到二维码图片。每一发的完整响应都已落盘到\n"                           "【支付二维码 \\debug\\】目录（含 HTTP 状态码 / stk / 服务端原文）\n"                           "把里面的 .txt 发过来就能定位。")
    out_png = str(out_dir / ("qrcode_%s.png" % order))
    Path(out_png).write_bytes(png)
    log("④ 二维码已存：%s" % out_png)
    if on_status:
        try:
            on_status("qr", out_png)
        except Exception:
            pass
    if open_image:
        _open_file(out_png)
    result = {"ok": True, "order": order, "png_path": out_png,
              "poll_url": poll_url, "pay_url": pay_url, "info": info, "result": ""}
    if poll:
        def _refresh():
            pu, pa, _ = step_pay_now(gp, base, order, token)
            if not pu:
                return None
            png2, _, poll2 = step_get_qr(gp, pu, pa, base, order)
            if png2:
                Path(out_png).write_bytes(png2)
                _open_file(out_png)
            return {"pay_url": pu, "poll_url": poll2, "png": png2}
        st = poll_payment(gp, poll_url, pay_url, png, out_png,
                          poll_sec=poll_sec, max_wait=max_wait, refresh=_refresh,
                          on_status=on_status)
        result["result"] = st
    return result
