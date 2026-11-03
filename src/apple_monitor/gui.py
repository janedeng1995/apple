# -*- coding: utf-8 -*-
"""图形界面 v3（ttkbootstrap）

v3 相比 v2 的变化（都是观感/交互，不碰下单逻辑）：
  · 主区改成上下可拖拽分栏 —— 日志不再被挤成一行，想多大拖多大
  · 日志按内容自动上色（成单=绿 / 失败=红 / 警告=橙 / 命中=蓝）
  · 每张任务卡片带「实时状态」——日志里这个 cookie 在干什么，卡片上直接看得到
  · 任务页 / 取货人页可滚动（任务多、城市多的时候不再顶出屏幕）
  · 卡片标题带摘要（任务名、型号；取货人姓名、城市、型号）
  · 底部状态栏显示配置文件路径；Ctrl+S 保存
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import urllib.parse
import uuid
import webbrowser
from tkinter import filedialog, messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import BOTH, END, LEFT, RIGHT, X, Y, YES, BOTTOM, TOP

from . import autologin
from .client import Client, MERCH
from .config import (HERE, Config, SKU_NAMES, WARMUP_FIRST, WARMUP_SNIPE,
                     sku_name, _norm_want, STORE_SOURCE_BUYER, STORE_SOURCE_STORE)
from .main import start_workers
from .session import CheckoutSession

LOG_MAX = 800
THEMES = ["flatly", "cosmo", "litera", "minty", "journal", "darkly", "cyborg", "superhero", "solar"]
SKU_ITEMS = ["%s（%s）" % (k, v) for k, v in SKU_NAMES.items()]

MODE_DRY = "演练（不下单）"
MODE_LIVE = "真实下单"
WARMUP_LABELS = {
    WARMUP_FIRST: "首发模式",
    WARMUP_SNIPE: "捡漏模式",
}

FG_OK, FG_ERR, FG_WARN, FG_HIT, FG_DIM = "#1f8b4c", "#c0392b", "#b9770e", "#1f6feb", "#7f8c8d"
PROXY_MODES = [("per_account", "一号一池（推荐）"), ("global", "全局共用一个 IP")]
PIN_NONE = "（不固定：扫城市里所有店）"   # ★ 设置页旧全局项的"不启用"文案
STORE_SRC_BUYER = "按取货人意向地址"   # ★★ 门店来源：按取货人填的城市扫店（默认）
STORE_SRC_STORE = "按门店自定义"       # ★★ 门店来源：只盯任务上勾选的几家店（可多选）


def _sku_of(item: str) -> str:
    return item.split("（", 1)[0].strip()


def _sku_item(sku: str) -> str:
    return "%s（%s）" % (sku, sku_name(sku))


def _store_code(item: str) -> str:
    return str(item or "").split("｜", 1)[0].strip().upper()


def _want_text(w) -> str:
    """「想要」就是一个台数"""
    try:
        return str(max(1, int(w)))
    except Exception:
        return "1"


def _parse_want(s, default=1):
    """「想要」只收一个数字（他总共要几台）。型号不在这里管。"""
    import re as _re
    nums = _re.findall(r"\d+", str(s or ""))
    return max(1, int(nums[-1])) if nums else default


def _positive_float(s, default=0.0):
    try:
        return max(0.0, float(s))
    except Exception:
        return default


def _positive_int(s, default=1):
    try:
        return max(1, int(float(s)))
    except Exception:
        return default


def _order_cap(n) -> int:
    """每单件数上限：2026-09-23 抓包确认苹果该 SKU 限购 2 件/单，界面直接夹住。"""
    try:
        return min(2, max(1, int(n)))
    except Exception:
        return 1


def _split_name(name: str, row: dict):
    """把界面上的「姓名」拆成 (姓, 名)。

    ★ 为什么需要它：以前 collect() 直接把建卡片时读进来的 _ln/_fn 写回配置，
      用户在界面上改了姓名也不会同步 → 提交出去的姓/名是旧的。
      规则：
        1) 原来的 _ln/_fn 拼起来还等于现在的姓名（不管谁前谁后）→ 沿用原拆分
           （保留复姓、英文名，不被首字硬拆）
        2) 否则（姓名被改过了）→ 按首字兜底拆，和 config.BuyerCfg 的兜底一致
    """
    nm = (name or "").strip()
    ln = str((row or {}).get("_ln") or "")
    fn = str((row or {}).get("_fn") or "")
    if nm and ln and fn and (ln + fn == nm or fn + ln == nm):
        return ln, fn
    if not nm:
        return "", ""
    return nm[:1], (nm[1:] or nm[:1])


def _center_window(win, parent=None):
    """把弹窗摆到【父窗口正中间】（父窗口不可用时退化为屏幕居中）。

    ★ 之前新建的 Toplevel 没设位置 → Windows 默认丢到左上角，看着很别扭。
    """
    try:
        win.update_idletasks()
        w = max(1, win.winfo_width())
        h = max(1, win.winfo_height())
        p = parent
        if p is not None and p.winfo_exists() and p.winfo_width() > 1 and p.winfo_height() > 1:
            x = p.winfo_rootx() + (p.winfo_width() - w) // 2
            y = p.winfo_rooty() + (p.winfo_height() - h) // 2
        else:
            x = (win.winfo_screenwidth() - w) // 2
            y = (win.winfo_screenheight() - h) // 2
        win.geometry("+%d+%d" % (max(0, x), max(0, y)))
    except Exception:
        pass


def _no_wheel(w):
    """★ 禁止鼠标滚轮改下拉框的值 —— 不然后台滚列表时指针扫过下拉框就会偷偷改掉选中的型号"""
    try:
        w.bind("<MouseWheel>", lambda e: "break")
        w.bind("<Button-4>", lambda e: "break")
        w.bind("<Button-5>", lambda e: "break")
    except Exception:
        pass
    return w


def _ident_kind(path: str) -> str:
    """看一眼身份文件，判断是手机档还是 PC 档（只看 ua 里有没有 Windows/Android）"""
    try:
        if not path or not os.path.exists(path):
            return "未选身份"
        t = open(path, encoding="utf-8", errors="replace").read()
        if "Windows" in t and "MicroMessenger" in t:
            return "PC 档（Windows 微信）"
        if "Android" in t:
            return "手机档（Android 微信）"
        return "未标明档位（按手机档发）"
    except Exception:
        return "读不了"


def _w(text: str) -> int:
    """中文按 2 个字符宽算，避免 Label 把后面的输入框压住"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text)) + 1


def _scroll_area(parent, bg):
    """可滚动容器：返回 (inner_frame, scroll_to_top)"""
    holder = tb.Frame(parent)
    holder.pack(fill=BOTH, expand=True)
    canvas = tk.Canvas(holder, highlightthickness=0, bd=0, bg=bg)
    sb = tb.Scrollbar(holder, orient="vertical", command=canvas.yview)
    inner = tb.Frame(canvas)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side=LEFT, fill=BOTH, expand=True)
    sb.pack(side=RIGHT, fill=Y)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

    def wheel(e):
        canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

    canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
    canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
    return inner


class _Cell:
    """表格视图用的"假控件"：只提供 get()/set()，
    让 collect()/summary()/_buyer_names() 这些老代码不用改。"""
    __slots__ = ("_v",)

    def __init__(self, v=None):
        self._v = v

    def get(self):
        return self._v

    def set(self, v, *a):
        self._v = v


def _store_item_for(items, code: str) -> str:
    """门店编号 → 下拉项文本（"R678｜Apple 静安"）；找不到返回空串。"""
    code = str(code or "").strip().upper()
    for it in (items or []):
        if _store_code(it) == code:
            return it
    return ""


def _store_source_of(label: str) -> str:
    """界面文案 → 配置值（store/buyer）。"""
    return STORE_SOURCE_STORE if str(label or "").strip() == STORE_SRC_STORE else STORE_SOURCE_BUYER


def _row_store_source(r: dict) -> str:
    """任务行的门店来源（老数据没有该键时按有没有填店推判）。"""
    src = str((r or {}).get("store_source") or "").strip().lower()
    if not src:
        src = STORE_SOURCE_STORE if (r or {}).get("store") else STORE_SOURCE_BUYER
    return src


def _row_stores(r: dict) -> list:
    """任务行自定义门店列表（兼容老的单家 store 字段）。"""
    codes = [str(x).strip().upper() for x in ((r or {}).get("stores") or []) if str(x).strip()]
    if not codes and (r or {}).get("store"):
        codes = [str(r["store"]).strip().upper()]
    out = []
    for c in codes:
        if c not in out:
            out.append(c)
    return out


def _store_summary(r: dict, items) -> str:
    """任务行「盯的门店」列的显示文本。"""
    if _row_store_source(r) == STORE_SOURCE_STORE:
        codes = _row_stores(r)
        if not codes:
            return "（未选门店）"
        if len(codes) == 1:
            return _store_item_for(items, codes[0]) or codes[0]
        return "%s 等 %d 家" % (codes[0], len(codes))
    return "按取货人城市"


