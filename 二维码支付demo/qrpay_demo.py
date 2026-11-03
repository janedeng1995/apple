# -*- coding: utf-8 -*-
"""
====================================================================
 苹果微信订单 · 支付二维码提取 Demo（独立演示版）
====================================================================
输入：一个订单链接（苹果发到取货人邮箱那封邮件里的链接），
      加上下单时用的那份 Cookie（浏览器抓包 -b 后面那一串）。
输出：微信支付二维码 PNG（自动打开）+ 轮询支付状态。

完整复刻浏览器里的手工流程（对应你抓的三个包）：
  ① GET  邮件里的订单链接
        → 302 跳到访客订单页，解析出 域名 / 订单号 / 令牌(token)
  ② POST /wechat/shop/orderx/guestx/<单号>/<token>?e=true&_a=fetchOrder&_m=guestOrderSpinner
        → 订单详情（顺手把响应 meta 里的 x-aos-stk 收下，下一步要用）
  ③ POST /wechat/shop/order/guestx/<单号>/<token>?e=true&_a=payNow&_m=orderDetail.orderHeader.payNow
        → 返回 head.status=302：url=.../paynow/qrcodepay，args={_st, ordernumber, sk}
  ④ POST .../wechat/shop/paynow/qrcodepay            （带 _st/ordernumber/sk 表单）
     POST .../wechat/shop/paynow/qrcodepayx/qrcodepaystatus
        → body.qrCodePayNow.d.qrCodePaymentImageData.src 就是二维码 base64
  ⑤ 存成 qrcode_<订单号>.png 并打开；随后轮询 qrcodepaystatus 看支付结果

用法：
  python qrpay_demo.py --curl 抓包.txt
  python qrpay_demo.py --cookie-file cookies.txt --link "https://www.apple.com.cn/xc/wechat/vieworder/..."
  python qrpay_demo.py            # 什么都不带 → 交互式提示粘贴
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

try:
    from curl_cffi import requests as creq
except ImportError:
    print("【缺依赖】请先执行:  pip install curl_cffi")
    sys.exit(1)

# Windows 控制台默认 GBK，遇到生僻符号会崩 → 强制 UTF-8 + 容错替换
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ------------------------------------------------------------------ 常量
IMPERSONATE = "chrome150"      # 抓包用的是 Edge153，取指纹最接近的 chrome 目标
UA_DEFAULT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

# 访客订单页地址：https://secure10.www.apple.com.cn/wechat/shop/order/guest/<单号>/<token>?e=true
# 注意：secure 后面的数字每次都可能不一样（secure7 / secure10 / 甚至没有数字），必须动态解析
GUEST_URL_RE = re.compile(
    r"(https://[A-Za-z0-9.-]*apple\.com\.cn)/wechat/shop/order/guest/([^/?#]+)/([^/?#]+)")

PAID_HINTS = ["支付成功", "已支付", "已完成付款", "付款成功", "感谢你购买", "感谢您的购买",
              "感谢您购买", "订单已完成", "payment received", "payment complete",
              "thank you for your order"]
EXPIRED_HINTS = ["二维码已过期", "二维码过期", "二维码已失效", "二维码失效", "已过期",
                 "已失效", "expired", "重新获取", "请刷新"]

# fetchOrder 响应里想挑出来给人看的字段（嵌套层级不定，全文递归找）
INFO_KEYS = ["orderNumber", "orderPlacedDate", "orderStatus", "statusDescription",
             "paymentStatus", "paymentType", "paymentMethodPaymentTypeName",
             "totalAmount", "payNowText", "paymentTimeExpiry",
             "paymentTimeToExpiryEpoch", "isWeChatOrder"]

RETRY_STATUS = (403, 541)      # 苹果风控/限流：退避后重试


# ================================================================== 工具
def _ui_fetch_id() -> str:
    """仿照浏览器的 x-aos-ui-fetch-call-1：10位随机-8位随机"""
    abc = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choices(abc, k=10)) + "-" + "".join(random.choices(abc, k=8))


def _unescape_paste(s: str) -> str:
    """把从浏览器/聊天软件里复制出来的 cmd 转义和 markdown 链接擦干净"""
    s = s.replace("^", "")
    s = s.replace("\\_", "_").replace("\\&", "&").replace("\\/", "/")
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)      # [url](url) → url
    return s


def parse_curl(text: str) -> dict:
    """从粘贴的 curl 命令里提取 url / cookie / UA / x-aos-stk（能提多少提多少）"""
    t = _unescape_paste(text or "")
    out = {"url": "", "cookie": "", "ua": "", "stk": "", "model_page": ""}
    m = (re.search(r'--url\s+"([^"]+)"', t) or re.search(r"--url\s+'([^']+)'", t)
         or re.search(r"--url\s+(\S+)", t))
    if m:
        out["url"] = m.group(1).strip()
    m = (re.search(r'(?:^|\s)-b\s+"([^"]+)"', t) or re.search(r"(?:^|\s)-b\s+'([^']+)'", t))
    if m:
        out["cookie"] = m.group(1).strip()
    for hm in re.finditer(r'-H\s+"([^"\n]+)"', t):
        line = hm.group(1)
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            out["ua"] = v
        elif k == "x-aos-stk":
            out["stk"] = v
        elif k == "x-aos-model-page":
            out["model_page"] = v
    return out


def _harvest(obj, keys, found=None, depth=0):
    """在JSON里递归找感兴趣的标量字段（苹果这套 graviton 模型嵌套层数不定）"""
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
    """paymentTimeToExpiryEpoch 这种时间戳 → 本地时间字符串"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(v)))
    except Exception:
        return str(v)


