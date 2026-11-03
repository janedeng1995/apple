# -*- coding: utf-8 -*-
"""工程级苹果小程序抢购工具 · 配置层

配置结构（config.json）：
{
  "cookies": [ {"name":"1号", "cookie_file":"身份-1号.txt", "sku":"MJTL4CH/A", "enabled":true} ],
  "buyers":  [ {"name":"示例取货人", "phone":"17000000000", "email":"example@example.com", "last4":"0000",
                "skus":["MJY64CH/A"], "cities":["上海"], "enabled":true} ],
  "cities":  { "上海": ["R359","R389"] },      // 不填则自动读 资料-城市门店对照.json
  "settings": { ... }
}

任务（cookies）门店来源（二选一）：
  "store_source": "buyer"  // 默认：按【取货人意向地址】（取货人城市）展开扫店
  "store_source": "store", "stores": ["R678","R575"]
                            // 按【自定义门店】：只盯任务上列的这几家（可多选）
  // 老配置只写了 "store": "R678" 也自动当成【自定义门店】（单家）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from dataclasses import dataclass, field

from .proxy_pool import YD_DEFAULT as _YD_DEFAULT

from .quota import Ledger


def _norm_want(w) -> int:
    """want 只认台数。老配置写成 {型号: 台数} 的，按总和迁移一次。"""
    if isinstance(w, dict):
        try:
            return max(1, sum(int(v) for v in w.values()))
        except Exception:
            return 1
    try:
        return max(1, int(w))
    except Exception:
        return 1


def _stable_id(kind: str, seed: str) -> str:
    """旧配置没有 id 时生成稳定 ID；GUI 下次保存会把它写回配置。"""
    return "%s-%s" % (kind, uuid.uuid5(
        uuid.NAMESPACE_URL, "apple-monitor:%s:%s" % (kind, seed or "missing")).hex)
from typing import Dict, List, Optional

# ★ 打包成 exe 后 __file__ 在临时解包目录里 → 必须用 exe 所在目录，否则找不到 config.json
# ★ 另外支持环境变量 APPLE_HOME：从源码直接跑时，用它指向真正的数据目录（根目录），
#   这样 config.json / 身份 / logs / sessions / 资料-*.json 都跟 exe 版完全一致。
HOME_ENV = os.environ.get("APPLE_HOME", "").strip()
if HOME_ENV and os.path.isdir(HOME_ENV):
    HERE = os.path.abspath(HOME_ENV)
elif getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_NAME = "config.json"

# 结算启动模式。旧版 none/cart/full 仍可读入，但统一迁移到两个正式模式：
#   first = 首发模式（不提前创建 checkout 会话）
#   snipe  = 捡漏模式（占位门店建会话并预热联系人/付款/发票，命中后复用）
WARMUP_FIRST = "first"
WARMUP_SNIPE = "snipe"
WARMUP_NONE = WARMUP_FIRST       # 旧导入兼容
WARMUP_CART = WARMUP_SNIPE       # 旧配置 cart → 捡漏
WARMUP_FULL = WARMUP_SNIPE       # 旧配置 full → 捡漏
WARMUP_MODES = (WARMUP_FIRST, WARMUP_SNIPE)

# ★★ 任务门店来源（任务卡上的二选一）：
#   buyer = 按取货人意向地址（取货人填的城市）展开成门店列表（默认）
#   store = 按任务自定义门店（stores 里的几家，可多选）
STORE_SOURCE_BUYER = "buyer"
STORE_SOURCE_STORE = "store"

SKU_NAMES = {
    "MJY64CH/A": "ProMax 256 黑", "MJY74CH/A": "ProMax 256 银", "MJY84CH/A": "ProMax 256 勃", "MJY94CH/A": "ProMax 256 冰",
    "MJYA4CH/A": "ProMax 512 黑", "MJYC4CH/A": "ProMax 512 银", "MJYD4CH/A": "ProMax 512 勃", "MJYE4CH/A": "ProMax 512 冰",
    "MJYF4CH/A": "ProMax 1TB 黑", "MJYG4CH/A": "ProMax 1TB 银", "MJYH4CH/A": "ProMax 1TB 勃", "MJYJ4CH/A": "ProMax 1TB 冰",
    "MJYK4CH/A": "ProMax 2TB 黑", "MJYL4CH/A": "ProMax 2TB 银", "MJYM4CH/A": "ProMax 2TB 勃", "MJYN4CH/A": "ProMax 2TB 冰",
    "MJT74CH/A": "Pro 256 黑", "MJT84CH/A": "Pro 256 银", "MJT94CH/A": "Pro 256 勃", "MJTA4CH/A": "Pro 256 冰",
    "MJTC4CH/A": "Pro 512 黑", "MJTD4CH/A": "Pro 512 银", "MJTE4CH/A": "Pro 512 勃", "MJTF4CH/A": "Pro 512 冰",
    "MJTG4CH/A": "Pro 1TB 黑", "MJTH4CH/A": "Pro 1TB 银", "MJTJ4CH/A": "Pro 1TB 勃", "MJTK4CH/A": "Pro 1TB 冰",
    "MJTL4CH/A": "Pro 2TB 黑", "MJTM4CH/A": "Pro 2TB 银", "MJTN4CH/A": "Pro 2TB 勃", "MJTP4CH/A": "Pro 2TB 冰",
}


def sku_name(sku: str) -> str:
    return SKU_NAMES.get(sku, sku)


@dataclass
class CookieCfg:
    name: str
    cookie_file: str
    sku: str
    id: str = ""
    enabled: bool = True
    ua: str = ""            # 客户端档位：留空=用身份文件里的 ua（再退回手机档）；填了就强制用它
    buyer: str = ""         # ★ 这个任务只用一个取货人；同一取货人可被多个任务复用
    store: str = ""         # ★★ 老字段（单家门店）；新配置用 store_source + stores，这里自动兼容
    store_source: str = STORE_SOURCE_BUYER   # ★★ 门店来源：buyer=取货人意向城市 / store=任务自定义门店
    stores: List[str] = field(default_factory=list)   # ★★ 自定义门店列表（store_source="store" 时生效，可多家）
    buyer_id: str = ""      # 稳定额度键
    per_order: int = 1      # ★ 单次购物袋/一张订单买几件（默认 1；苹果 Pro 系列实测上限 2）
    want: int = 1           # ★ 这个任务累计最多买几台（账本按稳定任务 ID 计数）

    def __post_init__(self):
        # 入口归一：大写、去重、老配置迁移。
        self.store = str(self.store or "").strip().upper()
        src = str(self.store_source or "").strip().lower()
        sts: List[str] = []
        for x in (self.stores or []):
            x = str(x or "").strip().upper()
            if x and x not in sts:
                sts.append(x)
        if src not in (STORE_SOURCE_BUYER, STORE_SOURCE_STORE):
            # 老配置没写 store_source：填了店就当【自定义门店】，否则按取货人意向城市
            src = STORE_SOURCE_STORE if (self.store or sts) else STORE_SOURCE_BUYER
        if src == STORE_SOURCE_STORE and not sts and self.store:
            sts = [self.store]
        if src == STORE_SOURCE_BUYER:
            sts = []
        self.store_source = src
        self.stores = sts

    @property
    def path(self) -> str:
        return self.cookie_file if os.path.isabs(self.cookie_file) else os.path.join(HERE, self.cookie_file)


@dataclass
class BuyerCfg:
    name: str
    phone: str
    email: str
    last4: str
    id: str = ""
    last_name: str = ""     # ★ 显式 姓/名，保留复姓、英文名，不按首字硬拆
    first_name: str = ""
    want: int = 1           # ★ 他总共想要几台（型号由 skus 决定）；抢够就把他停下来
    skus: List[str] = field(default_factory=list)
    cities: List[str] = field(default_factory=list)
    enabled: bool = True

    def __post_init__(self):
        if self.name and self.last_name and self.first_name:
            if self.last_name + self.first_name != self.name and self.first_name + self.last_name == self.name:
                self.last_name, self.first_name = self.first_name, self.last_name

    def wants(self, sku: str) -> bool:
        return not self.skus or sku in self.skus

    def want_for(self, sku: str = "") -> int:
        """他总共想要几台。★ 型号不在这里管 —— 要哪些型号看 skus"""
        try:
            return max(0, int(self.want))
        except Exception:
            return 1

    def want_total(self) -> int:
        return self.want_for()

    def contact(self) -> dict:
        nm = self.name.strip()
        if self.last_name and self.first_name:
            ln, fn = self.last_name, self.first_name
        else:
            ln, fn = nm[:1], (nm[1:] or nm[:1])
        return {"lastName": ln, "firstName": fn, "email": self.email, "phone": self.phone}


@dataclass
class Settings:
    warmup_mode: str = WARMUP_SNIPE
    time_slot_index: int = 1             # 默认日期下第几个可用时段，从 1 开始
    prewarm_interval_sec: float = 0.0    # 启动时账号之间的统一预热间隔
    # ★ 是否在预热阶段就把【取货人】写进会话。
    #   true  = 预热时写好该任务绑定的取货人，命中最快
    #   false = 预热不写联系人，命中后再当场填任务绑定的取货人（多 1~2 发）
    prewarm_contact: bool = True
    # ★★ 是否【需要时段】：
    #   false（默认）= 没有可填的时段也补完其它格、照常提交（服务端说算）
    #   true        = 发现没有填写时段的地方 → 直接当没货，不进结算、不提交
    require_time_slot: bool = False
    # ★★ 门店进结算失败后的固定刷新间隔（秒）。每一轮间隔一样，不指数增长。
    #   只影响【这家店多久再进结算】，探测（看有没货）照旧不受影响。
    store_cool_sec: int = 15
    # ★★ 2026-09-24【固定门店模式】填了就只盯这一家店（如 "R678"）：
    #   扫描也只扫这家店；但会话仍预热在【占位店】上（2026-09-25 改）。
    #   原因：预热时目标店太常没放时段，会话若建在目标店上，spokes 里就没有
    #   timeSlot 格，命中时不换店就拉不到时段。现在命中统一换店，拿目标店当剏的 spokes。
    #   留空 = 按城市扫所有店（同样占位店预热 + 命中换店）。
    target_store: str = ""
    # ★★ 2026-09-24【用户提议实测】：预热时是否再按【真机流程】把门店选一遍
    #   （changeStoreAction → selectStoreUrl）。
    #   现在固定门店只靠 cookie 里的 rtsid + start(RETAIL) 是快捷方式，
    #   怀疑服务端因此没把"自提+门店"完全定下来（timeSlot 格都不出现）。
    #   关掉它就回到纯快捷方式，方便 A/B 对比。
    warmup_store: str = "R390"          # 旧版字段，仅为读取旧配置兼容，稳定版不使用
    scan_concurrency: int = 12
    scan_interval_sec: float = 0.4
    session_rebuild_sec: int = 600      # 实测：10 分钟不碰仍可用，30 分钟必 session_expired
    alarm_sec: int = 180
    status_poll_sec: float = 3.0        # ★ 提交后查订单状态的间隔（秒）——别太密，密了容易 541
    status_poll_max: int = 20           # ★ 提交后最多查多少次（3 秒 × 20 次）
    max_parallel_buy: int = 2           # 同时允许几个账号进入结算链路
    retry_no_slot_sec: int = 15         # 拿不到时段后，这家店多久才再进结算（探测不受影响）
    debug_checkout: bool = False        # ★ 调试：把结算链路每次请求/返回原文存盘（分析弹回次数用）
    settle_gap_sec: float = 2.0         # 两次进结算之间的最小间隔
    # ★★ 代理（除扫描外全走）：加袋/清袋/建会话/时段/付款/提交 全走代理，每 proxy_rotate_sec 秒换一个 IP
    proxy_enabled: bool = False
    proxy_api_url: str = ""              # 代理接口（空=取不到 IP 时自动回退直连）；格式见 proxy_pool.py 顶部注释
    proxy_mode: str = "per_account"      # ★ per_account=一号一池各用自己的IP；global=所有号共用一个（旧行为）
    proxy_rotate_sec: float = 60.0
    startup_check: bool = False         # 启动前体检（默认关：多打一发结算族请求，拖慢且增541风险）
    auto_submit: bool = False
    dry_run: bool = True
    log_lines: int = 500
    email: dict = field(default_factory=dict)


class Config:
    def __init__(self, data: dict, path: str = ""):
        self.raw = data or {}
        self.path = path
        # GUI 会在主线程重建配置，worker 同时在后台读取。
        # 用可重入锁保证 worker 不会看到“新任务 + 旧取货人”这种半更新状态。
        self.lock = threading.RLock()
        self.ledger = Ledger(os.path.join(HERE, "orders-ledger.json"))
        with self.lock:
            self._parse()
        self.ledger.migrate_legacy(self.cookies)

    def rebuild(self):
        """★ 改完配置后热生效：重新解析 raw。
        worker 会按稳定任务 ID 重新绑定新对象，不再继续使用旧 CookieCfg。
        增删任务、更换身份文件和代理设置仍需重启运行任务。"""
        with self.lock:
            self._parse()

    def task_by_id(self, task_id: str) -> Optional[CookieCfg]:
        """按稳定 ID 取当前任务。GUI 保存会重建 CookieCfg，worker 必须用它换掉旧引用。"""
        with self.lock:
            for task in self.cookies:
                if task.id == task_id:
                    return task
        return None

    def _parse(self):
        data = self.raw or {}
        self.cookies: List[CookieCfg] = [
            CookieCfg(name=c.get("name") or os.path.basename(c.get("cookie_file", "")),
                      cookie_file=c.get("cookie_file", ""), sku=(c.get("sku") or "").upper(),
                      id=str(c.get("id") or _stable_id(
                          "task", "%s|%s" % (c.get("cookie_file", ""),
                                               (c.get("sku") or "").upper()))),
                      enabled=bool(c.get("enabled", True)), ua=(c.get("ua") or ""),
                      buyer=str(c.get("buyer") or "").strip(),
                      store=str(c.get("store") or "").strip().upper(),
                      store_source=str(c.get("store_source") or "").strip().lower(),
                      stores=[str(x).strip().upper() for x in (c.get("stores") or []) if str(x).strip()],
                      per_order=max(1, _norm_want(c.get("per_order", 1))),
                      want=_norm_want(c.get("want", 1)))
            for c in (data.get("cookies") or [])
        ]
        self.buyers: List[BuyerCfg] = [
            BuyerCfg(name=b.get("name", ""), phone=str(b.get("phone", "")), email=b.get("email", ""),
                     last4=str(b.get("last4", "")), skus=[s.upper() for s in (b.get("skus") or [])],
                     id=str(b.get("id") or _stable_id(
                         "buyer", "%s|%s|%s" % (b.get("name", ""), b.get("phone", ""),
                                                 b.get("last4", "")))),
                     last_name=b.get("last_name", "") or "", first_name=b.get("first_name", "") or "",
                     want=_norm_want(b.get("want", 1)),
                     cities=b.get("cities") or [], enabled=bool(b.get("enabled", True)))
            for b in (data.get("buyers") or [])
        ]
        for c in self.cookies:
            b = self.buyer_by_name(c.buyer)
            c.buyer_id = b.id if b else ""
        s = data.get("settings") or {}
        raw_mode = str(s.get("warmup_mode") or WARMUP_SNIPE).lower()
        warmup_mode = {"none": WARMUP_FIRST, "first": WARMUP_FIRST,
                       "cart": WARMUP_SNIPE, "full": WARMUP_SNIPE,
                       "snipe": WARMUP_SNIPE}.get(raw_mode, WARMUP_SNIPE)
        self.settings = Settings(
            warmup_mode=warmup_mode,
            time_slot_index=max(1, int(s.get("time_slot_index", 1) or 1)),
            prewarm_interval_sec=max(0.0, float(s.get("prewarm_interval_sec", 0) or 0)),
            prewarm_contact=bool(s.get("prewarm_contact", True)),
            require_time_slot=bool(s.get("require_time_slot", False)),
            store_cool_sec=max(0, int(s.get("store_cool_sec", 15) or 0)),
            target_store=str(s.get("target_store") or "").strip().upper(),
            warmup_store=str(s.get("warmup_store") or "R390").upper(),
            scan_concurrency=int(s.get("scan_concurrency", 12)),
            scan_interval_sec=float(s.get("scan_interval_sec", 0.4)),
            session_rebuild_sec=int(s.get("session_rebuild_sec", 600)),
            alarm_sec=int(s.get("alarm_sec", 180)),
            status_poll_sec=float(s.get("status_poll_sec", 3.0)),
            status_poll_max=int(s.get("status_poll_max", 20)),
            max_parallel_buy=max(1, int(s.get("max_parallel_buy", 2))),
            retry_no_slot_sec=int(s.get("retry_no_slot_sec", 15)),
            debug_checkout=bool(s.get("debug_checkout", False)),
            settle_gap_sec=float(s.get("settle_gap_sec", 2.0)),
            proxy_enabled=bool(s.get("proxy_enabled", False)),
            proxy_api_url=str(s.get("proxy_api_url") or _YD_DEFAULT),
            proxy_mode=str(s.get("proxy_mode") or "per_account").strip().lower(),
            proxy_rotate_sec=float(s.get("proxy_rotate_sec", 60)),
            startup_check=bool(s.get("startup_check", False)),
            auto_submit=bool(s.get("auto_submit", False)),
            dry_run=bool(s.get("dry_run", True)),
            log_lines=int(s.get("log_lines", 500)),
            email=s.get("email") or {},
        )
        self.cities: Dict[str, List[str]] = {k: list(v) for k, v in (data.get("cities") or {}).items()}
        if not self.cities:
            self.cities = self._load_city_map()

    # ---------- 城市 → 门店 ----------
    @staticmethod
    def _load_city_map() -> Dict[str, List[str]]:
        for fn in ("资料-城市门店对照.json", "city_stores.json"):
            p = os.path.join(HERE, fn)
            if os.path.exists(p):
                try:
                    return {k: list(v) for k, v in json.load(open(p, encoding="utf-8")).items()}
                except Exception:
                    pass
            p2 = os.path.join(HERE, "..", "Apple监控包", fn)
            if os.path.exists(p2):
                try:
                    return {k: list(v) for k, v in json.load(open(p2, encoding="utf-8")).items()}
                except Exception:
                    pass
        return {}

    # ---------- 门店编号 → 中文名 ----------
    @staticmethod
    def _load_store_names() -> Dict[str, str]:
        for base in (HERE, os.path.join(HERE, "..", "Apple监控包")):
            p = os.path.join(base, "资料-门店中文名.json")
            if os.path.exists(p):
                try:
                    raw = json.load(open(p, encoding="utf-8"))
                    out = {}
                    for k, v in raw.items():
                        nm = v.get("name") if isinstance(v, dict) else v
                        if nm:
                            out[k] = "Apple " + str(nm)
                    return out
                except Exception:
                    pass
        return {}

    def store_name(self, code: str) -> str:
        if not hasattr(self, "_store_names"):
            self._store_names = self._load_store_names()
        return self._store_names.get(code) or code

    def stores_for_cities(self, cities: List[str]) -> List[str]:
        out: List[str] = []
        for c in cities:
            for s in (self.cities.get(c) or []):
                if s not in out:
                    out.append(s)
        return out

    def buyer_by_name(self, name: str) -> Optional[BuyerCfg]:
        """★ 一对一绑定：按姓名精确取一个取货人（抢购任务用 cc.buyer 指定）。"""
        name = (name or "").strip()
        if not name:
            return None
        for b in self.buyers:
            if b.name == name:
                return b
        return None

    # ---------- 读写 ----------
    @classmethod
    def load(cls, path: str = "") -> "Config":
        p = path or os.path.join(HERE, CONFIG_NAME)
        if not os.path.exists(p):
            raise FileNotFoundError("找不到配置文件：%s" % p)
        return cls(json.load(open(p, encoding="utf-8")), p)

    def save(self, path: str = ""):
        p = path or self.path or os.path.join(HERE, CONFIG_NAME)
        json.dump(self.raw, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    def validate(self) -> List[str]:
        errs: List[str] = []
        if not self.cookies:
            errs.append("cookies 为空：至少要配一个 cookie")
        for c in self.cookies:
            if not c.enabled:
                continue
            if not c.sku:
                errs.append("cookie %s 没填 sku" % c.name)
            elif c.sku not in SKU_NAMES:
                errs.append("cookie %s 的 sku 不认识：%s" % (c.name, c.sku))
            if not os.path.exists(c.path):
                errs.append("cookie %s 的文件不存在：%s" % (c.name, c.path))
        seen = {}
        for c in self.cookies:
            if not c.enabled:
                continue
            k = os.path.normcase(os.path.abspath(c.path))
            if k in seen:
                errs.append("cookie %s 和 %s 用的是同一个身份文件（%s）→ 两个任务会互相删购物袋，"
                            "必须一个号一个任务" % (c.name, seen[k], os.path.basename(c.path)))
            else:
                seen[k] = c.name
        task_names = [c.name for c in self.cookies if c.enabled]
        if len(task_names) != len(set(task_names)):
            errs.append("启用任务的名称不能重复（名称用于日志和迁移旧账本）")
        buyer_names = [b.name for b in self.buyers]
        if len(buyer_names) != len(set(buyer_names)):
            errs.append("取货人姓名不能重复（任务绑定会无法区分）")
        # 每个启用任务只绑定一个取货人，但同一人可被多个账号复用。
        for c in self.cookies:
            if not c.enabled:
                continue
            label = "任务 %s" % c.name
            if not c.buyer:
                errs.append("%s 没绑定取货人（请在任务卡片上选一个「绑定取货人」）" % label)
                continue
            b = self.buyer_by_name(c.buyer)
            if b is None:
                errs.append("%s 绑定的取货人「%s」不在取货人列表里" % (label, c.buyer))
                continue
            if not b.enabled:
                errs.append("%s 绑定的取货人「%s」已被停用" % (label, c.buyer))
            if not b.wants(c.sku):
                errs.append("%s 的型号 %s 不在取货人「%s」的「要抢型号」中"
                            % (label, c.sku, c.buyer))
            if not (b.phone and b.last4):
                errs.append("%s 绑定的取货人「%s」缺手机号或证件后四位" % (label, c.buyer))
            _tstore = str(getattr(c, "store", "") or "").strip().upper()
            if _tstore and _tstore not in self._load_store_names():
                errs.append("%s 盯的门店 %s 不在门店表里（资料-门店中文名.json）" % (label, _tstore))
            if str(getattr(c, "store_source", "") or "").lower() == STORE_SOURCE_STORE:
                _cust = list(getattr(c, "stores", None) or [])
                if not _cust:
                    errs.append("%s 选了【自定义门店】但没勾任何门店（可改回【取货人意向地址】）" % label)
                for _s in _cust:
                    if _s not in self._load_store_names():
                        errs.append("%s 自定义门店 %s 不在门店表里（资料-门店中文名.json）" % (label, _s))
            if not b.cities:
                errs.append("%s 绑定的取货人「%s」没填城市" % (label, c.buyer))
            elif not self.stores_for_cities(b.cities):
                errs.append("%s 绑定的取货人「%s」的城市在门店表里找不到：%s"
                            % (label, c.buyer, b.cities))
            if c.per_order < 1:
                errs.append("%s 的「每单件数」必须 ≥1" % label)
            if c.per_order > 2:
                errs.append("%s 的「每单件数」不能超过 2（苹果 Pro 系列实测限购 2 件/单）" % label)
            if c.want < 1:
                errs.append("%s 的「累计台数」必须 ≥1" % label)
        # ★ 固定门店模式：指定的门店必须能在门店表里找到，否则静默不下单
        _pin = str(getattr(self.settings, "target_store", "") or "").strip().upper()
        if _pin and _pin not in self._load_store_names():
            errs.append("settings.target_store=%s 不在门店表里（资料-门店中文名.json）" % _pin)
        snipe_mode = self.settings.warmup_mode == WARMUP_SNIPE
        if not self.buyers:
            errs.append("buyers 为空：首发/只备袋模式至少要配一个普通取货人")
        import re as _re
        check_buyers = [b for b in self.buyers if b.enabled]
        for b in check_buyers:
            label = "取货人 %s" % b.name
            if not b.phone or not b.last4:
                errs.append("%s 缺手机号或证件后四位" % label)
            else:
                # ★★ 格式预检：手机号必须是 11 位数字、证件后四位必须是 4 位字母数字。
                #    实测（0916 00:01）：手机号填成中文（"试"）时苹果直接回 HTTP 500
                #    + 一整页 HTML 错误页，联系人永远存不进去、提交被弹回 5 次才发现。
                #    这里在【启动前】就拦住，别等抢单时白烧会话。
                if not _re.match(r"^\d{11}$", str(b.phone).strip()):
                    errs.append("%s的手机号格式不对（必须是 11 位数字）：%r"
                                % (label, b.phone))
                if not _re.match(r"^[A-Za-z0-9]{4}$", str(b.last4).strip()):
                    errs.append("%s的证件后四位格式不对（必须是 4 位字母/数字）：%r"
                                % (label, b.last4))
                _em = str(getattr(b, "email", "") or "").strip()
                if _em and not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", _em):
                    errs.append("%s的邮箱格式不对：%r" % (label, b.email))
            if not b.cities:
                errs.append("%s没填城市" % label)
            elif not self.stores_for_cities(b.cities):
                errs.append("%s的城市在门店表里找不到：%s" % (label, b.cities))
        # 捡漏模式默认直接复用第二页取货人；warmup_buyer/warmup_store 仅保留旧配置兼容。
        return errs