class MultiPick:
    """多选下拉：框里显示摘要，点「选择…」弹窗勾选，点【确定】才写回配置"""

    def __init__(self, parent, items, selected, width=28, title="请选择", on_change=None):
        self.items = list(items)
        self.title = title
        self.on_change = on_change
        self.frame = tb.Frame(parent)
        self.var = tb.StringVar(value="")
        self.entry = tb.Entry(self.frame, textvariable=self.var, width=width, state="readonly")
        self.entry.pack(side=LEFT)
        self.btn = tb.Button(self.frame, text="选择…", bootstyle="secondary-outline", width=7,
                           command=self.open)
        self.btn.pack(side=LEFT, padx=(4, 0))
        sel = set(selected or [])
        self.sel = [x for x in self.items if x in sel]
        self._text()

    def get(self):
        sel = set(self.sel)
        return [x for x in self.items if x in sel]

    def set(self, selected):
        sel = set(selected or [])
        self.sel = [x for x in self.items if x in sel]
        self._text()

    def set_enabled(self, on: bool):
        """门店来源不是【自定义】时，把多选框置灰。
        ★ 恢复时必须回到 readonly（不能是 normal）：这个框只用来显示摘要，
          变成可编辑会让人以为手打的文字能生效（其实 get() 只识别弹窗里勾选的）。"""
        st = "readonly" if on else "disabled"
        try:
            self.entry.configure(state=st)
            self.btn.configure(state=st)
        except Exception:
            pass

    def _text(self):
        p = self.get()
        if not p:
            self.var.set("（未选）")
        elif len(p) <= 2:
            self.var.set("、".join(p))
        else:
            self.var.set("已选 %d 项：" % len(p) + "、".join(p[:2]) + " …")

    def open(self):
        win = tk.Toplevel(self.frame)
        win.title(self.title)
        win.transient(self.frame.winfo_toplevel())
        win.resizable(False, False)
        tb.Label(win, text=self.title, padding=(14, 12, 14, 2),
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        tb.Label(win, text="勾选后点【确定】才生效（可多选）", padding=(14, 0, 14, 8),
                 bootstyle="secondary", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        body = tb.Frame(win, padding=(14, 0, 14, 6))
        body.pack(fill=BOTH, expand=True)
        lb = tk.Listbox(body, selectmode="multiple", exportselection=False,
                        height=min(13, max(6, len(self.items))), width=38,
                        font=("Microsoft YaHei UI", 10), activestyle="none",
                        highlightthickness=1, relief="flat", bd=0)
        for it in self.items:
            lb.insert(END, it)
        cur = set(self.sel)
        for i, it in enumerate(self.items):
            if it in cur:
                lb.selection_set(i)
        sb = tb.Scrollbar(body, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side=LEFT, fill=BOTH, expand=True)
        sb.pack(side=LEFT, fill=Y)
        side = tb.Frame(body)
        side.pack(side=LEFT, fill=Y, padx=(8, 0))
        tb.Button(side, text="全选", bootstyle="secondary-outline", width=6,
                  command=lambda: lb.selection_set(0, END)).pack(pady=2)
        tb.Button(side, text="清空", bootstyle="secondary-outline", width=6,
                  command=lambda: lb.selection_clear(0, END)).pack(pady=2)

        def ok(_=None):
            self.sel = [self.items[i] for i in lb.curselection()]
            self._text()
            win.destroy()
            if self.on_change:
                try:
                    self.on_change()
                except Exception:
                    pass

        foot = tb.Frame(win, padding=(14, 4, 14, 14))
        foot.pack(fill=X)
        tb.Button(foot, text="确定", bootstyle="success", width=10, command=ok).pack(side=RIGHT)
        tb.Button(foot, text="取消", bootstyle="secondary-outline", width=10,
                  command=win.destroy).pack(side=RIGHT, padx=8)
        lb.bind("<Double-Button-1>", ok)
        win.bind("<Escape>", lambda e: win.destroy())
        _center_window(win, self.frame.winfo_toplevel())
        win.grab_set()


class App:
    def __init__(self, root: tb.Window, config_path: str = ""):
        self.root = root
        self.path = config_path or os.path.join(HERE, "config.json")
        self.cfg: Config = None
        self.workers = []
        self._orders = []          # 本次运行成功订单（用于【订单/支付】页）
        self._order_links = {}     # 订单号/iid → 订单链接
        self._qr_pngs = {}         # iid → 二维码 PNG 路径
        self._qr_stop = None
        self._orders_file = os.path.join(HERE, "orders.json")   # 订单落盘（重启不丢）
        self.stop_evt = None
        self.task_rows = []
        self.buyer_rows = []
        self.status_vars = {}
        self.cart_windows = []           # ★ 购物袋弹窗（只用于人工查看/增删，不影响下单状态）
        self.q = queue.Queue()          # ★ worker 线程 → 主线程的唯一通道（Tk 不允许多线程乱碰）
        self._t_board = 0.0             # 看板刷新节流
        self._logfh = None              # 日志文件句柄
        self.city_list = []
        root.title("苹果抢购助手")
        root.geometry("1260x860")
        root.minsize(1080, 700)
        root.position_center()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.style = root.style
        self.bg = self._theme_bg()
        self._load_or_init()
        self._build()
        self.refresh()
        try:      # 全局兜底：ttk Combobox 默认吃滚轮 → 直接把这套类绑定删掉
            self.root.unbind_class("TCombobox", "<MouseWheel>")
        except Exception:
            pass
        self.root.after(200, self._drain)
        root.bind("<Control-s>", lambda e: self.save(False))

    def _theme_bg(self):
        try:
            return self.style.colors.bg
        except Exception:
            return "#f5f5f5"

    # ============================================================ 配置
    def _load_or_init(self):
        if not os.path.exists(self.path):
            data = {"cookies": [], "buyers": [], "cities": {},
                    "settings": {"dry_run": True, "auto_submit": False}}
            json.dump(data, open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        try:
            self.cfg = Config.load(self.path)
        except Exception as e:
            messagebox.showerror("配置文件读不了", "%s\n\n%s" % (self.path, e))
            self.cfg = Config({"cookies": [], "buyers": [], "cities": {}, "settings": {}}, self.path)
        self.city_list = sorted(set(self.cfg.cities.keys()))
        store_names = self.cfg._load_store_names()
        self.store_items = ["%s｜%s" % (code, store_names.get(code, code))
                            for code in sorted(store_names)]

    def save(self, silent=True):
        try:
            old_live = bool(getattr(self.cfg.settings, "auto_submit", False)) and \
                not bool(getattr(self.cfg.settings, "dry_run", True))
            self.collect()
            _s = self.cfg.raw.setdefault("settings", {})
            new_live = bool(_s.get("auto_submit")) and not bool(_s.get("dry_run", True))
            running = bool(self.workers and any(w.is_alive() for w in self.workers))
            if running and new_live and not old_live:
                if not messagebox.askyesno(
                        "确认运行中切换为真实下单",
                        "任务正在运行。保存后下一轮命中就会真实提交订单。\n\n"
                        "确定要立即切换吗？"):
                    self.v_mode.set(MODE_DRY)
                    _s["dry_run"] = True
                    _s["auto_submit"] = False
                    self.log("已取消切换真实下单；其他配置继续保存")
            json.dump(self.cfg.raw, open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            self.cfg.rebuild()          # worker 下一轮按稳定任务 ID 重新绑定最新配置
            self._refresh_task_buyer_choices()
            if not silent:
                self.log("已保存到 %s（取货人/SKU/数量/城市/运行参数下一轮生效；"
                         "增删任务、更换身份文件或代理设置需停止后重新开始）" % self.path)
            self._pill("已保存", "success")
            self._summary()
            return True
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return False

    def collect(self):
        raw = self.cfg.raw
        def _bind_of(r):
            cb = r.get("buyer")
            v = (cb.get() if cb else "") or ""
            v = v.strip()
            return "" if (not v or v.startswith("（")) else v
        raw["cookies"] = [{"id": r["_id"], "name": r["name"].get().strip(), "cookie_file": r["file"].get().strip(),
                           "sku": _sku_of(r["sku"].get()).upper(), "enabled": bool(r["on"].get()),
                           "buyer": _bind_of(r),
                           "store_source": _store_source_of(
                               r["store_source"].get() if r.get("store_source") else ""),
                           "stores": list(r.get("_stores") or []),
                           "store": ((r.get("_stores") or [None])[0]
                                      if (r.get("_stores") and len(r["_stores"]) == 1) else ""),
                           "per_order": _order_cap(_positive_int(r["per_order"].get() if r.get("per_order") else "1", 1)),
                           "want": _norm_want(r["want"].get() if r.get("want") else 1)}
                          for r in self.task_rows]
        def _buyer_raw(r):
            nm = r["name"].get().strip()
            ln, fn = _split_name(nm, r)      # ★ 姓/名跟着【当前界面上的姓名】现算
            return {"id": r["_id"], "name": nm,
                    "phone": r["phone"].get().strip(),
                    "email": r["email"].get().strip(),
                    "last4": r["last4"].get().strip(),
                    "skus": [_sku_of(x) for x in r["sku"].get()],
                    "cities": r["city"].get(),
                    "last_name": ln, "first_name": fn,
                    "want": _parse_want(r["want"].get() if "want" in r else 1),
                    "enabled": bool(r["on"].get())}
        raw["buyers"] = [_buyer_raw(r) for r in self.buyer_rows]
        s = raw.setdefault("settings", {})
        s["dry_run"] = (self.v_mode.get() == MODE_DRY)
        s["auto_submit"] = (self.v_mode.get() == MODE_LIVE)
        s["warmup_mode"] = self.v_warmup_mode.get()
        s["time_slot_index"] = _positive_int(self.sv["time_slot_index"].get(), 1)
        s["prewarm_interval_sec"] = _positive_float(self.sv["prewarm_interval_sec"].get(), 0)
        s["prewarm_contact"] = bool(self.sv["prewarm_contact"].get())
        # ★★ 需要时段：开=没有可填时段就当没货（不进结算）；关=补完其它根照常提交
        s["require_time_slot"] = bool(self.sv["require_time_slot"].get())
        # ★ 这些设置键代码里已经【完全没有引用】了：保存时顺手从旧配置里清掉，
        #   免得留着一堆看着在生效、其实没人读的键（改了没反应就是这么来的）。
        for obsolete in (
                # —— 已删除的配置项（config.Settings 里已经没有这些字段了）——
                # 探针/时段预热/真机重选门店 三个实验开关（2026-09-24 已废弃）
                "slot_probe_sku", "prewarm_slot", "pin_select_store",
                "post_order_cooldown_sec", "skip_invoice", "spoke_page_recovery",
                "pause_scan_on_buy", "max_orders", "fast_fill_contact",
                # —— 早期实验开关 / 已下线的功能 ——
                "test_no_status_poll", "spinner_once", "yield_to_others",
                "hit_recheck", "cart_pipeline_enabled", "cart_pipeline_delay_ms",
                "confirm_budget_sec",
                # —— 单店/时段预热/流水线 等旧链路遗留 ——
                "store_mode", "single_store", "warm_mode", "warm_seed_store",
                "async_landing", "fill_plan", "heat_session", "heat_ttl_sec",
                "one_shot_skip_store", "one_shot_submit", "prefetch_spokes",
                "preheat_billing", "prewrite_invoice", "restore_stagger_sec",
                "reuse_slot_token", "skip_billing_if_ready", "skip_contact_open",
                "start_stagger_sec", "session_touch_sec", "submit_proxy",
                "submit_proxy_api", "submit_proxy_hard_sec", "submit_proxy_max_sec",
                "timeslot_cookie", "timeslot_refresh_sec", "timeslot_sku",
                "timeslot_store", "timeslot_watch_sku",
        ):
            s.pop(obsolete, None)
        s["debug_checkout"] = bool(self.sv["dbg_on"].get())
        try:
            s["status_poll_sec"] = float(self.sv["status_poll_sec"].get() or 3)
        except Exception:
            s["status_poll_sec"] = 3.0
        try:
            s["status_poll_max"] = int(float(self.sv["status_poll_max"].get() or 20))
        except Exception:
            s["status_poll_max"] = 20
        try:
            s["max_parallel_buy"] = max(1, int(float(self.sv["max_parallel_buy"].get() or 2)))
        except Exception:
            s["max_parallel_buy"] = 2
        s["proxy_enabled"] = bool(self.sv["proxy_on"].get())
        s["proxy_api_url"] = (self.sv["proxy_api_url"].get() or "").strip()
        try:
            s["proxy_rotate_sec"] = float(self.sv["proxy_rotate_sec"].get() or 60)
        except Exception:
            s["proxy_rotate_sec"] = 60.0
        _pm = (self.sv["proxy_mode"].get() or "")
        s["proxy_mode"] = "global" if _pm.startswith("全局") else "per_account"
        s.pop("proxy_batch", None)   # 代理服务商决定返回数；程序每池只消费一个新 IP
        _qc = self.sv.get("qr_cookie")
        s["qr_cookie"] = ((_qc.get() if _qc else "") or "").strip()
        _svc = self.sv.get("account_service_url")
        s["account_service_url"] = ((_svc.get() if _svc else "") or "").strip()
        for k, cast, dflt in (("scan_concurrency", int, 12), ("scan_interval_sec", float, 0.4),
                              ("session_rebuild_sec", int, 600),
                              ("retry_no_slot_sec", int, 15), ("settle_gap_sec", float, 2.0),
                              ("store_cool_sec", int, 15),
                              ("alarm_sec", int, 45)):
            try:
                s[k] = cast(float(self.sv[k].get() or dflt))
            except Exception:
                s[k] = dflt
        e = s.setdefault("email", {})
        e["enabled"] = bool(self.sv["mail_on"].get())
        e["host"] = self.sv["mail_host"].get().strip()
        try:
            e["port"] = int(float(self.sv["mail_port"].get() or 465))
        except Exception:
            e["port"] = 465
        e["user"] = self.sv["mail_user"].get().strip()
        e["pass"] = self.sv["mail_pass"].get().strip()
        e["to"] = [x.strip() for x in self.sv["mail_to"].get().split(",") if x.strip()]

    # ============================================================ 界面
    def _build(self):
        # ---- 顶部标题
        head = tb.Frame(self.root, padding=(20, 16, 20, 0))
        head.pack(fill=X)
        left = tb.Frame(head)
        left.pack(side=LEFT)
        tb.Label(left, text="苹果抢购助手", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        tb.Label(left, text="Apple 小程序 · 到店取货 · 多 cookie 并行", bootstyle="secondary",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(2, 0))
        right = tb.Frame(head)
        right.pack(side=RIGHT)
        self.v_pill = tb.StringVar(value="● 已停止")
        self.l_pill = tb.Label(right, textvariable=self.v_pill, bootstyle="secondary-inverse", padding=(14, 5))
        self.l_pill.pack(side=RIGHT, padx=(14, 0))
        self.v_theme = tb.StringVar(value="flatly")
        cbx = _no_wheel(tb.Combobox(right, textvariable=self.v_theme, values=THEMES, width=10, state="readonly"))
        cbx.pack(side=RIGHT, padx=(6, 0))
        tb.Label(right, text="主题", bootstyle="secondary").pack(side=RIGHT)

        # ---- 工具条
        bar = tb.Frame(self.root, padding=(20, 14, 20, 10))
        bar.pack(fill=X)
        self.b_start = tb.Button(bar, text="▶  开始抢购", bootstyle="success", width=13, command=self.start)
        self.b_start.pack(side=LEFT)
        self.b_stop = tb.Button(bar, text="■  停止", bootstyle="danger-outline", width=9, command=self.stop,
                                state="disabled")
        self.b_stop.pack(side=LEFT, padx=8)
        tb.Separator(bar, orient="vertical").pack(side=LEFT, fill=Y, padx=12, pady=2)
        self.v_mode = tb.StringVar(value=MODE_DRY)
        mbox = tb.Frame(bar)
        mbox.pack(side=LEFT, padx=(0, 12))
        tb.Radiobutton(mbox, text=MODE_DRY, value=MODE_DRY, variable=self.v_mode,
                       bootstyle="secondary-round-toggle").pack(side=LEFT)
        tb.Radiobutton(mbox, text=MODE_LIVE, value=MODE_LIVE, variable=self.v_mode,
                       bootstyle="danger-round-toggle").pack(side=LEFT, padx=(6, 0))
        tb.Label(bar, text="演练＝只跑到提交前，不建单；真实下单＝命中就自动提交",
                 bootstyle="secondary", font=("Microsoft YaHei UI", 8)).pack(side=LEFT)
        tb.Button(bar, text="保存配置", bootstyle="secondary-outline", width=10,
                  command=lambda: self.save(False)).pack(side=RIGHT)
        tb.Button(bar, text="配置目录", bootstyle="secondary-outline", width=10,
                  command=lambda: os.startfile(HERE)).pack(side=RIGHT, padx=8)
        tb.Button(bar, text="📋 有货记录", bootstyle="info-outline", width=12,
                  command=self._show_hits).pack(side=RIGHT, padx=8)

        # ---- 主体：上(标签页) / 下(日志) 可拖拽
        pw = tb.Panedwindow(self.root, orient="vertical", bootstyle="secondary")
        pw.pack(fill=BOTH, expand=True, padx=16, pady=(0, 4))
        top = tb.Frame(pw)
        pw.add(top, weight=3)
        self.nb = tb.Notebook(top, bootstyle="primary")
        self.nb.pack(fill=BOTH, expand=True)
        self._tab_tasks()
        self._tab_buyers()
        self._tab_settings()
        self._tab_board()
        self._tab_orders()
        low = tb.Labelframe(pw, text=" 运行日志 ", padding=(10, 6, 10, 8), bootstyle="secondary")
        pw.add(low, weight=1)
        self._build_log(low)

        # ---- 底部状态栏
        foot = tb.Frame(self.root, padding=(20, 2, 20, 10))
        foot.pack(fill=X)
        tb.Label(foot, text="配置：%s" % self.path, bootstyle="secondary",
                 font=("Microsoft YaHei UI", 8)).pack(side=LEFT)
        self.v_sum = tb.StringVar(value="")
        tb.Label(foot, textvariable=self.v_sum, bootstyle="secondary",
                 font=("Microsoft YaHei UI", 8)).pack(side=RIGHT)

    def _build_log(self, low):
        top = tb.Frame(low)
        top.pack(fill=X)
        tb.Button(top, text="清空", bootstyle="secondary-outline", width=6, command=self._clear_log).pack(side=LEFT)
        self.v_got = tb.StringVar(value="")
        tb.Label(top, textvariable=self.v_got, bootstyle="secondary").pack(side=LEFT, padx=12)
        tb.Label(top, text="成单 / 失败 / 命中 自动上色 ｜ 上下拖动分隔条可调整高度",
                 bootstyle="secondary", font=("Microsoft YaHei UI", 8)).pack(side=RIGHT)
        box = tb.Frame(low)
        box.pack(fill=BOTH, expand=True, pady=(6, 0))
        self.txt = tb.Text(box, height=12, wrap="none", font=("Consolas", 9), padx=6, pady=4)
        sb = tb.Scrollbar(box, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side=LEFT, fill=BOTH, expand=True)
        sb.pack(side=RIGHT, fill=Y)
        for tag, color, bold in (("ok", FG_OK, True), ("err", FG_ERR, True), ("warn", FG_WARN, False),
                                 ("hit", FG_HIT, True), ("dim", FG_DIM, False)):
            self.txt.tag_configure(tag, foreground=color, font=("Consolas", 9, "bold" if bold else "normal"))



    # ---------------- ④ 看板 ----------------
    # ---------------- 订单 / 支付 ----------------
    def _tab_orders(self):
        page = tb.Frame(self.nb, padding=(14, 12, 14, 6))
        self.nb.add(page, text="  ⑤ 订单 / 支付  ")
        tb.Label(page, bootstyle="secondary",
                 text="成单后自动列在这里。选中一行 → 打开订单链接 / 获取微信支付二维码（自动存图并打开、轮询支付状态）。",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 8))
        crow = tb.Frame(page)
        crow.pack(fill=X, pady=(0, 6))
        self.sv["qr_use_proxy"] = tb.BooleanVar(value=True)
        tb.Checkbutton(crow, text="取码走代理（每次取码专用一个IP）",
                       variable=self.sv["qr_use_proxy"],
                       bootstyle="warning-round-toggle").pack(side=LEFT)
        tb.Label(crow, text="PC浏览器Cookie", bootstyle="secondary",
                 font=("Microsoft YaHei UI", 9)).pack(side=LEFT, padx=(12, 0))
        self.sv["qr_cookie"] = tb.Entry(crow)
        self.sv["qr_cookie"].pack(side=LEFT, fill=X, expand=True, padx=6)
        self.v_order_status = tb.StringVar(value="还没有成单订单")
        tb.Label(page, textvariable=self.v_order_status, bootstyle="info",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 6))
        body = tb.Frame(page)
        body.pack(fill=BOTH, expand=True)
        cols = ("time", "acc", "sku", "store", "buyer", "order")
        self.tv_orders = tb.Treeview(body, columns=cols, show="headings", height=12)
        for c, lab, w in (("time", "时间", 80), ("acc", "账号", 90),
                          ("sku", "型号", 170), ("store", "门店", 180),
                          ("buyer", "取货人", 90), ("order", "订单号", 130)):
            self.tv_orders.heading(c, text=lab)
            self.tv_orders.column(c, width=w, anchor="w")
        sb = tb.Scrollbar(body, orient="vertical", command=self.tv_orders.yview)
        self.tv_orders.configure(yscrollcommand=sb.set)
        self.tv_orders.pack(side=LEFT, fill=BOTH, expand=True)
        sb.pack(side=LEFT, fill=Y)
        foot = tb.Frame(page)
        foot.pack(fill=X, pady=(8, 2))
        tb.Button(foot, text="打开订单链接", bootstyle="secondary-outline", width=14,
                  command=self._open_order_link).pack(side=LEFT)
        tb.Button(foot, text="获取支付二维码", bootstyle="warning", width=16,
                  command=self._get_qr).pack(side=LEFT, padx=10)
        tb.Button(foot, text="打开二维码图片", bootstyle="info-outline", width=14,
                  command=self._open_qr_png).pack(side=LEFT)
        tb.Button(foot, text="＋ 手动添加订单", bootstyle="success-outline", width=14,
                  command=self._manual_order).pack(side=LEFT, padx=10)
        tb.Button(foot, text="停止取码/轮询", bootstyle="danger-outline", width=14,
                  command=self._stop_qr).pack(side=LEFT, padx=10)
        self._load_orders()

    def _on_order(self, info):
        # worker 线程回调 → 只入队，界面在主线程 _drain 里更新
        try:
            self.q.put(("order", dict(info or {})))
        except Exception:
            pass

    def _render_order(self, info):
        oid = info.get("order") or ("o%d" % (len(self._orders) + 1))
        if info.get("link"):
            self._order_links[oid] = info.get("link")
        sku = info.get("sku") or ""
        st = info.get("store") or ""
        vals = (info.get("t") or time.strftime("%H:%M:%S"), info.get("cookie", ""),
                ("%s %s" % (sku, sku_name(sku))) if sku else "",
                ("%s %s" % (st, self.cfg.store_name(st))).strip() if st else "",
                info.get("buyer", ""), info.get("order") or "(没抓到订单号)")
        if self.tv_orders.exists(oid):
            self.tv_orders.item(oid, values=vals)
        else:
            self.tv_orders.insert("", "end", iid=oid, values=vals)
        return oid

    def _save_orders(self):
        try:
            json.dump(self._orders, open(self._orders_file, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _load_orders(self):
        try:
            if os.path.exists(self._orders_file):
                data = json.load(open(self._orders_file, encoding="utf-8"))
                if isinstance(data, list):
                    self._orders = [d for d in data if isinstance(d, dict)]
                    for info in self._orders:
                        self._render_order(info)
        except Exception:
            pass

    def _add_order(self, info):
        onum = info.get("order") or ""
        if onum and any((o.get("order") or "") == onum for o in self._orders):
            for o in self._orders:
                if (o.get("order") or "") == onum:
                    o.update(info)
                    break
            self._render_order(info)
        else:
            info.setdefault("t", time.strftime("%H:%M:%S"))
            self._orders.append(info)
            oid = self._render_order(info)
            self.v_order_status.set("新订单 %s │ 选中它 → 打开链接 / 获取支付二维码" % oid)
        self._save_orders()

    def _manual_order(self):
        link = tk.simpledialog.askstring("手动添加订单",
                                         "粘贴订单链接（苹果邮件里的 vieworder 链接）：",
                                         parent=self.root)
        if not link or not link.strip():
            return
        link = link.strip()
        m = re.search(r"(W\d{9,})", link)
        order = m.group(1) if m else (tk.simpledialog.askstring(
            "订单号", "没能从链接里识别订单号，请手动输入：", parent=self.root) or "")
        if not order:
            return
        self._add_order({"cookie": "手动", "sku": "", "store": "", "buyer": "",
                         "order": order, "link": link, "date": "", "time": ""})

    def _sel_order(self):
        sel = self.tv_orders.selection()
        return sel[0] if sel else ""

    def _open_order_link(self):
        oid = self._sel_order()
        link = self._order_links.get(oid, "")
        if not link:
            messagebox.showinfo("订单链接", "这一单没有订单链接（感谢页没给出 olssSmartLink）。\n"
                                            "可以去取货人邮箱找苹果邮件里的链接。")
            return
        try:
            webbrowser.open(link)
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _open_qr_png(self):
        oid = self._sel_order()
        pth = self._qr_pngs.get(oid, "")
        if pth and os.path.exists(pth):
            try:
                os.startfile(pth)
            except Exception as e:
                messagebox.showerror("打开失败", str(e))
        else:
            messagebox.showinfo("二维码", "还没生成二维码，先点「获取支付二维码」。")

    def _stop_qr(self):
        if self._qr_stop is not None and not self._qr_stop.is_set():
            self._qr_stop.set()
            self.v_order_status.set("已要求停止（当前请求/退避结束即停，基本点完就断）")
        else:
            self.v_order_status.set("当前没有正在进行的取码/轮询")

    def _qr_dedicated_proxy(self):
        """★ 取码专用 IP：每点一次「获取支付二维码」单独拿一个 IP，并且这一次的
        整条流程（解析链接→fetchOrder→payNow→取码→轮询）都锁死用它。

        为什么锁死：苹果把「同一个访客订单 token 几秒内从多个出口 IP 访问」当成典型
        风控特征（payNow 这种敏感动作基本必 541），而全局池 60 秒定时轮换会让一次
        取码横跨 2~3 个 IP。
        为什么每次换新的：一个 IP 上累积太多次取码/轮询一样会被拉黑，所以
        「一次取码一个 IP，下次取码换下一个」——既保会话亲和，又不把号做死。
        返回 (provider, ip文本)；没开代理 → (None, "")。
        """
        try:
            if not bool(getattr(self.cfg.settings, "proxy_enabled", False)):
                return None, ""
            from .proxy_pool import get_pool
            pool = get_pool(self.cfg, self.log)
            ip = pool.advance("取码专用 IP") or pool.fetch_one(force=True)
            if not ip:
                return None, ""

            def _p(_ip=ip):
                return _ip or None
            _p.pool = pool
            return _p, ip.replace("http://", "")
        except Exception:
            return None, ""

    def _qr_proxy(self):
        try:
            if not bool(getattr(self.cfg.settings, "proxy_enabled", False)):
                return None
            from .proxy_pool import get_pool
            return get_pool(self.cfg, self.log).provider()
        except Exception:
            return None

    def _get_qr(self):
        oid = self._sel_order()
        if not oid:
            messagebox.showinfo("获取支付二维码", "先在表格里选中一个订单。")
            return
        link = self._order_links.get(oid, "")
        if not link:
            messagebox.showwarning("获取支付二维码", "这一单没有订单链接，无法自动取码。")
            return
        _qc = self.sv.get("qr_cookie")
        cookie = ((_qc.get() if _qc else "") or "").strip()
        # ★★ Cookie 必填：不带基本拿不到二维码（要么 541，要么服务端直接不给图）。
        if not cookie:
            messagebox.showerror(
                "获取支付二维码",
                "还没填 PC 浏览器 Cookie。\n\n"
                "拿法：浏览器打开 apple.com.cn → F12 → Network → "
                "点任意一个请求 → 复制 Request Headers 里整条 Cookie\n"
                "→ 粘进上面那个框，再点「获取支付二维码」。\n\n"
                "注意：必须是登录过/访问过 apple.com.cn 的那个浏览器的 Cookie。")
            self.v_order_status.set("还没填 Cookie → 已阻止取码")
            return
        out_dir = os.path.join(HERE, "支付二维码")
        if self._qr_stop is not None:
            self._qr_stop.set()
        stop_evt = threading.Event()
        self._qr_stop = stop_evt
        use_proxy = bool(self.sv["qr_use_proxy"].get()) if self.sv.get("qr_use_proxy") else False
        # ★ 一次取码锁死一个专用 IP（整条流程都用它）；下次点「获取二维码」再换下一个
        prov, _ip = (self._qr_dedicated_proxy() if use_proxy else (None, ""))
        self.v_order_status.set("正在获取支付二维码（%s）…%s"
                                % (oid, (" 专用 IP %s（本次取码全程锁死）" % _ip) if _ip
                                   else (" 走代理" if prov else " 直连")))

        def _log(m):
            self.q.put(("log", "[支付:%s] %s" % (oid, m)))

        def _status(state, text):
            self.q.put(("qr_status", (oid, state, text)))

        def _run():
            try:
                from .qrpay import fetch_qr
                res = fetch_qr(link, log=_log, stop_evt=stop_evt, out_dir=out_dir,
                               cookie=cookie, poll=True, poll_sec=3.0, max_wait=1800.0,
                               on_status=_status, proxy_provider=prov)
                self._qr_pngs[oid] = res.get("png_path", "")
            except Exception as e:
                self.q.put(("log", "[支付:%s] 失败：%s" % (oid, e)))
                self.q.put(("qr_status", (oid, "error", "失败：%s" % e)))

        threading.Thread(target=_run, name="qr-%s" % oid, daemon=True).start()

    def _tab_board(self):
        page = tb.Frame(self.nb, padding=(14, 12, 14, 6))
        self.nb.add(page, text="  ④ 看板  ")
        tb.Label(page, bootstyle="secondary",
                 text="每秒自动刷新。上面看每个号「还在不在跑」，下面看扫店的数量、耗时和是否被限流。",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 8))

        t1 = tb.Labelframe(page, text=" 心跳：每个 cookie 现在在干什么 ", padding=(8, 6, 8, 8), bootstyle="info")
        t1.pack(fill=BOTH, expand=True, pady=(0, 8))
        c1 = (("name", "号", 90), ("sku", "型号", 140), ("tier", "档位", 130), ("state", "状态", 90),
              ("alive", "会话活了", 80), ("rounds", "探测轮数", 80), ("orders", "已买到", 70),
              ("note", "最后一句", 430))
        self.tv1 = tb.Treeview(t1, columns=[c[0] for c in c1], show="headings", height=10, bootstyle="info")
        for k, t, w in c1:
            self.tv1.heading(k, text=t)
            self.tv1.column(k, width=w, anchor="w", stretch=(k == "note"))
        sb1 = tb.Scrollbar(t1, orient="vertical", command=self.tv1.yview)
        self.tv1.configure(yscrollcommand=sb1.set)
        self.tv1.pack(side=LEFT, fill=BOTH, expand=True)
        sb1.pack(side=RIGHT, fill=Y)

        t2 = tb.Labelframe(page, text=" 扫描情况：探测族（查货）的实时统计 ", padding=(8, 6, 8, 8),
                           bootstyle="secondary")
        t2.pack(fill=BOTH, expand=True)
        c2 = (("name", "号", 90), ("rounds", "轮数", 70), ("stores", "每轮店数", 80), ("ms", "本轮耗时", 80),
              ("req", "累计请求", 85), ("ok", "有货", 60), ("no", "无货", 60), ("err", "查不到", 70),
              ("f541", "541", 60), ("f403", "403", 60), ("hits", "最近有货门店", 360))
        self.tv2 = tb.Treeview(t2, columns=[c[0] for c in c2], show="headings", height=10, bootstyle="secondary")
        for k, t, w in c2:
            self.tv2.heading(k, text=t)
            self.tv2.column(k, width=w, anchor="w", stretch=(k == "hits"))
        sb2 = tb.Scrollbar(t2, orient="vertical", command=self.tv2.yview)
        self.tv2.configure(yscrollcommand=sb2.set)
        self.tv2.pack(side=LEFT, fill=BOTH, expand=True)
        sb2.pack(side=RIGHT, fill=Y)

    def _refresh_board(self):
        ws = list(self.workers or [])
        seen = set()
        tot = {"rounds": 0, "req": 0, "ok": 0, "no": 0, "err": 0, "541": 0, "403": 0, "orders": 0}
        for w in ws:
            st = getattr(w, "stat", None)
            if not st:
                continue
            nm = st.get("name") or "?"
            seen.add(nm)
            sess = getattr(w, "sess", None)
            try:
                alive = (time.time() - sess.t_alive) / 60.0 if sess and sess.t_alive else 0.0
                sid = (sess.sid or "")[:10]
            except Exception:
                alive, sid = 0.0, ""
            st5 = getattr(w, "cli", None)
            f541 = getattr(st5, "n_541", 0) if st5 else 0
            f403 = getattr(st5, "n_403", 0) if st5 else 0
            note = st.get("note") or ""
            if sid:
                note = "[%s] %s" % (sid, note)
            vals1 = (nm, sku_name(st.get("sku") or ""), _ident_kind(getattr(w.cc, "path", "")),
                     st.get("state") or "-", ("%.1f 分" % alive) if alive else "-",
                     st.get("rounds", 0), st.get("orders", 0), note)
            if self.tv1.exists(nm):
                self.tv1.item(nm, values=vals1)
            else:
                self.tv1.insert("", "end", iid=nm, values=vals1)
            hits_txt = "、".join(st.get("last_hits") or []) or "-"
            vals2 = (nm, st.get("rounds", 0), st.get("stores", 0),
                     ("%d ms" % st.get("ms_last", 0)) if st.get("ms_last") else "-",
                     st.get("req", 0), st.get("ok", 0), st.get("no", 0), st.get("err", 0),
                     f541, f403, hits_txt)
            if self.tv2.exists(nm):
                self.tv2.item(nm, values=vals2)
            else:
                self.tv2.insert("", "end", iid=nm, values=vals2)
            tot["rounds"] += st.get("rounds", 0); tot["req"] += st.get("req", 0)
            tot["ok"] += st.get("ok", 0); tot["no"] += st.get("no", 0)
            tot["err"] += st.get("err", 0); tot["541"] += f541; tot["403"] += f403
            tot["orders"] += st.get("orders", 0)
        # 取货人额度：已抢 / 想要（每个型号分开显示）
        try:
            led = self.cfg.ledger
            # 取货人额度：多个任务可共用一人，这里显示合计进度。
            for r in self.buyer_rows:
                nmv = r.get("_name") or ""
                b = next((x for x in self.cfg.buyers if x.name == nmv), None)
                if b is None or "quota" not in r:
                    continue
                bound = [c for c in self.cfg.cookies if (getattr(c, "buyer", "") or "") == b.name]
                if not bound:
                    r["quota"].set("未被任何任务绑定")
                    continue
                tot = led.buyer_total(b.id)
                want = int(getattr(b, "want", 1) or 1)
                tag = "（已收工）" if tot >= want else ""
                if not (r.get("on") and r["on"].get()):
                    tag = "（未启用）"
                r["quota"].set("共 %d 个任务 ｜ 合计已抢 %d / %d 台 %s"
                               % (len(bound), tot, want, tag))
                self._tv_buyers_cell(r.get("_id"))
            # 任务额度：每个号 已抢 / 共要
            for r in self.task_rows:
                if "quota" not in r:
                    continue
                nmv = (r["name"].get().strip() if r.get("name") else (r.get("_name") or ""))
                want = 1
                try:
                    want = max(1, _positive_int(r["want"].get(), 1)) if r.get("want") else 1
                except Exception:
                    pass
                cc = next((c for c in self.cfg.cookies if c.name == nmv), None)
                tot = led.task_total(cc.id) if cc else 0
                r["quota"].set("已抢 %d / %d 台%s" % (tot, want, "（已收工）" if tot >= want else ""))
                self._tv_tasks_cell(r.get("_id"), "quota")
        except Exception:
            pass
        # 清掉已经不存在的号
        for tv in (self.tv1, self.tv2):
            for iid in tv.get_children():
                if iid not in seen:
                    tv.delete(iid)
        if ws:
            t = self.tv1
            v = ("合计 %d 个号" % len(seen), "", "", "运行中" if self.stop_evt and not self.stop_evt.is_set() else "已停止",
                 "", tot["rounds"], tot["orders"], "累计请求 %d ｜ 有货 %d ｜ 无货 %d ｜ 查不到 %d"
                 % (tot["req"], tot["ok"], tot["no"], tot["err"]))
            (t.item("__tot__", values=v) if t.exists("__tot__")
             else t.insert("", "end", iid="__tot__", values=v, tags=("tot",)))
            t2 = self.tv2
            v2 = ("合计", tot["rounds"], "", "", tot["req"], tot["ok"], tot["no"], tot["err"],
                  tot["541"], tot["403"], "541 出现就该停" if tot["541"] else "没有被限流 ✓")
            (t2.item("__tot__", values=v2) if t2.exists("__tot__")
             else t2.insert("", "end", iid="__tot__", values=v2))

    # ---------------- 任务 ----------------
    # ---------------- 抢购任务（表格视图） ----------------
    TASK_COLS = (("on", "启用", 48), ("name", "任务名", 108), ("file", "身份文件", 200),
                 ("sku", "抢的型号", 128), ("store", "盯的门店", 148), ("buyer", "取货人", 88),
                 ("per_order", "每单", 46), ("want", "共要", 46),
                 ("quota", "进度", 120), ("status", "状态", 260))

    def _tab_tasks(self):
        page = tb.Frame(self.nb, padding=(14, 12, 14, 6))
        self.nb.add(page, text="  ① 抢购任务  ")
        tb.Label(page, bootstyle="secondary",
                 text="新增/编辑任务时，从【在线账号】下拉框选账号并点【获取Cookie】自动登录（对接本地协议服务 /accounts + /wx/getCode）；"
                      "每个账号抢一个型号。★ 勾选多行 → 用上面的批量按钮；双击一行编辑。",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 8))
        bar = tb.Frame(page)
        bar.pack(fill=X, pady=(0, 6))

        def _b(txt, cmd, style="secondary-outline", w=9):
            tb.Button(bar, text=txt, bootstyle=style, width=w,
                      command=cmd).pack(side=LEFT, padx=(0, 5))

        _b("＋ 新增", lambda: self._task_add(), "primary", 8)
        _b("编辑", lambda: self._task_edit(), "info-outline", 7)
        _b("删除", lambda: self._task_batch_delete(), "danger-outline", 7)
        _b("全选", lambda: self._tv_sel_all(self.tv_tasks))
        _b("反选", lambda: self._tv_invert(self.tv_tasks))
        tb.Separator(bar, orient="vertical").pack(side=LEFT, fill=Y, padx=6)
        _b("批量启用", lambda: self._task_batch_on(True), "success-outline", 9)
        _b("批量停用", lambda: self._task_batch_on(False), "secondary-outline", 9)
        _b("批量门店", lambda: self._task_batch_store(), "warning-outline", 9)
        _b("批量取货人", lambda: self._task_batch_buyer(), "warning-outline", 10)
        _b("批量数量", lambda: self._task_batch_qty(), "warning-outline", 9)
        _b("导入身份", lambda: self._task_import(), "info-outline", 9)

        foot = tb.Frame(page)
        foot.pack(side=BOTTOM, fill=X, pady=(6, 2))
        self.v_task_n = tb.StringVar(value="")
        tb.Label(foot, textvariable=self.v_task_n, bootstyle="secondary",
                 font=("Microsoft YaHei UI", 9)).pack(side=LEFT)
        tb.Label(foot, text="提示：勾选后可批量改门店/取货人/数量；「导入身份」会为每个 txt 自动建一个任务。",
                 bootstyle="secondary", font=("Microsoft YaHei UI", 8)).pack(side=RIGHT)

        body = tb.Frame(page)
        body.pack(fill=BOTH, expand=True)
        self.tv_tasks = tb.Treeview(body, columns=[c[0] for c in self.TASK_COLS],
                                    show="headings", selectmode="extended", height=14,
                                    bootstyle="info")
        for k, t, w in self.TASK_COLS:
            self.tv_tasks.heading(k, text=t, anchor="center")
            # ★ 数据全部居中（用户要求）
            self.tv_tasks.column(k, width=w, stretch=(k in ("status", "file", "quota")),
                                 anchor="center")
        sb = tb.Scrollbar(body, orient="vertical", command=self.tv_tasks.yview)
        self.tv_tasks.configure(yscrollcommand=sb.set)
        self.tv_tasks.pack(side=LEFT, fill=BOTH, expand=True)
        sb.pack(side=LEFT, fill=Y)
        self.tv_tasks.bind("<Double-1>", lambda _e: self._task_edit())
        self.tv_tasks.bind("<<TreeviewSelect>>", lambda _e: self._task_sel_changed())

    # ---- 行模型：把配置 dict 变成表格行（值用 _Cell 包着，兼容老代码）----
    def _task_row_from(self, cur=None) -> dict:
        cur = cur or {}
        nm = (cur.get("name") or "").strip()
        return {
            "_id": str(cur.get("id") or ("task-" + uuid.uuid4().hex)),
            "_name": nm,
            "name": _Cell(nm),
            "file": _Cell(cur.get("cookie_file") or ""),
            "sku": _Cell(_sku_item((cur.get("sku") or "").upper()) if cur.get("sku") else SKU_ITEMS[0]),
            "store": _Cell(_store_summary(cur, self.store_items)),
            "store_source": _Cell(STORE_SRC_STORE if _row_store_source(cur) == STORE_SOURCE_STORE
                                  else STORE_SRC_BUYER),
            "_stores": _row_stores(cur),
            "buyer": _Cell((cur.get("buyer") or "").strip() or "（未绑定）"),
            "on": _Cell(bool(cur.get("enabled", True))),
            "per_order": _Cell(str(_order_cap(cur.get("per_order", 1)))),
            "want": _Cell(_want_text(_norm_want(cur.get("want", 1)))),
            "quota": _Cell(""),
            "status": _Cell("待启动"),
        }

    def _task_values(self, r) -> tuple:
        return ("✓" if r["on"].get() else "—",
                r["name"].get() or "(未命名)",
                r["file"].get() or "(未选身份文件)",
                sku_name(_sku_of(r["sku"].get())),
                r["store"].get(),
                r["buyer"].get(),
                r["per_order"].get(), r["want"].get(),
                r["quota"].get(), r["status"].get())

    @staticmethod
    def _tv_sel_all(tv):
        try:
            tv.selection_set(tv.get_children())
        except Exception:
            pass

    @staticmethod
    def _tv_invert(tv):
        try:
            cur = set(tv.selection())
            tv.selection_set([i for i in tv.get_children() if i not in cur])
        except Exception:
            pass

    def _tv_tasks_render(self):
        """整表重画（增删改后调用）。"""
        try:
            tv = self.tv_tasks
        except Exception:
            return
        keep = set()
        for r in self.task_rows:
            keep.add(r["_id"])
            if tv.exists(r["_id"]):
                tv.item(r["_id"], values=self._task_values(r))
            else:
                tv.insert("", "end", iid=r["_id"], values=self._task_values(r))
        for iid in tv.get_children():
            if iid not in keep:
                tv.delete(iid)
        self._task_sel_changed()

    def _tv_tasks_cell(self, row_id: str, col: str):
        """只刷一个格（状态/进度用，避免整表重画）。"""
        try:
            r = next((x for x in self.task_rows if x["_id"] == row_id), None)
            if r is not None and self.tv_tasks.exists(row_id):
                self.tv_tasks.item(row_id, values=self._task_values(r))
        except Exception:
            pass

    def _selected_task_rows(self) -> list:
        try:
            sel = list(self.tv_tasks.selection())
        except Exception:
            return []
        return [r for r in self.task_rows if r["_id"] in sel]

    def _task_sel_changed(self):
        n = len(self._selected_task_rows())
        try:
            self.v_task_n.set("共 %d 个任务 ｜ 已选 %d 个" % (len(self.task_rows), n))
        except Exception:
            pass

    # ---- 增 / 改 / 删 ----
    def _task_add(self):
        cur = self._task_dialog({}, title="新增抢购任务")
        if cur is None:
            return
        self.task_rows.append(self._task_row_from(cur))
        self._tv_tasks_render()
        self._refresh_task_buyer_choices()
        self.save()

    def _task_edit(self):
        rows = self._selected_task_rows()
        if len(rows) != 1:
            messagebox.showinfo("编辑任务", "请先选中【一行】再点编辑（多选请用批量按钮）")
            return
        r = rows[0]
        cur = {"id": r["_id"], "name": r["name"].get(), "cookie_file": r["file"].get(),
               "sku": _sku_of(r["sku"].get()),
               "store_source": _store_source_of(r.get("store_source")),
               "stores": list(r.get("_stores") or []),
               "buyer": r["buyer"].get(), "per_order": r["per_order"].get(),
               "want": r["want"].get(), "enabled": bool(r["on"].get())}
        new = self._task_dialog(cur, title="编辑抢购任务")
        if new is None:
            return
        nr = self._task_row_from(new)
        nr["quota"] = r["quota"]
        nr["status"] = r["status"]
        idx = self.task_rows.index(r)
        self.task_rows[idx] = nr
        self._tv_tasks_render()
        self._refresh_task_buyer_choices()
        self.save()

    def _task_batch_delete(self):
        rows = self._selected_task_rows()
        if not rows:
            messagebox.showinfo("批量删除", "请先勾选要删除的任务（可多选）")
            return
        if not messagebox.askyesno("批量删除", "确认删除选中的 %d 个任务？" % len(rows)):
            return
        for r in rows:
            self.task_rows.remove(r)
            self.status_vars.pop(r.get("_name"), None)
        self._tv_tasks_render()
        self._refresh_task_buyer_choices()
        self.save()

    # ---- 批量操作 ----
    def _task_batch_on(self, on: bool):
        rows = self._selected_task_rows()
        if not rows:
            messagebox.showinfo("批量启用/停用", "请先勾选任务（可多选）")
            return
        for r in rows:
            r["on"].set(bool(on))
        self._tv_tasks_render()
        self.save()

    def _task_batch_store(self):
        rows = self._selected_task_rows()
        if not rows:
            messagebox.showinfo("批量门店", "请先勾选任务（可多选）")
            return
        BATCH_MULTI = "—— 自定义多家门店… ——"
        val = self._pick_one_dialog("批量设置门店来源",
                                    "把这 %d 个任务统一设成哪种门店来源？" % len(rows),
                                    [STORE_SRC_BUYER] + list(self.store_items or []) + [BATCH_MULTI])
        if val is None:
            return
        if val == BATCH_MULTI:
            codes = self._pick_stores_dialog()
            if not codes:
                return
            for r in rows:
                self._set_row_store(r, STORE_SOURCE_STORE, codes)
        elif val == STORE_SRC_BUYER:
            for r in rows:
                self._set_row_store(r, STORE_SOURCE_BUYER, [])
        else:
            for r in rows:
                self._set_row_store(r, STORE_SOURCE_STORE, [_store_code(val)])
        self._tv_tasks_render()
        self.save()

    def _set_row_store(self, r, source: str, codes):
        """把一行的门店来源/门店列表写进行模型（store 列同步刷新成摘要）。"""
        codes = [str(c).strip().upper() for c in (codes or []) if str(c).strip()]
        r["store_source"].set(STORE_SRC_STORE if source == STORE_SOURCE_STORE else STORE_SRC_BUYER)
        r["_stores"] = codes
        r["store"].set(_store_summary({"store_source": source, "stores": codes}, self.store_items))

    def _pick_stores_dialog(self, selected=None, title="选择要盯的门店（可多选）"):
        """弹窗多选门店；返回门店编号列表，取消/未选返回 None。"""
        win = tb.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        tb.Label(win, text="勾选要盯的门店（可多选），点【确定】生效", padding=(16, 14, 16, 6),
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        body = tb.Frame(win, padding=(16, 0, 16, 6))
        body.pack(fill=BOTH, expand=True)
        lb = tk.Listbox(body, selectmode="multiple", exportselection=False,
                        height=min(16, max(6, len(self.store_items or []))), width=40,
                        font=("Microsoft YaHei UI", 10), activestyle="none",
                        highlightthickness=1, relief="flat", bd=0)
        for it in (self.store_items or []):
            lb.insert(END, it)
        cur = {_store_code(x) for x in (selected or [])}
        for idx, it in enumerate(self.store_items or []):
            if _store_code(it) in cur:
                lb.selection_set(idx)
        sb = tb.Scrollbar(body, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side=LEFT, fill=BOTH, expand=True)
        sb.pack(side=LEFT, fill=Y)
        side = tb.Frame(body)
        side.pack(side=LEFT, fill=Y, padx=(8, 0))
        tb.Button(side, text="全选", bootstyle="secondary-outline", width=6,
                  command=lambda: lb.selection_set(0, END)).pack(pady=2)
        tb.Button(side, text="清空", bootstyle="secondary-outline", width=6,
                  command=lambda: lb.selection_clear(0, END)).pack(pady=2)
        out = {"v": None}

        def ok(_=None):
            out["v"] = [_store_code(self.store_items[i]) for i in lb.curselection()]
            win.destroy()
        foot = tb.Frame(win, padding=(16, 4, 16, 14))
        foot.pack(fill=X)
        tb.Button(foot, text="确定", bootstyle="primary", width=10, command=ok).pack(side=RIGHT)
        tb.Button(foot, text="取消", bootstyle="secondary-outline", width=10,
                  command=win.destroy).pack(side=RIGHT, padx=8)
        lb.bind("<Double-Button-1>", ok)
        _center_window(win, self.root)
        win.grab_set()
        self.root.wait_window(win)
        return out["v"]

    def _task_batch_buyer(self):
        rows = self._selected_task_rows()
        if not rows:
            messagebox.showinfo("批量取货人", "请先勾选任务（可多选）")
            return
        val = self._pick_one_dialog("批量绑定取货人",
                                    "把这 %d 个任务统一绑给谁？" % len(rows),
                                    ["（未绑定）"] + self._buyer_names())
        if val is None:
            return
        for r in rows:
            r["buyer"].set(val)
        self._tv_tasks_render()
        self.save()

    def _task_batch_qty(self):
        rows = self._selected_task_rows()
        if not rows:
            messagebox.showinfo("批量数量", "请先勾选任务（可多选）")
            return
        po, want = self._task_qty_dialog(len(rows))
        if po is None:
            return
        for r in rows:
            r["per_order"].set(str(po))
            r["want"].set(str(want))
        self._tv_tasks_render()
        self.save()

    def _task_import(self):
        """批量导入身份文件：一次选多个 txt，每个自动建一个任务。"""
        files = filedialog.askopenfilenames(title="选择身份文件（可多选）",
                                            initialdir=HERE, filetypes=[("身份文件", "*.txt"), ("全部", "*.*")])
        if not files:
            return
        added = 0
        for fp in files:
            try:
                rel = os.path.relpath(fp, HERE).replace("\\", "/")
            except Exception:
                rel = fp
            if any((r["file"].get() or "").strip() == rel for r in self.task_rows):
                continue
            nm = os.path.splitext(os.path.basename(fp))[0]
            self.task_rows.append(self._task_row_from({"name": nm, "cookie_file": rel, "enabled": True}))
            added += 1
        self._tv_tasks_render()
        self._refresh_task_buyer_choices()
        self.save()
        if added:
            self.log("已导入 %d 个任务（记得给每个任务选型号 / 盯的门店 / 绑定取货人）" % added)
        else:
            messagebox.showinfo("导入身份", "这些身份文件已经在任务列表里了")

    # ---- 对话框 ----
    def _pick_one_dialog(self, title: str, prompt: str, values: list):
        """通用"从列表里选一个"的模态对话框；返回选中的字符串或 None。"""
        win = tb.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        tb.Label(win, text=prompt, padding=(16, 14, 16, 6),
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        var = tb.StringVar(value=values[0] if values else "")
        cb = _no_wheel(tb.Combobox(win, values=values, textvariable=var, width=34, state="readonly"))
        cb.pack(padx=16, pady=(0, 10), fill=X)
        out = {"v": None}

        def ok():
            out["v"] = var.get()
            win.destroy()
        row = tb.Frame(win, padding=(16, 0, 16, 14))
        row.pack(fill=X)
        tb.Button(row, text="确定", bootstyle="primary", width=10, command=ok).pack(side=RIGHT)
        tb.Button(row, text="取消", bootstyle="secondary-outline", width=8,
                  command=win.destroy).pack(side=RIGHT, padx=(0, 8))
        _center_window(win, self.root)
        win.grab_set()
        self.root.wait_window(win)
        return out["v"]

    def _task_qty_dialog(self, n: int):
        """批量设置「每单件数 / 共要台数」；返回 (per_order, want) 或 (None, None)。"""
        win = tb.Toplevel(self.root)
        win.title("批量数量")
        win.transient(self.root)
        win.resizable(False, False)
        tb.Label(win, text="把这 %d 个任务统一设成：" % n, padding=(16, 14, 16, 6),
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        row = tb.Frame(win, padding=(16, 0, 16, 6))
        row.pack(fill=X)
        tb.Label(row, text="每单件数").pack(side=LEFT)
        e1 = tb.Entry(row, width=6)
        e1.insert(0, "1")
        e1.pack(side=LEFT, padx=(6, 16))
        tb.Label(row, text="共要台数").pack(side=LEFT)
        e2 = tb.Entry(row, width=6)
        e2.insert(0, "1")
        e2.pack(side=LEFT, padx=(6, 0))
        tb.Label(win, text="每单件数上限 2 件（苹果限购）；共要台数是这个号累计要抢几台",
                 bootstyle="secondary", font=("Microsoft YaHei UI", 8),
                 padding=(16, 0, 16, 10)).pack(anchor="w")
        out = {"v": (None, None)}

        def ok():
            out["v"] = (_order_cap(_positive_int(e1.get(), 1)), _norm_want(e2.get() or 1))
            win.destroy()
        btns = tb.Frame(win, padding=(16, 0, 16, 14))
        btns.pack(fill=X)
        tb.Button(btns, text="确定", bootstyle="primary", width=10, command=ok).pack(side=RIGHT)
        tb.Button(btns, text="取消", bootstyle="secondary-outline", width=8,
                  command=win.destroy).pack(side=RIGHT, padx=(0, 8))
        _center_window(win, self.root)
        win.grab_set()
        self.root.wait_window(win)
        return out["v"]

    def _task_dialog(self, cur: dict, title: str = "抢购任务"):
        """新增/编辑一个任务的模态对话框；返回新的 dict 或 None。"""
        win = tb.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        cur = cur or {}
        sv = {}

        def row(label, width=30):
            f = tb.Frame(win, padding=(16, 6, 16, 0))
            f.pack(fill=X)
            tb.Label(f, text=label, width=12).pack(side=LEFT)
            e = tb.Entry(f, width=width)
            e.pack(side=LEFT, fill=X, expand=True)
            return e

        e_nm = row("任务名")
        e_nm.insert(0, cur.get("name") or "")

        # ---- 在线账号（原“身份文件”选择：改为从本地协议服务 /accounts 读取并登录取 Cookie）----
        _state = {"file": (cur.get("cookie_file") or "").strip(), "acct": None}
        _base = ""
        try:
            _s = (self.cfg.raw or {}).get("settings", {}) if getattr(self, "cfg", None) else {}
            _base = str((_s or {}).get("account_service_url") or "").strip()
        except Exception:
            _base = ""
        _base = _base or autologin.DEFAULT_SERVICE

        f_acct = tb.Frame(win, padding=(16, 6, 16, 0))
        f_acct.pack(fill=X)
        tb.Label(f_acct, text="在线账号", width=12).pack(side=LEFT)
        v_acct = tk.StringVar(value="")
        c_acct = _no_wheel(tb.Combobox(f_acct, values=[], width=22, state="readonly",
                                       textvariable=v_acct))
        c_acct.pack(side=LEFT, fill=X, expand=True)
        _acct_map = {}

        def _acct_desc(a):
            nm = (a.get("nickname") or a.get("alias") or "账号").strip()
            return "%s（…%s｜%s）" % (nm, str(a.get("openid") or "")[-6:], a.get("channel") or "wxapp")

        def _fill_accounts(accounts):
            vals = [_acct_desc(a) for a in accounts]
            _acct_map.clear()
            for d, a in zip(vals, accounts):
                _acct_map[d] = a
            c_acct.configure(values=vals)
            if vals and v_acct.get() not in _acct_map:
                v_acct.set(vals[0])
            elif not vals:
                v_acct.set("")

        f_fst = tb.Frame(win, padding=(16, 2, 16, 0))
        f_fst.pack(fill=X)
        tb.Label(f_fst, text="", width=12).pack(side=LEFT)
        st_a = tb.Label(f_fst,
                        text=("当前身份文件：%s" % os.path.basename(_state["file"])) if _state["file"]
                        else "尚未获取 Cookie",
                        bootstyle="secondary", font=("Microsoft YaHei UI", 8),
                        wraplength=380, justify="left")
        st_a.pack(side=LEFT, fill=X, expand=True)

        def _reload_accounts(silent=True):
            st_a.configure(text="正在读取在线账号…", bootstyle="secondary")
            win.update_idletasks()
            try:
                accts = autologin.list_online_accounts(base=_base)
                _fill_accounts(accts)
                st_a.configure(text=("在线账号 %d 个（服务 %s）" % (len(accts), _base)) if accts
                               else ("没有在线账号（服务 %s）" % _base),
                               bootstyle="secondary" if accts else "warning")
            except Exception as e:
                st_a.configure(text="读取账号失败：%s" % e, bootstyle="danger")
                if not silent:
                    messagebox.showwarning(title, "读取在线账号失败：\n%s" % e, parent=win)

        def _do_login():
            a = _acct_map.get(v_acct.get())
            if not a:
                messagebox.showwarning(title, "请先选择要登录的在线账号", parent=win)
                return
            who = (a.get("nickname") or a.get("alias") or a.get("openid") or "")
            st_a.configure(text="正在用 %s 登录并获取 Cookie…" % who, bootstyle="secondary")
            win.update_idletasks()
            try:
                res = autologin.fetch_cookie_for_account(a.get("openid"), base=_base)
                nm = (e_nm.get().strip()
                      or (a.get("nickname") or a.get("alias") or "").strip()
                      or ("账号" + str(a.get("openid"))[-6:]))
                safe = re.sub(r'[\\/:*?"<>|\s]+', "_", nm)[:32] or "acct"
                tail = str(a.get("openid") or "")[-6:]
                dest = os.path.join(HERE, "身份", "自动登录-%s-%s.txt" % (safe, tail))
                autologin.write_identity(dest, res["cookie"], ua="")
                _state["file"] = dest
                _state["acct"] = a
                if not e_nm.get().strip() and (a.get("nickname") or a.get("alias")):
                    e_nm.insert(0, (a.get("nickname") or a.get("alias") or "").strip())
                st_a.configure(text="✓ 已获取 Cookie（%d 字节）→ %s"
                                    % (len(res["cookie"]), os.path.basename(dest)),
                               bootstyle="success")
            except Exception as e:
                st_a.configure(text="登录失败：%s" % e, bootstyle="danger")
                messagebox.showwarning(title, "自动登录失败：\n%s" % e, parent=win)

        def _pick_local():
            p = filedialog.askopenfilename(title="选择身份文件（cookie txt）", initialdir=HERE,
                                           filetypes=[("文本", "*.txt"), ("全部", "*.*")])
            if p:
                _state["file"] = p
                st_a.configure(text="已选择本地文件：%s" % os.path.basename(p), bootstyle="secondary")

        tb.Button(f_acct, text="刷新", bootstyle="secondary-outline", width=5,
                  command=lambda: _reload_accounts(silent=False)).pack(side=LEFT, padx=(6, 0))
        tb.Button(f_acct, text="获取Cookie", bootstyle="primary", width=9,
                  command=_do_login).pack(side=LEFT, padx=(4, 0))
        tb.Button(f_acct, text="本地文件…", bootstyle="secondary-outline", width=8,
                  command=_pick_local).pack(side=LEFT, padx=(4, 0))
        win.after(60, lambda: _reload_accounts(silent=True))
        f_sku = tb.Frame(win, padding=(16, 6, 16, 0))
        f_sku.pack(fill=X)
        tb.Label(f_sku, text="抢的型号", width=12).pack(side=LEFT)
        c_sku = _no_wheel(tb.Combobox(f_sku, values=SKU_ITEMS, width=30, state="readonly"))
        c_sku.set(_sku_item(cur["sku"]) if cur.get("sku") else SKU_ITEMS[0])
        c_sku.pack(side=LEFT, fill=X, expand=True)
        # ★★ 门店来源（任务级二选一）：按取货人意向地址 / 按门店自定义（多选）
        f_src = tb.Frame(win, padding=(16, 6, 16, 0))
        f_src.pack(fill=X)
        tb.Label(f_src, text="门店来源", width=12).pack(side=LEFT)
        c_src = _no_wheel(tb.Combobox(f_src, values=[STORE_SRC_BUYER, STORE_SRC_STORE],
                                      width=30, state="readonly"))
        _src_cur = str(cur.get("store_source") or "").strip().lower()
        if _src_cur not in (STORE_SOURCE_BUYER, STORE_SOURCE_STORE):
            _src_cur = (STORE_SOURCE_STORE if (cur.get("store") or cur.get("stores"))
                        else STORE_SOURCE_BUYER)
        c_src.set(STORE_SRC_STORE if _src_cur == STORE_SOURCE_STORE else STORE_SRC_BUYER)
        c_src.pack(side=LEFT, fill=X, expand=True)
        f_st = tb.Frame(win, padding=(16, 2, 16, 0))
        f_st.pack(fill=X)
        tb.Label(f_st, text="", width=12).pack(side=LEFT)
        _preset = _row_stores({"stores": cur.get("stores"), "store": cur.get("store")})
        _preset_items = [x for x in (_store_item_for(self.store_items, c) for c in _preset) if x]
        mp_stores = MultiPick(f_st, list(self.store_items or []), _preset_items,
                              width=30, title="选择要盯的门店（可多选）")
        mp_stores.frame.pack(side=LEFT, fill=X, expand=True)

        def _src_changed(_e=None):
            mp_stores.set_enabled(_store_source_of(c_src.get()) == STORE_SOURCE_STORE)
        c_src.bind("<<ComboboxSelected>>", _src_changed)
        _src_changed()

        f_by = tb.Frame(win, padding=(16, 6, 16, 0))
        f_by.pack(fill=X)
        tb.Label(f_by, text="绑定取货人", width=12).pack(side=LEFT)
        c_by = _no_wheel(tb.Combobox(f_by, values=["（未绑定）"] + self._buyer_names(),
                                     width=30, state="readonly"))
        _cb = (cur.get("buyer") or "").strip()
        c_by.set(_cb if _cb in self._buyer_names() else "（未绑定）")
        c_by.pack(side=LEFT, fill=X, expand=True)
        f_q = tb.Frame(win, padding=(16, 6, 16, 0))
        f_q.pack(fill=X)
        tb.Label(f_q, text="每单 / 共要", width=12).pack(side=LEFT)
        e_po = tb.Entry(f_q, width=6)
        e_po.insert(0, str(_order_cap(cur.get("per_order", 1))))
        e_po.pack(side=LEFT, padx=(0, 6))
        tb.Label(f_q, text="件 /").pack(side=LEFT, padx=(0, 6))
        e_want = tb.Entry(f_q, width=6)
        e_want.insert(0, _want_text(_norm_want(cur.get("want", 1))))
        e_want.pack(side=LEFT, padx=(0, 6))
        tb.Label(f_q, text="台").pack(side=LEFT)
        v_on = tb.BooleanVar(value=bool(cur.get("enabled", True)))
        tb.Checkbutton(win, text="启用这个任务", variable=v_on,
                       bootstyle="success-round-toggle").pack(anchor="w", padx=16, pady=(10, 0))
        tb.Label(win, text="门店来源：【取货人意向地址】=扫取货人城市里的所有店；【门店自定义】=只盯勾选的几家（会话预热在占位店，命中时再切到目标店，这样才拿得到时段格）",
                 bootstyle="secondary", font=("Microsoft YaHei UI", 8),
                 padding=(16, 8, 16, 10)).pack(anchor="w")
        out = {"v": None}

        def ok():
            src = _store_source_of(c_src.get())
            picked = [_store_code(x) for x in mp_stores.get()]
            if src == STORE_SOURCE_STORE and not picked:
                messagebox.showwarning(title, "选了【按门店自定义】就至少勾一家门店；"
                                          "不想固定门店请选【按取货人意向地址】", parent=win)
                return
            if src != STORE_SOURCE_STORE:
                picked = []
            nm = e_nm.get().strip()
            fp = (_state["file"] or "").strip()
            if not nm or not fp:
                messagebox.showwarning(title, "任务名要填；并在【在线账号】里选账号点【获取Cookie】", parent=win)
                return
            if not os.path.exists(self._abs_ident(fp)):
                messagebox.showwarning(title, "身份文件不存在：\n%s" % fp, parent=win)
                return
            out["v"] = {"id": cur.get("id"), "name": nm, "cookie_file": fp,
                        "sku": _sku_of(c_sku.get()).upper(),
                        "store": (picked[0] if len(picked) == 1 else ""),
                        "stores": picked, "store_source": src,
                        "buyer": ("" if c_by.get().startswith("（") else c_by.get().strip()),
                        "per_order": _order_cap(_positive_int(e_po.get(), 1)),
                        "want": _norm_want(e_want.get() or 1),
                        "enabled": bool(v_on.get())}
            win.destroy()
        btns = tb.Frame(win, padding=(16, 0, 16, 14))
        btns.pack(fill=X)
        tb.Button(btns, text="确定", bootstyle="primary", width=10, command=ok).pack(side=RIGHT)
        tb.Button(btns, text="取消", bootstyle="secondary-outline", width=8,
                  command=win.destroy).pack(side=RIGHT, padx=(0, 8))
        _center_window(win, self.root)
        win.grab_set()
        self.root.wait_window(win)
        return out["v"]

    # ---------------- 购物袋（人工查看/加购/删除） ----------------
    def _open_cart(self, r):
        path = self._abs_ident((r.get("file").get() if r.get("file") else "").strip())
        if not path or not os.path.exists(path):
            messagebox.showwarning("购物袋", "先选择这个任务的身份文件（cookie txt）")
            return
        name = (r.get("name").get() if r.get("name") else "").strip() or "任务"
        sku = _sku_of(r.get("sku").get()) if r.get("sku") else ""
        win = tb.Toplevel(self.root)
        win.title("购物袋 - %s" % name)
        win.geometry("850x460")
        win.minsize(680, 340)
        win.transient(self.root)
        top = tb.Frame(win, padding=(14, 12, 14, 6))
        top.pack(fill=X)
        tb.Label(top, text="账号：%s" % name, font=("Microsoft YaHei UI", 11, "bold")).pack(side=LEFT)
        tb.Label(top, text="  任务型号：%s（%s）" % (sku, sku_name(sku)) if sku else "  未选择任务型号",
                 bootstyle="secondary").pack(side=LEFT)
        body = tb.Frame(win, padding=(14, 0, 14, 6))
        body.pack(fill=BOTH, expand=True)
        cols = ("sku", "name", "qty", "lid")
        tree = tb.Treeview(body, columns=cols, show="headings", selectmode="browse", height=10)
        for c, label, width in (("sku", "型号", 105), ("name", "商品", 360),
                                ("qty", "数量", 65), ("lid", "lineItemId", 260)):
            tree.heading(c, text=label)
            tree.column(c, width=width, anchor="w" if c in ("sku", "name", "lid") else "center")
        sb = tb.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side=LEFT, fill=BOTH, expand=True)
        sb.pack(side=LEFT, fill=Y)
        status = tb.StringVar(value="正在读取购物袋…")
        foot = tb.Frame(win, padding=(14, 6, 14, 14))
        foot.pack(fill=X)
        tb.Label(foot, textvariable=status, bootstyle="secondary",
                 font=("Microsoft YaHei UI", 9)).pack(side=LEFT, fill=X, expand=True)
        cart = {"win": win, "name": name, "path": path, "sku": sku, "cli": None,
                "sess": None, "items": [], "busy": False, "tree": tree,
                "status": status, "selected_lid": "", "source": "身份文件"}
        self.cart_windows.append(cart)

        def sel(_e=None):
            s = tree.selection()
            cart["selected_lid"] = s[0] if s else ""
        tree.bind("<<TreeviewSelect>>", sel)

        tb.Button(foot, text="关闭", bootstyle="secondary-outline", width=7,
                  command=lambda: self._close_cart(cart)).pack(side=RIGHT, padx=(6, 0))
        tb.Button(foot, text="＋ 加购一件", bootstyle="success", width=12,
                  command=lambda: self._cart_action(cart, "add")).pack(side=RIGHT, padx=(6, 0))
        tb.Button(foot, text="－ 删除选中", bootstyle="danger", width=12,
                  command=lambda: self._cart_action(cart, "delete")).pack(side=RIGHT, padx=(6, 0))
        tb.Button(foot, text="刷新", bootstyle="info-outline", width=8,
                  command=lambda: self._cart_action(cart, "load")).pack(side=RIGHT, padx=(6, 0))
        tb.Label(win, text="注意：这里的操作立即作用于 Apple 服务端购物袋；运行中的抢单会与它共用同一个袋子。",
                 bootstyle="warning", padding=(14, 0, 14, 10)).pack(fill=X)
        self._cart_action(cart, "load")

    def _close_cart(self, cart):
        try:
            cart["win"].destroy()
        except Exception:
            pass
        try:
            self.cart_windows.remove(cart)
        except Exception:
            pass

    @staticmethod
    def _cart_rows_from_text(txt: str):
        try:
            data = json.loads(txt or "{}")
        except Exception:
            return []
        out = []
        for it in (data.get("lineItems") or []):
            if not isinstance(it, dict) or not it.get("lineItemId"):
                continue
            try:
                q = max(1, int(it.get("quantity") or 1))
            except Exception:
                q = 1
            out.append({"lid": it.get("lineItemId") or "",
                        "sku": it.get("partNumber") or "",
                        "name": it.get("name") or "",
                        "qty": q})
        return out

    @staticmethod
    def _identity_pair(path: str):
        raw = open(path, encoding="utf-8", errors="replace").read().strip()
        if raw.startswith("{"):
            j = json.loads(raw)
            return (j.get("cookie") or ""), (j.get("ua") or "")
        return raw, ""

    def _cart_action(self, cart, action: str):
        if cart.get("busy"):
            return
        if action == "delete" and not cart.get("selected_lid"):
            messagebox.showinfo("购物袋", "先在列表里选中要删除的条目", parent=cart["win"])
            return
        # ★★ 运行中的任务【独占】购物袋：抢单已经按「每单件数」把袋子备好（命中时甚至
        #    已经按当前袋子建好结算会话），这时手动加购/删除会把服务端袋子顶高，
        #    提交就会变成"想要2、实际下4"。要手工维护请先停止任务。
        if action in ("add", "delete"):
            _nm = cart.get("name") or ""
            _running = any(
                getattr(w, "is_alive", lambda: False)()
                and getattr(getattr(w, "cc", None), "name", "") == _nm
                for w in (self.workers or []))
            if _running:
                messagebox.showwarning(
                    "购物袋",
                    "任务「%s」正在运行中，抢单期间不允许手动加购/删除。\n"
                    "否则会把袋子件数顶高，导致按超额件数下单。\n"
                    "请先停止任务，再手动维护购物袋。" % (_nm or "未命名"),
                    parent=cart["win"])
                return
        cart["busy"] = True
        cart["status"].set({"load": "正在刷新…", "add": "正在加购…",
                            "delete": "正在删除…"}.get(action, "处理中…"))
        threading.Thread(target=self._cart_worker, args=(cart, action), daemon=True).start()

    def _live_client_snapshot(self, name: str):
        """从正在运行的 worker 复制实时 cookie / live headers / x-aos-stk。"""
        for w in (self.workers or []):
            try:
                if getattr(getattr(w, "cc", None), "name", "") == name:
                    return w.cli.dump()
            except Exception:
                continue
        return None

    def _cart_worker(self, cart, action: str):
        try:
            if cart.get("cli") is None:
                ck, ua = self._identity_pair(cart["path"])
                cart["cli"] = Client(ck, ua, proxy_provider=self._proxy_provider())
                snap = self._live_client_snapshot(cart.get("name") or "")
                if snap:
                    cart["cli"].load(snap)
                    cart["source"] = "worker实时"
                else:
                    cart["source"] = "身份文件"
            cli = cart["cli"]
            if cart.get("sess") is None:
                cart["sess"] = CheckoutSession(cli, cart.get("sku") or "")
            sess = cart["sess"]
            if action == "add":
                sku = (cart.get("sku") or "").strip()
                if not sku:
                    raise RuntimeError("这个任务还没选抢购型号")
                r = cli.post(MERCH + "/cart/add", urllib.parse.urlencode({"p": sku, "code": ""}), tries=2)
                t = (r.text or "") if r is not None else ""
                if r is None or r.status_code != 200:
                    raise RuntimeError("加购失败（HTTP %s）" % (getattr(r, "status_code", 0) or 0))
                if "userbind" in t or "IDMSWebAuth/signin" in t:
                    raise RuntimeError("这个身份需要先登录/绑定 Apple ID")
            elif action == "delete":
                if not cli.bag_delete(cart.get("selected_lid") or ""):
                    raise RuntimeError("删除失败；请刷新后重试")
            items, txt, ok = sess.bag()
            if not ok:
                raise RuntimeError("读取购物袋失败（HTTP %s）" % cli.last_status)
            rows = self._cart_rows_from_text(txt)
            self.q.put(("cart", (cart, "ok", {"rows": rows, "bytes": len(txt or "")})))
        except Exception as e:
            self.q.put(("cart", (cart, "err", str(e))))

    def _cart_result(self, cart, state: str, payload):
        try:
            if not cart["win"].winfo_exists():
                return
        except Exception:
            return
        cart["busy"] = False
        if state == "err":
            cart["status"].set("失败：%s" % payload)
            return
        rows = payload.get("rows") or []
        cart["items"] = rows
        tree = cart["tree"]
        for x in tree.get_children():
            tree.delete(x)
        for it in rows:
            tree.insert("", "end", iid=it["lid"],
                        values=(it["sku"], it["name"], it["qty"], it["lid"]))
        n = sum(int(x.get("qty") or 1) for x in rows)
        cart["status"].set("来源=%s ｜ 共 %d 个条目 / %d 件 ｜ 响应 %d 字节"
                           % (cart.get("source") or "-", len(rows), n, payload.get("bytes") or 0))

    def _abs_ident(self, p: str) -> str:
        if not p:
            return ""
        return p if os.path.isabs(p) else os.path.join(HERE, p)

    def _pick_file(self, entry):
        p = filedialog.askopenfilename(title="选择身份文件（cookie txt）", initialdir=HERE,
                                       filetypes=[("文本", "*.txt"), ("全部", "*.*")])
        if p:
            entry.delete(0, END)
            entry.insert(0, p)

    # ---------------- 取货人 ----------------
    # ---------------- 取货人（表格视图） ----------------
    BUYER_COLS = (("on", "启用", 48), ("name", "姓名", 86), ("phone", "手机号", 104),
                  ("last4", "后四位", 64), ("email", "邮箱", 150),
                  ("skus", "要抢的型号", 180), ("cities", "城市", 140),
                  ("want", "想要", 50), ("quota", "额度 / 进度", 250))

    def _tab_buyers(self):
        page = tb.Frame(self.nb, padding=(14, 12, 14, 6))
        self.nb.add(page, text="  ② 取货人  ")
        tb.Label(page, bootstyle="secondary",
                 text="每个任务只绑定一个取货人；多个账号可共享同一人（程序合并限制他的总额度）。勾选多行用批量按钮，双击编辑。",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 8))
        bar = tb.Frame(page)
        bar.pack(fill=X, pady=(0, 6))

        def _b(txt, cmd, style="secondary-outline", w=9):
            tb.Button(bar, text=txt, bootstyle=style, width=w,
                      command=cmd).pack(side=LEFT, padx=(0, 5))

        _b("＋ 新增", lambda: self._buyer_add(), "primary", 8)
        _b("编辑", lambda: self._buyer_edit(), "info-outline", 7)
        _b("删除", lambda: self._buyer_batch_delete(), "danger-outline", 7)
        _b("全选", lambda: self._tv_sel_all(self.tv_buyers))
        _b("反选", lambda: self._tv_invert(self.tv_buyers))
        tb.Separator(bar, orient="vertical").pack(side=LEFT, fill=Y, padx=6)
        _b("批量启用", lambda: self._buyer_batch_on(True), "success-outline", 9)
        _b("批量停用", lambda: self._buyer_batch_on(False), "secondary-outline", 9)
        _b("批量城市", lambda: self._buyer_batch_cities(), "warning-outline", 9)
        _b("批量型号", lambda: self._buyer_batch_skus(), "warning-outline", 9)
        _b("批量想要", lambda: self._buyer_batch_want(), "warning-outline", 9)

        foot = tb.Frame(page)
        foot.pack(side=BOTTOM, fill=X, pady=(6, 2))
        self.v_buyer_n = tb.StringVar(value="")
        tb.Label(foot, textvariable=self.v_buyer_n, bootstyle="secondary",
                 font=("Microsoft YaHei UI", 9)).pack(side=LEFT)
        tb.Label(foot, text="提示：「要抢的型号」留空 = 不限制（任何任务都能绑这个人）；填了就只认列表里的型号。",
                 bootstyle="secondary", font=("Microsoft YaHei UI", 8)).pack(side=RIGHT)

        body = tb.Frame(page)
        body.pack(fill=BOTH, expand=True)
        self.tv_buyers = tb.Treeview(body, columns=[c[0] for c in self.BUYER_COLS],
                                     show="headings", selectmode="extended", height=14,
                                     bootstyle="info")
        for k, t, w in self.BUYER_COLS:
            self.tv_buyers.heading(k, text=t, anchor="center")
            # ★ 数据全部居中（用户要求）
            self.tv_buyers.column(k, width=w, stretch=(k in ("quota", "email", "skus")),
                                  anchor="center")
        sb = tb.Scrollbar(body, orient="vertical", command=self.tv_buyers.yview)
        self.tv_buyers.configure(yscrollcommand=sb.set)
        self.tv_buyers.pack(side=LEFT, fill=BOTH, expand=True)
        sb.pack(side=LEFT, fill=Y)
        self.tv_buyers.bind("<Double-1>", lambda _e: self._buyer_edit())
        self.tv_buyers.bind("<<TreeviewSelect>>", lambda _e: self._buyer_sel_changed())

    # ---- 行模型 ----
    def _buyer_row_from(self, cur=None) -> dict:
        cur = cur or {}
        nm = (cur.get("name") or "").strip()
        _ln = cur.get("last_name") or ""
        _fn = cur.get("first_name") or ""
        if nm and _ln and _fn and _ln + _fn != nm and _fn + _ln == nm:
            _ln, _fn = _fn, _ln
        skus = [it for it in SKU_ITEMS if _sku_of(it) in (cur.get("skus") or [])]
        return {
            "_id": str(cur.get("id") or ("buyer-" + uuid.uuid4().hex)),
            "_name": nm, "_ln": _ln, "_fn": _fn,
            "name": _Cell(nm),
            "phone": _Cell(str(cur.get("phone") or "")),
            "email": _Cell(str(cur.get("email") or "")),
            "last4": _Cell(str(cur.get("last4") or "")),
            "sku": _Cell(skus),                      # collect() 要的是"SKU_ITEMS 里的项"列表
            "city": _Cell(list(cur.get("cities") or [])),
            "want": _Cell(_want_text(_norm_want(cur.get("want", 1)))),
            "on": _Cell(bool(cur.get("enabled", True))),
            "quota": _Cell(""),
        }

    def _buyer_values(self, r) -> tuple:
        sk = [_sku_of(x) for x in (r["sku"].get() or [])]
        return ("✓" if r["on"].get() else "—", r["name"].get() or "(未命名)",
                r["phone"].get(), r["last4"].get(), r["email"].get(),
                ("不限型号" if not sk else "、".join(sk[:3]) + (" …" if len(sk) > 3 else "")),
                ("、".join(r["city"].get() or []) or "(未选城市)"),
                r["want"].get(), r["quota"].get())

    def _tv_buyers_render(self):
        try:
            tv = self.tv_buyers
        except Exception:
            return
        keep = set()
        for r in self.buyer_rows:
            keep.add(r["_id"])
            if tv.exists(r["_id"]):
                tv.item(r["_id"], values=self._buyer_values(r))
            else:
                tv.insert("", "end", iid=r["_id"], values=self._buyer_values(r))
        for iid in tv.get_children():
            if iid not in keep:
                tv.delete(iid)
        self._buyer_sel_changed()

    def _tv_buyers_cell(self, row_id: str, col: str = ""):
        try:
            r = next((x for x in self.buyer_rows if x["_id"] == row_id), None)
            if r is not None and self.tv_buyers.exists(row_id):
                self.tv_buyers.item(row_id, values=self._buyer_values(r))
        except Exception:
            pass

    def _selected_buyer_rows(self) -> list:
        try:
            sel = list(self.tv_buyers.selection())
        except Exception:
            return []
        return [r for r in self.buyer_rows if r["_id"] in sel]

    def _buyer_sel_changed(self):
        n = len(self._selected_buyer_rows())
        try:
            self.v_buyer_n.set("共 %d 个取货人 ｜ 已选 %d 个" % (len(self.buyer_rows), n))
        except Exception:
            pass

    # ---- 增 / 改 / 删 ----
    def _buyer_add(self):
        cur = self._buyer_dialog({}, title="新增取货人")
        if cur is None:
            return
        self.buyer_rows.append(self._buyer_row_from(cur))
        self._tv_buyers_render()
        self._refresh_task_buyer_choices()
        self.save()

    def _buyer_edit(self):
        rows = self._selected_buyer_rows()
        if len(rows) != 1:
            messagebox.showinfo("编辑取货人", "请先选中【一行】再点编辑（多选请用批量按钮）")
            return
        r = rows[0]
        cur = {"id": r["_id"], "name": r["name"].get(), "phone": r["phone"].get(),
               "email": r["email"].get(), "last4": r["last4"].get(),
               "last_name": r.get("_ln"), "first_name": r.get("_fn"),
               "skus": [_sku_of(x) for x in (r["sku"].get() or [])],
               "cities": list(r["city"].get() or []), "want": r["want"].get(),
               "enabled": bool(r["on"].get())}
        new = self._buyer_dialog(cur, title="编辑取货人")
        if new is None:
            return
        nr = self._buyer_row_from(new)
        nr["quota"] = r["quota"]
        idx = self.buyer_rows.index(r)
        self.buyer_rows[idx] = nr
        self._tv_buyers_render()
        self._refresh_task_buyer_choices()
        self.save()

    def _buyer_batch_delete(self):
        rows = self._selected_buyer_rows()
        if not rows:
            messagebox.showinfo("批量删除", "请先勾选取货人（可多选）")
            return
        names = [r["name"].get() or "(未命名)" for r in rows]
        bound = [c.get("name") for c in (self.cfg.raw.get("cookies") or [])
                 if (c.get("buyer") or "") in names]
        msg = "确认删除选中的 %d 个取货人？" % len(rows)
        if bound:
            msg += "\n\n⚠ 有 %d 个任务还绑着他们，删除后这些任务会失去取货人：\n%s" % (
                len(bound), "、".join(bound[:6]))
        if not messagebox.askyesno("批量删除", msg):
            return
        for r in rows:
            self.buyer_rows.remove(r)
            self.status_vars.pop(r.get("_name"), None)
        self._tv_buyers_render()
        self._refresh_task_buyer_choices()
        self.save()

    # ---- 批量操作 ----
    def _buyer_batch_on(self, on: bool):
        rows = self._selected_buyer_rows()
        if not rows:
            messagebox.showinfo("批量启用/停用", "请先勾选取货人（可多选）")
            return
        for r in rows:
            r["on"].set(bool(on))
        self._tv_buyers_render()
        self.save()

    def _buyer_batch_cities(self):
        rows = self._selected_buyer_rows()
        if not rows:
            messagebox.showinfo("批量城市", "请先勾选取货人（可多选）")
            return
        val = self._pick_multi_dialog("批量城市",
                                      "把这 %d 个取货人的城市统一设成（可多选）：" % len(rows),
                                      self.city_list,
                                      selected=(rows[0]["city"].get() or []))
        if val is None:
            return
        for r in rows:
            r["city"].set(list(val))
        self._tv_buyers_render()
        self.save()

    def _buyer_batch_skus(self):
        rows = self._selected_buyer_rows()
        if not rows:
            messagebox.showinfo("批量型号", "请先勾选取货人（可多选）")
            return
        val = self._pick_multi_dialog("批量要抢的型号",
                                      "把这 %d 个取货人的「要抢的型号」统一设成（可多选，全不勾 = 不限）：" % len(rows),
                                      SKU_ITEMS,
                                      selected=(rows[0]["sku"].get() or []))
        if val is None:
            return
        for r in rows:
            r["sku"].set(list(val))
        self._tv_buyers_render()
        self.save()

    def _buyer_batch_want(self):
        rows = self._selected_buyer_rows()
        if not rows:
            messagebox.showinfo("批量想要", "请先勾选取货人（可多选）")
            return
        val = self._number_dialog("批量想要", "把这 %d 个取货人的「想要台数」统一设成：" % len(rows),
                                 default="1", hint="想要 = 这个人合计最多买几台（多个号共用同一人时是总闸）")
        if val is None:
            return
        for r in rows:
            r["want"].set(_want_text(_norm_want(val)))
        self._tv_buyers_render()
        self.save()

    # ---- 通用小对话框 ----
    def _number_dialog(self, title: str, prompt: str, default: str = "1", hint: str = ""):
        win = tb.Toplevel(self.root)
        win.title(title); win.transient(self.root); win.resizable(False, False)
        tb.Label(win, text=prompt, padding=(16, 14, 16, 6),
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        e = tb.Entry(win, width=12); e.insert(0, default); e.pack(padx=16)
        if hint:
            tb.Label(win, text=hint, bootstyle="secondary", font=("Microsoft YaHei UI", 8),
                     padding=(16, 8, 16, 10)).pack(anchor="w")
        out = {"v": None}

        def ok():
            out["v"] = e.get().strip(); win.destroy()
        btns = tb.Frame(win, padding=(16, 0, 16, 14)); btns.pack(fill=X)
        tb.Button(btns, text="确定", bootstyle="primary", width=10, command=ok).pack(side=RIGHT)
        tb.Button(btns, text="取消", bootstyle="secondary-outline", width=8,
                  command=win.destroy).pack(side=RIGHT, padx=(0, 8))
        _center_window(win, self.root)
        win.grab_set(); self.root.wait_window(win)
        return out["v"]

    def _pick_multi_dialog(self, title: str, prompt: str, items: list, selected=None):
        """多选对话框（全选/清空）；返回选中的 list 或 None。"""
        win = tb.Toplevel(self.root)
        win.title(title); win.transient(self.root); win.resizable(False, False)
        tb.Label(win, text=prompt, padding=(16, 14, 16, 6),
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tb.Label(win, text="按住 Ctrl 可多选；右侧有全选/清空", bootstyle="secondary",
                 font=("Microsoft YaHei UI", 8), padding=(16, 0, 16, 6)).pack(anchor="w")
        body = tb.Frame(win, padding=(16, 0, 16, 6)); body.pack(fill=BOTH, expand=True)
        lb = tk.Listbox(body, selectmode="multiple", exportselection=False,
                        height=min(14, max(6, len(items))), width=40,
                        font=("Microsoft YaHei UI", 10), activestyle="none")
        for it in items:
            lb.insert(END, it)
        cur = set(selected or [])
        for i, it in enumerate(items):
            if it in cur:
                lb.selection_set(i)
        sb = tb.Scrollbar(body, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side=LEFT, fill=BOTH, expand=True); sb.pack(side=LEFT, fill=Y)
        side = tb.Frame(body); side.pack(side=LEFT, fill=Y, padx=(8, 0))
        tb.Button(side, text="全选", bootstyle="secondary-outline", width=6,
                  command=lambda: lb.selection_set(0, END)).pack(pady=2)
        tb.Button(side, text="清空", bootstyle="secondary-outline", width=6,
                  command=lambda: lb.selection_clear(0, END)).pack(pady=2)
        out = {"v": None}

        def ok():
            out["v"] = [items[i] for i in lb.curselection()]; win.destroy()
        btns = tb.Frame(win, padding=(16, 0, 16, 14)); btns.pack(fill=X)
        tb.Button(btns, text="确定", bootstyle="primary", width=10, command=ok).pack(side=RIGHT)
        tb.Button(btns, text="取消", bootstyle="secondary-outline", width=8,
                  command=win.destroy).pack(side=RIGHT, padx=(0, 8))
        _center_window(win, self.root)
        win.grab_set(); self.root.wait_window(win)
        return out["v"]

    def _buyer_dialog(self, cur: dict, title: str = "取货人"):
        win = tb.Toplevel(self.root)
        win.title(title); win.transient(self.root); win.resizable(False, False)
        cur = cur or {}

        def row(label, val="", width=28):
            f = tb.Frame(win, padding=(16, 6, 16, 0)); f.pack(fill=X)
            tb.Label(f, text=label, width=12).pack(side=LEFT)
            e = tb.Entry(f, width=width); e.insert(0, str(val)); e.pack(side=LEFT, fill=X, expand=True)
            return e

        e_nm = row("姓名", cur.get("name") or "")
        e_ph = row("手机号", cur.get("phone") or "")
        e_l4 = row("证件后四位", cur.get("last4") or "")
        e_em = row("邮箱", cur.get("email") or "", 34)
        e_want = row("想要台数", _want_text(_norm_want(cur.get("want", 1))), 8)

        def mbox(label, items, selected):
            f = tb.Frame(win, padding=(16, 8, 16, 0)); f.pack(fill=BOTH, expand=True)
            tb.Label(f, text=label, width=12).pack(side=LEFT, anchor="n")
            lb = tk.Listbox(f, selectmode="multiple", exportselection=False,
                            height=min(10, max(4, len(items))), width=40,
                            font=("Microsoft YaHei UI", 10), activestyle="none")
            for it in items:
                lb.insert(END, it)
            _sel = set(selected or [])
            for i, it in enumerate(items):
                if it in _sel:
                    lb.selection_set(i)
            sb = tb.Scrollbar(f, orient="vertical", command=lb.yview)
            lb.configure(yscrollcommand=sb.set)
            lb.pack(side=LEFT, fill=BOTH, expand=True); sb.pack(side=LEFT, fill=Y)
            return lb, items

        lb_sku, items_sku = mbox("要抢的型号", SKU_ITEMS,
                                 [it for it in SKU_ITEMS if _sku_of(it) in (cur.get("skus") or [])])
        lb_city, items_city = mbox("城市", self.city_list, cur.get("cities") or [])
        tb.Label(win, text="要抢的型号全不勾 = 不限型号；城市决定扫哪些门店（固定门店模式下也照它校验）",
                 bootstyle="secondary", font=("Microsoft YaHei UI", 8),
                 padding=(16, 8, 16, 0)).pack(anchor="w")
        v_on = tb.BooleanVar(value=bool(cur.get("enabled", True)))
        tb.Checkbutton(win, text="启用", variable=v_on,
                       bootstyle="success-round-toggle").pack(anchor="w", padx=16, pady=(8, 0))
        out = {"v": None}

        def ok():
            nm = e_nm.get().strip()
            if not nm:
                messagebox.showwarning(title, "姓名必填", parent=win)
                return
            out["v"] = {"id": cur.get("id"), "name": nm,
                        "phone": e_ph.get().strip(), "email": e_em.get().strip(),
                        "last4": e_l4.get().strip(),
                        "last_name": cur.get("last_name") or "", "first_name": cur.get("first_name") or "",
                        "skus": [_sku_of(items_sku[i]) for i in lb_sku.curselection()],
                        "cities": [items_city[i] for i in lb_city.curselection()],
                        "want": _norm_want(e_want.get() or 1), "enabled": bool(v_on.get())}
            win.destroy()
        btns = tb.Frame(win, padding=(16, 0, 16, 14)); btns.pack(fill=X)
        tb.Button(btns, text="确定", bootstyle="primary", width=10, command=ok).pack(side=RIGHT)
        tb.Button(btns, text="取消", bootstyle="secondary-outline", width=8,
                  command=win.destroy).pack(side=RIGHT, padx=(0, 8))
        _center_window(win, self.root)
        win.grab_set(); self.root.wait_window(win)
        return out["v"]

    # ---------------- 设置 ----------------
    def _toggle_rebuild_row(self):
        """会话重建只属于捡漏模式，首发模式不显示该策略。"""
        try:
            if self.v_warmup_mode.get() == WARMUP_SNIPE:
                self._rebuild_row.grid()
            else:
                self._rebuild_row.grid_remove()
        except Exception:
            pass

    def _buyer_names(self):
        """所有可绑定的取货人姓名：以 cfg.buyers 为基底（含保存/加载的全部），再拼上界面上尚未保存的新名。"""
        names = [b.name for b in self.cfg.buyers]
        for r in getattr(self, "buyer_rows", []) or []:
            nm = r["name"].get().strip() if r.get("name") else (r.get("_name") or "")
            if nm and nm not in names:
                names.append(nm)
        return names

    def _refresh_task_buyer_choices(self):
        """取货人增删/改名后，刷新每个任务卡片上的「绑定取货人」下拉。
        ★ 只更新选项，从不改已选值；否则逐张建取货人卡时（buyer_rows 还没建全）会把后面任务的绑定误清成「未绑定」。"""
        # ★ 表格视图：任务行里的取货人是文本，取货人增删/改名后重画一次即可
        self._tv_tasks_render()

    def _tab_settings(self):
        page = tb.Frame(self.nb, padding=(14, 12, 14, 6))
        self.nb.add(page, text="  ③ 设置  ")
        self.f_set = _scroll_area(page, self.bg)
        self.sv = {}
        g0 = tb.Labelframe(self.f_set, text=" 启动模式与结算策略 ", padding=(14, 10, 14, 14),
                           bootstyle="success")
        g0.pack(fill=X, pady=(0, 10))
        self.v_warmup_mode = tb.StringVar(value=WARMUP_SNIPE)
        for col, mode in enumerate((WARMUP_FIRST, WARMUP_SNIPE)):
            tb.Radiobutton(g0, text=WARMUP_LABELS[mode], value=mode,
                           variable=self.v_warmup_mode,
                           bootstyle="success-toolbutton").grid(row=0, column=col, sticky="w", padx=(0, 10))
        tb.Label(g0, text="时段：有可用项就先保存；没有也会继续完整提交，以 Apple 最终响应为准。",
                 bootstyle="secondary").grid(row=1, column=0, sticky="w", pady=(10, 0))
        row_slot = tb.Frame(g0)
        row_slot.grid(row=1, column=1, columnspan=2, sticky="w", pady=(10, 0), padx=(16, 0))
        tb.Label(row_slot, text="选择第几个可用时段（从 1 开始）").pack(side=LEFT)
        self.sv["time_slot_index"] = tb.Entry(row_slot, width=7)
        self.sv["time_slot_index"].insert(0, "1")
        self.sv["time_slot_index"].pack(side=LEFT, padx=(8, 6))
        tb.Label(row_slot, text="超出数量时自动取最后一个", bootstyle="secondary").pack(side=LEFT)
        row_gap = tb.Frame(g0)
        row_gap.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        tb.Label(row_gap, text="启动时统一预热间隔（秒）").pack(side=LEFT)
        self.sv["prewarm_interval_sec"] = tb.Entry(row_gap, width=7)
        self.sv["prewarm_interval_sec"].insert(0, "0")
        self.sv["prewarm_interval_sec"].pack(side=LEFT, padx=(8, 6))
        tb.Label(row_gap, text="例如 5 = 每 5 秒启动一个账号预热", bootstyle="secondary").pack(side=LEFT)
        self._rebuild_row = tb.Frame(g0)
        self._rebuild_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        tb.Label(self._rebuild_row, text="捡漏会话重建（秒）").pack(side=LEFT)
        self.sv["session_rebuild_sec"] = tb.Entry(self._rebuild_row, width=10)
        self.sv["session_rebuild_sec"].insert(0, "600")
        self.sv["session_rebuild_sec"].pack(side=LEFT, padx=(12, 8))
        tb.Label(self._rebuild_row, text="仅捡漏模式维护预热会话").pack(side=LEFT)
        # ★ 是否在预热阶段就把取货人写进会话
        self.sv["prewarm_contact"] = tb.BooleanVar(value=True)
        tb.Checkbutton(g0, text="预热取货人（关掉则命中时再现场填写绑定取货人）",
                       variable=self.sv["prewarm_contact"],
                       bootstyle="success-round-toggle").grid(
                           row=4, column=1, sticky="w", pady=(8, 0), padx=(16, 0))
        tb.Label(g0, text="首发模式命中后创建目标门店会话；捡漏模式先占位预热付款与发票，再复用会话换店。",
                 bootstyle="secondary", justify="left").grid(row=5, column=0, columnspan=3,
                                                               sticky="w", pady=(10, 0))
        tb.Label(g0, text="每个号用它自己绑定的取货人预热；开=预热就写好联系人（命中最快），关=命中时再填（多 1~2 发）。",
                 bootstyle="secondary", justify="left").grid(row=6, column=0, columnspan=3,
                                                              sticky="w", pady=(2, 0))
        self.v_warmup_mode.trace_add("write", lambda *_: self._toggle_rebuild_row())

        g1 = tb.Labelframe(self.f_set, text=" 扫描 / 抢单 ", padding=(14, 10, 14, 14), bootstyle="info")
        g1.pack(fill=X, pady=(0, 10))
        for i, (k, lab, hint) in enumerate([("scan_concurrency", "扫描并发", "12 够用；再大会被限流"),
                                            ("scan_interval_sec", "每轮间隔（秒）", "0.4 左右"),
                                            ("max_parallel_buy", "同时抢单上限", "达到上限后其它账号继续等下一轮；建议 2"),
                                            ("alarm_sec", "成单响铃（秒）", "能吵醒你的时长"),
                                            ("retry_no_slot_sec", "同店重试间隔（秒）",
                                             "服务端最终明确拒绝后多久再进结算；★ 期间探测照常"),
                                            ("settle_gap_sec", "结算最小间隔（秒）",
                                             "两次进结算之间至少隔这么久，防止多店同时命中把结算族打暴"),
                                            ("store_cool_sec", "门店失败冷却（秒）",
                                             "进结算失败/没时段/门店被抚后，这家店隔这么长才再进；每轮间隔固定一样，探测不受影响")]):
            tb.Label(g1, text=lab, width=20).grid(row=i, column=0, sticky="w", pady=5)
            e = tb.Entry(g1, width=10)
            e.grid(row=i, column=1, sticky="w")
            self.sv[k] = e
            tb.Label(g1, text=hint, bootstyle="secondary").grid(row=i, column=2, sticky="w", padx=12)
        # ★★ 需要时段开关
        self.sv["require_time_slot"] = tb.BooleanVar(value=False)
        tb.Checkbutton(g1, text="需要时段（没有可填的时段 = 没货，不进结算）",
                       variable=self.sv["require_time_slot"],
                       bootstyle="warning-round-toggle").grid(
                           row=7, column=1, sticky="w", pady=(10, 0))
        tb.Label(g1, text="关=没时段也补完其它根照常提交（服务端说算）；开=发现没填时段的地方就当没货，跳过继续扫",
                  bootstyle="secondary").grid(row=8, column=2, sticky="w", padx=12, pady=(6, 0))
        g2 = tb.Labelframe(self.f_set, text=" 运行与诊断 ", padding=(14, 10, 14, 14), bootstyle="info")
        g2.pack(fill=X, pady=(0, 10))
        self.sv["dbg_on"] = tb.BooleanVar(value=False)
        tb.Checkbutton(g2, text="🐞 记录结算调试日志", variable=self.sv["dbg_on"],
                       bootstyle="warning-round-toggle").grid(row=9, column=1, sticky="w", pady=(10, 0))
        tb.Label(g2, text="★ 开启后：每次抢单的【每个请求 + 返回原文】按顺序存到 logs\\结算调试\\，"
                          "用来分析为什么弹回次数不稳定", bootstyle="secondary").grid(row=9, column=2, sticky="w", padx=12)
        tb.Button(g2, text="打开调试目录", bootstyle="secondary-outline",
                  command=lambda: os.startfile(os.path.join(HERE, "logs", "结算调试"))
                  if os.path.isdir(os.path.join(HERE, "logs", "结算调试")) else os.startfile(os.path.join(HERE, "logs"))
                  ).grid(row=10, column=1, sticky="w", pady=(6, 0))
        tb.Button(g2, text="清空抢购账本", bootstyle="secondary-outline",
                  command=self._clear_ledger).grid(row=8, column=1, sticky="w", pady=(6, 0))
        tb.Label(g2, text="按服务端 spokes 动态决定填写项；请求、响应、耗时和当前状态可写入调试目录。",
                 bootstyle="secondary", justify="left").grid(row=11, column=0, columnspan=3,
                                                               sticky="w", pady=(14, 8))
        tb.Label(g2, text="取货人额度是唯一的成单上限；账本落盘后自动从监控池移除。",
                 bootstyle="secondary").grid(row=8, column=2, sticky="w", padx=12)
        g3 = tb.Labelframe(self.f_set, text=" 邮件通知（成单时发到邮箱） ", padding=(14, 10, 14, 14),
                           bootstyle="info")
        g3.pack(fill=X)
        self.sv["mail_on"] = tb.BooleanVar(value=True)
        tb.Checkbutton(g3, text="开启邮件", variable=self.sv["mail_on"],
                       bootstyle="success-round-toggle").grid(row=0, column=1, sticky="w", pady=5)
        for i, (k, lab) in enumerate([("mail_host", "SMTP 服务器"), ("mail_port", "端口"),
                                      ("mail_user", "发件邮箱"), ("mail_pass", "授权码"),
                                      ("mail_to", "收件（逗号分隔）")], 1):
            tb.Label(g3, text=lab, width=20).grid(row=i, column=0, sticky="w", pady=5)
            e = tb.Entry(g3, width=48, show="*" if k == "mail_pass" else "")
            e.grid(row=i, column=1, columnspan=2, sticky="w")
            self.sv[k] = e
        tb.Button(g3, text="发送测试邮件", bootstyle="secondary-outline",
                  command=self._test_mail).grid(row=6, column=1, sticky="w", pady=(10, 0))
        tb.Button(g3, text="补发没发出去的邮件", bootstyle="warning-outline", width=18,
                  command=self._resend_mail).grid(row=7, column=1, sticky="w", pady=(6, 0))
        tb.Label(g3, text="改完记得点右上角「保存配置」", bootstyle="secondary",
                 font=("Microsoft YaHei UI", 8)).grid(row=6, column=2, sticky="w", padx=10)

        # ---------------- 🌐 代理（除扫描外全走） ----------------
        g5 = tb.Labelframe(self.f_set, text=" 代理（除扫描外全走） ", padding=(14, 10, 14, 14),
                           bootstyle="warning")
        g5.pack(fill=X, pady=(10, 0))
        self.sv["proxy_on"] = tb.BooleanVar(value=False)
        tb.Checkbutton(g5, text="开启代理（手机扫描仍然直连）", variable=self.sv["proxy_on"],
                       bootstyle="warning-round-toggle").grid(row=0, column=0, sticky="w", pady=4)
        tb.Label(g5, text="取 IP 链接").grid(row=1, column=0, sticky="w", pady=5)
        e_px = tb.Entry(g5, width=70)
        e_px.grid(row=1, column=1, columnspan=3, sticky="w")
        self.sv["proxy_api_url"] = e_px
        tb.Label(g5, text="换 IP 间隔（秒）").grid(row=2, column=0, sticky="w", pady=5)
        e_px2 = tb.Entry(g5, width=8)
        e_px2.grid(row=2, column=1, sticky="w")
        self.sv["proxy_rotate_sec"] = e_px2
        tb.Button(g5, text="测试取一个 IP", bootstyle="warning-outline", width=16,
                  command=self._test_proxy).grid(row=2, column=2, sticky="w", padx=10)
        tb.Label(g5, text="代理模式").grid(row=3, column=0, sticky="w", pady=5)
        self.sv["proxy_mode"] = tb.StringVar(value="一号一池（推荐）")
        _cbpm = _no_wheel(tb.Combobox(g5, textvariable=self.sv["proxy_mode"], width=18, state="readonly"))
        _cbpm["values"] = [lab for _v, lab in PROXY_MODES]
        _cbpm.grid(row=3, column=1, sticky="w")
        tb.Label(g5, text="★ 交给【手机扫描以外】的所有请求：加袋/清袋/建会话/取时段/取货人/付款/提交。"
                          "一号一池=每个账号各用一个独立 IP（一个号被风控不影响其它号），"
                          "程序不改写提取数，每池只消费返回的第一个 IP，快过期或遇 403/541 时异步换新；"
                          "全局共用=所有号共用一个 IP（旧行为）。取不到 IP 会自动退回直连，不卡主流程。",
                 bootstyle="secondary", justify="left", wraplength=820).grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---------------- 🆔 在线账号 / 协议服务 ----------------
        g7 = tb.Labelframe(self.f_set, text=" 在线账号 / 协议服务 ", padding=(14, 10, 14, 14),
                           bootstyle="primary")
        g7.pack(fill=X, pady=(10, 0))
        tb.Label(g7, text="协议服务地址").grid(row=0, column=0, sticky="w", pady=5)
        e_svc = tb.Entry(g7, width=44)
        e_svc.grid(row=0, column=1, sticky="w")
        self.sv["account_service_url"] = e_svc
        tb.Button(g7, text="测试连接", bootstyle="primary-outline", width=10,
                  command=self._test_account_service).grid(row=0, column=2, sticky="w", padx=10)
        tb.Label(g7, text="新增/编辑任务时「在线账号」下拉、以及「获取Cookie」自动登录都从这里的服务读取；"
                          "留空=默认 http://127.0.0.1:5050。改完记得点右上角「保存配置」。",
                 bootstyle="secondary", justify="left", wraplength=820).grid(
                     row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---------------- 🧾 订单确认（提交后查 status） ----------------
        g6 = tb.Labelframe(self.f_set, text=" 订单确认（提交后查 status 拿订单号/失败原因） ",
                           padding=(14, 10, 14, 14), bootstyle="success")
        g6.pack(fill=X, pady=(10, 0))
        tb.Label(g6, text="查状态间隔（秒）").grid(row=0, column=0, sticky="w", pady=5)
        e_s1 = tb.Entry(g6, width=8); e_s1.grid(row=0, column=1, sticky="w")
        self.sv["status_poll_sec"] = e_s1
        tb.Button(g6, text="按 3 秒", bootstyle="success-outline", width=8,
                  command=lambda: (self.sv["status_poll_sec"].delete(0, END),
                                   self.sv["status_poll_sec"].insert(0, "3"))
                  ).grid(row=0, column=2, sticky="w", padx=8)
        tb.Label(g6, text="最多查几次").grid(row=1, column=0, sticky="w", pady=5)
        e_s2 = tb.Entry(g6, width=8); e_s2.grid(row=1, column=1, sticky="w")
        self.sv["status_poll_max"] = e_s2
        tb.Button(g6, text="按 20 次", bootstyle="success-outline", width=8,
                  command=lambda: (self.sv["status_poll_max"].delete(0, END),
                                   self.sv["status_poll_max"].insert(0, "20"))
                  ).grid(row=1, column=2, sticky="w", padx=8)
        tb.Label(g6, text="提交后固定按上面的间隔和次数查询；查到感谢页/订单号才判定成单。",
                 bootstyle="secondary", justify="left").grid(row=2, column=0, columnspan=3,
                                                                sticky="w", pady=(6, 0))

    def _show_hits(self):
        """★ 有货记录：每一次扫到有货都在这，点一行看它的完整执行流程"""
        import glob as _g
        d = os.path.join(HERE, "logs")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        win = tk.Toplevel(self.root)
        win.title("有货记录 / 执行流程")
        win.geometry("1180x700")
        win.transient(self.root)

        top = tb.Frame(win, padding=(12, 10, 12, 6))
        top.pack(fill=X)
        cols = (("ts", "时间", 145), ("acc", "号", 85), ("sku_name", "型号", 125), ("store_name", "门店", 150),
                ("event", "类型", 70), ("result", "结果", 260), ("sec", "耗时", 60), ("bounces", "弹回", 55))
        v_cnt = tb.StringVar(value="")
        tv = tb.Treeview(win, columns=[c[0] for c in cols], show="headings", height=16, bootstyle="info")
        for k, t, w in cols:
            tv.heading(k, text=t); tv.column(k, width=w, anchor="w", stretch=(k == "result"))
        sb = tb.Scrollbar(win, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(fill=BOTH, expand=True, padx=12, side=TOP)
        sb.place(in_=tv, relx=1.0, relheight=1.0, anchor="ne")
        mid = tb.Frame(win, padding=(12, 0, 12, 4))
        mid.pack(fill=X)
        tb.Label(mid, text="选中一行 → 下面显示这次有货的完整执行流程", bootstyle="secondary",
                 font=("Microsoft YaHei UI", 8)).pack(side=LEFT)
        steps = tb.Text(win, height=12, wrap="word", font=("Microsoft YaHei UI", 9))
        steps.pack(fill=BOTH, expand=True, padx=12, pady=(0, 10))
        steps.tag_configure("ok", foreground="#1f8b4c", font=("Microsoft YaHei UI", 9, "bold"))
        steps.tag_configure("bad", foreground="#c0392b", font=("Microsoft YaHei UI", 9, "bold"))

        cache = {}

        def load():
            rows = []
            for fn in sorted(_g.glob(os.path.join(d, "有货-*.jsonl")))[-3:]:
                try:
                    for line in open(fn, encoding="utf-8", errors="replace"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if r.get("event") in ("attempt", "skip"):
                            rows.append(r)
                except Exception:
                    pass
            rows.sort(key=lambda r: r.get("ts", ""))
            rows = rows[-400:]
            seen = set(tv.get_children())
            for i, r in enumerate(rows):
                key = "%s|%s|%s|%s" % (r.get("ts"), r.get("acc"), r.get("store"), r.get("event"))
                iid = key
                cache[iid] = r
                vals = (r.get("ts", ""), r.get("acc", ""), r.get("sku_name", ""), r.get("store_name", r.get("store", "")),
                        "开抢" if r.get("event") == "attempt" else "跳过",
                        r.get("result", ""), ("%.1fs" % r["sec"]) if r.get("sec") is not None else "",
                        r.get("bounces", ""))
                if tv.exists(iid):
                    tv.item(iid, values=vals)
                else:
                    tv.insert("", "end", iid=iid, values=vals)
                seen.discard(iid)
            for iid in seen:
                try:
                    tv.delete(iid)
                except Exception:
                    pass
            kids = tv.get_children()
            v_cnt.set("共 %d 次有货事件（文件：logs\\有货-*.jsonl）%s"
                      % (len(kids), ("｜最新：" + cache[kids[-1]].get("ts", "") if kids else "")))

        def on_sel(_e=None):
            sel = tv.selection()
            if not sel:
                return
            r = cache.get(sel[0], {})
            steps.delete("1.0", END)
            steps.insert(END, "%s  %s  %s ｜ 门店 %s" % (r.get("ts", ""), r.get("acc", ""),
                                                       r.get("sku_name", ""),
                                                       r.get("store_name", r.get("store", ""))))
            steps.insert(END, "\n")
            steps.insert(END, "结果：%s" % r.get("result", ""),
                         ("ok" if "成单" in (r.get("result") or "") else "bad",))
            steps.insert(END, "\n" + "─" * 78 + "\n")
            for st in (r.get("steps") or []):
                tag = ("ok",) if ("成功" in st or "成单" in st) else (("bad",) if ("没成" in st or "失败" in st) else ())
                steps.insert(END, st + "\n", tag)

        tv.bind("<<TreeviewSelect>>", on_sel)
        tb.Button(top, text="刷新", bootstyle="secondary-outline", width=8, command=load).pack(side=LEFT)
        tb.Button(top, text="打开日志目录", bootstyle="secondary-outline",
                  command=lambda: os.startfile(d)).pack(side=LEFT, padx=8)
        tb.Label(top, textvariable=v_cnt, bootstyle="secondary").pack(side=LEFT, padx=12)
        load()

        def tick():
            if not win.winfo_exists():
                return
            try:
                load()
            except Exception:
                pass
            win.after(2000, tick)

        tick()

    def _clear_ledger(self):
        if not messagebox.askyesno("清空抢购账本", "把所有取货人的「已抢数量」清零？\n\n"
                                                   "（已经买到的订单不会消失，只是账本重新计数）"):
            return
        try:
            self.cfg.ledger.reset()
            self.cfg.ledger.save()
            self.log("[账本] 已清空所有人的已抢数量")
        except Exception as e:
            self.log("[账本] 清空失败：%s" % e)

    def _clear_heat(self):
        import glob as _g
        n = 0
        for f in _g.glob(os.path.join(HERE, "sessions", "*.json")):
            try:
                os.remove(f)
                n += 1
            except Exception:
                pass
        self.log("[会话] 已清空热会话存档：%d 个（下次启动会重新预热）" % n)

    def _proxy_provider(self):
        """给界面自己发的请求（购物袋查看/加购/删除）取代理；没开代理就返回 None"""
        try:
            from .proxy_pool import get_pool
            if not bool(getattr(self.cfg.settings, "proxy_enabled", False)):
                return None
            return get_pool(self.cfg, self.log).provider()
        except Exception:
            return None

    def _test_proxy(self):
        """取一个 IP + 真打一发：先看【能不能过苹果】，再尽量取出口 IP（ipify 在国内不通，别用它）"""
        try:
            self.collect()
            json.dump(self.cfg.raw, open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            self.cfg.rebuild()
            import re as _re
            import urllib.request as _ur
            from .proxy_pool import get_pool
            pool = get_pool(self.cfg, self.log)
            t0 = time.time()
            ip = pool.fetch_one(force=True)
            if not ip:
                messagebox.showwarning("测试代理", "没取到 IP。\n接口原话：%s" % (pool.last_err or "?"))
                return
            lines = ["取到代理：%s（%.1fs）" % (ip.replace("http://", ""), time.time() - t0), ""]
            op = _ur.build_opener(_ur.ProxyHandler({"http": ip, "https": ip}))
            # ① 走代理打苹果（这才是真正要用的）
            try:
                t1 = time.time()
                r = op.open("https://www.apple.com.cn/", timeout=15)
                apple = "HTTP %s ｜ %.1fs → 苹果能过 ✓" % (getattr(r, "status", "?"), time.time() - t1)
            except Exception as ex:
                apple = "✗ %s: %s" % (type(ex).__name__, str(ex)[:60])
            lines += ["① 走代理访问苹果：%s" % apple, ""]
            # ② 出口 IP（多试几个站；api.ipify.org 在这台机器上直连都不通，别用）
            got = ""
            for u in ("http://ip.3322.net", "https://myip.ipip.net", "https://ip.useragentinfo.com/json"):
                try:
                    op2 = _ur.build_opener(_ur.ProxyHandler({"http": ip, "https": ip}))
                    body = op2.open(u, timeout=10).read().decode("utf-8", "replace")
                    m = _re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", body)
                    if m:
                        got = "%s（来自 %s）" % (m.group(1), u)
                        break
                except Exception:
                    continue
            lines += ["② 出口 IP：%s" % (got or "取不到（这些检测站在国内常被墙，不影响抢购）")]
            self.log("[代理] 测试：%s ｜ 苹果 %s ｜ 出口 %s" % (ip.replace("http://", ""), apple, got or "未知"))
            messagebox.showinfo("测试代理", "\n".join(lines))
        except Exception as ex:
            messagebox.showerror("测试代理失败", "%s: %s" % (type(ex).__name__, ex))

    def _test_account_service(self):
        """测试「协议服务地址」是否可连，并列出在线账号。"""
        base = ""
        try:
            base = (self.sv["account_service_url"].get() or "").strip()
        except Exception:
            base = ""
        base = base or autologin.DEFAULT_SERVICE
        try:
            accts = autologin.list_online_accounts(base=base)
        except Exception as ex:
            messagebox.showwarning("协议服务", "连接失败：\n%s\n%s" % (base, ex))
            return
        names = "、".join((a.get("nickname") or a.get("alias") or a.get("openid") or "?") for a in accts[:8])
        messagebox.showinfo("协议服务", "连接成功：%s\n在线账号 %d 个%s"
                            % (base, len(accts), ("\n" + names) if names else ""))

    def _resend_mail(self):
        r"""把 logs\邮件-没发出去\ 里积压的邮件重发一遍（SMTP 限流导致失败的都在那）"""
        try:
            self.collect()
            json.dump(self.cfg.raw, open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            e = self.cfg.raw.get("settings", {}).get("email") or {}
            from .notify import resend_pending
            r = resend_pending(e, logger=self.log)
            self.log("[邮件] " + r)
            messagebox.showinfo("补发邮件", r)
        except Exception as ex:
            messagebox.showerror("补发邮件失败", "%s: %s" % (type(ex).__name__, ex))

    def _test_mail(self):
        try:
            self.collect()
            json.dump(self.cfg.raw, open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            e = self.cfg.raw.get("settings", {}).get("email") or {}
            from .notify import send_mail
            r = send_mail(e, "【测试】苹果抢购助手 邮件通道正常",
                          "这是一封测试邮件。\n时间：%s\n发件：%s\n收件：%s\n"
                          % (time.strftime("%Y-%m-%d %H:%M:%S"), e.get("user"), "、".join(e.get("to") or [])),
                          logger=self.log)
            self.log("[邮件] " + r)
            messagebox.showinfo("测试邮件", r)
        except Exception as ex:
            messagebox.showerror("测试邮件失败", "%s: %s" % (type(ex).__name__, ex))

    # ============================================================ 状态
    def _pill(self, text, kind="secondary"):
        try:
            self.l_pill.configure(bootstyle="%s-inverse" % kind)
        except Exception:
            pass
        self.v_pill.set("● " + text)

    def _summary(self):
        try:
            n1 = len([r for r in self.task_rows if r["on"].get()])
            n2 = len([r for r in self.buyer_rows if r["on"].get()])
            warm = WARMUP_LABELS.get(self.v_warmup_mode.get(), WARMUP_LABELS[WARMUP_SNIPE])
            self.v_sum.set("%d 个任务 ｜ %d 个普通取货人 ｜ %s ｜ %s"
                           % (n1, n2, warm,
                              MODE_DRY if self.v_mode.get() == MODE_DRY else "★ " + MODE_LIVE))
        except Exception:
            pass

    def refresh(self, first=False):
        try:
            for c in self.cart_windows:
                c["win"].destroy()
        except Exception:
            pass
        self.cart_windows = []
        self.task_rows, self.buyer_rows = [], []
        self.status_vars = {}
        self.q = queue.Queue()          # ★ worker 线程 → 主线程的唯一通道（Tk 不允许多线程乱碰）
        self._t_board = 0.0             # 看板刷新节流
        self._logfh = None              # 日志文件句柄
        # ★★ 表格视图：直接从配置建行模型，再整表重画
        self.task_rows = [self._task_row_from(c) for c in (self.cfg.raw.get("cookies") or [])]
        self.buyer_rows = [self._buyer_row_from(c) for c in (self.cfg.raw.get("buyers") or [])]
        for r in self.task_rows:
            self.status_vars[r["name"].get() or r["_name"]] = tb.StringVar(value="待启动")
        self._tv_tasks_render()
        self._tv_buyers_render()
        s = self.cfg.raw.get("settings") or {}
        for k, dflt in (("scan_concurrency", 12), ("scan_interval_sec", 0.4),
                        ("max_parallel_buy", 2),
                        ("alarm_sec", 45),
                        ("retry_no_slot_sec", 15), ("settle_gap_sec", 2),
                        ("store_cool_sec", 15),
                        ):
            self.sv[k].delete(0, END)
            self.sv[k].insert(0, str(s.get(k, dflt)))
        self.sv["proxy_on"].set(bool(s.get("proxy_enabled", False)))
        self.sv["proxy_api_url"].delete(0, END)
        self.sv["proxy_api_url"].insert(0, s.get("proxy_api_url") or "")
        self.sv["proxy_rotate_sec"].delete(0, END)
        self.sv["proxy_rotate_sec"].insert(0, str(s.get("proxy_rotate_sec", 60)))
        self.sv["proxy_mode"].set("全局共用一个 IP" if str(s.get("proxy_mode") or "per_account").lower() == "global" else "一号一池（推荐）")
        _qc = self.sv.get("qr_cookie")
        if _qc is not None:
            _qc.delete(0, END)
            _qc.insert(0, str(s.get("qr_cookie") or ""))
        _svc = self.sv.get("account_service_url")
        if _svc is not None:
            _svc.delete(0, END)
            _svc.insert(0, str(s.get("account_service_url") or ""))
        self.sv["status_poll_sec"].delete(0, END)
        self.sv["status_poll_sec"].insert(0, str(s.get("status_poll_sec", 3)))
        self.sv["status_poll_max"].delete(0, END)
        self.sv["status_poll_max"].insert(0, str(s.get("status_poll_max", 20)))
        self.sv["time_slot_index"].delete(0, END)
        self.sv["time_slot_index"].insert(0, str(s.get("time_slot_index", 1)))
        self.sv["prewarm_interval_sec"].delete(0, END)
        self.sv["prewarm_interval_sec"].insert(0, str(s.get("prewarm_interval_sec", 0)))
        self.sv["prewarm_contact"].set(bool(s.get("prewarm_contact", True)))
        self.sv["require_time_slot"].set(bool(s.get("require_time_slot", False)))
        mode = {"none": WARMUP_FIRST, "first": WARMUP_FIRST,
                "cart": WARMUP_SNIPE, "full": WARMUP_SNIPE,
                "snipe": WARMUP_SNIPE}.get(str(s.get("warmup_mode", WARMUP_SNIPE)).lower(), WARMUP_SNIPE)
        self.v_warmup_mode.set(mode)
        self.sv["session_rebuild_sec"].delete(0, END)
        self.sv["session_rebuild_sec"].insert(0, str(s.get("session_rebuild_sec", 600)))
        self._toggle_rebuild_row()
        e = s.get("email") or {}
        self.sv["mail_on"].set(bool(e.get("enabled", True)))
        # ★ 界面控件名 → 配置键名，必须一一对应！（原来写错成 mail_user/mail_pass，
        #   导致界面显示为空、一保存就把配置里的 发件邮箱/授权码 清成空字符串）
        for wkey, ckey, dflt in (("mail_host", "host", "smtp.qq.com"), ("mail_port", "port", 465),
                                 ("mail_user", "user", ""), ("mail_pass", "pass", ""),
                                 ("mail_to", "to", "")):
            val = e.get(ckey, dflt)
            if wkey == "mail_to":
                val = ",".join(val or []) if isinstance(val, list) else (val or "")
            self.sv[wkey].delete(0, END)
            self.sv[wkey].insert(0, str(val if val is not None else ""))
        try:
            self.v_task_n.set("共 %d 个任务（%d 启用）"
                              % (len(self.task_rows), len([r for r in self.task_rows if r["on"].get()])))
            self.v_buyer_n.set("共 %d 个取货人（%d 启用）"
                               % (len(self.buyer_rows), len([r for r in self.buyer_rows if r["on"].get()])))
        except Exception:
            pass
        self.sv["dbg_on"].set(bool(s.get("debug_checkout", False)))
        self.v_mode.set(MODE_DRY if s.get("dry_run", True) else MODE_LIVE)
        self._summary()
        if first:
            if not self.task_rows:
                self.log("① 到「抢购任务」页 → 选身份文件 + 选要抢的型号（一个身份抢一个型号）")
            if not self.buyer_rows:
                self.log("② 到「取货人」页 → 填姓名/手机/邮箱/证件后四位 + 选要的型号 + 选城市")
            self.log("③ 先选「演练（不下单）」跑一遍（日志里会看到 命中→建会话→时段→取货人），确认无误再切「真实下单」")

    def log(self, m: str):
        """worker 线程也会调它 → 只入队；真正的 Tk 操作全部在主线程 _drain() 里做"""
        self.q.put(("log", m))

    def _drain(self):
        """主线程：把队列里的日志/状态刷进界面"""
        for _ in range(400):
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "log":
                    self._append_log(payload)
                elif kind == "got":
                    self.v_got.set(payload)
                elif kind == "pill":
                    self._pill(payload[0], payload[1])
                elif kind == "runstate":
                    # payload = (开始按钮状态, 停止按钮状态)
                    self.b_start.configure(state=payload[0])
                    self.b_stop.configure(state=payload[1])
                elif kind == "cart":
                    self._cart_result(payload[0], payload[1], payload[2])
                elif kind == "order":
                    self._add_order(payload)
                elif kind == "qr_status":
                    _oid, _state, _text = payload
                    self.v_order_status.set("[%s] %s │ %s" % (_oid, _state, _text))
            except Exception:
                pass
        if time.time() - self._t_board >= 1.0:
            self._t_board = time.time()
            try:
                self._refresh_board()
            except Exception:
                pass
        try:
            self.root.after(120, self._drain)
        except Exception:
            pass

    def _log_file(self):
        """日志同时写文件，方便事后排查（dist\\logs\\monitor-日期.log）"""
        try:
            if self._logfh is None:
                d = os.path.join(HERE, "logs")
                os.makedirs(d, exist_ok=True)
                self._logfh = open(os.path.join(d, "monitor-%s.log" % time.strftime("%Y%m%d")),
                                   "a", encoding="utf-8")
            return self._logfh
        except Exception:
            return None

    def _append_log(self, m: str):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), m)
        try:
            fh = self._log_file()
            if fh:
                fh.write(line + "\n")
                fh.flush()
        except Exception:
            pass
        tag = ""
        if "下单成功" in m or "★ 成功" in m:
            tag = "ok"
        elif "★★★" in m or "命中" in m:
            tag = "hit"
        elif "✗" in m or "QUOTA" in m or "异常" in m or "配置错误" in m or "失败" in m:
            tag = "err"
        elif "⚠" in m:
            tag = "warn"
        try:
            self.txt.insert(END, line + "\n", (tag,))
            if int(self.txt.index("end-1c").split(".")[0]) > LOG_MAX:
                self.txt.delete("1.0", "160.0")
            self.txt.see(END)
        except Exception:
            pass
        for name, sv in list(self.status_vars.items()):
            if not name:
                continue
            pre = "[%s]" % name
            if m.startswith(pre):
                _txt = m[len(pre):].strip()[:70] or "…"
                sv.set(_txt)
                # ★ 表格视图：同步到该任务行的「状态」列
                for _r in list(getattr(self, "task_rows", []) or []):
                    if (_r["name"].get() or _r["_name"]) == name:
                        _r["status"].set(_txt)
                        self._tv_tasks_cell(_r["_id"], "status")
                        break

    def _clear_log(self):
        self.txt.delete("1.0", END)

    # ============================================================ 运行
    def _check(self) -> bool:
        self.collect()
        # ★ 防线：多个任务抢同一个型号时提醒（不是错误，但通常是你点错了下拉框）
        try:
            from collections import Counter
            cnt = Counter(c.sku for c in self.cfg.cookies if c.enabled and c.sku)
            dup = {k: v for k, v in cnt.items() if v > 1}
            for k, v in dup.items():
                who = "、".join(c.name for c in self.cfg.cookies if c.enabled and c.sku == k)
                self.log("[提醒] 有 %d 个任务在抢同一个型号 %s（%s）—— 确认是你想要的吗？" % (v, k, who))
        except Exception:
            pass
        errs = self.cfg.validate()
        for e in errs:
            self.log("[配置] " + e)
        if errs:
            messagebox.showwarning("还差点东西", "\n".join(errs))
            return False
        return True

    def start(self):
        live = (self.v_mode.get() == MODE_LIVE)
        if live and not messagebox.askyesno(
                "确认真实下单",
                "当前是「真实下单」：命中后会自动提交、真的建单。\n\n确定要这样跑吗？"):
            return
        if not self.save() or not self._check():
            return
        self.cfg.settings.dry_run = not live
        self.cfg.settings.auto_submit = live
        self.cfg.raw.setdefault("settings", {})["dry_run"] = self.cfg.settings.dry_run
        self.cfg.raw["settings"]["auto_submit"] = self.cfg.settings.auto_submit
        self.save()
        self._summary()
        self.log("=" * 64)
        self.log("启动：干跑=%s ｜ 自动提交=%s ｜ %d 个任务 / %d 个取货人"
                 % (self.cfg.settings.dry_run, self.cfg.settings.auto_submit,
                    len(self.cfg.cookies), len(self.cfg.buyers)))
        self._pill("运行中", "success")
        # GUI 不传固定 override，让运行中保存的演练/真实下单参数能够热生效。
        # 命令行 --dry / --auto-submit 仍由 main.py 传 override，不受配置热更新影响。
        ws, stop = start_workers(self.cfg, self.log, on_order=self._on_order)
        if not ws:
            self._pill("没起来", "danger")
            return
        self.workers, self.stop_evt = ws, stop
        self.b_start.configure(state="disabled")
        self.b_stop.configure(state="normal")
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        while self.workers and any(w.is_alive() for w in self.workers):
            self.q.put(("got", "已买到 %d 台" % sum(getattr(w, "orders", 0) for w in self.workers)))
            time.sleep(1)
        try:
            alive = [w.cc.name for w in (self.workers or []) if w.is_alive()]
            self.log("■ 全部任务已退出%s" % (("（仍在跑：%s）" % "、".join(alive)) if alive else ""))
        except Exception:
            pass
        # ★★ 绝不能在这个线程里直接 configure 按钮：Tk 不是线程安全的，
        #    调用失败会被 except 吞掉 → 「开始」按钮永远停在禁用状态，只能重启程序。
        #    统一丢进队列，由主线程 _drain() 去改。
        self.q.put(("pill", ("已停止", "secondary")))
        self.q.put(("runstate", ("normal", "disabled")))

    def stop(self):
        if self.stop_evt:
            self.stop_evt.set()
            try:                       # ★ 停止时把代理池的定时换 IP 也停掉
                from .proxy_pool import stop_all_pools
                stop_all_pools()
            except Exception:
                pass
            self.log("■ 收到停止 → 已打断各号的等待/请求，正在退出…")
            self._pill("停止中…（打断等待中）", "warning")
        else:
            self._pill("没在跑", "secondary")

    def on_close(self):
        try:
            self.save()
        except Exception:
            pass
        if self.stop_evt:
            self.stop_evt.set()
        try:
            from .proxy_pool import stop_all_pools
            stop_all_pools()
        except Exception:
            pass
        self.root.destroy()


def run_gui(config_path: str = "") -> int:
    root = tb.Window(themename="flatly")
    app = App(root, config_path)
    app.refresh(first=True)
    root.mainloop()
    return 0
