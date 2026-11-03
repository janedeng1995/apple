# -*- coding: utf-8 -*-
"""一个 cookie 一个 worker（并行）：

  ① 预热：只备购物袋；不在命中前创建/恢复结算会话
  ② 保活：worker 主线程每 30 秒发一次非结算 fetchOpenId，仅保持 HTTP/TCP/TLS 连接热
  ③ 探测：用 pickup/quote 扫绑定取货人所选城市下的门店
  ④ 命中：换店 → 有时段则保存，无时段也继续补齐其他信息 → 提交
  ⑤ 联系人：始终使用任务明确绑定的取货人；多个账号可共享同一人
  ⑥ 成单：响铃 + 邮件；任务额度或取货人合计额度到量后停止
"""
from __future__ import annotations

import io as _io
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, List, Optional

from .client import BASE, MERCH, Client
from .config import (HERE, CookieCfg, Config, WARMUP_FIRST, WARMUP_SNIPE,
                     WARMUP_CART, WARMUP_FULL, WARMUP_NONE, sku_name,
                     STORE_SOURCE_STORE)
from .notify import alarm, send_mail
from .scan import quote_one, release_executor, scan
from .session import CheckoutSession, FastFillMiss, NeedLogin, StoreUnavailable


class BusyGate:
    """引用计数版的全局"有号正在抢单"标志。
    单个 Event 的问题：两个号先后进 try_buy 时，先结束的那个 clear 会把
    另一个还在抢的单也解除让路，其它号立刻恢复探测。计数器在
    所有在途抢单都结束（计数归零）后才放行。"""

    def __init__(self, limit: int = 1):
        self._lock = threading.Lock()
        self._n = 0
        self.limit = max(1, int(limit or 1))

    def try_enter(self) -> bool:
        with self._lock:
            if self._n >= self.limit:
                return False
            self._n += 1
            return True

    def set(self):
        with self._lock:
            self._n += 1

    def clear(self):
        with self._lock:
            self._n = max(0, self._n - 1)

    def is_set(self) -> bool:
        with self._lock:
            return self._n > 0

    def is_full(self) -> bool:
        with self._lock:
            return self._n >= self.limit

    def set_limit(self, limit: int) -> None:
        with self._lock:
            self.limit = max(1, int(limit or 1))


class WarmupGate:
    """把多个账号的首次预热排成固定间隔，避免启动瞬间同时打结算请求。"""
    def __init__(self, interval_sec: float = 0.0):
        self.interval_sec = max(0.0, float(interval_sec or 0.0))
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait_turn(self, stop_evt: threading.Event) -> float:
        with self._lock:
            now = time.time()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.interval_sec
        if wait > 0:
            stop_evt.wait(wait)
        return wait