def _open_file(p: str):
    """尽量把图片打开（Windows 直接调默认程序）"""
    try:
        if os.name == "nt":
            os.startfile(p)                       # noqa: S606
        elif sys.platform == "darwin":
            os.system('open "%s"' % p)
        else:
            os.system('xdg-open "%s"' % p)
    except Exception as e:
        print("（自动打开失败，请手动双击：%s ｜ %s）" % (p, e))


# ================================================================== 会话
class GuestPay:
    """维护 cookie 罐 + x-aos-stk 的访客支付会话（stk 每一步都会变，必须跟着刷新）"""

    def __init__(self, cookie="", ua=UA_DEFAULT, stk="", debug=False, outdir="."):
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
        self.s = creq.Session(impersonate=IMPERSONATE)

    # ---------------- cookie / stk 维持 ----------------
    def cookie_header(self) -> str:
        return "; ".join("%s=%s" % (k, v) for k, v in self.cookies.items())

    def _absorb(self, r):
        """吸收 Set-Cookie 和响应 meta 里的 x-aos-stk（不做后面大概率 302/失效）"""
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
        return j

    # ---------------- 请求 ----------------
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

    def _retry_wait(self, i):
        wait = 4.0 + 2.0 * i + random.random() * 2.0
        print("   · 被苹果风控/限流 → %.1f 秒后重试" % wait)
        time.sleep(wait)

    def post(self, url, model_page, referer="", data=None, timeout=30, tries=4):
        """AOS 的 graviton POST（空 body 也带 content-type，和抓包一致）"""
        h = self._base_headers(referer)
        if model_page:
            h["x-aos-model-page"] = model_page
        h["content-type"] = "application/x-www-form-urlencoded"
        r = j = None
        for i in range(max(1, tries)):
            r = self.s.request("POST", url, headers=h,
                               data=(data if data is not None else ""),
                               timeout=timeout, allow_redirects=False)
            self.n += 1
            j = self._absorb(r)
            self._dump("POST", url, r)
            if r.status_code not in RETRY_STATUS or i == tries - 1:
                break
            self._retry_wait(i)
        return r, j

    def get(self, url, browser=False, timeout=30, tries=3):
        """browser=True 用导航态头（打开邮件链接那种）；否则用 fetch 态头"""
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
        r = j = None
        for i in range(max(1, tries)):
            r = self.s.request("GET", url, headers=h, timeout=timeout, allow_redirects=True)
            self.n += 1
            j = self._absorb(r)
            self._dump("GET", url, r)
            if r.status_code not in RETRY_STATUS or i == tries - 1:
                break
            self._retry_wait(i)
        return r, j


