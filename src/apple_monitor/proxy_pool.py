# -*- coding: utf-8 -*-
"""proxy_pool.py —— 全局代理池（★ 只给"非扫描"链路用）
   需求（用户）：除了【扫描】直连，其余一切（加袋/清袋/建会话/时段/付款/提交/订单查询）都走代理，
   并且【每 1 分钟换一个 IP】。
   取 IP：易代理 API（txt 格式，一行 ip:port）。IP 约 1 分钟失效，所以定时换是刚需。
   失败兜底：取不到 IP / 配额用完 → 打日志并【暂时直连】，绝不让主流程卡死。"""
from __future__ import annotations

import collections
import os
import random
import re
import threading
import time
import urllib.request

# ★ 默认代理接口留空：请在 config.json 的 settings.proxy_api_url 里填自己的。
#   格式：
#   http://api3.ydaili.cn/tools/BMeasureApi.ashx?action=BEAPI&secret=<你的secret>
#          &number=1&orderId=<你的订单号>&format=txt&type=1&split=3
#   留空时：取不到 IP 就自动回退直连（不会把主流程卡死）。
YD_DEFAULT = ""

_IPRE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})")


class ProxyPool:
    def __init__(self, api_url: str = "", rotate_sec: float = 60.0, enabled: bool = True,
                 logger=None, min_gap: float = 3.0, batch: int = 1):
        # ★ 粘贴进来的 URL 常带换行/空格（例如 pat=\n&rep=1），urllib 会直接报
 #   "URL can't contain control characters" → 三次重试全废。这里把空白拦掉。
        _raw = str(api_url or YD_DEFAULT or "")
        self.api_url = re.sub(r"[ \t\r\n]+", "", _raw)
        self.rotate_sec = max(5.0, float(rotate_sec or 60))
        self.enabled = bool(enabled)
        self.log = logger or (lambda m: None)
        self.min_gap = float(min_gap)               # 两次取 IP 的最小间隔（防把配额打光）
        self.batch = 1                              # 兼容旧字段；每个池只消费一个新 IP
        self.fetch_tries = 3          # ★ 取 IP 失败自动重试次数（避免一次超时就掉直连）
        self.fetch_retry_wait = 3.0   # ★ 重试间隔（秒）
        self.name = ""                              # 账号名（日志标识用）
        self._q = collections.deque()               # ★ 备用 IP 队列
        self._cur = ""                              # 形如 http://1.2.3.4:8080
        self._t_got = 0.0
        self._t_cur = 0.0
        self._t_bad = 0.0            # 上次"坏 IP 触发换"的时刻（防连环换）
        self._t_try = 0.0
        self._n_fail = 0
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._fetching = False
        self._stop = threading.Event()
        self._th = None
        # 统计
        self.n_get = 0
        self.n_err = 0
        self.n_switch = 0          # ★ 一一共切换了多少次 IP
        self.last_err = ""
        self.last_reason = ""

    def _tag(self) -> str:
        return ("[%s]" % self.name) if self.name else ""

    def _url_for(self, n: int) -> str:
        # 提取数是代理服务商/套餐的语义，程序不再擅自改写 URL。
        return self.api_url

    # ---------------- 取 IP ----------------
    def _fetch_once(self, n: int):
        """拉一次 IP；返回 (ips, err)。ips 为空表示失败，err 是原因。"""
        try:
            req = urllib.request.Request(self._url_for(n), headers={"User-Agent": "AppleMonitorPro"})
            with urllib.request.urlopen(req, timeout=10) as r:
                txt = (r.read() or b"").decode("utf-8", "replace").strip()
        except Exception as e:
            return [], "%s: %s" % (type(e).__name__, e)
        pairs = _IPRE.findall(txt)                 # 一行一个 ip:port（number>1 时多行）
        ips = ["http://%s:%s" % (a, b) for a, b in pairs]
        if not ips:
            return [], (txt[:80] or "(空响应)")
        return ips[:1], ""                         # 不蓄积同一批快过期 IP

    def fetch_batch(self, n: int = 0, force: bool = False):
        """调用用户原样配置的提取 URL，只消费返回的第一个 IP。

        n 只为兼容旧调用保留，程序不再用它改写服务商的提取数。force=False 时按 min_gap 节流。
        取 IP 失败会自动重试几次（隔几秒），尽量不要一次超时就掉成直连。"""
        n = 1
        if not self.api_url:
            self.last_err = "未配置代理接口"
            return []
        if not self._fetch_lock.acquire(blocking=False):
            return []
        self._fetching = True
        try:
            return self._fetch_batch_locked(n, force)
        finally:
            self._fetching = False
            self._fetch_lock.release()

    def _fetch_batch_locked(self, n: int, force: bool = False):
        with self._lock:
            wait = self._t_try + self.min_gap - time.time()
            if wait > 0 and not force and self._cur:
                return []                          # 太频繁就先用现有的
            self._t_try = time.time()
        ips, err = [], ""
        for attempt in range(1, self.fetch_tries + 1):
            ips, err = self._fetch_once(n)
            if ips:
                break
            if attempt < self.fetch_tries:
                self.log("[代理%s] ⚠ 取 IP 失败（第 %d/%d 次）：%s → %.1f 秒后重试"
                         % (self._tag(), attempt, self.fetch_tries, err, self.fetch_retry_wait))
                if self._stop.wait(self.fetch_retry_wait):   # 等待时被停止 → 收手
                    break
        if not ips:
            self.n_err += 1
            self.last_err = err
            self.log("[代理%s] ⚠ 取 IP 失败（已重试 %d 次）：%s → 暂时直连"
                     % (self._tag(), self.fetch_tries, err))
            return []
        with self._lock:
            self._q.clear()
            self._q.extend(ips[:1])
            self._t_got = time.time()
            self._n_fail = 0
        self.n_get += len(ips)
        return ips

    def fetch_one(self, force: bool = False) -> str:
        """取一条、立即设为当前 IP，不在队列里留一份过期副本。"""
        ips = self.fetch_batch(1, force=force)
        with self._lock:
            if ips and self._q:
                self._cur = self._q.popleft()
                self._t_cur = self._t_got
            return self._cur or ""

    def _fetch_async(self, why: str = ""):
        if not self.enabled or self._fetching:
            return
        def _run():
            ips = self.fetch_batch(force=True)
            if not ips:
                return
            with self._lock:
                if self._q:
                    self._cur = self._q.popleft()
                    self._t_cur = self._t_got
            self.log("[代理%s] 异步换 IP → %s（%s）"
                     % (self._tag(), self.shortcut() or "无", why or "按需"))
        threading.Thread(target=_run, name="proxy-fetch-%s" % (self.name or "pool"), daemon=True).start()

    # ---------------- 切换到下一个 IP ----------------
    def advance(self, why: str = "") -> str:
        """★ 切到队列里的下一个 IP；队列空了就先拉一批。返回新 IP（失败返回 ''）。"""
        if not self.enabled:
            return ""
        with self._lock:
            empty = not self._q
        if empty:
            self.fetch_batch(force=True)
        nxt = ""
        with self._lock:
            if self._q:
                nxt = self._q.popleft()
                self._cur = nxt
                self._t_cur = self._t_got
        if not nxt:
            with self._lock:
                self._cur = ""
            self.log("[代理%s] ⚠ 取不到新 IP → 暂时直连（原因：%s）" % (self._tag(), why or "取不到 IP"))
            return ""
        try:                       # 换了出口 → 旧 keep-alive 连接池作废
            from .client import _reset_http_session
            _reset_http_session()
        except Exception:
            pass
        self.n_switch += 1
        self.last_reason = why
        with self._lock:
            left = len(self._q)
        self.log("[代理%s] 换 IP → %s（原因：%s ｜ 队列剩 %d 个）"
                 % (self._tag(), nxt.replace("http://", ""), why or "轮换", left))
        return nxt

    def rotate(self, why: str = ""):
        # 旧名兼容：rotate == advance（切下一个）
        return self.advance(why)

    # ---------------- 给 Client 用 ----------------
    def provider(self):
        """返回一个可调用对象：每次请求调它拿当前代理（未开启/没取到 → None = 直连）"""
        def _p():
            if not self.enabled:
                return None
            # 快到轮换时间就不再发新请求给旧 IP；后台取新 IP，本发直连。
            if self._cur and self._t_cur and time.time() - self._t_cur >= self.rotate_sec * 0.9:
                with self._lock:
                    self._cur = ""
            if not self._cur:
                with self._lock:
                    if self._q:
                        self._cur = self._q.popleft()
                        self._t_cur = self._t_got
                if not self._cur:
                    self._fetch_async("空池/快过期")
            return self._cur or None
        _p.pool = self          # ★ client 拿到坏代理时可以通过它立刻换 IP
        return _p

    def current(self) -> str:
        return self._cur if self.enabled else ""

    def shortcut(self) -> str:
        """给日志用：1.2.3.4:8080"""
        return (self._cur or "").replace("http://", "")

    def queue_len(self) -> int:
        with self._lock:
            return len(self._q)

    def mark_bad(self, bad: str = ""):
        """请求失败/被限流(403/541) → 立刻切到下一个 IP
           ★ 防连环换：刚换过不到 2 秒又报坏，只记不换（一次结算 burst 里连报好几次，换一次就够）"""
        if not self.enabled:
            return
        with self._lock:
            if bad and self._cur and bad != self._cur:
                return                      # 报的不是当前这个 → 过期的回报，忽略
            if time.time() - self._t_bad < 2.0:
                self._n_fail += 1
                return
            self._t_bad = time.time()
            self._n_fail += 1
        with self._lock:
            self._cur = ""
            self._t_cur = 0.0
            self._q.clear()
        self._fetch_async("403/541 或连不上")

    # ---------------- 后台定时轮换 ----------------
    def _loop(self):
        # ★ 随机相位：别让所有号同一秒一起换 IP（错开出口，降低被一锅端概率）
        if self._stop.wait(self.rotate_sec * random.uniform(0.80, 0.90)):
            return
        while not self._stop.is_set():
            if self.enabled:
                try:
                    self.advance("定时 %d 秒" % int(self.rotate_sec))
                except Exception as e:
                    self.log("[代理%s] ⚠ 换 IP 异常：%s: %s" % (self._tag(), type(e).__name__, e))
            if self._stop.wait(self.rotate_sec):
                break

    def start(self, lazy: bool = False):
        if not self.enabled:
            self.log("[代理%s] 关（除手机扫描外也直连）" % self._tag())
            return self
        # ★★ 幂等：反复开始/停止绝不能叠加出多条换 IP 线程
        th = self._th
        if th is not None and th.is_alive():
            return self
        if not lazy:
            self.fetch_batch(force=True)
            with self._lock:
                if self._q and not self._cur:
                    self._cur = self._q.popleft()
                    self._t_cur = self._t_got
            self.log("[代理%s] 已开启：每 %d 秒前换新 IP，当前 %s"
                     % (self._tag(), int(self.rotate_sec), self.shortcut() or "无"))
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="proxy-%s" % (self.name or "pool"), daemon=True)
        self._th.start()
        return self

    def stop(self):
        self._stop.set()