class CookieWorker(threading.Thread):
    def __init__(self, cfg: Config, cc: CookieCfg, logger: Callable[[str], None],
                 on_order: Optional[Callable[[dict], None]] = None,
                 stop_evt: Optional[threading.Event] = None,
                 dry_run: Optional[bool] = None, auto_submit: Optional[bool] = None,
                 busy_evt=None, proxy_provider=None, warmup_gate=None):
        super().__init__(daemon=True, name="worker-%s" % cc.name)
        self.cfg = cfg
        self.cc = cc
        self.sku = cc.sku
        self.log = logger
        self.on_order = on_order
        self.stop = stop_evt or threading.Event()
        # ★★ 全局"有人正在抢单"标志（所有号共用同一个 BusyGate 计数器）：
        #    谁进 try_buy 谁计数 +1 → 其它号立刻暂停探测，把资源让给抢单那些号。
        self.busy = busy_evt if busy_evt is not None else BusyGate()
        self.warmup_gate = warmup_gate
        raw_mode = str(getattr(cfg.settings, "warmup_mode", WARMUP_SNIPE)).lower()
        self.warmup_mode = WARMUP_FIRST if raw_mode in (WARMUP_FIRST, "none") else WARMUP_SNIPE
        self._paused_logged = False     # 让路日志去重（只在进入/离开时各打一行）
        self._dry_run_override = dry_run
        self._auto_submit_override = auto_submit
        self.dry_run = cfg.settings.dry_run if dry_run is None else dry_run
        self.auto_submit = cfg.settings.auto_submit if auto_submit is None else auto_submit
        self.cookie, self.file_ua = self._read_identity()
        self.ua = cc.ua or self.file_ua          # 优先级：配置里写死的 > 身份文件里的 > 手机档（Client 默认）
        # ★★ 代理：加袋/清袋/建会话/时段/付款/提交 全部走它（全局的，每 60 秒换一个 IP）
        self.proxy_provider = proxy_provider
        self.cli = Client(self.cookie, ua=self.ua or None, cookie_file=cc.path,
                          proxy_provider=proxy_provider)
        # ★★ 开了"记录结算调试日志"→ 这个号发的每一发（加袋/清袋/建会话/时段/取货人/付款/提交/确认）
        #    都带【请求头 + 返回头 + 原文】落到 logs\结算调试\全程-<日期>-<账号>.jsonl
        if getattr(cfg.settings, "debug_checkout", False):
            self.cli.trace = self._trace_all
        self.cli.stop_evt = self.stop      # ★ 点停止时，正在飞的请求不再打完剩余重试
        self.cli.on_block = lambda msg: (self.log("[%s]   ⚠ %s" % (self.cc.name, msg)),
                                         self.stat.update(state="被限流", note=msg, t_note=time.time()))[1]
        # ★ 扫描与 checkout 使用两个 Client：扫描线程只碰 scan_cli，
        #   不会在抢单过程中污染主会话的 cookie / x-aos-stk。
        # ★ 扫描【故意不带代理】：直连，跟结算链路分出口（这样被 541 的只会是其中一边）
        self.scan_cli = Client(self.cookie, ua=self.ua or None, cookie_file=cc.path)
        self.scan_cli.stop_evt = self.stop
        self.scan_cli.on_block = lambda msg: self.log("[%s]   [扫描] ⚠ %s" % (self.cc.name, msg))
        self.sess = CheckoutSession(self.cli, self.sku, log=logger)
        # 捡漏预热提速：后台只负责付款页 GET，主线程同时保存联系人。
        # 每个账号固定一个线程，让该线程的 HTTP/TLS 连接可在多次命中间复用。
        self._prefetch_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="billing-get-%s" % cc.name)
        try:
            # ★ 提交后查订单状态：间隔 + 次数上限（默认 3 秒 / 20 次）
            self.sess.status_poll_sec = float(getattr(cfg.settings, "status_poll_sec", 3.0) or 3.0)
            self.sess.status_poll_max = int(getattr(cfg.settings, "status_poll_max", 20) or 20)
        except Exception:
            pass
        self.orders = 0
        self._prepared = False          # 当前模式的启动准备是否完成
        self._cart_ok = False           # ★ 袋子备好了没（预热只做这件事，不再建会话）
        self._full_ready = False        # 捡漏预热 SID 是否已写好联系人/付款/发票
        self._cart_touched = False      # 首发模式命中后是否临时加过购物袋
        # ★★ 2026-09-24：袋子是否【已被消耗/清空/状态未知】→ 必须回主循环重建，
        #    不能再拿它去试下一家店（成单一次后会连撞一堆 isEmptyCart）。
        self._cart_dirty = False
        self._fast_fill_off = False     # ★ 本单是否已放弃「快速填格」（一次没落库就回退标准流程）
        self._fast_fill_miss = 0        # ★ 快速填格「连续没落库」次数
        self._fast_fill_dead = False    # ★ 连续 2 次没落库 → 本号彻底停用快速填格（自愈，不硬撑）
        self._prewarm_contact = True    # 预热阶段是否预写取货人（settings.prewarm_contact）
        self._startup_delay_done = False
        self._last_http_keepalive = time.time()
        self._last_keepalive_warn = 0.0
        self.settle_until = {}      # store -> 到这个时刻前不再"进结算"（假有货保护）；★ 不影响探测
        self.fail_n = {}            # store -> 连续失败次数（用来做指数退避，别对着假货白打）
        self._skip_t = {}           # store -> 上次写"跳过"事件的时间（节流，免得刷屏）
        self.prev_hit = {}          # store -> 上一轮扫描是否报"有货"（用来识别 无货→有货 的新信号）
        self.last_settle = 0.0      # 上一次进结算的时刻（结算族全局最小间隔）
        # ★★ 下完一单后的"本号冷却"：这期间照常扫描/记有货，但【不进结算、不下第二单】
        self._warned_no_buyer = False
        self.rounds = 0                 # 探测轮数
        self.t_last_beat = 0.0          # 上次心跳
        self.hits_last = 0              # 上一轮有货家数
        self._busy = threading.Lock()
        m = re.search(r"as_dima=([^;]{0,16})", self.cookie or "")
        self.fp = m.group(1) if m else "?"      # 账号指纹：用来区分"这单是哪个微信号下的"
        self.dead = False          # ★ 只让"这个 cookie"退出（本号本型号被限购）；不影响其它 cookie
        # ★★ 界面看板用的实时状态（GUI 每秒读一次）
        self.stat = {
            "name": cc.name, "sku": cc.sku,
            "state": "待启动", "note": "还没启动", "t_note": 0.0,
            "rounds": 0, "req": 0, "ok": 0, "no": 0, "err": 0,
            "stores": 0, "hits_last": 0, "ms_last": 0, "t_last": 0.0, "last_hits": [],
            "orders": 0,
        }
        self._runtime_signature = None
        self._hot_pause_reason = ""
        self._proxy_signature = (
            bool(getattr(cfg.settings, "proxy_enabled", False)),
            str(getattr(cfg.settings, "proxy_api_url", "") or ""),
            str(getattr(cfg.settings, "proxy_mode", "per_account") or "per_account"),
            float(getattr(cfg.settings, "proxy_rotate_sec", 60) or 60),
        )

    # ---------------- 工具 ----------------
    def _read_identity(self):
        """返回 (cookie, ua)。★ 身份文件里带 ua 就必须用它：
        手机档 = Android 微信 UA；PC 档 = Windows 微信 UA。
        之前这里只取了 cookie、把 ua 丢了 → 拿 PC 的 cookie 却发 Android 的头，不匹配。"""
        with open(self.cc.path, encoding="utf-8", errors="replace") as _f:
            raw = _f.read().strip()
        if raw.startswith("{"):
            import json
            j = json.loads(raw)
            return (j.get("cookie", "") or ""), (j.get("ua", "") or "")
        return raw, ""

    @staticmethod
    def _runtime_task_signature(task: CookieCfg, buyer) -> tuple:
        """会影响购物袋/结算会话的配置快照。这些值一变，旧预热会话就不能复用。"""
        buyer_sig = None
        if buyer is not None:
            buyer_sig = (buyer.id, buyer.name, buyer.phone, buyer.email, buyer.last4,
                         buyer.last_name, buyer.first_name, buyer.want,
                         tuple(buyer.skus), tuple(buyer.cities), buyer.enabled)
        return (task.name, task.sku, task.buyer, task.per_order, task.want,
                task.enabled, getattr(task, "store", ""), buyer_sig)

    def _refresh_runtime_config(self) -> bool:
        """把 GUI 重建后的新 CookieCfg 绑回运行中的 worker。

        返回 False 表示当前任务应待命（被停用、删除，或更换了身份文件）。
        身份和代理属于 Client 级资源，不在运行中偷换，避免一半请求用旧 Cookie、
        一半请求用新 Cookie。
        """
        with self.cfg.lock:
            fresh = next((x for x in self.cfg.cookies if x.id == self.cc.id), None)
            settings = self.cfg.settings
            buyer = self.cfg.buyer_by_name(fresh.buyer) if fresh is not None else None
            raw_mode = str(getattr(settings, "warmup_mode", WARMUP_SNIPE)).lower()
            new_mode = WARMUP_FIRST if raw_mode in (WARMUP_FIRST, "none") else WARMUP_SNIPE
            new_dry = (bool(getattr(settings, "dry_run", True))
                       if self._dry_run_override is None else bool(self._dry_run_override))
            new_auto = (bool(getattr(settings, "auto_submit", False))
                        if self._auto_submit_override is None else bool(self._auto_submit_override))
            poll_sec = float(getattr(settings, "status_poll_sec", 3.0) or 3.0)
            poll_max = int(getattr(settings, "status_poll_max", 20) or 20)
            prewarm_contact = bool(getattr(settings, "prewarm_contact", True))
            warmup_interval = float(getattr(settings, "prewarm_interval_sec", 0.0) or 0.0)
            max_parallel = int(getattr(settings, "max_parallel_buy", 2) or 2)
            debug_checkout = bool(getattr(settings, "debug_checkout", False))
            proxy_signature = (
                bool(getattr(settings, "proxy_enabled", False)),
                str(getattr(settings, "proxy_api_url", "") or ""),
                str(getattr(settings, "proxy_mode", "per_account") or "per_account"),
                float(getattr(settings, "proxy_rotate_sec", 60) or 60),
            )

        if fresh is None:
            reason = "任务已删除；新增/删除任务后请停止并重新开始"
            if self._hot_pause_reason != reason:
                self.log("[%s] ⏸ %s" % (self.cc.name, reason))
                self._hot_pause_reason = reason
            self.stat.update(state="需重启", note=reason, t_note=time.time())
            return False

        if fresh.path != self.cc.path or fresh.ua != self.cc.ua:
            reason = "身份文件/UA 已更换；为避免混用 Cookie，请停止并重新开始"
            if self._hot_pause_reason != reason:
                self.log("[%s] ⏸ %s" % (self.cc.name, reason))
                self._hot_pause_reason = reason
            self.stat.update(state="需重启", note=reason, t_note=time.time())
            return False

        if proxy_signature != self._proxy_signature:
            reason = "代理设置已更换；代理池属于运行级资源，请停止并重新开始"
            if self._hot_pause_reason != reason:
                self.log("[%s] ⏸ %s" % (self.cc.name, reason))
                self._hot_pause_reason = reason
            self.stat.update(state="需重启", note=reason, t_note=time.time())
            return False

        old_mode = self.warmup_mode
        old_sku = self.sku
        signature = self._runtime_task_signature(fresh, buyer)
        changed = self._runtime_signature is not None and signature != self._runtime_signature
        mode_changed = new_mode != old_mode
        prewarm_changed = prewarm_contact != self._prewarm_contact

        self.cc = fresh
        self.sku = fresh.sku
        self.warmup_mode = new_mode
        self.dry_run = new_dry
        self.auto_submit = new_auto
        self.sess.status_poll_sec = poll_sec
        self.sess.status_poll_max = poll_max
        self.busy.set_limit(max_parallel)
        self.cli.trace = self._trace_all if debug_checkout else None
        if self.warmup_gate is not None:
            self.warmup_gate.interval_sec = max(0.0, warmup_interval)
        self.stat.update(name=fresh.name, sku=fresh.sku)

        if changed or mode_changed or prewarm_changed:
            # 取货人/SKU/数量/预热模式任一变化，丢弃旧结算会话。
            # 下一轮会用新配置重备袋并预热，不会把旧取货人带入新订单。
            self.sess = CheckoutSession(self.cli, self.sku, log=self.log)
            self.sess.status_poll_sec = poll_sec
            self.sess.status_poll_max = poll_max
            self._prepared = False
            self._cart_ok = False
            self._full_ready = False
            self._cart_touched = False
            self.prev_hit.clear()
            self.settle_until.clear()
            self.fail_n.clear()
            self._prewarm_contact = prewarm_contact
            if self._runtime_signature is not None:
                changes = []
                if old_sku != self.sku:
                    changes.append("SKU %s→%s" % (old_sku, self.sku))
                if mode_changed:
                    changes.append("预热模式已更新")
                if not changes:
                    changes.append("取货人/数量/城市已更新")
                self.log("[%s] ♻ 热配置生效：%s；旧结算会话已作废，下一轮重建"
                         % (self.cc.name, "、".join(changes)))

        self._runtime_signature = signature
        if not fresh.enabled:
            reason = "任务已停用，保持待命（重新启用后自动恢复）"
            if self._hot_pause_reason != reason:
                self.log("[%s] ⏸ %s" % (self.cc.name, reason))
                self._hot_pause_reason = reason
            self.stat.update(state="已停用", note=reason, t_note=time.time())
            return False
        self._hot_pause_reason = ""
        return True

    def _bound_buyer(self):
        """★ 这个号一对一绑定的取货人；worker 每轮已换成最新 CookieCfg，资料和绑定均可热生效。"""
        return self.cfg.buyer_by_name(getattr(self.cc, "buyer", "") or "")

    def _order_qty(self) -> int:
        """这个号每单下几件（cookie.per_order）。

        ★ 上限 2：2026-09-23 抓包确认服务端 quantityData.maxQuantity=2（该 SKU 限购 2 件），
          配更大只会换来 quantity_limit_exceeded、白烧一个会话。
        """
        configured = min(2, max(1, int(getattr(self.cc, "per_order", 1) or 1)))
        b = self._bound_buyer()
        if not b:
            return configured
        left = self.cfg.ledger.available(
            self.cc.id, b.id, int(getattr(self.cc, "want", 1) or 1),
            int(getattr(b, "want", 1) or 1))
        return min(configured, left) if left > 0 else 1

    def _preheat_cart_check(self) -> None:
        """★★ 每一次【预热之前】先查一次购物袋里有多少件（用户要求）。

        · 捡漏模式：目标 = 每单件数；多了/少了都当场收敛（prepare_cart 删掉重加）。
        · 首发模式：袋子必须为空，发现残留当场清掉（否则命中时加购会叠成超额）。
        时机：这一步在【扫描之前】跑，只打购物袋族请求（读袋/删件/加购），
              完全不占"命中 → 提交"那一枪的时间。
        读袋失败不硬扛：抛出去由主循环按原节奏退避重试，下一轮预热会自动再来一次。
        """
        want = 0 if self.warmup_mode == WARMUP_FIRST else self._order_qty()
        have, all_items = self.sess.cart_now()          # ★ 只读一发现状
        others = [(p, q) for p, q in all_items if p != self.sku]
        self.log("[%s] 🔎 预热前查袋：%s 当前 %d 件 ｜ %s%s"
                 % (self.cc.name, sku_name(self.sku), have,
                    ("本单目标 %d 件" % want) if want else "首发模式要求空袋",
                    (" ｜ 还有其它型号 %s" % "、".join("%s×%d" % (p, q) for p, q in others))
                    if others else ""))
        if have == want and not others:
            self.log("[%s]   → 件数正好，直接沿用" % self.cc.name)
            return
        if self.warmup_mode == WARMUP_FIRST:
            self.log("[%s]   → 袋里有残留（%d 件/其它型号 %d 个）→ 清空，首发模式保持空袋"
                     % (self.cc.name, have, len(others)))
            self.sess.clear_cart()
            return
        self.log("[%s]   → 件数与目标不符 → 当场收敛到 %d 件" % (self.cc.name, want))
        self.sess.prepare_cart(want)                     # 收敛不了会抛错，绝不带超标袋往下走

    def _buyers(self) -> list:
        """★ 这个号绑定的取货人；累计抢够 self.cc.want 台就返回空（触发收工）。"""
        b = self._bound_buyer()
        if (b and b.enabled and b.wants(self.sku) and
                self.cfg.ledger.available(
                    self.cc.id, b.id, int(getattr(self.cc, "want", 1) or 1),
                    int(getattr(b, "want", 1) or 1), include_reserved=False) > 0):
            return [b]
        return []

    def _custom_stores(self) -> List[str]:
        """★★ 任务上自定义要盯的门店列表（store_source="store" 时才有；否则空列表）。"""
        if str(getattr(self.cc, "store_source", "") or "").lower() != STORE_SOURCE_STORE:
            return []
        sts = [str(x).strip().upper() for x in (getattr(self.cc, "stores", None) or []) if str(x).strip()]
        if not sts:
            one = str(getattr(self.cc, "store", "") or "").strip().upper()
            if one:
                sts = [one]
        out: List[str] = []
        for x in sts:
            if x not in out:
                out.append(x)
        return out

    def store_pin(self) -> str:
        """★ 本号盯死的唯一门店：任务自定义门店（恰好 1 家）优先，其次兼容旧的全局 settings.target_store。"""
        custom = self._custom_stores()
        if custom:
            return custom[0] if len(custom) == 1 else ""
        return str(getattr(self.cfg.settings, "target_store", "") or "").strip().upper()

    def target_stores(self) -> List[str]:
        b = self._bound_buyer()
        if b and (not b.enabled or not b.wants(self.sku)):
            return []
        # ★★ 门店来源二选一（任务卡上的选项）：
        #   【自定义门店】任务上列了几家就只扫几家（可多家）；
        #   【取货人意向地址】按取货人城市展开的门店列表扫。
        custom = self._custom_stores()
        if custom:
            return custom
        pin = str(getattr(self.cfg.settings, "target_store", "") or "").strip().upper()
        if pin:
            return [pin]
        return self.cfg.stores_for_cities(b.cities) if b else []

    def buyer_for_store(self, store: str):
        # ★ 一对一：这个号只用它绑定的取货人。仅当门店确实在该取货人城市里才返回（否则只记录不下单）。
        b = self._bound_buyer()
        if b and (not b.enabled or not b.wants(self.sku)):
            return None
        pin = self.store_pin()
        if pin:
            # ★ 门店被盯死（任务单店/旧全局 target_store）→ 用户明确指定，绕过城市校验
            return b if (b and (not store or str(store).upper() == pin)) else None
        custom = self._custom_stores()
        if custom:
            # ★ 自定义多家门店：同样绕过城市校验（否则会静默不下单），但只认列表里的店
            return b if (b and (not store or str(store).upper() in custom)) else None
        if b and (not store or store in self.cfg.stores_for_cities(b.cities)):
            return b
        return None

    # ---------------- 结算链路调试抓取 ----------------
    @staticmethod
    def _hdrs(h, lim: int = 6000) -> dict:
        """把请求头/返回头整理成可读 dict（cookie 会截到 3000 字，别把日志撑爆）"""
        out = {}
        try:
            for k, v in (h or {}).items():
                s = str(v)
                if k.lower() == "cookie":
                    s = s[:3000]
                out[str(k)] = s[:lim]
        except Exception:
            pass
        return out

    def _trace_all(self, method, url, req_body, status, headers, text, req_headers=None, ms=None):
        """★ 全程抓取：不管在不在"抢单中"，这个号发的每一发（加袋/清袋/建会话/时段/取货人/付款/提交/确认）
           都带请求头 + 返回头 + 原文落到 logs\结算调试\全程-<日期>-<账号>.jsonl"""
        try:
            d = os.path.join(HERE, "logs", "结算调试")
            os.makedirs(d, exist_ok=True)
            fn = os.path.join(d, "全程-%s-%s.jsonl" % (time.strftime("%m%d"), re.sub(r'[\\/:*?"<>|]', "_", self.cc.name)))
            rec = {"t": datetime.now().strftime("%H:%M:%S.%f")[:-3], "kind": "http",
                   "ms": ms,          # ★ 这一发的净耗时（毫秒），用来量化每一步提速
                   "acc": self.cc.name, "sku": self.sku, "method": method, "url": url[:600],
                   "path": url.replace("https://web.mmap.apple.com", "")[:200],
                   "req_headers": self._hdrs(req_headers), "req": (req_body or "")[:6000],
                   "status": status, "resp_headers": self._hdrs(headers, 4000),
                   "loc": ((headers or {}).get("Location") or "")[:300],
                   "resp_len": len(text or ""), "resp": (text or "")[:300000]}
            with _io.open(fn, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
    def _dbg_open(self, store: str):
        """开启逐条抓取：返回 (文件句柄, 写入函数)"""
        if not getattr(self.cfg.settings, "debug_checkout", False):
            return None, None
        d = os.path.join(HERE, "logs", "结算调试")
        os.makedirs(d, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|]', "_", "%s-%s-%s" % (time.strftime("%m%d-%H%M%S"), self.cc.name, sku_name(self.sku)))
        safe += "-" + str(store)
        fp = _io.open(os.path.join(d, safe + ".jsonl"), "w", encoding="utf-8")
        n = [0, False]        # [行号, 是否已经报过写入错误]
        write_lock = threading.Lock()

        def wr(kind, **kw):
            try:
                with write_lock:
                    n[0] += 1
                    # ★ 不能用 time.strftime("%H:%M:%S.%f")：本平台直接 ValueError
                    rec = {"i": n[0], "t": datetime.now().strftime("%H:%M:%S.%f")[:-3], "kind": kind}
                    rec.update(kw)
                    fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fp.flush()
            except Exception as e:
                if not n[1]:          # ★ 不再静默：第一次失败就报出来
                    n[1] = True
                    try:
                        self.log("[%s]   [调试抓取] 写盘失败：%s: %s" % (self.cc.name, type(e).__name__, e))
                    except Exception:
                        pass
        fp.write(json.dumps({"kind": "start", "t": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "acc": self.cc.name, "sku": self.sku, "sku_name": sku_name(self.sku),
                             "store": store, "store_name": self.cfg.store_name(store),
                             "sid": getattr(self.sess, "sid", "")[:16]}, ensure_ascii=False) + "\n")
        fp.flush()
        return fp, wr

    def _dbg_close(self, fp, wr, result: str):
        try:
            if wr:
                wr("end", result=result)
            if fp:
                fp.close()
                self.log("[%s]   调试抓取已存盘：logs\\结算调试\\%s" % (self.cc.name, os.path.basename(fp.name)))
        except Exception:
            pass

    def _dump_slot_page(self, store: str, raw: str):
        """把"没拿到时段"时的原始返回存盘（每店一份，覆盖），方便事后排查"""
        try:
            d = os.path.join(HERE, "logs", "没时段原始返回")
            os.makedirs(d, exist_ok=True)
            io_p = os.path.join(d, "%s.json" % store)
            with _io.open(io_p, "w", encoding="utf-8") as fp:
                fp.write("%s\n/* %s  %s  %d 字节 */\n\n" % (
                    ("// 最近一次：%s ｜ 号=%s ｜ 门店=%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), self.cc.name,
                                                              self.cfg.store_name(store))),
                    self.sku, getattr(self.sess, "last_slot_kind", "?"), len(raw or "")))
                fp.write(raw or "")
        except Exception:
            pass

    def _cool_store(self, store: str, base: int = None, reason: str = ""):
        """★ 记一次失败并冷却：固定等于 settings.store_cool_sec（默认 15 秒，界面可改）。
        ★ 每一轮间隔一样（不再指数增长）：反复"假有货"的店不会一直被我们对着打，省结算额度。
        ★ 只影响【这家店多久再进结算】，探测（看有没货）照旧不受影响。"""
        n = self.fail_n.get(store, 0) + 1
        self.fail_n[store] = n
        _cs = base if base is not None else getattr(self.cfg.settings, "store_cool_sec", 15)
        try:
            wait = max(0, int(_cs))
        except Exception:
            wait = 15
        self.settle_until[store] = time.time() + wait
        if reason:
            self.log("[%s]   这家店失败第 %d 次 → 冷却 %d 秒（%s）" % (self.cc.name, n, wait, reason))
        return wait

    def _cool_ok(self, store: str):
        """这家店这次成功了 → 失败计数清零"""
        self.fail_n.pop(store, None)

    # ---------------- 事件落盘（有货/命中/成单，方便事后排查） ----------------
    def _hit_jsonl(self, rec: dict):
        try:
            d = os.path.join(HERE, "logs")
            os.makedirs(d, exist_ok=True)
            fn = os.path.join(d, "有货-%s.jsonl" % time.strftime("%Y%m%d"))
            rec = dict(rec)
            rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            rec["acc"] = self.cc.name
            rec["sku"] = self.sku
            with _io.open(fn, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _log_hit(self, store: str, fresh: bool, action: str):
        """★ 只要扫到有货就打一行日志；被跳过的写进事件文件（"开抢"的由 try_buy 写完整流程，不重复）"""
        nm = self.cfg.store_name(store)
        self.log("[%s] 🔔 有货 %s（%s）｜%s" % (self.cc.name, store, nm, action))
        if action.startswith("→ 开抢"):
            return
        # ★ 节流：同一家店 15 秒内只写一条跳过记录（否则 0.4 秒一轮会把事件文件刷爆）
        if time.time() - self._skip_t.get(store, 0) < 15:
            return
        self._skip_t[store] = time.time()
        self._hit_jsonl({"event": "skip", "store": store, "store_name": nm, "sku_name": sku_name(self.sku),
                         "fresh": bool(fresh), "result": action,
                         "steps": ["%s  %s" % (time.strftime("%H:%M:%S"), action)],
                         "batch_stores": len(self._batch_stores)})

    # ---------------- 预热 / 保活 ----------------
    def _prewarm_store(self, sts: List[str] = None) -> str:
        """★★ 预热会话建在哪家店。

        固定/自定义门店模式也一律用【占位店】，不建在目标店上：
          预热时目标店大概率还没放时段，start 返回的 spokes 里没有 timeSlot 格；
          会话一旦建在目标店上，命中时就走“会话已在本店 → 不换店”的快捷路径，
          spokes 永远停在预热时的状态 → 拿不到时段格。
          改用占位店后命中必然换店，拿到的是目标店当前的 spokes。

        占位店优先用 settings.warmup_store（旧行为），再取货人城市里第一家，
        最后才退回目标列表（确实没别的店可用时）。"""
        sts = sts if sts is not None else self.target_stores()
        # 只有【自定义门店】模式需要避开目标店；按城市扫的模式保持旧行为不变。
        custom = self._custom_stores()
        avoid = {str(x).strip().upper() for x in (custom or []) if str(x).strip()}
        _pin = str(self.store_pin() or "").upper()
        if _pin:
            avoid.add(_pin)
        cand: List[str] = []
        _w = str(getattr(self.cfg.settings, "warmup_store", "") or "").strip().upper()
        if _w:
            cand.append(_w)
        b = self._bound_buyer()
        if b:
            cand.extend(self.cfg.stores_for_cities(b.cities))
        cand.extend(sts or [])
        for c in cand:
            c = str(c or "").strip().upper()
            if c and c not in avoid:
                return c
        return (sts[0] if sts else "")

    def _restore_prewarm_store(self):
        """★★ 把预热会话退回占位店。

        命中时会话会被切到目标店；这次没成的话必须退回来，
        否则下次命中跟“会话已在本店 → 不换店”，spokes 还是旧的（没有 timeSlot 格），
        又拿不到时段。退不回去就销毁会话，下一轮 open_session 会在占位店重建。"""
        if not self.sess.sid:
            return
        # ★ 别躁在别人抢单的节骤眼上发请求：全局规则是"有号在抢单→其它号暂停一切 HTTP"。
        #   这一发只是恢复性操作，跳过不会丢数据：会话旧旧留在目标店，下次命中时
        #   复用分支里的"补选店刷新"会把它救回来（那层兙底就是为此加的）。
        try:
            if self.busy.is_full() and not self._busy.locked():
                return
        except Exception:
            pass
        ph = self._prewarm_store()
        if not ph:
            return
        if str(getattr(self.sess, "store", "") or "").strip().upper() == ph.upper():
            return
        try:
            self.sess.switch_store(ph, self.cfg.store_name(ph))
            self.log("[%s]   ↩ 本次未成 → 会话已退回占位店 %s（下次命中再切目标店，保证拿到最新 spokes）"
                     % (self.cc.name, ph))
        except Exception:
            # 退不过去就销毁会话＋下一轮重建（重建时就在占位店上）
            self.sess.sid = ""
            self._full_ready = False
            self._prepared = False
            self._cart_ok = False

    def _open_full_session(self):
        sts = self.target_stores()
        buyer = self._bound_buyer()
        if not buyer:
            raise RuntimeError("捡漏模式没有可用的取货人")
        # ★★ 预热店怎么定：一律用占位店，不建在目标店上。
        #   原因（实测）：预热时目标店大概率还没放时段 → start 返回的 spokes 列表里
        #   根本没有 timeSlot 这一项；命中时若复用这个会话（不换店），后面逐格填的
        #   循环连 timeSlot 都看不到，直接提交一个没有时段的单。
        #   改用占位店后，命中时 session.store != 目标店 → 必然走 switch_store，
        #   拿到目标店【当前】的 spokes：有时段才会出现 timeSlot 格，才拉得到时段。
        _custom = self._custom_stores()
        placeholder = self._prewarm_store(sts)
        if not placeholder:
            raise RuntimeError("捡漏模式没有可用的占位门店")
        self.stat.update(state="捡漏预热", note="预热店 %s：预写联系人/付款/发票" % placeholder,
                         t_note=time.time())
        _cover = ""
        if _custom:
            _cover = " ｜ 自定义门店 %d 家（会话预热在占位店 %s，命中再切目标店）" % (len(_custom), placeholder)
        else:
            try:
                _mine = set(self.cfg.stores_for_cities(buyer.cities))
                _all = set(sts)
                if _all:
                    _cover = " ｜ 覆盖 %d/%d 家门店" % (len(_mine & _all), len(_all))
            except Exception:
                pass
        self.log("[%s] 捡漏预热：%s（%s）｜预热店=%s ｜ 预热人=%s%s%s"
                 % (self.cc.name, sku_name(self.sku), self.sku, placeholder, buyer.name, _cover,
                    "" if self._prewarm_contact else " ｜ 联系人未预热（命中再填绑定取货人）"))
        # ★★ 现在才准备【下单用的购物袋】（目标商品）—— 只做这一次。
        self.sess.sku = self.sku
        self._preheat_cart_check()
        self._cart_dirty = False
        self.sess.openid = self.cli.fetch_openid()
        self._cart_ok = True
        self.sess.save_store(placeholder)
        self.sess.sid = ""
        try:
            self.sess.start()
        except StoreUnavailable:
            if not self.sess.sid:
                raise
        contact, last4 = buyer.contact(), buyer.last4

        def ready(key):
            info = (getattr(self.sess, "last_spokes", {}) or {}).get(key) or {}
            value = str(info.get("value") or "")
            return bool(info.get("complete") or (value and "输入联系信息" not in value))

        # ★ prewarm_contact=false → 预热阶段【不写联系人】，命中时再填任务绑定的人。
        #   代价是命中时多 1~2 发（联系人格 GET+POST）。
        self._prewarm_contact = bool(getattr(self.cfg.settings, "prewarm_contact", True))
        if self._prewarm_contact:
            for _ in range(2):
                try:
                    self.sess.write_contact(contact, last4, need_get=True)
                except StoreUnavailable:
                    pass
                if ready("3pp"):
                    break
        else:
            self.log("[%s] 预热跳过联系人（设置 prewarm_contact=false）→ 命中时填入绑定取货人"
                     % self.cc.name)
        # ★ 没写过联系人时，付款这一格不需要前置 GET（session.write_billing 注释里的实测）。
        #   所以"不预热取货人"模式下先试省 GET 的快速填格；没落库就回退标准 GET+POST。
        #   预热过联系人时保持原样（必须 GET，否则不落库）。
        _billing_need_get = self._prewarm_contact
        for _ in range(2):
            try:
                self.sess.write_billing(need_get=_billing_need_get)
            except StoreUnavailable:
                pass
            except Exception:
                if not _billing_need_get:
                    _billing_need_get = True      # 快速填格没成 → 回退标准流程再试一次
                    try:
                        self.sess.write_billing(need_get=True)
                    except StoreUnavailable:
                        pass
            if ready("billing"):
                break
        # 发票是否需要由当前 spokes 决定；有该格且未完成才写入，避免盲发旧字段。
        if "invoice" in (getattr(self.sess, "last_spokes", {}) or {}):
            for _ in range(2):
                try:
                    self.sess.write_invoice(buyer.email)
                except StoreUnavailable:
                    pass
                if ready("invoice"):
                    break
        if not self._prewarm_contact:
            if not ready("billing"):
                raise RuntimeError("捡漏预热未落库：微信=%s" % ready("billing"))
        elif not ready("3pp") or not ready("billing"):
            raise RuntimeError("捡漏预热未落库：联系人=%s，微信=%s" % (ready("3pp"), ready("billing")))
        # ★ 提前打开选店页（changeStoreAction）。真机抓包证明它和 selectStoreUrl
        #   中间能隔 20 个请求，没有耦合；提前发掉后命中时只剩 selectStoreUrl 一发
        #   （约省 0.45s，且少传 69.5KB）。失效时 switch_store 会自动补开，不影响正确性。
        try:
            self.sess.open_store_picker()
            _picker = "选店页已提前打开"
        except Exception as e:
            _picker = "选店页提前打开失败（%s，命中时会补）" % e
        self._full_ready = True
        self._prepared = True
        _terms = getattr(self.sess, "last_terms_accepted", None)
        self.log("[%s] 预热条款状态：%s（条款接受字段将在提交时随 placeOrder 一并发送）"
                 % (self.cc.name, "已接受" if _terms is True else
                    "未返回" if _terms is None else "未接受"))
        self.stat.update(state="探测中", note="捡漏预热已就绪，待扫 %d 家店" % len(sts), t_note=time.time())
        self.log("[%s] 捡漏预热就绪 ｜ 联系人、付款及可用发票已保存 ｜ 待扫 %d 家店 ｜ %s"
                 % (self.cc.name, len(sts), _picker))
        # ★ 预热完成后把这几个状态打出来（用户要核对是否全 true）
        self.log("[%s]   预热状态：%s" % (self.cc.name, self._states_text()))
        if self._spoke_ready("timeSlot", (getattr(self.sess, "last_spokes", {}) or {}).get("timeSlot") or {}):
            self.log("[%s]   ★ 时段格已是 true → 命中时这一格会被直接跳过" % self.cc.name)
        else:
            self.log("[%s]   ⏳ 时段格当前是 false（该店此刻可能没时段）→ 命中时现场拉取" % self.cc.name)

    def open_session(self):
        """★ 现在只做一件事：【备袋】。不再建会话、不再摆占位店。

        为什么砍掉建会话（2026-09-15 手机抓包实证）：
          新流程是【先定店(改 rtsid) → 再 start】，会话是"生下来就带着门店"的。
          而命中之前不知道是哪家店 → 会话只能等命中才建。
          预热阶段那个会话会在命中时被 sid="" 丢掉，纯属白打：
              start 1 发 + 摆占位店 3 发 = 每号 4 发结算族，7 个号白打 28 发，
              而且全在"扫描之前"——最容易累积到 541 的那一段。
        现在预热阶段【零结算族请求】：只有购物袋族（读袋/删件/加购）+ fetchOpenId。
        """
        # ★ 捡漏模式的备袋/建会话/写表单都在 _open_full_session 里做；
        #   首发/普通模式才在这里先查一次袋子。
        if self.warmup_mode == WARMUP_FIRST:
            self._preheat_cart_check()          # 首发：袋子必须为空
            self._cart_dirty = False            # 袋子已核对/重建完毕
            self.sess.sid = ""
            self.sess._set_lids([])     # ★ 行号一起清（一单多件时袋里可能是 N 行）
            self._cart_ok = False
            self._full_ready = False
            self._prepared = True
            sts = self.target_stores()
            self.stat.update(state="探测中", note="首发模式：购物袋保持为空，待扫 %d 家店" % len(sts),
                             t_note=time.time())
            self.log("[%s] 首发模式就绪 ｜ 不读袋、不加购、不建会话 ｜ 待扫 %d 家店"
                     % (self.cc.name, len(sts)))
            return
        if self.warmup_mode == WARMUP_SNIPE:
            self._open_full_session()
            return

        # 普通"只备袋"模式：预热前先查一次袋子
        self._preheat_cart_check()
        self._cart_dirty = False
        self.stat.update(state="备袋中", note="正在备袋（命中后再建会话）", t_note=time.time())
        self.log("[%s] 备袋：%s（%s）" % (self.cc.name, sku_name(self.sku), self.sku))
        self.sess.openid = self.cli.fetch_openid()
        self.sess.sid = ""          # ★ 预热阶段不建会话（命中时定好店再 start）
        self.sess.picker_open = False
        self.sess.expired = False
        # ★★ 启动前体检（默认【关】，config: startup_check）
        #    为什么默认关：这一发是结算族请求，多打一次就多一点 541 风险；
        #    而且命中时本来就要 start 一次，那时限购/空袋一样会暴露（已能识别）。
        #    预热阶段该做的只有一件事：prepare_cart() 保证"袋里只有 1 件目标商品"。
        #    想要提前体检的，把 config.json 里 startup_check 改成 true。
        #      · 已限购 → start 会返回限购提示页（"每名顾客最多可购买 2 个…"）
        #      · 袋被清空 → 返回 {"isEmptyCart": true}
        #      · 正常 → 能拿到 sessionID，并且返回里带着 spokes 列表
        #   体检完【立刻作废会话】（sid=""）——命中时会用正确的门店重新 start，不浪费。
        #   代价：每个号 1 发结算族请求（错峰 5 秒一个），比老版本的"预热建会话+摆店"(4 发) 便宜。
        if getattr(self.cfg.settings, "startup_check", False):
            try:
                self.sess.start()
                _sp = getattr(self.sess, "last_spokes", {}) or {}
                _keys = "、".join("%s#%d" % (k, v["index"]) for k, v in sorted(
                    _sp.items(), key=lambda x: x[1]["index"])) or "（没给 spokes 列表）"
                self.log("[%s]   体检：会话正常 ｜ spokes=[%s]" % (self.cc.name, _keys))
                if "realIdVerification" in _sp:
                    self.log("[%s]   ⚠ 体检发现 realIdVerification 格子（仅提示；是否真的需要实名，以提交时服务端返回的实名页为准）" % self.cc.name)
                self.sess.sid = ""          # 体检用完就作废，命中时按门店重建
                self.sess.expired = False
            except Exception as e:
                es = str(e)
                if es.startswith("QUOTA"):
                    self.log("[%s] ⛔ 体检发现这个号已限购：%s" % (self.cc.name, es[6:]))
                    self.log("[%s]    → 这个号直接退出，不会浪费后面的命中机会" % self.cc.name)
                    self.stat.update(state="已达购买上限", note=es[6:], t_note=time.time())
                    self.dead = True
                    return
                if "isEmptyCart" in es or "购物袋是空的" in es:
                    self.log("[%s]   ⚠ 体检发现购物袋是空的 → 再备一次袋" % self.cc.name)
                    try:
                        self.sess.prepare_cart(self._order_qty())
                    except Exception as e2:
                        self.log("[%s]   重新备袋失败：%s" % (self.cc.name, e2))
                else:
                    self.log("[%s]   ⚠ 体检异常（不影响后面的流程）：%s" % (self.cc.name, es))
                self.sess.sid = ""
                self.sess.expired = False
        self._cart_ok = True
        self._prepared = True
        sts = self.target_stores()
        self.stat.update(state="探测中", note="袋子已备好，待扫 %d 家店" % len(sts), t_note=time.time())
        self.log("[%s] 预热准备就绪 ｜ 待扫门店 %d 家" % (self.cc.name, len(sts)))

    def keepalive_if_needed(self):
        """★ HTTP 连接保活，不是 checkout 会话保活。

        必须在 worker 主线程发送，才能复用 start 所在的 thread-local 连接。
        只打非结算的 fetchOpenId；有人抢单、自己抢单、限流冷却时一律不发。
        """
        now = time.time()
        if now - self._last_http_keepalive < 30.0:
            return
        if self.stop.is_set() or self.dead:
            return
        if self.busy.is_full() or self._busy.locked():
            return
        if getattr(self.cli, "blocked_until", 0) > now:
            return
        self._last_http_keepalive = now
        url = MERCH.rsplit("/miniprogram-merch", 1)[0] + "/miniprogram-bootstrap/fetchOpenId"
        try:
            r = self.cli.get(url, tries=1, timeout=5)
            if r is None or r.status_code != 200:
                if now - self._last_keepalive_warn >= 300:
                    self._last_keepalive_warn = now
                    self.log("[%s] ⚠ HTTP 保活失败（HTTP %s）" %
                             (self.cc.name, getattr(r, "status_code", 0) or 0))
        except Exception as e:
            if now - self._last_keepalive_warn >= 300:
                self._last_keepalive_warn = now
                self.log("[%s] ⚠ HTTP 保活异常：%s" % (self.cc.name, e))

    def _parallel_contact_and_billing_get(self, contact: dict, last4: str) -> dict:
        """并发保存联系人并打开付款页；付款 POST 仍由主流程随后串行发送。"""
        out = {"contact_ok": False, "billing_ok": False,
               "contact_error": None, "billing_error": None,
               "contact_sec": 0.0, "billing_sec": 0.0, "total_sec": 0.0}
        t0 = time.perf_counter()

        def open_billing():
            tb = time.perf_counter()
            try:
                self.sess.open_spoke_only("billing", "付款")
                return True, None, time.perf_counter() - tb
            except Exception as e:
                return False, e, time.perf_counter() - tb

        billing_future = self._prefetch_executor.submit(open_billing)
        tc = time.perf_counter()
        try:
            self.sess.write_contact(contact, last4, need_get=False,
                                    verify_complete=False)
            out["contact_ok"] = True
        except Exception as e:
            out["contact_error"] = e
        out["contact_sec"] = time.perf_counter() - tc
        out["billing_ok"], out["billing_error"], out["billing_sec"] = billing_future.result()
        out["total_sec"] = time.perf_counter() - t0
        return out

    # ---------------- 抢单 ----------------
    def _checkout_state_ready(self, sid: str) -> Optional[bool]:
        states = getattr(self.sess, "last_checkout_states", {}) or {}
        # pickupMethodReady 实测跟 timeSlot 绑定：时段保存后它才从 false 翻成 true
        # （0921-085252 R766 前后对比确认），所以它归 timeSlot，不归 pickup。
        key = {"billing": "isPaymentReady", "invoice": "invoiceReady",
               "3pp": "pickupContactReady", "timeSlot": "pickupMethodReady"}.get(sid)
        if not key or key not in states:
            return None
        return bool(states.get(key))

    @staticmethod
    def _spoke_value_ready(sid: str, info: dict) -> bool:
        """苹果部分版本会把已保存字段标成 isComplete=false；字段 value/状态才是有效信号。"""
        if not isinstance(info, dict):
            return False
        if info.get("complete"):
            return True
        value = str(info.get("value") or "").strip()
        if not value:
            return False
        if sid == "3pp":
            return not any(x in value for x in ("输入联系", "输入取货", "选择取货"))
        if sid == "billing":
            return not any(x in value for x in ("输入付款", "选择付款", "付款方式"))
        if sid == "invoice":
            return not any(x in value for x in ("输入发票", "选择发票"))
        if sid == "pickup":
            return not any(x in value for x in ("选择门店", "输入地址"))
        if sid == "timeSlot":
            # 未填时服务端给的是占位文案；填好是"今天, 11:00 - 11:15"这类
            return not any(x in value for x in
                           ("选择取货时间", "选择时段", "取货时间段", "选择时间"))
        return False

    def _spoke_ready(self, sid: str, info: dict) -> bool:
        """聚合状态优先；若状态未提供，再回退到 spoke 完成标志和值。"""
        state = self._checkout_state_ready(sid)
        if state is True and sid in ("billing", "invoice", "timeSlot"):
            return True
        if state is False:
            return False
        return self._spoke_value_ready(sid, info)

    def _spokes_text(self) -> str:
        sp = getattr(self.sess, "last_spokes", {}) or {}
        if not sp:
            return "-"
        return ",".join("%s#%s%s" % (k, v.get("index", "?"),
                                      "✓" if self._spoke_ready(k, v) else "!")
                        for k, v in sorted(sp.items(), key=lambda x: x[1].get("index", 999)))

    @staticmethod
    def _page_hint(t: str) -> str:
        """★ 从服务端【退回的那个结算页】里抠出"为什么没受理"，写进日志。

        这一页就是 placeOrder 返回 HTTP 200 但没受理时 body 的内容
        （结算模型原样退回）。以前代码抓了它（detail["last"]）却从没打印过。
        """
        t = t or ""
        out = []
        m = (re.search(r'"alerts"\s*:\s*\[\s*\{[^}]*?"message"\s*:\s*"([^"]{2,120})"', t)
             or re.search(r'"alerts"\s*:\s*\[\s*"([^"]{2,120})"', t))
        if m:
            out.append("服务端提示=%s" % m.group(1))
        m = re.search(r'"view"\s*:\s*"([^"]{2,60})"', t)
        if m:
            out.append("退回页面=%s" % m.group(1))
        m = re.search(r'"errorCode"\s*:\s*"?([A-Za-z0-9_]{2,30})"?', t)
        if m:
            out.append("errorCode=%s" % m.group(1))
        for kw in ("时段", "不可在", "该商品在你选择的门店", "不能在你挑选的",
                   "暂时无法", "售罄", "没货", "请选择", "无法购买"):
            if kw in t:
                out.append("含「%s」" % kw)
        return " ｜ ".join(out[:4])

    def _states_text(self) -> str:
        states = getattr(self.sess, "last_checkout_states", {}) or {}
        keys = ("isPlaceOrderReady", "isPaymentReady", "invoiceReady",
                "pickupMethodReady", "pickupContactReady", "isTermsSubmitReady")
        return ",".join("%s=%s" % (k, str(bool(states[k])).lower()) for k in keys if k in states) or "-"

    def try_buy(self, store: str) -> bool:
        """抢这一家店。★ 全过程都记进事件文件（一次有货 = 一条带完整步骤的记录）"""
        if not self._busy.acquire(blocking=False):
            return False
        if not self.busy.try_enter():
            self._busy.release()
            return False
        t0 = time.time()
        t0_perf = time.perf_counter()
        trace = []
        bounces = [0]
        self._fast_fill_off = False     # ★ 每次抢单都给「快速填格」一次机会（没落库就本单回退）
        whose = None
        day = slot = None
        ack_sec = None
        land_sec = None
        reservation_token = ""
        reserved_qty = 0

        def step(msg):
            line = "[%s] +%.3fs %s | spokes=%s | states=%s" % (
                self.cc.name, time.perf_counter() - t0_perf, msg,
                self._spokes_text(), self._states_text())
            self.log(line)
            trace.append(line)

        def finish(res, extra=None):
            try:
                rec = {"event": "attempt", "store": store, "store_name": self.cfg.store_name(store),
                       "sku_name": sku_name(self.sku), "buyer": (whose.name if whose else ""),
                       "result": res, "sec": round(time.time() - t0, 1), "bounces": bounces[0],
                       "ack_sec": ack_sec, "land_sec": land_sec, "steps": trace}
                if extra:
                    rec.update(extra)
                self._hit_jsonl(rec)
            except Exception:
                pass

        dbg_fp, dbg_wr = (None, None)
        try:
            dbg_fp, dbg_wr = self._dbg_open(store)
            if dbg_wr:
                def _trace(method, url, req_body, status, headers, text, req_headers=None, ms=None):
                    dbg_wr("http", method=method, ms=ms,
                           url=url[:600],
                           path=url.replace("https://web.mmap.apple.com", "")[:160],
                           req=("" if url.endswith("pickup/quote") else (req_body or "")[:4000]),
                           req_headers=self._hdrs(req_headers),
                           status=status,
                           resp_headers=self._hdrs(headers, 4000),
                           loc=(headers.get("Location") or "")[:200],
                           resp_len=len(text or ""),
                           resp=(text or "")[:300000])
                self.cli.trace = _trace
                self.sess.dbg = dbg_wr      # ★ 确认阶段的每一发 status 也写进这份逐单抓包
                self.log("[%s]   🐞 调试抓取已开启（这次抢单的每个请求/返回都会存盘）" % self.cc.name)
            whose = self.buyer_for_store(store)
            self.stat.update(state="命中", note="%s 有货 → 抢（换店/时段/提交）" % store, t_note=time.time())
            head = "★ 命中 %s（%s）｜%s" % (store, self.cfg.store_name(store), sku_name(self.sku))
            self.log("[%s] ★★★ %s → 开抢" % (self.cc.name, head))
            trace.append("%s  %s" % (time.strftime("%H:%M:%S"), head))
            if not whose:
                step("这家店所在城市没有可用取货人（都抢够/没配）→ 只记录不下单")
                finish("没人要素")
                return False
            reservation_token, reserved_qty = self.cfg.ledger.reserve(
                self.cc.id, whose.id, self._order_qty(),
                int(getattr(self.cc, "want", 1) or 1), int(getattr(whose, "want", 1) or 1))
            if not reservation_token:
                step("任务或取货人额度已用完/已被其他账号预占 → 本次不重复下单")
                finish("额度不足或已预占")
                return False
            # 其他账号可能刚消耗了一部分取货人额度，把本单件数收缩到实际预占数。
            if self.warmup_mode == WARMUP_SNIPE and int(getattr(self.sess, "qty", 1) or 1) != reserved_qty:
                self.sess.prepare_cart(reserved_qty)
                self._cart_dirty = False        # ★ 袋子刚重建过 → 清脏
                self.sess.openid = self.cli.fetch_openid()
                self.sess.sid = ""
                self._full_ready = False
                self._prepared = False
                self._cart_ok = True
            contact, last4 = whose.contact(), whose.last4
            step("取货人 = %s%s / %s / 后四位 %s" % (contact["lastName"], contact["firstName"],
                                                   contact["phone"], last4))
            # ---- ① 按模式进入目标店会话 ----
            t1 = time.time()
            reused_full = False
            if self.warmup_mode == WARMUP_SNIPE and self._full_ready and self.sess.sid:
                if str(getattr(self.sess, "store", "") or "").upper() == str(store or "").upper():
                    # ★★ 2026-09-24【固定门店模式】预热会话本来就建在这家店上 →
                    #    完全不用换店，省掉一发 selectStoreUrl（实测约 0.8s）。
                    reused_full = True
                    step("① 会话已在本店（固定门店）→ 不换店直接复用（%.2fs）" % (time.time() - t1))
                    # ★★ 自愈：会话在目标店上但 spokes 里没有 timeSlot 格（预热时这家店还没放时段）。
                    #   此时不刷新的话，后面逐格填的循环连 timeSlot 都看不到，会直接提交没时段的单。
                    #   补一发 selectStoreUrl（选店页预热时已打开，就一发 POST）让服务端重新评估。
                    if "timeSlot" not in (getattr(self.sess, "last_spokes", {}) or {}):
                        try:
                            self.sess.switch_store(store, self.cfg.store_name(store))
                            step("① 会话在本店但 spokes 无 timeSlot → 补选店刷新（%.1fs）"
                                 % (time.time() - t1))
                        except StoreUnavailable:
                            self._full_ready = False
                            self._prepared = False
                            self._cart_ok = False
                            self.sess.sid = ""
                            step("① 补选店失败：门店取不了（被抢了）")
                            self._cool_store(store, reason="门店取不了")
                            finish("没成：门店取不了（被抢了）")
                            return False
                        except Exception as e:
                            step("① 补选店刷新失败（%s）→ 按原 spokes 继续" % e)
                else:
                    try:
                        self.sess.switch_store(store, self.cfg.store_name(store))
                        reused_full = True
                        step("① 捡漏预热会话换店 OK（%.1fs）｜门店=%s ｜ 已复用预热资料"
                             % (time.time() - t1, store))
                    except StoreUnavailable:
                        # 服务端已经明确拒绝目标门店。此时再用同一购物袋执行标准
                        # start 既不会恢复库存，还可能把换店失败后的空袋误报成主因。
                        self._full_ready = False
                        self._prepared = False
                        self._cart_ok = False
                        self.sess.sid = ""
                        step("① 换店失败：门店取不了（被抢了）")
                        self._cool_store(store, reason="门店取不了")
                        finish("没成：门店取不了（被抢了）")
                        return False
                    except Exception as e:
                        step("① 预热会话换店失败（%s）→ 当场退回标准建会话" % e)
                        self._full_ready = False
                        self._prepared = False
                        self.sess.sid = ""
            try:
                if not reused_full:
                    if self.warmup_mode == WARMUP_FIRST:
                        step("① 首发模式：命中后才加入购物袋")
                        self.sess.prepare_cart(reserved_qty)
                        self._cart_dirty = False    # ★ 首发模式每次命中都现备袋 → 清脏
                        self.sess.openid = self.cli.fetch_openid()
                        self._cart_touched = True
                        self._cart_ok = True
                    self.sess.save_store(store)
                    self.sess.sid = ""
                    self.sess.start()
            except Exception as e:
                es = str(e)
                self._cart_ok = False
                self._prepared = False if self.warmup_mode != WARMUP_FIRST else True
                if isinstance(e, StoreUnavailable):
                    step("① 门店取不了（被抢了）")
                    self._cool_store(store, reason="门店取不了")
                    finish("没成：门店取不了（被抢了）")
                    return False
                step("① 建会话失败：%s" % es)
                if es.startswith("QUOTA"):
                    # ★★ 这个 Apple ID 买满了 → 直接让这个号收工，别再拿它去撞
                    self.log("[%s] ⛔ %s" % (self.cc.name, es[6:]))
                    self.log("[%s]    → 这个号先退出（不影响其它号）；要再用请换号或等苹果放开"
                             % self.cc.name)
                    self.stat.update(state="已达购买上限", note=es[6:], t_note=time.time())
                    self.dead = True
                    finish(es[6:])
                    return False
                # ★★ 购物袋空了：这是【账号侧】的状态（苹果会主动清袋，比如临近限购时），
                #    跟门店完全无关 —— 所以【绝不能冷却门店】，否则会白白丢掉一家一直有货的店。
                #    正确做法：当场重新备袋，下一轮命中时袋子已经好了。
                if "isEmptyCart" in es or "购物袋是空的" in es:
                    self._cart_dirty = True      # ★ 袋子空了 → 必须先重建再试下一家
                    self.log("[%s]   ⚠ 购物袋被清空了（账号侧，和门店无关）→ 下一轮按当前模式重建"
                             % self.cc.name)
                    finish("购物袋被清空（下一轮重建，不冷却门店）")
                    return False
                # 其它建会话失败（网络/541/服务端异常）→ 才冷却门店
                self._cool_store(store, reason="建会话失败")
                finish("建会话失败：%s" % es)
                return False
            t2 = time.time()
            # ★★★ 关键：服务端在返回里给了 spokes 列表（每一格都标了 id）——
            #    这是权威信息，不用再猜索引。先看它说了什么：
            #      realIdVerification 在列表里 → 这个号要实名验证，工具做不了，别白打
            #      timeSlot 不在列表里        → 记为软信号，仍继续完整提交
            sp = getattr(self.sess, "last_spokes", {}) or {}
            prewarm_contact_value = str((sp.get("3pp") or {}).get("value") or "")
            prewarm_billing_value = str((sp.get("billing") or {}).get("value") or "")
            get_only_contact = bool(reused_full and prewarm_contact_value and
                                    "输入联系信息" not in prewarm_contact_value)
            get_only_billing = bool(reused_full and "微信" in prewarm_billing_value)
            if reused_full and not self._prewarm_contact:
                # ★ 本次没预热联系人 → 联系人格必然是空的，命中时使用任务绑定的
                #   whose 现场填（GET+POST）。这里只把日志说清楚，不用改 spokes。
                step("① 未预热联系人 → 命中时现场填写绑定取货人=%s" % whose.name)
            # ★ 每格带上服务端的 isComplete：标 (未完) 的才是真正要填的格；
            #   某格服务端已标完成 → 下面的循环自动跳过它 = 白省一发（提速的关键情报）
            _sp_sorted = sorted(sp.items(), key=lambda x: x[1]["index"])
            sp_txt = "、".join("%s#%d%s" % (k, v["index"], "" if self._spoke_ready(k, v) else "(未完)")
                               for k, v in _sp_sorted)
            todo_txt = "、".join("%s#%d" % (k, v["index"]) for k, v in _sp_sorted
                                 if not self._spoke_ready(k, v))
            if dbg_wr:
                dbg_wr("phase", name="spokes清单", sec=round(t2 - t1, 2), keys=sp_txt,
                       todo=todo_txt)
            step("① %s OK（%.1fs）｜门店=%s ｜ spokes=[%s]｜未完=[%s]"
                 % ("预热会话换店" if reused_full else "定店 + 建会话",
                    t2 - t1, store, sp_txt, todo_txt or "无"))
            if "realIdVerification" in sp:
                # ★ 这个格子只能说明服务端在当前 checkout 里挂了一个实名入口，
                #   不等于已经返回“验证信息”实名页。绝不能在这里提前判定并退出，
                #   否则会把门店取货不可用、普通弹回等响应全部误报成实名。
                #   真正的实名页只在 placeOrder 返回 mmap-realid-verification /
                #   authRequired + idVerificationType 时判定（session.submit 已精确处理）。
                step("① 注意到 spokes 含 realIdVerification（%s）→ 先按服务端流程继续，提交时精确确认"
                     % (sp["realIdVerification"].get("value") or ""))
            if "timeSlot" not in sp:
                step("① 当前 spokes 无 timeSlot → 视为软信号，继续补齐其他格并尝试提交")
            # ★★ 2026-09-24 观测点：预热写进去的时段，【换店之后】还算不算数？
            #    这就是判断"时段能不能提前预热"的唯一依据（日志里直接看 true/false）。
            if "timeSlot" in sp:
                _info = sp.get("timeSlot") or {}
                if self._spoke_ready("timeSlot", _info):
                    step("🔥 时段预暖生效：换店后 timeSlot=true ✓ → 本单省掉「拉时段页+存时段」")
                elif reused_full:
                    step("时段预暖未生效：换店后 timeSlot=%s（值=%s）→ 本单照常现场拉取保存"
                         % (str(bool(_info.get("complete"))).lower(), str(_info.get("value") or "")[:40]))
            if dbg_wr: dbg_wr("phase", name="定店建会话", sec=round(t2 - t1, 2), store=store)
            # ---- ②③ 照服务端的 spokes 列表逐格填（"调什么填什么"）----
            #   服务端在每次 checkoutx 返回里都带 spokes 列表，每一格都有 id + isComplete：
            #       pickup=门店(已由定店完成)  timeSlot=时段  3pp=取货联系人
            #       billing=付款   invoice=发票   realIdVerification=实名验证
            #   → 我们【不再硬编码填什么、按什么顺序】，只照着列表把没完成的格子填掉。
            #     发票要不要填也由服务端说了算（列表里有就填、没有就跳）。
            #   每一格仍然是"先 GET 这一格 → 再 POST 保存"（实测少 GET 会被别的格顶掉）。
            t3 = time.time()
            filled, day, slot = [], None, None
            ok_bill = True
            parallel_billing_ready = False
            parallel_prefill_tried = False
            NEED = ["timeSlot", "3pp", "billing", "invoice"]
            # ★ 不再并行预取 spoke。3pp 联系人页必须在时段保存后现场 GET，
            #   否则预取页会在时段落库后失效，反而多一次 GET+POST。
            for _rd in range(10):
                sp = getattr(self.sess, "last_spokes", {}) or {}
                todo = sorted([(v["index"], k) for k, v in sp.items()
                               if k in NEED and k not in filled and not self._spoke_ready(k, v)])
                if not todo:
                    break
                _idx, sid = todo[0]
                if sid == "timeSlot":
                    _slot_idx = max(1, int(getattr(self.cfg.settings, "time_slot_index", 1) or 1))
                    day, slot, raw = self.sess.pull_slot(_slot_idx)
                    if not slot:
                        if "timeSlot" not in sp:
                            why = "服务端 spokes 列表里没有可用的 timeSlot"
                        elif getattr(self.sess, "last_slot_kind", "") != "时段页":
                            why = "时段格定位后打开的是%s，自动纠偏仍失败" % \
                                  getattr(self.sess, "last_slot_kind", "其他页")
                        elif len(raw) < 200:
                            why = "请求失败（%d 字节）" % len(raw)
                        else:
                            why = "时段页里没有可选的时段（假有货/被抢走）"
                        step("② 时段：%s（%.1fs）→ %s"
                             % (why, time.time() - t3,
                                "开关要求需要时段 → 判定没货，不进结算"
                                if getattr(self.cfg.settings, "require_time_slot", False)
                                else "不提前返回，继续完整提交流程"))
                        self._dump_slot_page(store, raw)
                        # ★ 空时段（假有货）也要冷却：否则扫描每轮 0.4 秒就会再把这家店拉进结算，
                        #   每轮白打一遍结算族请求 + placeOrder，容易招 403/541。
                        #   冷却期间探测（看有没货）照旧不受影响。
                        self._cool_store(store, reason="时段为空（假有货）")
                        if getattr(self.cfg.settings, "require_time_slot", False):
                            # ★★ 开关=开：没有可填时段的地方 就当没货 → 不进结算、不提交
                            finish("没成：没有时段（已按【需要时段】判定为没货，未提交）")
                            return False
                        filled.append(sid)  # 本轮不再重复拉同一个空时段页
                        # 时段是后续表单的前置，所以空时段时联系人/付款/发票
                        # 可能也会被服务端拒绝。这里逐格 best-effort：能存的都存，
                        # 某一格存不进也不会再提前 return，最后仍然发 placeOrder。
                        for _soft_sid in ("3pp", "billing", "invoice"):
                            _soft_sp = (getattr(self.sess, "last_spokes", {}) or {}).get(_soft_sid)
                            if not _soft_sp or self._spoke_ready(_soft_sid, _soft_sp):
                                continue
                            try:
                                if _soft_sid == "3pp":
                                    self.sess.write_contact(contact, last4, need_get=True)
                                    step("③ 空时段兜底：联系人已尝试保存")
                                elif _soft_sid == "billing":
                                    self.sess.write_billing(need_get=True)
                                    step("③ 空时段兜底：付款已尝试保存")
                                else:
                                    self.sess.write_invoice(contact.get("email", ""))
                                    step("③ 空时段兜底：发票已尝试保存")
                            except Exception as e:
                                step("③ 空时段兜底：%s保存未成（%s）→ 仍继续 placeOrder"
                                     % ({"3pp": "联系人", "billing": "付款", "invoice": "发票"}[_soft_sid], e))
                            filled.append(_soft_sid)
                        break
                    self._cool_ok(store)
                    try:
                        self.sess.save_slot(day, slot)
                    except StoreUnavailable:
                        step("② 门店取不了（被抢了）")
                        self._cool_store(store, reason="门店取不了")
                        finish("没成：门店取不了（被抢了）")
                        return False
                    step("② 时段 OK：%s %s（第 %d 个可用时段）" %
                         (day.get("date"), slot.get("time"), _slot_idx))
                    if dbg_wr: dbg_wr("phase", name="时段", date=day.get("date"), time=slot.get("time"))
                elif sid == "3pp":
                    if get_only_contact:
                        if (get_only_billing and "billing" in NEED and
                                "billing" not in filled and not parallel_prefill_tried):
                            parallel_prefill_tried = True
                            pr = self._parallel_contact_and_billing_get(contact, last4)
                            errors = [x for x in (pr["contact_error"], pr["billing_error"]) if x]
                            if any(isinstance(x, StoreUnavailable) for x in errors):
                                step("③ 联系人/付款并发阶段：门店取不了（被抢了）")
                                self._cool_store(store, reason="门店取不了")
                                finish("没成：门店取不了（被抢了）")
                                return False
                            if pr["contact_ok"] and pr["billing_ok"]:
                                parallel_billing_ready = True
                                filled.append(sid)
                                step("③ 并发预取成功：联系人 POST %.2fs ∥ 付款 GET %.2fs"
                                     " → 并发段 %.2fs"
                                     % (pr["contact_sec"], pr["billing_sec"], pr["total_sec"]))
                                continue
                            why_parallel = "；".join(str(x) for x in errors) or "未知错误"
                            step("③ 并发预取未完成（%s）→ 回退原串行流程" % why_parallel)
                            # 联系人已经成功时无需重复写；付款稍后按原流程重新 GET。
                            if pr["contact_ok"]:
                                filled.append(sid)
                                continue
                        try:
                            # 换店后 isComplete 始终是 false，不能再拿它判断 POST 是否落库。
                            # 真实 placeOrder 只弹回付款，证明这发联系人 POST 已经生效。
                            self.sess.write_contact(contact, last4, need_get=False,
                                                    verify_complete=False)
                            step("③ 预热联系人：直接 POST 确认「%s」（省掉前置 GET）"
                                 % prewarm_contact_value.strip())
                        except FastFillMiss:
                            try:
                                self.sess.write_contact(contact, last4, need_get=True)
                                step("③ 预热联系人：首次保存未落库，已按 GET+POST 重存")
                            except StoreUnavailable:
                                step("③ 重存联系人时：门店取不了（被抢了）")
                                self._cool_store(store, reason="门店取不了")
                                finish("没成：门店取不了（被抢了）")
                                return False
                            except Exception as e:
                                step("③ 重存联系人失败：%s" % e)
                                finish("存联系人失败：%s" % e)
                                return False
                        except StoreUnavailable:
                            step("③ 打开联系人页时：门店取不了（被抢了）")
                            self._cool_store(store, reason="门店取不了")
                            finish("没成：门店取不了（被抢了）")
                            return False
                        except Exception as e:
                            step("③ 预热联系人 GET 失败：%s" % e)
                            self._cool_store(store, reason="联系人页打不开")
                            finish("联系人页失败：%s" % e)
                            return False
                        filled.append(sid)
                        continue
                    # ★ 提速（默认开）：省掉联系人那一发的「前置 GET」（-1 发 ≈ -0.5s）。
                    #   安全网：① 响应里 3pp 仍(未完) → FastFillMiss → 本格立刻回退 GET+POST；
                    #          ② 本单只要回退过一次，后面全走标准流程（_fast_fill_off）。
                    # 稳定版固定走普通联系人流程：GET 联系人页 → POST 保存。
                    # 不再尝试省 GET 的快速填格，避免先打一发无效 POST 再回退。
                    fast_3pp = False
                    try:
                        self.sess.write_contact(contact, last4, need_get=not fast_3pp)
                        if fast_3pp:
                            self._fast_fill_miss = 0
                            step("③ 3pp：快速填格（省掉前置 GET，已落库）")
                    except FastFillMiss as e:
                        self._fast_fill_off = True
                        self._fast_fill_miss = getattr(self, "_fast_fill_miss", 0) + 1
                        if self._fast_fill_miss >= 2:
                            self._fast_fill_dead = True
                        step("③ 3pp 快速填格没落库（%s）→ 回退标准流程（GET+POST）｜连续没落库 %d 次%s"
                             % (e, self._fast_fill_miss,
                                "，本号已停用快速填格（config 里 fast_fill_contact=false 也同理）"
                                if getattr(self, "_fast_fill_dead", False) else ""))
                        try:
                            self.sess.write_contact(contact, last4, need_get=True)
                        except StoreUnavailable:
                            step("③ 联系人阶段：门店取不了（被抢了）")
                            self._cool_store(store, reason="门店取不了")
                            finish("没成：门店取不了（被抢了）")
                            return False
                        except Exception as e2:
                            step("③ 回退后仍存不进联系人：%s" % e2)
                            self._cool_store(store, reason="联系人存不进去")
                            finish("存联系人失败：%s" % e2)
                            return False
                    except StoreUnavailable:
                        step("③ 联系人阶段：门店取不了（被抢了）")
                        self._cool_store(store, reason="门店取不了")
                        finish("没成：门店取不了（被抢了）")
                        return False
                    except Exception as e:
                        step("③ 存联系人失败：%s" % e)
                        self._cool_store(store, reason="联系人存不进去")
                        finish("存联系人失败：%s" % e)
                        return False
                elif sid == "billing":
                    if get_only_billing:
                        try:
                            if not parallel_billing_ready:
                                try:
                                    self.sess.open_spoke_only("billing", "付款")
                                except StoreUnavailable:
                                    raise
                                except RuntimeError:
                                    # 如果直 POST 联系人尚未解锁付款页，才走一次完整联系人保存。
                                    self.sess.write_contact(contact, last4, need_get=True,
                                                            verify_complete=False)
                                    step("③ 付款页仍被联系人页拦住 → 已补联系人 GET+POST")
                                    self.sess.open_spoke_only("billing", "付款")
                            self.sess.write_billing(need_get=False)
                            step("③ 预热付款：%s后 POST 确认「%s」，直接提交"
                                 % ("复用并发 GET " if parallel_billing_ready else "GET ",
                                    prewarm_billing_value.strip()))
                        except StoreUnavailable:
                            step("③ 打开付款页时：门店取不了（被抢了）")
                            self._cool_store(store, reason="门店取不了")
                            finish("没成：门店取不了（被抢了）")
                            return False
                        except Exception as e:
                            step("③ 预热付款 GET 失败：%s" % e)
                            self._cool_store(store, reason="付款页打不开")
                            finish("付款页失败：%s" % e)
                            return False
                        filled.append(sid)
                        continue
                    try:
                        ok_bill = self.sess.write_billing()
                    except StoreUnavailable:
                        ok_bill = False
                        step("③ 付款阶段：门店取不了（被抢了）")
                        self._cool_store(store, reason="门店取不了")
                        finish("没成：门店取不了（被抢了）")
                        return False
                    except Exception as e:
                        ok_bill = False
                        step("③ 存付款失败：%s" % e)
                        self._cool_store(store, reason="付款存不进去")
                        finish("存付款失败：%s" % e)
                        return False
                elif sid == "invoice":
                    try:
                        self.sess.write_invoice(contact.get("email", ""))
                    except StoreUnavailable:
                        step("③ 发票阶段：门店取不了（被抢了）")
                        self._cool_store(store, reason="门店取不了")
                        finish("没成：门店取不了（被抢了）")
                        return False
                    except Exception as e:
                        step("③ 存发票失败：%s" % e)
                        self._cool_store(store, reason="发票存不进去")
                        finish("存发票失败：%s" % e)
                        return False
                filled.append(sid)
            t5 = time.time()
            if self.sess.expired:
                step("填表时发现会话已过期 → 立刻重建")
                finish("会话过期")
                return False
            # ★ 不再因为"没拿到时段"就跳过：无论列表没有 timeSlot，
            #   还是时段页暂时为空，都继续补齐其他格并尝试 placeOrder。
            if dbg_wr:
                dbg_wr("phase", name="填表完成", sec=round(t5 - t3, 2), billing_ok=bool(ok_bill),
                       filled="、".join(filled))
            step("③ 照 spokes 逐格填：%s（%.1fs）｜共 %d 格：%s"
                 % ("OK" if ok_bill else "付款失败", t5 - t3, len(filled), "→".join(filled) or "无"))
            if self.sess.expired:
                step("预写时发现会话已过期 → 立刻重建")
                finish("会话过期")
                return False
            if self.dry_run:
                step("⚠ 干跑模式 → 到此为止，不提交（整条链已验证：摆店→时段→挑人 都正常）")
                self.settle_until[store] = time.time() + self.cfg.settings.retry_no_slot_sec
                finish("干跑（未提交）")
                return False
            if not self.auto_submit:
                step("⚠ auto_submit=false → 不自动提交（已就绪，就差提交）")
                finish("未开自动提交")
                return False
            # ---- ④ 提交 ----
            t6 = time.time()
            step("④ 提交前检查：条款接受字段已准备，将随 placeOrder 一并发送")

            def _on_bounce(n):
                bounces[0] = n
                trace.append("%s  ④ 服务端要补信息（第 %d 次）→ 一并补上" % (time.strftime("%H:%M:%S"), n))

            done, why, detail = self.sess.submit(
                contact, last4, on_bounce=_on_bounce,
                require_slot=bool(getattr(self.cfg.settings, "require_time_slot", False)))
            t7 = time.time()
            # ★★ 时段可能不是我们填的：① 服务端 spokes 里 timeSlot 已标完成；
            #    ② 提交被弹回后由 session.submit 现补 —— 都要捞回来，否则下面 day.get 会炸
            day = day or getattr(self.sess, "last_saved_day", None)
            slot = slot or getattr(self.sess, "last_saved_slot", None)
            bounces[0] = detail.get("bounces", bounces[0])
            used = t7 - t0
            ack_sec = detail.get("ack_sec")
            land_sec = detail.get("land_sec")
            ack_txt = ("%.2fs" % ack_sec) if ack_sec is not None else "-"
            land_txt = ("%.2fs" % land_sec) if land_sec is not None else "-"
            step("④ 提交：%s ｜ 弹回 %d 次 ｜ placeOrder受理 %s ｜ 感谢页确认 %s ｜ 合计 %.1fs"
                 % ("成功" if done else "没成（" + why + "）", detail.get("bounces", 0),
                    ack_txt, land_txt, t7 - t6))
            if not done:
                _last = str(detail.get("last") or "")
                _hint = self._page_hint(_last) if _last else ""
                if _hint:
                    step("   ↳ 服务端退回的页面线索：%s" % _hint)
                else:
                    step("   ↳ 服务端退回了一页，但没抓到提示文字（长度 %d 字节）" % len(_last))
            if dbg_wr:
                dbg_wr("phase", name="提交结束", done=bool(done), why=why,
                       bounces=detail.get("bounces", 0), needs=detail.get("needs", []),
                       order=detail.get("order") or "", sec=round(t7 - t6, 2),
                       ack_sec=ack_sec, land_sec=land_sec)
            step("⏱ 时序：本地定店+建会话 %.1fs ｜ 填表 %.1fs ｜ "
                 "提交受理 %s ｜ 落库确认 %s ｜ 合计 %.1fs"
                 % (t2 - t1, t5 - t3, ack_txt, land_txt, used))
            if done:
                self.orders += 1
                # ★★ 成单后购物袋已被消耗空 → 立刻作废会话，逼下一轮重新备袋加购
                #    （否则下次命中会用已经不存在的 lineItemId 提交 = 幽灵单：服务端受理但内容无效）
                self.sess.sid = ""
                self._cart_ok = False
                self._full_ready = False
                self._prepared = (self.warmup_mode == WARMUP_FIRST)
                self._cart_touched = False
                self._cart_dirty = True      # ★ 成单 → 袋子已被消耗空
                self.log("[%s]   袋子已被这一单消耗 → 下一轮重新备袋" % self.cc.name)
                try:
                    self.cfg.ledger.commit(reservation_token, self.sku, self.cc.name, whose.name)
                    reservation_token = ""
                    n = self.cfg.ledger.task_total(self.cc.id)
                    bn = self.cfg.ledger.buyer_total(whose.id)
                    w, bw = int(getattr(self.cc, "want", 1) or 1), int(getattr(whose, "want", 1) or 1)
                    step("📒 账本：任务 %d/%d，取货人合计 %d/%d%s"
                         % (n, w, bn, bw, "（到量，移出监控）" if n >= w or bn >= bw else ""))
                    if not self._buyers():
                        step("⏸ %s 下面没人还要了 → 这个型号停监控，等新取货人加入" % sku_name(self.sku))
                except Exception as e:
                    step("记账失败：%s" % e)
                self.stat.update(state="已成单", note="订单 %s @ %s" % (detail.get("order") or "?", store),
                                 t_note=time.time())
                self.log("[%s] ★ 下单成功！%s @ %s 取货 %s %s ｜ 弹回 %d 次 ｜ 受理 %s ｜ 确认 %s ｜ 总 %.1f 秒"
                         % (self.cc.name, self.sku, store, (day or {}).get("date") or "-",
                            (slot or {}).get("time") or "-",
                            detail.get("bounces", 0), ack_txt, land_txt, used))
                on = detail.get("order") or ""
                subj = ("【抢到了】%s%s  %s %s @ %s%s"
                        % (contact["lastName"], contact["firstName"], self.sku, sku_name(self.sku),
                           self.cfg.store_name(store), ("  | 订单号 " + on) if on else ""))
                body = (
                    "★ 订单号：{order}\n"
                    "\n"
                    "机型：{sku} {sku_cn}（{sku}）\n"
                    "门店：{store_name} [{store}]\n"
                    "取货时间：{date} {slot}\n"
                    "取货人：{last}{first}  {phone}（证件后四位 {last4}）\n"
                    "下单时间：{now}\n"
                    "下单身份：{acc} ｜ 账号指纹 as_dima={fp}…\n"
                    "提交弹回：{bounces} 次 ｜ placeOrder受理 {ack} ｜ 感谢页确认 {land} ｜ 命中到成单 {used:.1f} 秒\n"
                    "\n"
                    "★ 订单查看链接（不用登录，浏览器直接打开）：\n"
                    "   {link}\n"
                    "   （短时间反复刷会被苹果 541 防爬，点一次就好）\n"
                    "\n"
                    "────────── 怎么付款 ──────────\n"
                    "付款要在微信里点（小程序内调起微信支付）：\n"
                    "  1) 打开【{acc}】那个微信号的微信（必须是下单用的那个号，换号看不到）\n"
                    "  2) 微信 → 我 → 服务 → 我的订单\n"
                    "  3) 找到订单号 {order} 这一单 → 点【立即支付】\n"
                    "  ⚠ 未支付有时限，过期订单会被自动取消、额度也就白占\n"
                    "\n"
                    "────────── 苹果官方那封邮件 ──────────\n"
                    "苹果会把订单确认发到【取货人的邮箱】：{email}\n"
                    "  → 去那个邮箱找 Apple 发来的邮件（里面是官方订单信息）\n"
                    "\n"
                    "Apple 官网订单入口（需 Apple ID，微信渠道单通常查不到）：\n"
                    "   https://www.apple.com.cn/shop/order/list\n"
                ).format(order=on or "(没抓到)",
                         sku=self.sku, sku_cn=sku_name(self.sku),
                         store_name=self.cfg.store_name(store), store=store,
                         date=(day or {}).get("date") or "-", slot=(slot or {}).get("time") or "-",
                         last=contact["lastName"], first=contact["firstName"],
                         phone=contact["phone"], last4=last4,
                         now=time.strftime("%Y-%m-%d %H:%M:%S"), acc=self.cc.name, fp=self.fp,
                         bounces=detail.get("bounces", 0), ack=ack_txt, land=land_txt, used=used,
                         link=detail.get("link") or "(没拿到，去订单列表看最新那单)",
                         email=contact.get("email") or "(取货人邮箱)")
                send_mail(self.cfg.settings.email, subj, body, logger=self.log)
                alarm(self.cfg.settings.alarm_sec)
                finish("★ 成单", {"order": on})
                if self.on_order:
                    self.on_order({"cookie": self.cc.name, "sku": self.sku, "store": store,
                                   "buyer": whose.name, "order": on or "",
                                   "link": detail.get("link") or "",
                                   "date": (day or {}).get("date"),
                                   "time": (slot or {}).get("time"),
                                   "bounces": detail.get("bounces", 0), "ack_sec": ack_sec,
                                   "land_sec": land_sec, "sec": round(used, 1)})
                return True
            self.log("[%s]   没成：%s（弹回 %d 次，用时 %.1f 秒）"
                     % (self.cc.name, why, detail.get("bounces", 0), used))
            finish("没成：" + why, {"needs": detail.get("needs", [])})
            # placeOrder 已经发出后，会话和购物袋都不能继续视为可复用。
            self.sess.sid = ""
            self._cart_ok = False
            self._full_ready = False
            self._prepared = (self.warmup_mode == WARMUP_FIRST)
            if any(k in why for k in ("门店取不了", "没时段", "未确认", "提交未受理", "实名")):
                self.log("[%s]   ⚠ 结算后购物袋状态未知 → 下一轮先在后台复核" % self.cc.name)
            if "门店取不了" in why:
                self._cool_store(store, reason="提交后门店取不了")
            elif "没时段" in why:
                # ★ 拉时段失败时已经按【时段为空】冷却过一次了；若那次冷却还在有效期，就只把理由说准：
                #   再次冷却会让失败计数多算一次（日志里的"失败第 N 次"就会读不准）。
                #   若那次冷却已经过期（提交耗时崩了），才补冷一次，避免下一轮驪着再试。
                if time.time() >= self.settle_until.get(store, 0):
                    self._cool_store(store, reason="时段为空（提交后）")
                else:
                    self.log("[%s]   ⏱ 这家店已在冷却中（拉时段失败时已记）→ 不重复计数" % self.cc.name)
            if "未确认" in why or "没走到感谢页" in why:
                # ★★ 提交后服务端已经 302 受理，只是我们没确认到感谢页 —— 大概率单已经建出来了
                #    保守处理：记账（免得同一个人的额度被重复用掉）+ 发警示邮件 + 长时间冷却这家店
                self.log("[%s]   ⚠⚠ 提交已受理但【未确认到感谢页】→ 大概率已经建单！"
                         "去 微信 → 我的订单 里确认一下，别重复下单" % self.cc.name)
                self.stat.update(state="可能已成单", note="%s @ %s（未确认）" % (self.sku, store),
                                 t_note=time.time())
                try:
                    send_mail(self.cfg.settings.email,
                              "【注意】可能已成单，请去订单页确认 %s %s @ %s"
                              % (whose.name, sku_name(self.sku), self.cfg.store_name(store)),
                              ("★ 订单号：未抓到（服务端没渲染出感谢页）→ 去订单列表看【最新那一单】\n"
                               "\n"
                               "时间：{now}\n"
                               "机型：{sku} {sku_cn}（{sku}）\n门店：{store_name} [{store}]\n"
                               "取货时间：{date} {slot}\n"
                               "取货人：{last}{first}  {phone}（证件后四位 {last4}）\n"
                               "下单身份：{acc} ｜ as_dima={fp}…\n\n"
                               "★ 提交后服务端已受理（302 → 状态页），但查 status 没确认到感谢页。\n"
                               "★ 请去【{acc}】的微信 → 我 → 服务 → 我的订单，看最新那单有没有建出来，别重复下单。\n"
                               ).format(now=time.strftime("%Y-%m-%d %H:%M:%S"),
                                        sku=self.sku, sku_cn=sku_name(self.sku),
                                        store_name=self.cfg.store_name(store), store=store,
                                        date=(day or {}).get("date"), slot=(slot or {}).get("time"),
                                        last=contact["lastName"], first=contact["firstName"],
                                        phone=contact["phone"], last4=last4,
                                        acc=self.cc.name, fp=self.fp),
                              logger=self.log)
                except Exception as e:
                    self.log("[%s]   [邮件] 拼装失败：%s" % (self.cc.name, e))
                try:
                    self.cfg.ledger.commit(reservation_token, self.sku, self.cc.name, whose.name)
                    reservation_token = ""
                    self.log("[%s]   📒 账本（保守记账）：%s 本任务 %d/%d，取货人合计 %d/%d"
                             % (self.cc.name, whose.name,
                                self.cfg.ledger.task_total(self.cc.id), int(getattr(self.cc, "want", 1) or 1),
                                self.cfg.ledger.buyer_total(whose.id), int(getattr(whose, "want", 1) or 1)))
                except Exception:
                    pass
                self.settle_until[store] = time.time() + 600        # 这家店 10 分钟别再碰，避免重复下单
                self.sess.sid = ""            # ★ 同上：可能已成单 → 袋子状态未知，重新备袋
                self._cart_ok = False
                self._full_ready = False
                self._cart_dirty = True       # ★ 袋子状态未知 → 先重建再试下一家
            self._prepared = (self.warmup_mode == WARMUP_FIRST)
            if why == "QUOTA":
                _qm = (detail or {}).get("quota_msg") or ""
                self.log("[%s]   ⛔ 服务端拒绝（限购）｜苹果原文：%s" % (self.cc.name, _qm))
                self.log("[%s]   → 这个号先退出，其它号继续跑" % self.cc.name)
                self.dead = True
                self.stat.update(state="被限购", note=_qm[:60], t_note=time.time())
                # ★ 用 .format() 而不是 %：这里原来是 %s 占位符比实参多一个，
                #   一发料就抛 TypeError（被外层 except 吞掉，只留下一行"抢单异常"）。
                send_mail(self.cfg.settings.email,
                          "【购买上限】%s 这个号买不了 %s" % (self.cc.name, self.sku),
                          ("时间：{now}\n"
                           "服务端原文：{qm}\n"
                           "（quantity_limit_exceeded）\n"
                           "机型：{sku} {sku_cn}（{sku}）\n"
                           "门店：{store_name} [{store}]\n"
                           "\n"
                           "→ 这是【账号级】限制：这个号买这个型号已经到上限了，换店/换时段都没用。\n"
                           "→ 想要继续抢，请换账号或等待 Apple 解除限制；换取货人通常无效。\n"
                           ).format(now=time.strftime("%Y-%m-%d %H:%M:%S"), qm=_qm,
                                    sku=self.sku, sku_cn=sku_name(self.sku),
                                    store_name=self.cfg.store_name(store), store=store),
                          logger=self.log)
            return False
        except Exception as e:
            self.stat.update(state="出错", note="%s: %s" % (type(e).__name__, e), t_note=time.time())
            self.log("[%s] 抢单异常：%s: %s" % (self.cc.name, type(e).__name__, e))
            try:
                finish("异常：%s" % type(e).__name__)
            except Exception:
                pass
            return False
        finally:
            try:
                if reservation_token:
                    self.cfg.ledger.release(reservation_token)
                if self.warmup_mode == WARMUP_FIRST and self._cart_touched:
                    cleared = self.sess.clear_cart()
                    self.log("[%s]   首发模式收尾：购物袋%s，继续保持空袋扫描"
                             % (self.cc.name, "已清空" if cleared else "清空失败"))
                    self._cart_touched = False
                    self._cart_ok = False
                if dbg_wr is not None:
                    dbg_wr("phase", name="本次结束")
                # ★ 抢单结束后不关抓取：回到"全程"抓取（下一发的加袋/建会话也要记）
                self.cli.trace = self._trace_all if getattr(self.cfg.settings, "debug_checkout", False) else None
                self.sess.dbg = None
                if dbg_fp is not None:
                    self._dbg_close(dbg_fp, dbg_wr, "done")
            except Exception:
                pass
            self._busy.release()
            self.busy.clear()          # ★ 抢单结束 → 让其它号恢复探测

    # ---------------- 冷却判定（★ 冷却只挡"进结算"，不挡"探测"） ----------------
    def _due_stores(self, hits, fresh):
        """从本轮"有货"的门店里挑出允许进结算的：
             · 不在冷却里            → 放行
             · 在冷却里 + 老信号      → 跳过（不白烧结算额度）
             · 在冷却里 + 新信号(无货→有货) → 破例放行（这就是"下一秒真放货"的情况）
           返回 [(store, 是否破例)]"""
        now = time.time()
        out = []
        for st in hits:
            if now >= self.settle_until.get(st, 0):
                out.append((st, False))
            elif st in fresh:
                out.append((st, True))
        return out

    # ---------------- 可中断的等待 ----------------
    def _nap(self, secs: float) -> bool:
        """★ 用 Event.wait 取代 time.sleep —— 收到停止信号【立刻返回】。
        返回 True = 已经收到停止信号，调用方应立刻 break/return。

        为什么必须这么做：time.sleep 是"睡死"的，点「停止」只是设了个标志，
        线程要睡完才去看它。之前错峰启动最长要睡 360 秒（7 个号 × 60 秒），
        所以点停止要等 6 分钟才退。"""
        try:
            return bool(self.stop.wait(max(0.0, float(secs))))
        except Exception:
            time.sleep(secs)
            return bool(self.stop.is_set())

    # ---------------- 主循环 ----------------
    def run(self):
        # ★ 不恢复、也不预热结算会话：启动阶段只备购物袋，
        #   等真实命中门店后再用正确的 rtsid 执行 sess.start()。
        while not self.stop.is_set() and not self.dead:
            try:
                # GUI 保存会重建 CookieCfg；每轮先按稳定 ID 换成最新对象。
                # 这样改绑定取货人、SKU、数量、城市和提交参数才是真正热生效。
                if not self._refresh_runtime_config():
                    if self._nap(2):
                        break
                    continue
                if not self._startup_delay_done:
                    self._startup_delay_done = True
                    if self.warmup_gate is not None and self.warmup_gate.interval_sec > 0:
                        waited = self.warmup_gate.wait_turn(self.stop)
                        if waited > 0:
                            self.stat.update(state="错峰等待", note="统一预热间隔 %.1f 秒" % self.warmup_gate.interval_sec, t_note=time.time())
                            self.log("[%s] ⏳ 统一预热错峰等待 %.1f 秒" % (self.cc.name, waited))
                        if self.stop.is_set():
                            break
                # ★★ 先看有没有取货人要素这个型号：没人要 → 纯待命
                #    不建会话、不清袋、不加购、不扫店，一分结算额度都不烧；
                #    等你把取货人配好，下一轮（5 秒内）自动开始预热。
                sts = self.target_stores()
                if not sts:
                    if not self._warned_no_buyer:
                        _b = self._bound_buyer()
                        if _b is None:
                            why = "这个号没有绑定取货人（在抢购任务卡片上选一个《绑定取货人》）"
                        elif not _b.cities:
                            why = "绑定的取货人「%s」没填城市" % _b.name
                        elif not self.cfg.stores_for_cities(_b.cities):
                            why = "绑定的取货人「%s」的城市在门店表里找不到：%s" % (_b.name, "、".join(_b.cities))
                        else:
                            why = "绑定的取货人「%s」暂无可扫门店" % _b.name
                        self.log("[%s] ⏸ %s → 已停止监控该型号，等新的取货人加入（每 5 秒自动检查一次）"
                                 % (self.cc.name, why))
                        self.stat.update(state="无人要素", note=why + "；型号已停监控",
                                         t_note=time.time())
                        self._warned_no_buyer = True
                    self.sess.sid = ""          # 之前若建过会话，一并丢掉，别占额度
                    self._cart_ok = False       # 袋子也不用备了
                    self._full_ready = False
                    self._prepared = False
                    self.stat.update(state="待命", note="没有取货人要素这个型号", t_note=time.time())
                    if self._nap(5):
                        break
                    continue
                self._warned_no_buyer = False
                # ★★ 有别的号正在抢单 → 本号【暂停一切 HTTP】，把资源让给那一单。
                #    这是用户实测推断的瓶颈：抢单那 10 秒里其它 6 个号还在以
                #    每秒几十发扫 50 家店，把连接和限流窗口全占了。
                if self.busy.is_full() and not self._busy.locked():
                    if not self._paused_logged:
                        self._paused_logged = True
                        self.log("[%s] ⏸ 有号正在抢单 → 本号暂停探测（把带宽/连接让给那一单）"
                                 % self.cc.name)
                    self.stat.update(state="让路", note="有号正在抢单 → 暂停探测",
                                     t_note=time.time())
                    if self._nap(0.15):
                        break
                    continue
                if self._paused_logged:
                    self._paused_logged = False
                    self.log("[%s] ▶ 那一单结束了 → 本号恢复探测" % self.cc.name)
                if not self._prepared:
                    self.open_session()
                self.keepalive_if_needed()
                if self.sess.expired:      # ★ 服务端说会话过期 → 重新备袋（会话等下命中时再建）
                    self.stat.update(state="重建", note="服务端说会话过期", t_note=time.time())
                    self.log("[%s] 会话已过期 → 重新备袋" % self.cc.name)
                    self.sess.sid = ""
                    self.sess.expired = False
                    self._cart_ok = False
                    self._full_ready = False
                    self._prepared = False
                    continue
                # 只有捡漏预热会话需要在扫描期间定期重建。
                if (self.warmup_mode == WARMUP_SNIPE and self.sess.sid and
                        time.time() - self.sess.t_alive > self.cfg.settings.session_rebuild_sec):
                    self.stat.update(state="重建会话", note="到期重建（每 %d 分钟）"
                                     % (self.cfg.settings.session_rebuild_sec // 60), t_note=time.time())
                    self.log("[%s] 会话超 %d 分钟 → 重建（空闲时做）"
                             % (self.cc.name, self.cfg.settings.session_rebuild_sec // 60))
                    self.sess.sid = ""
                    self._cart_ok = False
                    self._full_ready = False
                    self._prepared = False
                    continue
                # 目标门店 = 这个型号要它的取货人 → 他们选的城市 → 这些城市下的门店（不是全国）
                # 冷却只挡"进结算"，绝不挡"看有没有货"：所以这里不做任何过滤
                # ★ 这个号在限流冷却期 → 探测照常，但不去打结算（省得越打越封）
                if getattr(self.cli, "blocked_until", 0) > time.time():
                    _left = int(self.cli.blocked_until - time.time())
                    self.stat.update(state="被限流", note="结算族冷却中，还剩 %d 秒（探测照常）" % _left,
                                     t_note=time.time())
                    if self._nap(min(3, max(1, self.cfg.settings.scan_interval_sec))):
                        break
                    continue
                hits, sdetail = scan(self.scan_cli, self.sku, sts, self.cfg.settings.scan_concurrency,
                            stats=self.stat,
                            # ★ 一旦有别的号开始抢单 → 本轮扫描立刻中断，不发剩下的探测
                            should_stop=lambda: (self.busy.is_full() and not self._busy.locked()),
                            recheck=False)
                self.stat.update(state="探测中", note="第 %d 轮：%d 家店，有货 %d"
                                 % (self.stat["rounds"], len(sts), len(hits)), t_note=time.time())
                stuck = [x for x in hits if not self.prev_hit.get(x)]      # 无货→有货 的新信号
                self.rounds += 1
                self.hits_last = len(hits)
                # ★ 心跳：平时扫店是静默的，每 60 秒打一行，证明这个号还活着
                if self.stat.get("err", 0) > getattr(self, "_err_seen", 0):
                    self._err_seen = self.stat["err"]
                    self.log("[%s] ⚠ 有请求查不到：%s（累计 %d 次）"
                             % (self.cc.name, self.stat.get("err_last", "?"), self.stat["err"]))
                if time.time() - self.t_last_beat >= 60:
                    self.t_last_beat = time.time()
                    self.log("[%s] 探测中：第 %d 轮 ｜ 扫 %d 家店 ｜ 有货 %d 家 ｜ 会话已活 %.0f 分钟 ｜ 已买到 %d%s"
                             % (self.cc.name, self.rounds, len(sts), len(hits),
                                (time.time() - self.sess.t_alive) / 60.0, self.orders,
                                (" ｜ 查不到 %d（最近：%s）" % (self.stat["err"], self.stat.get("err_last", "?")))
                                if self.stat.get("err", 0) else ""))
                self._batch_stores = sts
                for st in hits:                       # ★ 每一家有货都留痕
                    _cool = time.time() < self.settle_until.get(st, 0)
                    if _cool and st not in stuck:
                        _act = "结算冷却中（还剩 %d 秒）→ 跳过结算，探测继续" % max(0, int(self.settle_until.get(st, 0) - time.time()))
                    elif not self.buyer_for_store(st):
                        _act = "这家店不在绑定取货人的城市里 → 只记录不下单"
                    else:
                        _d = (sdetail or {}).get(st) or {}
                        _act = "→ 开抢（%s）" % (_d.get("why") or "有档期")
                    self._log_hit(st, st in stuck, _act)
                # ★★ 下完一单后的本号冷却：冷却期【只扫不抢】，一家都不进结算
                # ★ 被"命中复核"拦下来的假信号：日志说清是哪家、为什么（最多 3 行，不刷屏）
                _fl = [(st, wy) for st, wy in (self.stat.get("filtered_last") or [])][:3]
                for _st, _wy in _fl:
                    self.log("[%s] ⏭ 跳过 %s（%s）｜%s"
                             % (self.cc.name, _st, self.cfg.store_name(_st), _wy))
                for st, broke in self._due_stores(hits, stuck):
                    if self.stop.is_set() or self.dead or not self._buyers():
                        break
                    if broke:
                        self.log("[%s] %s 冷却中，但刚由「无货」变「有货」（新信号）→ 破例立刻试一次"
                                 % (self.cc.name, st))
                    gap = self.cfg.settings.settle_gap_sec - (time.time() - self.last_settle)
                    if gap > 0:
                        if self._nap(min(gap, 3.0)):   # 结算族最小间隔，防多店同时命中贴脸打
                            break
                    self.last_settle = time.time()
                    self.try_buy(st)
                    # ★★ 这一把如果把会话切到了目标店、又没成，赶紧退回占位店。
                    #   否则下次命中会走“会话已在本店 → 不换店”，spokes 还是预热时的旧列表（没时段就没有
                    #   timeSlot 格）→ 又拿不到时段。这一发在冷却期做，不占命中时间。
                    self._restore_prewarm_store()
                    # ★★ 2026-09-24 修「成单后拿着空袋子把剩下的店全撞一遍」：
                    #    成单 / 账号侧清袋 / 提交后状态未知 → 袋子已经不可用，
                    #    必须立刻退出本轮的连续试店循环，回主循环走
                    #    open_session() → 预热前查袋 → 重新备袋 + 重建会话。
                    #    （实测 00:26:20~00:26:38 成单后连撞 9 家，每家都只能拿到
                    #      isEmptyCart，白白烧掉一轮的命中。）
                    #    ★ 注意：只在【袋子脏】时退出 —— 普通试店失败（门店被抢等）
                    #      袋子还是好的，继续换下一家店才是最快的，不能每次重建
                    #      （一次完整预热约 12 秒，44 家店会拖成几分钟）。
                    if self._cart_dirty:
                        break
                for x in sts:
                    self.prev_hit[x] = (x in hits)
                if not self._buyers():
                    self.stat.update(state="已收工",
                                     note="任务或取货人已到累计上限（任务 %d/%d 台）"
                                          % (self.cfg.ledger.task_total(self.cc.id), int(getattr(self.cc, "want", 1) or 1)),
                                     t_note=time.time())
                    self.log("[%s] 任务或取货人已到额度（任务 %d/%d 台）→ 这个号收工"
                             % (self.cc.name, self.cfg.ledger.task_total(self.cc.id),
                                int(getattr(self.cc, "want", 1) or 1)))
                    break
                if self._nap(self.cfg.settings.scan_interval_sec):
                    break
            except NeedLogin as e:
                # ★ 账号没绑定 → 重试一万次也没用，直接停掉这个号（不影响其它号）
                self.stat.update(state="需要登录", note="这个微信号没在小程序里登录/绑定 Apple ID",
                                 t_note=time.time())
                self.log("[%s] ⛔ 需要先登录：%s" % (self.cc.name, e))
                self.log("[%s]    → 这个号先退出，其它号继续跑" % self.cc.name)
                self.dead = True
                continue
            except Exception as e:
                self.stat.update(state="出错", note="%s: %s" % (type(e).__name__, e), t_note=time.time())
                self.log("[%s] 循环异常：%s: %s" % (self.cc.name, type(e).__name__, e))
                self.sess.sid = ""
                self._cart_ok = False       # 出错就重新备袋
                self._full_ready = False
                self._prepared = False
                if self._nap(5):
                    break
        try:
            self._prefetch_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        # ★ 扫描线程池也跟着收工：否则池子和它的 TLS 连接会漏到进程退出
        try:
            release_executor()
        except Exception:
            pass