# ================================================================== 流程
def resolve_link(gp: GuestPay, link: str):
    """① 邮件链接 → (base, 订单号, token, 最终URL)。直接给访客页链接则不用请求。"""
    link = (link or "").strip()
    m = GUEST_URL_RE.search(link)
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(0)
    if not link:
        raise RuntimeError("没有订单链接")
    print("① 打开订单链接，等它跳到访客订单页 …")
    r, _ = gp.get(link, browser=True)
    if r.status_code in RETRY_STATUS:
        raise RuntimeError("打开订单链接被苹果风控（HTTP %s）。等几十秒重试，"
                           "或者自己在浏览器里打开链接，把地址栏的订单页链接贴进来。" % r.status_code)
    candidates = [getattr(r, "url", "")] + [getattr(h, "url", "") for h in (getattr(r, "history", None) or [])]
    for u in candidates:
        m = GUEST_URL_RE.search(u or "")
        if m:
            print("   [OK] 落到访客订单页：%s" % u)
            return m.group(1), m.group(2), m.group(3), u
    m = GUEST_URL_RE.search(getattr(r, "text", "") or "")   # 兜底：页面里JS跳转
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(0)
    raise RuntimeError("没能解析到访客订单页（最终URL: %s）。\n"
                       "请直接把浏览器地址栏里的链接贴进来，形如：\n"
                       "  https://secure10.www.apple.com.cn/wechat/shop/order/guest/订单号/一长串token?e=true"
                       % getattr(r, "url", "?"))


def step_fetch_order(gp: GuestPay, base: str, order: str, token: str):
    """② fetchOrder：拉订单详情 + 刷新 x-aos-stk"""
    ref = "%s/wechat/shop/order/guest/%s/%s?e=true" % (base, order, token)
    url = "%s/wechat/shop/orderx/guestx/%s/%s?e=true&_a=fetchOrder&_m=guestOrderSpinner" % (base, order, token)
    r, j = gp.post(url, "OrderStatusGuestSummary", referer=ref)
    info = _harvest((j or {}).get("body") or {}, INFO_KEYS)
    return r, j, info


def step_pay_now(gp: GuestPay, base: str, order: str, token: str):
    """③ payNow：服务端返回 302，给出 qrcodepay 地址和 _st/sk"""
    ref = "%s/wechat/shop/order/guest/%s/%s?e=true" % (base, order, token)
    url = ("%s/wechat/shop/order/guestx/%s/%s?e=true&_a=payNow"
           "&_m=orderDetail.orderHeader.payNow" % (base, order, token))
    r, j = gp.post(url, "OrderStatusGuestSummary", referer=ref)
    head = (j or {}).get("head") or {}
    if head.get("status") == 302 and isinstance(head.get("data"), dict):
        d = head["data"]
        return (d.get("url") or ""), dict(d.get("args") or {}), j
    return "", {}, j


def _qr_png_from_json(j):
    """从 graviton 响应里抠二维码：body.qrCodePayNow.d.qrCodePaymentImageData.src"""
    d = (((j or {}).get("body") or {}).get("qrCodePayNow") or {}).get("d") or {}
    src = ((d.get("qrCodePaymentImageData") or {}).get("src")) or ""
    if "base64," in src:
        try:
            return base64.b64decode(src.split("base64,", 1)[1]), d
        except Exception:
            return None, d
    return None, d


def _qr_png_from_text(text):
    """兜底：整页文本里直接搜 data:image/png;base64,..."""
    m = re.search(r"data:image/png;base64,([A-Za-z0-9+/=\s]+)", text or "")
    if m:
        try:
            return base64.b64decode(re.sub(r"\s+", "", m.group(1)))
        except Exception:
            return None
    return None


