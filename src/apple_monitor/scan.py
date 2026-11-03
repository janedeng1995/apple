# -*- coding: utf-8 -*-
"""库存探测：用【商品定位家族】的 pickup/quote（实测 35 请求/秒不 541，且 saveStoreEnabled 权威）

⚠ 不要用 recommend（会漏报：实测全国 5 家能订它只报 1 家）；
⚠ 不要用结算家族接口做高频探测（2 秒一次第 4 次就 541）。
"""
from __future__ import annotations

from typing import List, Optional

from .client import Client, MERCH

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ★★ 按【调用线程对象】复用 executor —— 每个 CookieWorker 都是独立线程，
#    所以每个号有自己的线程池、互不排队；而那些线程里的 TLS 连接
#    （client._TLS.s 是 thread-local 的 Session）也就跟着留下来了，
#    下一轮扫描直接复用，不再每轮重做 8 次 TLS 握手。
#    实测：新建 Session 每发 0.583s，复用 0.439s → 每发省 0.144s。
#    ★ 用 Thread 对象做 key，绝不能用 get_ident()：线程结束后 ident 会被
#      OS 回收复用，新线程会拿到一个属于已死线程的池（里面的 thread-local
#      Session 还带着上一个号的 cookie/stk）→ 串号。
#    ★ 线程收工时必须调 release_executor()，否则池子和它的连接会漏到进程退出。
_EX_LOCK = threading.Lock()
_EX_BY_THREAD = {}


def _executor(n: int) -> ThreadPoolExecutor:
    key = threading.current_thread()
    with _EX_LOCK:
        ex = _EX_BY_THREAD.get(key)
        if ex is None or getattr(ex, "_max_workers", None) != n:
            if ex is not None:                 # 并发数变了 → 旧的先关掉，别白养着
                try:
                    ex.shutdown(wait=False)
                except Exception:
                    pass
            ex = ThreadPoolExecutor(max_workers=n)
            _EX_BY_THREAD[key] = ex
        return ex


def release_executor() -> None:
    """★ worker.run() 收工时调用：关掉这个线程的扫描线程池（含它的 TLS 连接）。"""
    key = threading.current_thread()
    with _EX_LOCK:
        ex = _EX_BY_THREAD.pop(key, None)
    if ex is None:
        return
    try:
        ex.shutdown(wait=False)
    except Exception:
        pass


def parse_verdict(t: str):
    """★ 从 pickup/quote 的返回里判"这家店是否值得进结算"：
       返回 (verdict, info)：
         verdict=True  → saveStoreEnabled=true；有日期或没日期都继续结算
         verdict=False → 服务端明确标记不支持取货
         verdict=None  → 查不到（没有库存字段）

       ★★ saveStoreEnabled 只代表"这家店支持这个型号取货"，
          不等于"此刻已经下发可取时段"。为了不漏掉服务端稍后补全的时段，
          只要允许取货就进完整结算链，由 placeOrder 给最终结论。
          真机同款字段（我们有原文）：
            有档期： availabilityInfo=明天 有货, encodedUpperDateString=20260920
            没档期： availabilityStatus=NOT_AVAILABLE,  availabilityInfo=暂无供应,  encodedUpperDateString=null

          availabilityStatus 只用于日志展示，不能硬限定 AVAILABLE_TODAY：
          实测门店有次日/未来时段时，该字段并不稳定，但 encodedUpperDateString 已给出有效日期。
    """
    info = {}
    m = re.search(r'"saveStoreEnabled"\s*:\s*(true|false)', t or "")
    if not m:
        return None, info
    info["allow"] = (m.group(1) == "true")
    def g(key):
        mm = re.search(r'"%s"\s*:\s*"([^"]*)"' % key, t or "")
        if mm: return mm.group(1)
        mm = re.search(r'"%s"\s*:\s*(null|[^,\n}]+)' % key, t or "")
        return "" if (not mm or mm.group(1) == "null") else mm.group(1).strip('" ')
    info["status"] = g("availabilityStatus")
    info["text"] = g("availabilityInfo")
    info["quote"] = g("pickupQuote")
    info["date"] = g("encodedUpperDateString")
    if not info["allow"]:
        return False, info
    if "encodedUpperDateString" in (t or "") and not info["date"]:
        # saveStoreEnabled 已是有货信号；没有预告日期只是软信号。
        # 仍进结算并至少尝试一次 placeOrder，由结算状态机给最终结论。
        info["date_missing"] = True
    return True, info


def why_text(verdict, info: dict) -> str:
    """把判定翻成人话（给日志用）"""
    info = info or {}
    if verdict is True:
        if info.get("date_missing"):
            return "%s ｜ 未给预告日期，仍进结算尝试" % (info.get("text") or "支持取货")
        return "%s ｜ 可取 %s" % (info.get("text") or "有货", info.get("date") or "-")
    if info.get("allow") and not info.get("date"):
        return "支持取货但未给预告日期（%s）→ 仍进结算" % (info.get("text") or "暂无供应")
    return info.get("text") or info.get("quote") or "无货"


