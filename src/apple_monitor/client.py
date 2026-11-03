# -*- coding: utf-8 -*-
"""HTTP 层：连接复用 + 会话三件套（cookie / live / x-aos-stk）+ 403/541 瞬时限流重试

★ 关键（实测）：结算会话靠两样东西维持
   1) Set-Cookie 刷出来的新 cookie（CKJ）
   2) 响应 meta.mandatoryAdditionalHeaders 里的 x-aos-stk（每步都会刷）
   两者任一丢失 → 后续请求 302。跨进程/重开也必须带着，所以可序列化。
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from collections import deque
from typing import Dict, Optional

try:
    from curl_cffi import requests as creq
except Exception:      # 允许在没装 curl_cffi 的环境下仅做配置编辑
    creq = None

UA_ANDROID = ("Mozilla/5.0 (Linux; Android 16; PJD110 Build/BP2A.250605.015; wv) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 Mobile Safari/537.36 "
              "XWEB/1500117 MMWEBSDK/20260604 MMWEBID/9105 MicroMessenger/8.0.77.3160(0x28004D30) "
              "WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android")
REFERER = "https://servicewechat.com/wxbc25a7df24ae0e6a/164/page-frame.html"
MERCH = "https://web.mmap.apple.com/26.07.0-MAIN/miniprogram-merch/p/wechat"
BASE = "https://web.mmap.apple.com/moe/p/wechat"

_TLS = threading.local()

# ★ HTTP keep-alive 连接生命周期：
#   · 每个线程独立会话，5~8 分钟随机轮换，避免所有线程同时重建
#   · 或累计 1500~3000 次请求后轮换
_SESSION_MIN_SEC = 300.0
_SESSION_MAX_SEC = 480.0
_SESSION_MIN_REQ = 1500
_SESSION_MAX_REQ = 3000


def _reset_http_session():
    """关闭当前线程的 keep-alive 池；下次请求会新建 Session。"""
    s = getattr(_TLS, "s", None)
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
    _TLS.s = None
    _TLS.born = 0.0
    _TLS.req = 0
    _TLS.recycle_sec = 0.0
    _TLS.recycle_req = 0


def _session():
    if creq is None:
        raise RuntimeError("没装 curl_cffi：请先  pip install curl_cffi")
    now = time.time()
    s = getattr(_TLS, "s", None)
    need_new = s is None
    if s is not None:
        age = now - float(getattr(_TLS, "born", now) or now)
        n = int(getattr(_TLS, "req", 0) or 0)
        if age >= float(getattr(_TLS, "recycle_sec", _SESSION_MIN_SEC) or _SESSION_MIN_SEC):
            need_new = True
        if n >= int(getattr(_TLS, "recycle_req", _SESSION_MIN_REQ) or _SESSION_MIN_REQ):
            need_new = True
    if need_new:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        s = creq.Session(impersonate="chrome131")      # 连接复用（keep-alive）
        _TLS.s = s
        _TLS.born = now
        _TLS.req = 0
        _TLS.recycle_sec = random.uniform(_SESSION_MIN_SEC, _SESSION_MAX_SEC)
        _TLS.recycle_req = random.randint(_SESSION_MIN_REQ, _SESSION_MAX_REQ)
    return s


def _session_note_request():
    _TLS.req = int(getattr(_TLS, "req", 0) or 0) + 1


class _Global503Breaker:
    """全局上游 503 熔断：短窗口内多个线程命中 503 时，全体短暂等待。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.hits = deque()
        self.blocked_until = 0.0

    def note(self):
        now = time.time()
        with self.lock:
            self.hits.append(now)
            while self.hits and self.hits[0] < now - 5.0:
                self.hits.popleft()
            tripped = len(self.hits) >= 3
            delay = 0.0
            if tripped:
                delay = random.uniform(3.0, 5.0)
                self.blocked_until = max(self.blocked_until, now + delay)
                self.hits.clear()
            return tripped, delay

    def wait(self, stop_evt=None) -> bool:
        """返回 True 表示等待期间收到停止信号。"""
        while True:
            now = time.time()
            with self.lock:
                left = self.blocked_until - now
            if left <= 0:
                return False
            # 分散恢复时间，避免所有账号同一毫秒重新打满
            w = min(left, 0.5) + random.uniform(0.0, 0.15)
            if stop_evt is not None:
                if stop_evt.wait(w):
                    return True
            else:
                time.sleep(w)


_GLOBAL_503 = _Global503Breaker()