def _poll_url_from_json(j, base, order, sk):
    """优先用响应里轮询组件给的URL，拿不到就按抓包的格式自己拼"""
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


def step_get_qr(gp: GuestPay, pay_url: str, args: dict, base: str, order: str):
    """④ 拿二维码：先打 qrcodepay 页面，不行再打 qrcodepaystatus 轮询接口"""
    r, j = gp.post(pay_url, "QRCodePayNowNP", referer=pay_url, data=(args or None))
    # 万一服务端真的给了 HTTP 302，跟过去
    if r.status_code in (301, 302) and r.headers.get("location"):
        loc = r.headers["location"]
        if loc.startswith("/"):
            m = re.match(r"(https://[^/]+)", pay_url)
            loc = m.group(1) + loc if m else loc
        r, j = gp.get(loc)
    png, d = _qr_png_from_json(j)
    if png is None:
        png = _qr_png_from_text(getattr(r, "text", "") or "")
    poll = _poll_url_from_json(j, base, order, (args or {}).get("sk", ""))
    if png is None:                       # 页面里没抠到 → 直接打轮询接口要
        r2, j2 = gp.post(poll, "QRCodePayNowNP", referer=pay_url)
        png, d = _qr_png_from_json(j2)
        if png is None:
            png = _qr_png_from_text(getattr(r2, "text", "") or "")
    return png, d, poll


def poll_payment(gp: GuestPay, poll_url, pay_url, png0, out_png,
                 poll_sec=3.0, max_wait=1800.0, refresh=None):
    """⑤ 轮询支付状态：已支付 / 二维码过期(可自动重开) / 超时"""
    print("\n⑤ 开始轮询支付状态（每 %.0f 秒一次，Ctrl+C 可随时退出）…" % poll_sec)
    t0 = time.time()
    last_png = png0
    n = 0
    refreshed = 0          # 过期重开次数硬上限，防止刷新回调失灵时死循环
    while True:
        n += 1
        try:
            r, j = gp.post(poll_url, "QRCodePayNowNP", referer=pay_url, tries=2)
        except Exception as e:
            print("   [%s] 轮询出错：%s" % (time.strftime("%H:%M:%S"), e))
            time.sleep(poll_sec)
            continue
        text = getattr(r, "text", "") or ""
        head = (j or {}).get("head") or {}
        stamp = time.strftime("%H:%M:%S")

        # 1) 服务端 302 → 多半是支付完成后的跳转
        if head.get("status") == 302 or r.status_code in (301, 302):
            url = (head.get("data") or {}).get("url") or r.headers.get("location") or ""
            print("[%s] ★ 服务端返回 302 → %s" % (stamp, url))
            if url:
                try:
                    r2, _ = gp.get(url)
                    t2 = getattr(r2, "text", "") or ""
                    if any(h in t2 for h in PAID_HINTS):
                        print("★ 支付成功！（跳转页已出现成功文案）")
                    else:
                        print("   跳转页片段：%s" % re.sub(r"\s+", " ", t2)[:200])
                except Exception as e:
                    print("   跟跳失败：%s" % e)
            return "paid"

        # 2) 二维码被换过（过期后重新 payNow 会拿到新图）
        png, d = _qr_png_from_json(j)
        if png is None:
            png = _qr_png_from_text(text)
        if png and png != last_png:
            last_png = png
            try:
                Path(out_png).write_bytes(png)
                print("[%s] 二维码已更新，重新打开图片" % stamp)
                _open_file(out_png)
            except Exception as e:
                print("   写图片失败：%s" % e)

        # 3) 过期 / 成功 关键词（响应里还带着二维码图 → 认为还没支付，避免误判）
        if any(h in text for h in EXPIRED_HINTS):
            print("[%s] [!] 二维码已过期/失效" % stamp)
            if refresh and refreshed < 5:
                refreshed += 1
                got = refresh()
                if got:
                    pay_url = got["pay_url"]
                    poll_url = got["poll_url"]
                    last_png = got.get("png") or last_png
                    print("   已重新发起支付，换成新二维码，继续轮询")
                    continue
            return "expired"
        if not png and any(h in text for h in PAID_HINTS):
            print("[%s] ★ 响应里出现支付成功文案（二维码已下线）" % stamp)
            return "paid"

        # 4) 常态心跳
        brief = {k: v for k, v in d.items() if isinstance(v, (str, int, float, bool))}
        print("[%s] 轮询#%d · head=%s · %s"
              % (stamp, n, head.get("status"),
                 json.dumps(brief, ensure_ascii=False)[:150]))
        if max_wait and (time.time() - t0) > max_wait:
            print("已等待 %.0f 秒，退出轮询（二维码可能还有效，确认后再来）" % max_wait)
            return "timeout"
        time.sleep(poll_sec)