def quote_one(cli: Client, sku: str, store: str, info: dict = None,
              cancel_evt=None, tries: int = 2) -> Optional[bool]:
    """返回 True=这家店值得进结算 / False=明确不可取 / None=查不到

    info：可选出参，填 {"status": HTTP码, "len": 字节, "why": 原因,
                        "allow"/"status=".../  "text"/"quote"/"date"}，方便日志与排查
    cancel_evt：本级扫描命中后设置；阻止还没发出的重试和退避等待。
    """
    if cancel_evt is not None and cancel_evt.is_set():
        return None
    u = MERCH + "/pickup/quote?pn=" + sku.replace("/", "%2F") + "&store=" + store
    r = cli.get(u, tries=tries, cancel_evt=cancel_evt)
    if r is None:
        if info is not None:
            info.update(status=0, len=0, why="请求失败（超时/连接被掐）")
        return None
    t = r.text or ""
    v, extra = parse_verdict(t)
    if v is None:
        if info is not None:
            info.update(status=r.status_code, len=len(t),
                        why=("HTTP %s 且页面里没有库存字段" % r.status_code))
        return None
    if info is not None:
        info.update(status=r.status_code, len=len(t), why="")
        info.update(extra)
    return v


def scan(cli: Client, sku: str, stores: List[str], concurrency: int = 12,
         stats: Optional[dict] = None, should_stop=None, cancel_evt=None,
         recheck: bool = True):
    # ★ 返回值：(hits, detail)；detail[store] = {"verdict":…, "info":…, "filtered":bool, "why":str}
    """并发扫一批门店，返回"能订"的门店列表

    stats：可选出参。传了就把本轮统计累加进去（给界面看板用）：
        rounds 轮数 / req 请求数 / ok 有货 / no 无货 / err 查不到 / stores 本轮店数 /
        hits_last 本轮有货数 / ms_last 本轮耗时(毫秒) / t_last 时间戳 / last_hits 最近有货门店
    """
    if not stores:
        return [], {}
    t0 = time.time()
    infos = {}
    cancel = cancel_evt if cancel_evt is not None else threading.Event()

    def _cancelled() -> bool:
        if cancel.is_set():
            return True
        return bool(should_stop is not None and should_stop())

    def _one(s):
        if cancel.is_set():
            return (s, None)
        i = {}
        v = quote_one(cli, sku, s, i, cancel_evt=cancel)
        infos[s] = i
        return (s, v)

    # 等完本批已发出的探测，收集所有有货店。以前遇第一家就取消剩余任务，
    # 会让“第一家正在冷却”反复遮住其他真正可下单的门店。
    res = []
    filt = {}                   # 被过滤掉的"假信号"：store → 原因（给日志用）
    ex = _executor(max(1, min(concurrency, len(stores))))
    futs = {ex.submit(_one, st): st for st in stores}
    try:
        for fu in as_completed(futs, timeout=20):
            # ★ 外部要求中断（比如别的号开始抢单了）→ 立刻收工，
            #   不等这一轮扫完，把链路让给抢单那个号
            if _cancelled():
                cancel.set()
                break
            try:
                s_, v_ = fu.result()
            except Exception:
                continue
            res.append((s_, v_))
            if v_ is True:
                # 稳定版命中即进入抢单，不再额外复核一发；复核会增加一发扫描请求和延迟。
                if recheck:
                    i2 = {}
                    again = quote_one(cli, sku, s_, i2)
                    if again is not True:
                        filt[s_] = ("复核时档期没了（%s）" % why_text(again, i2))
                        res = [(x, y) for x, y in res if x != s_]
                        continue                     # ★ 不 break：剩下没扫的店继续有机会
    except Exception:
        pass                            # 超时也把已经拿到的结果用上
    cancel.set()                        # ★ 命中/超时/停止后，未开始的任务不再发；重试也停止
    for fu in futs:                     # ★ 取消还没开始跑的那些
        try:
            fu.cancel()
        except Exception:
            pass
    hits = [s for s, v in res if v is True]
    detail = {}
    for s_, v_ in res:
        i_ = infos.get(s_) or {}
        detail[s_] = {"verdict": v_, "info": i_, "filtered": False,
                      "why": why_text(v_, i_) if v_ is not None else (i_.get("why") or "查不到")}
    for s_, why in filt.items():
        detail[s_] = {"verdict": False, "info": {}, "filtered": True, "why": why}
    if stats is not None:
        stats["filtered_last"] = list(filt.items())[:4]
        stats["rounds"] = stats.get("rounds", 0) + 1
        stats["req"] = stats.get("req", 0) + len(res)
        stats["ok"] = stats.get("ok", 0) + sum(1 for _, v in res if v is True)
        stats["no"] = stats.get("no", 0) + sum(1 for _, v in res if v is False)
        stats["err"] = stats.get("err", 0) + sum(1 for _, v in res if v is None)
        # ★ 把"查不到"的真实原因一起记下来，方便排查
        for st, v in res:
            if v is None:
                i = infos.get(st) or {}
                key = "%s（%s）" % (i.get("why") or "?", i.get("status"))
                es = stats.setdefault("err_status", {})
                es[key] = es.get(key, 0) + 1
                stats["err_last"] = key
        stats["stores"] = len(stores)
        stats["hits_last"] = len(hits)
        stats["last_hits"] = hits[:6]
        stats["ms_last"] = int((time.time() - t0) * 1000)
        stats["t_last"] = time.time()
    return hits, detail