class Client:
    """一个 cookie = 一个 Client；内部维护 CKJ/live/stk，可导出/导入（热会话）"""

    def __init__(self, cookie: str, ua: str = UA_ANDROID, cookie_file: str = "",
                 proxy_provider=None):
        self.ua = ua or UA_ANDROID
        self.cookie_file = cookie_file
        self.ckj: Dict[str, str] = {}
        for part in (cookie or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    self.ckj[k] = v
        self.live: Dict[str, str] = {"x-aos-model-page": "mobileWeChatCheckout"}
        self.stk: Optional[str] = None
        self.last_status = 0
        self.blocked_until = 0.0
        # ★ 统计（给界面看板用）：请求数 / 被限流次数 / 重试次数
        self.n_req = 0
        self.n_541 = 0
        self.n_403 = 0
        self.n_503 = 0
        self.n_retry = 0
        self.trace = None          # ★ 调试钩子：设了它就把每次请求/返回原文交出去
        self.on_block = None       # 被限流时的回调（worker 用来打日志/暂停这个号）
        # ★★ 代理（全局的、除扫描外全走）：一个可调用对象，每次请求现取当前代理；
        #    返回 None = 这次直连。扫描用的 Client 不设它（= 永远直连）。
        self.proxy_provider = proxy_provider
        self.proxy_used = ""       # 最近一次实际用的代理（日志/看板用）
        self.stop_evt = None       # ★ 可选的停止信号：置位后【立刻放弃后续重试】，
                                   #   否则点停止时一个请求还要把 3 次重试打完（最多 3×15 秒）

    # ---------- 序列化 ----------
    def dump(self) -> dict:
        return {"ckj": dict(self.ckj), "live": dict(self.live), "stk": self.stk}

    def load(self, d: dict):
        if not d:
            return
        if d.get("ckj"):
            self.ckj = dict(d["ckj"])
        if d.get("live"):
            self.live = dict(d["live"])
        if d.get("stk"):
            self.stk = d["stk"]

    @property
    def cookie_header(self) -> str:
        return "; ".join("%s=%s" % (k, v) for k, v in self.ckj.items())

    @property
    def fp(self) -> str:
        return (self.ckj.get("as_dima") or "")[:16]

    # ---------- 请求 ----------
    def headers(self, form: bool = False) -> dict:
        h = {"User-Agent": self.ua, "Referer": REFERER, "Accept": "*/*",
             "Accept-Language": "zh-CN,zh;q=0.9", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
             "Sec-Fetch-Site": "cross-site", "charset": "utf-8", "xweb_xhr": "1",
             "Cookie": self.cookie_header}
        h.update({k: v for k, v in self.live.items() if v})
        if self.stk:
            h["x-aos-stk"] = self.stk
        if form:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        return h

    def log_block(self):
        try:
            if self.on_block:
                self.on_block("被限流（%s）→ 这个号进入 30 秒冷却" % self.last_status)
        except Exception:
            pass

    def _emit_trace(self, method, url, body, status, headers, text,
                    req_headers=None, ms=None):
        """★ 调试钩子唯一出口：把这一发的原文 + 耗时(ms) 交给回调。

        回调签名可能新旧不一（有的只吃 6 个位置参数）→ 逐级降级调用，
        保证「调试代码」永远不会把抢单链路炸掉。
        """
        if self.trace is None:
            return
        try:
            self.trace(method, url, body, status, headers, text,
                       req_headers=req_headers, ms=ms)
            return
        except TypeError:
            pass
        except Exception:
            return
        try:
            self.trace(method, url, body, status, headers, text, req_headers=req_headers)
            return
        except TypeError:
            pass
        except Exception:
            return
        try:
            self.trace(method, url, body, status, headers, text)
        except Exception:
            pass

    def request(self, method: str, url: str, body: str = "", timeout: int = 15, tries: int = 3,
                extra: dict = None, cancel_evt=None):
        """返回 response（可能 None）。403/541 = 瞬时限流 → 退避重试（实测几十秒自愈）

        cancel_evt：可选的局部取消信号。扫描命中后设置它，可以阻止尚未发出的
        重试和退避等待；已经发出的同步 HTTP 请求无法从另一个线程强制中断。
        """

        def _stopped() -> bool:
            return bool((self.stop_evt is not None and self.stop_evt.is_set()) or
                        (cancel_evt is not None and cancel_evt.is_set()))

        def _wait_stop(seconds: float) -> bool:
            """等待指定秒数；命中局部取消或全局停止时返回 True。"""
            left = max(0.0, float(seconds or 0.0))
            while left > 0:
                if _stopped():
                    return True
                step = min(left, 0.1)
                if self.stop_evt is not None:
                    if self.stop_evt.wait(step):
                        return True
                elif cancel_evt is not None:
                    if cancel_evt.wait(step):
                        return True
                else:
                    time.sleep(step)
                left -= step
            return _stopped()
        # ★★ 冷却期内直接快速失败：不再白打请求（实测越打恢复越慢）
        if self.blocked_until > time.time():
            self._emit_trace(method, url, body, -1, {}, "", None, 0.0)
            return None
        # ★ 全局 503 熔断：任何账号都等一会儿再继续打上游
        if _GLOBAL_503.wait(self.stop_evt):
            return None
        if cancel_evt is not None and cancel_evt.is_set():
            return None
        for a in range(max(1, tries)):
            # ★ 停止信号：已要求退出/本轮扫描已命中 → 剩下的重试一发都不打了
            if _stopped():
                return None
            if self.blocked_until > time.time():
                _w = min(2.0, self.blocked_until - time.time())
                if _wait_stop(_w):
                    return None
            _h = {}
            _px = None
            _ms = None
            _t0 = time.perf_counter()   # ★ 这一发的净耗时（含重试的每一次）
            try:
                _h = self.headers(method != "GET")
                if extra:
                    _h.update(extra)
                _kw = {}
                if self.proxy_provider is not None:
                    try:
                        _px = self.proxy_provider()
                    except Exception:
                        _px = None
                # 必须在真正发请求的 worker 线程里重置 thread-local Session。
                # 代理池后台线程里调 _reset_http_session 只会重置它自己，对 worker 无效。
                if (_px or "") != (self.proxy_used or ""):
                    _reset_http_session()
                    self.proxy_used = _px or ""
                if _px:
                    _kw["proxies"] = {"http": _px, "https": _px}
                r = _session().request(method, url, headers=_h,
                                       data=body or None, timeout=timeout, **_kw)
            except Exception:
                r = None
                _ms = round((time.perf_counter() - _t0) * 1000, 1)
                # ★ 用了代理还连不上 → 这个 IP 基本是死的，让池子立刻换一个（不会卡住本次重试）
                if _px:
                    _pool = getattr(self.proxy_provider, "pool", None)
                    if _pool is not None:
                        try:
                            _pool.mark_bad(_px)
                        except Exception:
                            pass
            if r is None:
                self.n_retry += 1
                _reset_http_session()               # 超时/断连的连接池直接作废，避免继续复用坏连接
                # ★ 请求失败也要记（含这一发耗时）——这正是要排查的
                if _ms is None:
                    _ms = round((time.perf_counter() - _t0) * 1000, 1)
                self._emit_trace(method, url, body, 0, {}, "",
                                 dict(_h) if _h else None, _ms)
                if _wait_stop(0.6):
                    return None
                continue
            self.n_req += 1
            _session_note_request()
            self.last_status = r.status_code
            # 正常 pickup/quote 不记，免得刷屏；但 503/541/403 和失败必须留证
            if "pickup/quote" not in url or r.status_code in (403, 503, 541):
                self._emit_trace(method, url, body, r.status_code, dict(r.headers), r.text or "",
                                 dict(_h), round((time.perf_counter() - _t0) * 1000, 1))
            if r.status_code == 541:
                self.n_541 += 1
            elif r.status_code == 403:
                self.n_403 += 1
            elif r.status_code == 503:
                self.n_503 += 1
            # ★★ 403/541 = 被风控/限流 → 让这个号的代理池立刻切到下一个 IP（下一次重试就换出口）
            if _px and r.status_code in (403, 541):
                _pool = getattr(self.proxy_provider, "pool", None)
                if _pool is not None:
                    try:
                        _pool.mark_bad(_px)
                    except Exception:
                        pass
            self._absorb(r)
            if r.status_code == 503:
                # ★ 503：当前线程丢弃旧 keep-alive 连接，短暂退避；
                #   短窗口内多个线程都 503 时，触发全局熔断。
                _reset_http_session()
                tripped, delay = _GLOBAL_503.note()
                backoff = random.uniform(1.0, 3.0)
                if tripped:
                    try:
                        if self.on_block:
                            self.on_block("上游 503 → 全局暂停 %.1f 秒（本线程连接已重建）" % delay)
                    except Exception:
                        pass
                if _wait_stop(backoff):
                    return r if cancel_evt is None else None
                return r
            if r.status_code in (403, 541):
                # ★★ 一遇限流【立即停手】：这个号进 30 秒冷却，期间不再发任何请求。
                #    （实测重试解决不了 541，反而把冷却期越拖越长）
                self.blocked_until = max(self.blocked_until, time.time() + 30)
                self.log_block()
                return r
            return r
        return None

    def _absorb(self, r):
        """把响应里的 Set-Cookie / meta 头吸收进会话状态（这一步不做，后面必 302）"""
        try:
            for sc in (r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else []):
                kv = sc.split(";")[0]
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    if v.strip() and v.strip() != "deleted":
                        self.ckj[k.strip()] = v.strip()
        except Exception:
            pass
        try:
            j = r.json()
            for k, v in ((j.get("meta") or {}).get("mandatoryAdditionalHeaders") or {}).items():
                if v:
                    self.live[k] = v
                    if k == "x-aos-stk":
                        self.stk = v
        except Exception:
            pass
        # ★ 有些响应把 x-aos-stk 放在【正文 JSON】里（读购物袋就是这样）→ 不抓的话，
        #   删除商品会返回 200 但静默不删（实测），于是袋子越攒越多
        try:
            m = re.search(r'"x-aos-stk"\s*:\s*"([^"]+)"', r.text or "")
            if m:
                self.stk = m.group(1)
                self.live["x-aos-stk"] = m.group(1)
        except Exception:
            pass

    # ---------- 购物袋删除（★ 头不对就会静默失败） ----------
    def bag_delete(self, lid: str) -> bool:
        """删掉购物袋里的一件。必须带 x-aos-stk + content-type: application/json + Content-Length: 0，
        否则服务端返回 HTTP 200 但根本不删（实测）。"""
        u = BASE + "/chat/bagx?_a=deleteItem&_m=wechatcart.lineItems." + lid
        r = self.post(u, "", extra={"content-type": "application/json", "Content-Length": "0"}, tries=2)
        return r is not None and r.status_code == 200

    # ---------- 快速结帐（Apple Pay express） ----------
    def express_checkout(self):
        """按【实测口径】打 bagx?_a=expressCheckout（2026-09-19 实测确认）。

        1) 先 GET /chat/bag 拿购物袋模型；
        2) transactions.expressCheckoutURL 就是接口 URL；
           expressCheckoutFormData 是服务端下发的表单字段数组（[{name,value}]），原样 POST 过去；
        3) 返回三种：
           · 购物袋模型（含 isEmptyCart）        = 袋空 / 本端无 Apple Pay，无副作用
           · head.data.errorCode==12000          = 当前会话没有 Apple ID 登录态，带 body.signInURL
           · 其它模型                            = 结帐 / Apple Pay 模型
        返回 (kind, payload)：kind 取 "cart" / "signin" / "model" / "none"
        """
        import urllib.parse
        try:
            m = json.loads(self.text(BASE + "/chat/bag") or "")
        except Exception:
            return "none", None
        url = ((m.get("transactions") or {}).get("expressCheckoutURL") or "")
        if not url:
            return "none", None
        form = m.get("expressCheckoutFormData") or []
        body = urllib.parse.urlencode([(x.get("name"), x.get("value")) for x in form]) if form else ""
        r = self.post(url, body, extra={"content-type": "application/x-www-form-urlencoded"}, tries=2)
        if r is None:
            return "none", None
        try:
            j = json.loads(r.text or "")
        except Exception:
            return "model", r.text
        err = ((j.get("head") or {}).get("data") or {}).get("errorCode")
        if err == 12000:
            return "signin", (j.get("body") or {}).get("signInURL", "")
        if j.get("isEmptyCart") is not None:
            return "cart", j
        return "model", j

    # ---------- 便捷 ----------
    def get(self, url: str, **kw):
        return self.request("GET", url, **kw)

    def post(self, url: str, body: str = "", **kw):
        return self.request("POST", url, body, **kw)

    def text(self, url: str, **kw) -> str:
        r = self.get(url, **kw)
        return (r.text or "") if r is not None else ""

    def fetch_openid(self) -> str:
        t = self.text(MERCH.rsplit("/miniprogram-merch", 1)[0] + "/miniprogram-bootstrap/fetchOpenId")
        import re
        m = re.search(r'"openID"\s*:\s*"([^"]+)"', t)
        return m.group(1) if m else ""