# ---------------- 全局单例（界面 / 各 worker / 看板 共用同一个池） ----------------
_POOL = None
_POOL_KEY = None
_ALL_POOLS = []      # ★ 所有已创建的池（一号一池+单例），用于停止时一律全停


def get_pool(cfg, logger=None) -> ProxyPool:
    global _POOL, _POOL_KEY
    s = getattr(cfg, "settings", None)
    key = (bool(getattr(s, "proxy_enabled", False)),
           str(getattr(s, "proxy_api_url", "") or ""),
           float(getattr(s, "proxy_rotate_sec", 60) or 60))
    if _POOL is None or _POOL_KEY != key:
        if _POOL is not None:
            _POOL.stop()
        _POOL = ProxyPool(api_url=key[1], rotate_sec=key[2], enabled=key[0],
                          logger=logger, batch=1)
        _POOL_KEY = key
        if _POOL not in _ALL_POOLS:
            _ALL_POOLS.append(_POOL)
    elif logger is not None:
        _POOL.log = logger
    return _POOL


def make_pool(cfg, logger=None, name=""):
    """★ 一号一池：给单个账号建一个独立代理池（非单例），互不影响。"""
    st = getattr(cfg, "settings", None)
    pl = ProxyPool(api_url=str(getattr(st, "proxy_api_url", "") or ""),
                   rotate_sec=float(getattr(st, "proxy_rotate_sec", 60) or 60),
                   enabled=bool(getattr(st, "proxy_enabled", False)),
                   logger=logger, batch=1)
    pl.name = name or ""
    _ALL_POOLS.append(pl)
    return pl


def stop_all_pools():
    """★ 停掉所有代理池（各账号一号一池 + 全局单例）。
    “停止抢购”和“关闭窗口”都调它，确保没有池子在后台继续轮换 IP、白烧配额。"""
    for pl in list(_ALL_POOLS):
        try:
            pl.stop()
        except Exception:
            pass
    _ALL_POOLS.clear()
    global _POOL
    if _POOL is not None:
        try:
            _POOL.stop()
        except Exception:
            pass