# ================================================================== 主流程
def main():
    if os.name == "nt":
        try:
            os.system("chcp 65001 >NUL")     # 让控制台按 UTF-8 显示，避免中文变乱码
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="苹果微信订单支付二维码提取 Demo")
    ap.add_argument("--link", default="", help="订单链接（邮件里那个，或浏览器地址栏的访客订单页链接）")
    ap.add_argument("--cookie", default="", help="Cookie 字符串（抓包 -b 后面那一整串）")
    ap.add_argument("--cookie-file", default="", help="Cookie 文件路径（默认找同目录 cookies.txt）")
    ap.add_argument("--curl", default="", help="粘贴的 curl 命令txt（自动提取 cookie/UA/stk/链接）")
    ap.add_argument("--out", default="", help="二维码 PNG 输出路径（默认 qrcode_<订单号>.png）")
    ap.add_argument("--poll-sec", type=float, default=3.0, help="轮询间隔秒（默认3）")
    ap.add_argument("--max-wait", type=float, default=1800.0, help="最长轮询秒数（默认1800=30分钟）")
    ap.add_argument("--no-cookie", action="store_true",
                    help="完全不提供 Cookie，仅凭订单链接尝试（邮件链接自带授权）")
    ap.add_argument("--no-poll", action="store_true", help="只拿二维码，不轮询支付状态")
    ap.add_argument("--no-refresh", action="store_true", help="二维码过期后不自动重新发起支付")
    ap.add_argument("--debug", action="store_true", help="把每个请求/响应原文存到 debug/ 目录")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent

    # ---------- 收集输入 ----------
    curl_info = {}
    if args.curl:
        p = Path(args.curl)
        curl_info = parse_curl(p.read_text(encoding="utf-8", errors="replace") if p.exists() else args.curl)

    cookie = args.cookie or curl_info.get("cookie") or ""
    if not cookie:
        for cf in [args.cookie_file, str(here / "cookies.txt")]:
            if cf and Path(cf).exists():
                cookie = Path(cf).read_text(encoding="utf-8", errors="replace").strip()
                print("· 已从 %s 读取 Cookie" % cf)
                break
    link = args.link or curl_info.get("url") or ""
    ua = curl_info.get("ua") or UA_DEFAULT
    stk = curl_info.get("stk") or ""

    if not cookie and not args.no_cookie:
        cookie = input("请粘贴 Cookie（抓包 -b 后面那一整串；直接回车 = 不带 Cookie 硬刚）:\n> ").strip()
    if not link:
        link = input("请粘贴订单链接（苹果邮件里那个）:\n> ").strip()
    if not cookie:
        print("· 未提供 Cookie → 仅凭订单链接尝试（①解析链接无需 cookie；"
              "②③④ 若被 541 风控，请补一份 cookie 再试）")

    print("=" * 64)
    print("订单链接 : %s" % link[:120])
    print("Cookie   : %d 个字段 ｜ UA: %s" % (len([c for c in cookie.split(";") if "=" in c]), ua[:60]))
    print("=" * 64)

    gp = GuestPay(cookie=cookie, ua=ua, stk=stk, debug=args.debug, outdir=here)

    # ---------- ① 解析订单链接 ----------
    base, order, token, final_url = resolve_link(gp, link)
    print("① 订单页 → 域名 %s ｜ 订单号 %s ｜ 令牌 %s…" % (base, order, token[:16]))

    # ---------- ② fetchOrder ----------
    try:
        r2, j2, info = step_fetch_order(gp, base, order, token)
        print("② fetchOrder → HTTP %s ｜ stk=%s…" % (r2.status_code, (gp.stk or "-")[:12]))
        for k in INFO_KEYS:
            if k in info:
                v = str(info[k])
                if k == "paymentTimeToExpiryEpoch":
                    v = _fmt_epoch(info[k])
                print("   · %-26s %s" % (k, v[:80]))
    except Exception as e:
        print("② fetchOrder 失败（不致命，继续）: %s" % e)

    # ---------- ③ payNow ----------
    try:
        pay_url, pay_args, j3 = step_pay_now(gp, base, order, token)
    except Exception as e:
        print("③ payNow 请求异常：%s" % e)
        pay_url, pay_args, j3 = "", {}, None
    if not pay_url:
        print("③ payNow 没拿到 302 跳转。原始返回：")
        if j3:
            print(json.dumps(j3, ensure_ascii=False)[:1500])
        else:
            print("(非JSON响应，加 --debug 重跑可看原文)")
        print("\n常见原因：Cookie 失效/过期 → 重新抓一份；被苹果风控(403/541) → 等几十秒重试；"
              "订单已支付/已取消。")
        return 2
    print("③ payNow → 302 ｜ %s" % pay_url)
    print("   args: ordernumber=%s ｜ sk=%s…" % (pay_args.get("ordernumber"), str(pay_args.get("sk"))[:16]))

    # ---------- ④ 拿二维码 ----------
    png, qr_d, poll_url = step_get_qr(gp, pay_url, pay_args, base, order)
    if not png:
        print("④ 没抠到二维码图片（加 --debug 重跑可看每个响应原文）")
        return 3
    out_png = args.out or str(here / ("qrcode_%s.png" % order))
    Path(out_png).write_bytes(png)
    print("④ 二维码到手 → %s（%.1f KB）" % (out_png, len(png) / 1024.0))
    for k in ("qrCodeExpiryTime", "orderTotal", "isWeChat", "orderDetailUrl"):
        if k in qr_d:
            print("   · %-16s %s" % (k, str(qr_d[k])[:100]))
    print("   · %-16s %s" % ("poll_url", poll_url[:110]))
    _open_file(out_png)
    print("   用微信扫上面的码即可付款")

    if args.no_poll:
        return 0

    # ---------- ⑤ 轮询支付状态 ----------
    refreshes = {"n": 0}

    def refresh():
        if args.no_refresh or refreshes["n"] >= 3:
            return None
        refreshes["n"] += 1
        try:
            pu, pa, _ = step_pay_now(gp, base, order, token)
            if not pu:
                return None
            png2, _, poll2 = step_get_qr(gp, pu, pa, base, order)
            if not png2:
                return None
            Path(out_png).write_bytes(png2)
            _open_file(out_png)
            return {"pay_url": pu, "poll_url": poll2, "png": png2}
        except Exception as e:
            print("   重新发起支付失败：%s" % e)
            return None

    try:
        poll_payment(gp, poll_url, pay_url, png, out_png,
                     poll_sec=args.poll_sec, max_wait=args.max_wait, refresh=refresh)
    except KeyboardInterrupt:
        print("\n手动停止轮询。二维码图片在：%s" % out_png)
        return 0
    print("\n完成。二维码图片在：%s" % out_png)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
    except Exception as e:
        print("\n【出错了】%s" % e)
        print("（加上 --debug 重跑，可以把每个请求/响应原文存到 debug/ 目录排查）")
        sys.exit(1)