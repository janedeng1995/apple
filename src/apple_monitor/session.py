# -*- coding: utf-8 -*-
"""结算会话：按【实测口径】实现完整的下单链路

链路（每一步都是真机抓包/实测确认过的）：
  ① 备袋   GET /chat/bag → 清掉非目标件 → POST /cart/add
  ② start  POST /v2/chat/checkout/start {dv}  → sid
  ③ 摆门店 GET changeStoreAction ∥ GET checkAvailabilityUrl → POST selectStoreUrl
  ④ 时段   GET spokes.1（有时段 19~49KB / 没时段 ~6KB，这是"到底有没有货"的唯一真相）
  ④.5     POST saveUrl&_m=checkout.timeSlot（★ 必须早于发票/联系人/付款：它是它们的前置）
  ④.6     POST selectInvoiceOptionUrl + saveUrl&_m=checkout.invoice
  ⑤ 联系人 GET spokes.3（必须！载入表单）→ POST saveThirdPartyPickupUrl
  ⑥ 付款   POST /checkoutx/billing?_a=selectBillingOptionUrl
  ⑦ 提交   POST placeOrderUrl → （多数直接 302；偶尔被要求补 发票/时段）→ 302
  ⑦.5     POST /v2/checkout/status 轮询到 /thankyou 才算真落库

★ 铁律（实测）：时段是后面表单的前置；最后写的那步会顶掉前一步（所以付款最后写）。
  ★ 弹回统计（2026-09-20 核对，808 次尝试）：弹回 0 次 702（87%）｜1 次 76｜2 次 26｜3 次 3｜5 次 1；
    弹回时服务端说缺的主要是【发票 52 次】，其次 时段 12 / 付款 2，【联系人仅 1 次】。
    （旧注释写"提交必弹一次联系人"——实测不成立，已按数据改掉。）
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Callable, List, Optional, Tuple

from .client import BASE, MERCH, Client

# ★ GET 错页自动纠偏开关（原来由 config 的 settings.spoke_page_recovery 控制，
#   那个配置项已经废弃删除：它永远是硬编码的 False，等于把纠偏整个关掉了）。
#   现在的行为与之前【完全一致】= 关闭；想打开就改这一个常量，不用动别处。
#   关闭时的效果：GET 某一格却打开成别的页面 → 直接判定失败并阻止后续保存，
#               不会盲发 POST（宁可失败，不可写错格子）。
SPOKE_PAGE_RECOVERY = False
def _lid_of(txt: str) -> str:
    m = re.search(r'"lineItemId"\s*:\s*"([0-9a-f\-]{36})"', txt or "")
    return m.group(1) if m else ""


def _parts_of(txt: str) -> List[Tuple[str, str, int]]:
    """返回 [(lineItemId, partNumber, quantity)]

    ★★ 一定要带 quantity！实测（0916 00:19）：一个 line item 的 quantity 可以是 4，
       以前只返回 (lid, part) → 工具数"条目数" → 把"1 个条目、数量 4"当成"只有 1 件"，
       结果苹果报"每名顾客最多可购买 2 个 iPhone 18 Pro"（超过 maxQuantity=2）。
    """
    out = []
    try:
        j = json.loads(txt or "")
    except Exception:
        return out
    for it in (j.get("lineItems") or []):
        if isinstance(it, dict) and it.get("lineItemId"):
            try:
                q = int(it.get("quantity") or 1)
            except Exception:
                q = 1
            out.append((it["lineItemId"], it.get("partNumber") or "", max(1, q)))
    return out


class NeedLogin(RuntimeError):
    """★ 服务端要求先去登录/绑定 Apple ID（cart/add 返回 userbind 的 returnURL）"""


class StoreUnavailable(RuntimeError):
    """Apple 已明确表示当前门店/商品不可取货。"""


class FastFillMiss(RuntimeError):
    """★ 快速填格（省掉前置 GET）时，服务端返回里这一格仍【未完成】→ 上层立刻回退标准流程。"""


def _store_gone(txt: str, include_quote: bool = True) -> bool:
    """★ 门店是否真的取不了。

    ★★ 2026-09-24 加 include_quote 开关，修"无时段被误判成门店被抢"：
      · 两条中文错误句 = 服务端明确的"这家店不行" → 任何时候都算硬证据。
      · pickup.quote="不可在…取货" = 软文案：**没有时段时苹果的 checkout 模型里
        本来就会带这句**（时段本身还没放出来）→ 只有配"没建出会话 / 页面里没有
        门店名"这种硬证据时才敢信它，所以填表阶段一律 include_quote=False。
    """
    t = txt or ""
    if "不能在你挑选的" in t or "该商品在你选择的门店" in t:
        return True
    if not include_quote:
        return False
    return bool(re.search(r'"pickup"\s*:\s*\{[^{}]*?"quote"\s*:\s*"不可在[^"]*取货"', t, re.S))


def _needs_login(txt: str) -> bool:
    t = txt or ""
    return ("userbind" in t) or ("IDMSWebAuth/signin" in t) or ('"returnURL"' in t and "idmsa.apple.com" in t)


class CheckoutSession:
    def __init__(self, cli: Client, sku: str, log: Optional[Callable] = None):
        self.c = cli
        self.sku = sku
        self.log = log or (lambda m: None)
        self.lid = ""
        self.lids = []              # ★ 袋里【全部】目标 SKU 行的 lineItemId（一单多件时 >1 个）
        self.multi_line = False     # ★ 是否“一行以上”→ 决定 cartLineItemId 发 lid 还是 ALL_ITEMS
        self.qty = 1               # ★ 备袋后袋里目标型的实际件数（prepare_cart 写入，成单记账用）
        self.expired = False       # ★ 会话是否已被服务端判定过期
        self.sid = ""
        self.store = ""
        self.openid = ""
        self.landed_text = ""
        self.t_alive = time.time()
        self.dbg = None                # dbg hook injected by worker
        self.spoke_page_recovery = SPOKE_PAGE_RECOVERY
        # 选店页（changeStoreAction）是否已打开。它可以提前到预热阶段发掉，
        # 命中时就只剩 selectStoreUrl 一发；失效时 switch_store 会自动补开。
        self.picker_open = False
        self.last_checkout_states = {}
        self.last_terms_accepted = None
        self.last_terms_submitted = False
        # ★ 最近一次成功落库的取货时段（含「提交被弹回后在 submit 里现补」的情况）
        #   —— 成单日志/邮件要用它，否则会是 None
        self.last_saved_day = None
        self.last_saved_slot = None

    # ------------------------------------------------------------------ ① 备袋
    def bag(self) -> Tuple[list, str, bool]:
        r = self.c.get(BASE + "/chat/bag")
        t = (r.text or "") if r is not None else ""
        ok = r is not None and r.status_code == 200 and '"lineItems"' in t
        return _parts_of(t), t, ok

    def _delete(self, lid: str) -> bool:
        """★ 走 Client.bag_delete（带 stk 和正确的 content-type），否则会静默失败"""
        try:
            return self.c.bag_delete(lid)
        except Exception:
            return False

    def _set_lids(self, lids) -> None:
        """记下袋里【所有】目标行的 lineItemId（一单多件时袋里会是 N 行）。

        单行时 self.lid 与旧行为逐字一致；多行时 self.lids 收全，
        self.multi_line 决定 selectStoreUrl 发具体 lid 还是 ALL_ITEMS。
        """
        self.lids = [x for x in (lids or []) if x]
        self.lid = self.lids[0] if self.lids else ""
        self.multi_line = len(self.lids) > 1

    def cart_now(self):
        """★ 只读：回读购物袋，返回 (目标型号件数, 袋里全部条目[(型号, 件数)])。

        给"每次预热之前先查一次购物袋有多少"用：只读一眼，发现件数不对，
        再由 prepare_cart 去收敛。提交那一枪不做任何额外读袋。
        """
        items, _txt, ok = self.bag()
        if not ok:
            raise RuntimeError("读购物袋失败（HTTP %s）" % self.c.last_status)
        mine = [it for it in items if it[1] == self.sku]
        return sum(int(q) for _l, _p, q in mine), [(p2, q2) for _, p2, q2 in items]

    def prepare_cart(self, qty: int = 1, hard_cap: int = 2) -> str:
        """★ 保证袋里【只有目标型号、且件数正好 = qty】；做不到就报错中止，绝不带超标袋子进会话。

        qty = 这个号配的「每单件数」(cookie.per_order)，默认 1。
          · qty=1：袋里只有 1 个条目、那一行 quantity 正好是 1（久经实战，最稳）。
          · qty>1：通过重复加购把同一型号的数量叠到 qty（实测一个 line item 的 quantity 可以 >1）。
        注意是"件数"不是"条目数"———实测（0916 00:19）：一个 line item 的 quantity 可以是 4，
        苹果就会报"每名顾客最多可购买 2 个 iPhone 18 Pro"（超过 maxQuantity=2）。

        ★★ 2026-09-24 修「想要2实际存4」：
          每轮只做【一次】动作，做完一律 continue 回读复核，件数全部从回读推导。
          旧写法删一行就手工 have -= q、全删后直接假定袋子空了；一旦服务端
          "删除返回 200 但静默不删"（详见 client.bag_delete 注释），状态就记错，
          最后兜底分支「按当前件数继续」会把 4 件甚至更多的袋子原样提交。
          现在收敛不了就抛错 → 这一单失败，而不是超买。
        ★ 请求数：正常「空袋 → 逐件加购」路径与旧版【完全一致】；
          异常路径因为不再空转到循环上限，只会更少，不会更多。
        """
        qty = max(1, int(qty or 1))
        if qty > hard_cap:
            self.log("   ⚠ 每单件数 %d 超过服务端 maxQuantity=%d → 收敛到 %d 件"
                     % (qty, hard_cap, hard_cap))
            qty = hard_cap
        self.qty = qty
        seen = {}
        for _rd in range(max(16, qty * 8)):
            items, _txt, ok = self.bag()
            if not ok:
                raise RuntimeError("读购物袋失败（HTTP %s）" % self.c.last_status)
            mine = [it for it in items if it[1] == self.sku]
            foreign = [it for it in items if it[1] != self.sku]
            have = sum(int(q) for _l, _p, q in mine)

            # ★ 循环检测：同一个袋子状态反复出现 = 服务端加购/删除的粒度和目标件数对不上
            #   （典型：每发 /cart/add 固定 +2，却要收敛到 1）。旧写法会一直空转到上限，
            #   再把超标袋子当成功返回；这里直接判失败，绝不提交超标件数。
            fp = (tuple(sorted(int(q) for _l, _p, q in mine)), len(foreign))
            seen[fp] = seen.get(fp, 0) + 1
            if seen[fp] >= 3:
                raise RuntimeError(
                    "备袋凑不到 %d 件：袋子在 %s 之间反复循环"
                    "（苹果每次加购的粒度和目标件数对不上）→ 中止本单，不按超标件数下单"
                    % (qty, [(p2, q2) for _, p2, q2 in items]))

            # ① 非目标型号 → 全清，下一轮回读核对
            if foreign:
                self.log("   袋里有 %d 个非目标条目（%s）→ 清掉"
                         % (len(foreign), [p2 for _, p2, _q in foreign]))
                for lid, _p, _q in foreign:
                    if not self._delete(lid):
                        self.log("   ⚠ 删除非目标条目 %s 未确认（下轮回读核对）" % str(lid)[:8])
                continue

            # ② 正好 qty 件 → 达标
            if have == qty and mine:
                self._set_lids([l for l, _p, _q in mine])
                self.qty = qty
                self.log("   袋里已只有目标件 %d 件 ✓" % qty)
                return self.lid

            # ③ 超了 → 先剔整行；剔不到 qty 就把目标行全删，下一轮回读重加
            if have > qty:
                base = have
                for lid, _p, q in sorted(mine, key=lambda x: x[2]):
                    if have - int(q) < qty:
                        break
                    if not self._delete(lid):
                        self.log("   ⚠ 删除条目 %s 未确认（下轮回读核对）" % str(lid)[:8])
                    have -= int(q)
                if have != qty:
                    self.log("   ⚠ 袋里目标件 %d 件（要 %d），剔不到 → 全删重加" % (base, qty))
                    for lid, _p, _q in mine:
                        self._delete(lid)
                continue

            # ④ 少了/空了 → 只加 1 件，下一轮回读复核（不假设一发加多少）
            self.log("   加购 1 件（当前 %d / 要 %d）…" % (have, qty))
            r = self.c.post(MERCH + "/cart/add",
                            urllib.parse.urlencode({"p": self.sku, "code": ""}))
            t2 = (r.text or "") if r is not None else ""
            if _needs_login(t2):
                raise NeedLogin(
                    "这个微信号还没在苹果小程序里登录/绑定 Apple ID（服务端要求 userbind）\n"
                    "     → 请在【这个微信号】里打开苹果小程序，把手机加进购物袋（会弹出登录），完成绑定\n"
                    "     → 然后重新跑一次「抓身份-PC.py」换上新身份文件")

        # ★ 兜底：收敛不了就【中止】（旧行为是"按当前件数继续"，会把超标袋子提交出去）
        items, _t, ok = self.bag()
        have = sum(int(q) for _l, _p, q in items if _p == self.sku) if ok else -1
        raise RuntimeError("备袋没收敛到 %d 件（当前 %d 件）→ 中止本单，不按超标件数下单"
                           % (qty, have))

    def clear_cart(self) -> bool:
        """清空购物袋。首发模式尝试结束后调用，确保下次放货前袋里没有商品。"""
        items, _txt, ok = self.bag()
        if not ok:
            return False
        for lid, _part, _qty in items:
            if not self._delete(lid):
                return False
        items, _txt, ok = self.bag()
        self._set_lids([])
        return bool(ok and not items)

    # ------------------------------------------------------------------ ② start
    def start(self) -> str:
        dv = "as_dima^%s|%s|V3" % (self.c.ckj.get("as_dima", ""), self.c.ckj.get("as_pvi", ""))
        # ★ 兜底：万一 lids 是空的（正常流程 prepare_cart 一定先跑过），读一次袋子把
        #   行数补上，免得一单多件只发了 items.0。读不到就按旧行为只发 items.0。
        if not self.lids:
            try:
                _items, _t, _ok = self.bag()
                if _ok:
                    self._set_lids([l for l, _p, _q in _items if _p == self.sku])
            except Exception:
                pass
        # ★ 实测：start 可以直接接收 deliveryLocationType=RETAIL，
        #   建出的会话直接是「到店自提」，不需要再发 selectDeliveryMethod。
        # ★ 一单多件时袋里是 N 行 → deliveryLocationType 必须按行展开到 items.{i}；
        #   单行时展开结果与旧行为逐字一致（仍旧只有 items.0），不会回归。
        bm = {"dv": dv}
        for _i in range(max(1, len(self.lids))):
            bm["checkout.review.fulfillment.items.%d.deliveryLocationType" % _i] = "RETAIL"
            bm["checkout.review.fulfillment.items.%d.consolidationSelected" % _i] = "0"
        body = urllib.parse.urlencode(bm)
        r = self.c.post(BASE + "/v2/chat/checkout/start", body)
        t = (r.text or "") if r is not None else ""
        m = re.search(r'"sessionID"\s*:\s*"([0-9a-f]{64})"', t)
        if not m:
            if self.c.last_status == 541:
                raise RuntimeError("Apple 风控（541）→ 等一下重试")
            if ("quantity_limit_exceeded" in t or "超出单件购买数量限制" in t
                    or ("最多可购买" in t and "iPhone" in t)):
                _m = self._alerts(t)
                raise RuntimeError("QUOTA:服务端原文：" + (_m or "（没抓到文字，但有 quantity_limit_exceeded）"))
            if '"isEmptyCart"' in t and ('"isEmptyCart" : true' in t or '"isEmptyCart":true' in t):
                raise RuntimeError("购物袋是空的（isEmptyCart=true）→ 必须重新备袋加购")
            # ★★ 2026-09-24 修误判：只有【会话根本没建出来】时，才把"_store_gone 文案"当真。
            #    没有时段时苹果的 checkout 模型里本来就会写 pickup.quote="不可在…取货"，
            #    旧写法在【已经拿到 sessionID】之后还去匹配这段文案 → 把一家有货的店
            #    误判成"被抢了"并冷却 15 秒。会话都建出来了、SKU 也在里面，门店就是可用的。
            self._raise_if_store_gone(t, include_quote=False)
            raise RuntimeError("没拿到会话 ID（HTTP %s，响应 %d 字节）" % (self.c.last_status, len(t)))
        self.sid = m.group(1)
        ml = re.findall(r'"lineItemId"\s*:\s*"([0-9a-f\-]{36})"', t)
        if ml:
            self._set_lids(ml)   # 多行会话响应里每行各有一个 lineItemId，全收
        if self.sku not in t:
            raise RuntimeError("会话里没有目标件（袋子是空的？）→ 中止，别白烧会话")
        self.t_alive = time.time()
        self.expired = False       # 新会话 → 清掉过期标记
        self.refresh_spokes(t)     # ★ 记下这一版的 spokes 列表（id → 第几格）
        # ★★ 2026-09-24：这里【不再】判"门店取不了"。能走到这行说明 sessionID 已拿到、
        #    目标 SKU 也在会话里 —— 门店是好的。无时段/资料未落库都交给后面的
        #    "逐格补齐 + 照常提交"流程处理，不再一上来就判死刑、冷掉一家有货的店。
        try:
            self._assert_retail(t)     # ★ 只靠 start 参数时必须确认服务端真的进入了 RETAIL
        except RuntimeError:
            # ★ 一单多件时 items.N 展开是【未抓包验证】的写法；服务端若不认，
            #   照真机默认 flow 补一招：changeStoreAction(items.0) + selectStoreUrl(ALL_ITEMS)
            #   把全部行翻成 RETAIL（2026-09-23 抓包实证该路径有效）。
            if (not self.multi_line and self._rows_in(t) < 2) or not self.store:
                raise
            self.log("   ⚠ 多行 start 没直接进 RETAIL → 按真机补 selectStoreUrl(ALL_ITEMS)")
            self.open_store_picker()
            t2, ok = self._post_select_store(self.store)
            if not ok:
                raise RuntimeError("多行会话切自提失败（selectStoreUrl 未受理）")
            t = t2
            self.refresh_spokes(t)
            self._assert_retail(t)
        return self.sid

    @staticmethod
    def _raise_if_store_gone(txt: str, include_quote: bool = True):
        """★ 填表阶段（存时段/联系人/付款/发票、开格）一律传 include_quote=False：
        那些返回里本来就带 pickup.quote，不能据此判"门店被抢"。"""
        if _store_gone(txt, include_quote=include_quote):
            raise StoreUnavailable("门店取不了（被抢了）")

    @staticmethod
    def _rows_in(t: str) -> int:
        """响应里 fulfillment.items 有几行（一单多件 = N 行）。"""
        try:
            return len(((json.loads(t or "{}").get("review") or {}).get("fulfillment") or {})
                       .get("items") or [])
        except Exception:
            return 0

    @staticmethod
    def _assert_retail(t: str):
        """确认 start 返回的会话确实选中了到店自提。

        ★ 逐行校验：一单多件时 fulfillment.items 有 N 行，【每一行】都必须是 RETAIL，
          否则后面 selectStoreUrl/时段只会对第一行生效，另一行还挂在送货上。
        这是防 Apple 改接口后静默回退成送货的硬校验；不再补发 selectDeliveryMethod。
        """
        try:
            d = json.loads(t or "{}")
            items = ((d.get("review") or {}).get("fulfillment") or {}).get("items", []) or []
            if not items:
                raise RuntimeError("响应里没有 fulfillment.items")
            bad = []
            for it in items:
                selected = (it.get("deliveryLocationType") or "").upper()
                opts = it.get("deliveryOptions") or []
                retail_checked = any((o.get("deliveryLocationType") or "").upper() == "RETAIL"
                                     and bool(o.get("checked")) for o in opts)
                if selected != "RETAIL" or not retail_checked:
                    bad.append("%s:selected=%s,checked=%s"
                               % (it.get("partNumber") or "?", selected or "-", retail_checked))
            pickup_spoke = any((s.get("id") or "") == "pickup"
                               for s in ((d.get("review") or {}).get("spokes") or []))
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError("会话返回无法确认 RETAIL：%s" % e)
        if bad or not pickup_spoke:
            raise RuntimeError("start 未生效 RETAIL（共 %d 行，异常 %d 行：[%s]│pickup=%s）"
                               % (len(items), len(bad), " ; ".join(bad), pickup_spoke))

    # ------------------------------------------------------------------ 会话过期识别
    # ★ 实测（对照实验）：会话过期时 HTTP 仍然是 200，页面里是 sorryKey=session_expired / errorCode 3001。
    #   之前把它当成"这家店没货"处理 → 会去冷却门店、然后拿着一个死会话永远扫下去（看着在跑，其实必空）。
    @staticmethod
    def _expired(t: str) -> bool:
        # ★ errorCode 必须精确匹配字段值 3001（同 realId 的教训：裸子串会把
        #   页面里其它位置出现的 3001/30010 误判成会话过期，且 expired 一旦置位粘住不放）
        return ("session_expired" in t) or ("你的会话已过期" in t) or \
            bool(re.search(r'"errorCode"\s*:\s*"?3001"?(?![0-9])', t or ""))

    def note(self, t: str) -> bool:
        if self._expired(t):
            self.expired = True
        return self.expired

    def save_store(self, store: str) -> bool:
        """★ 不再请求 pickup/quote&save=true。

        实测：服务端直接认 cookie 里的 rtsid，格式为
            {CN={t=a;i=R643;};}
        URL 编码后写入 Cookie，随后 start 创建的会话就是该门店。
        这样省掉定店接口的 1.3~2.5 秒，且不占 checkoutx 限流窗口。
        """
        raw = "{CN={t=a;i=%s;};}" % store
        self.c.ckj["rtsid"] = urllib.parse.quote(raw, safe="")
        self.store = store
        self.last_store_msg = ""
        self.last_store_status = "local rtsid=%s" % self.c.ckj["rtsid"]
        self.store_step_lost = False
        self.last_spokes = {}
        return True

    def open_store_picker(self) -> str:
        """只打开选店页（changeStoreAction），不选店。

        ★ 这一发可以提前到预热阶段发掉，依据是 2026-09-20 真机抓包：
          #61  changeStoreAction（打开选店页）
          #66~#75  5 次 checkAvailabilityUrl（用户在选店页里看各家店有没有货）
          #81  selectStoreUrl（这才真正提交选店）
          两者中间隔了 20 个请求，说明没有耦合，选店页打开后能放很久。
        另外它返回的 69.5KB 只是全国城市/门店列表，工具根本用不上
        （门店表在本地），提前发掉不损失任何信息。
        """
        r1 = self.c.get(
            BASE + "/v2/checkoutx?_a=changeStoreAction&_m=checkout.review.fulfillment.items.0")
        t1 = (r1.text or "") if r1 is not None else ""
        self.note(t1)
        if r1 is None or r1.status_code != 200:
            self.picker_open = False
            raise RuntimeError("打开选店页失败（HTTP %s）"
                               % (r1.status_code if r1 is not None else 0))
        self.picker_open = True
        return t1

    def _post_select_store(self, store: str, expected_name: str = ""):
        """发一次 selectStoreUrl。返回 (响应文本, 是否算成功)。

        成功 = HTTP 200 且响应里确实出现了目标门店名。门店真的被抢走时抛
        StoreUnavailable（这个不该重试，重试也没用）；其余失败算"选店页可能过期"，
        交给调用方决定要不要补开一次选店页。
        """
        # ★ 一单多件（袋里 N 行）时 cartLineItemId 必须是 ALL_ITEMS：2026-09-23 抓包实证，
        #   一发 ALL_ITEMS 就把全部行的 deliveryLocationType 翻成 RETAIL，不必逐行 selectStoreUrl。
        #   单行仍发具体 lid（与旧行为逐字一致，避免回归）。
        body = urllib.parse.urlencode({
            "checkout.pickup.cartLineItemId": ("ALL_ITEMS" if self.multi_line else self.lid),
            "checkout.pickup.currentRetailId": store,
        })
        r2 = self.c.post(BASE + "/v2/checkoutx?_a=selectStoreUrl&_m=checkout.pickup", body)
        t2 = (r2.text or "") if r2 is not None else ""
        self.note(t2)
        if r2 is None or r2.status_code != 200:
            return t2, False
        # ★★ 2026-09-24 修误判：先看【目标门店名在不在响应里】这个硬证据 ——
        #    名字在 = 选店确实成功了，直接放行。只有名字不在时，才去看那段文案。
        #    旧写法把文案匹配排在名字检查之前，结果没有时段时页面里的
        #    pickup.quote="不可在…取货" 会把一家好店误判成"被抢了"。
        wanted = (expected_name or "").replace("Apple ", "").strip()
        if wanted and wanted in t2:
            return t2, True
        self._raise_if_store_gone(t2, include_quote=False)
        if wanted:
            return t2, False
        return t2, True

    def switch_store(self, store: str, expected_name: str = "") -> str:
        """在已存在的完整预热 SID 中换店，并刷新该店最新 spokes。

        选店页优先复用预热阶段那发；只有 selectStoreUrl 不正常时才当场补开一次
        （并且只补一次），避免"预热那发过期了"把一整次抢购白白浪费掉。
        """
        if not self.sid or not self.lids:
            raise RuntimeError("没有可换店的预热会话")
        if not self.picker_open:
            self.open_store_picker()
        t2, ok = self._post_select_store(store, expected_name)
        if not ok and self.picker_open:
            self.picker_open = False
            self.open_store_picker()
            t2, ok = self._post_select_store(store, expected_name)
        if not ok:
            raise RuntimeError("提交选店失败（补开选店页后仍未切到 %s）" % store)
        self.refresh_spokes(t2)
        self.store = store
        return t2


    def open_spoke_only(self, speak_id: str, label: str) -> str:
        """只打开并核对一个 spoke，不保存。用于验证完整预热值能否直接复用。"""
        # 这里验证的是服务端状态机，不做多索引纠偏。若付款索引仍返回联系人页，
        # 说明联系人是硬前置条件；继续遍历其它索引只会重复 GET 同一页面。
        old_recovery = self.spoke_page_recovery
        self.spoke_page_recovery = False
        try:
            idx, page = self.load_spoke_by_id(speak_id)
        finally:
            self.spoke_page_recovery = old_recovery
        self._raise_if_store_gone(page, include_quote=False)
        if idx is None or self.spoke_page_id(page) != speak_id:
            raise RuntimeError("%s页定位失败，已停止 GET-only 测试" % label)
        return page

    @staticmethod
    def parse_spokes(t: str) -> dict:
        out = {}
        try:
            i = (t or "").find('"spokes"')
            if i < 0:
                return out
            j = t.find("]", i)
            seg = t[i:j + 1]
        except Exception:
            return out
        for m in re.finditer(r'\{[^{}]*"keyPath"\s*:\s*"checkout\.review\.spokes\.(\d+)"[^{}]*\}', seg):
            blk = m.group(0); idx = int(m.group(1))
            sid = re.search(r'"id"\s*:\s*"([^"]*)"', blk)
            val = re.search(r'"value"\s*:\s*"([^"]*)"', blk)
            cmp_ = re.search(r'"isComplete"\s*:\s*(true|false)', blk)
            key = sid.group(1) if sid else ("spoke%d" % idx)
            out[key] = {"index": idx,
                        "value": val.group(1) if val else "",
                        "complete": (cmp_.group(1) == "true") if cmp_ else False}
        return out

    @staticmethod
    def parse_checkout_states(t: str) -> dict:
        """解析服务端聚合状态；它比单个 spoke 的 isComplete 更接近提交判定。

        注意：苹果把 checkoutStates 嵌在 review 里面，顶层并不放（0921 三单实测
        top_cs=False / review_cs=True）。以前只读顶层，结果恒为空：states 永远打不出来，
        isPlaceOrderReady / pickupMethodReady 也一直没被用上。两个位置都找。
        """
        try:
            d = json.loads(t or "{}")
        except Exception:
            return {}
        if not isinstance(d, dict):
            return {}
        states = d.get("checkoutStates")
        if not isinstance(states, dict):
            review = d.get("review")
            states = review.get("checkoutStates") if isinstance(review, dict) else None
        return states if isinstance(states, dict) else {}

    @staticmethod
    def parse_terms_accepted(t: str):
        """读取 review.terms.sections.0.termsAccepted；None 表示响应未带该结构。"""
        try:
            d = json.loads(t or "{}")
            review = d.get("review") or {}
            terms = review.get("terms") or {}
            sections = terms.get("sections") or []
            if sections and isinstance(sections[0], dict) and "termsAccepted" in sections[0]:
                return bool(sections[0].get("termsAccepted"))
        except Exception:
            pass
        return None

    def refresh_spokes(self, t: str = "") -> dict:
        d = self.parse_spokes(t)
        if d:
            self.last_spokes = d
        states = self.parse_checkout_states(t)
        if states:
            self.last_checkout_states = states
        terms = self.parse_terms_accepted(t)
        if terms is not None:
            self.last_terms_accepted = terms
        return getattr(self, "last_spokes", {}) or {}

    @staticmethod
    def spoke_page_id(t: str) -> str:
        """识别 selectUrl 实际打开的页面；只看顶层模型字段，避免正文误命中。"""
        try:
            model = json.loads(t or "{}")
        except Exception:
            return ""
        if not isinstance(model, dict):
            return ""
        if "timeSlot" in model:
            return "timeSlot"
        if "pickupContact" in model:
            return "3pp"
        if any(key in model for key in
               ("paymentOptions", "billingOptions", "paymentOptionsAvailableForm")):
            return "billing"
        return ""

    def load_spoke_by_id(self, speak_id: str):
        sp = getattr(self, "last_spokes", None) or {}
        info = sp.get(speak_id)
        if not info:
            return None, ""

        # 正常路径仍然只 GET 一次。只有苹果把旧 index 打开成其它页面时，
        # 才沿旧映射继续找；最终没找到目标页就返回 None，调用方不得 POST。
        original_index = info["index"]
        candidates = [original_index]
        if self.spoke_page_recovery:
            candidates.extend(sorted({v.get("index") for v in sp.values()
                                      if isinstance(v.get("index"), int)
                                      and v.get("index") != original_index}))
        tried = set()
        last_text = ""
        first_actual = ""
        while candidates and len(tried) < max(1, len(sp)):
            idx = candidates.pop(0)
            if idx in tried:
                continue
            tried.add(idx)
            r = self.c.get(
                BASE + "/v2/checkoutx?_a=selectUrl&_m=checkout.review.spokes.%d" % idx)
            t = (r.text or "") if r is not None else ""
            last_text = t
            self.note(t)
            actual = self.spoke_page_id(t)
            if not actual or actual == speak_id:
                sp2 = self.parse_spokes(t)
                if sp2:
                    self.last_spokes = sp2
                if idx != original_index:
                    self.log("   GET %s 第一次打开成 %s，已自动改到 spokes.%d"
                             % (speak_id, first_actual or "其他页", idx))
                return idx, t

            if not first_actual and self.spoke_page_recovery:
                first_actual = actual
                self.log("   GET %s 实际打开成 %s → 正在重新定位，暂不保存"
                         % (speak_id, actual))
            # 最可能是两个格子的 index 对调，优先尝试“实际页面”的旧 index。
            alternate = (sp.get(actual) or {}).get("index") if self.spoke_page_recovery else None
            if isinstance(alternate, int) and alternate not in tried:
                candidates = [alternate] + [x for x in candidates if x != alternate]

        if self.spoke_page_recovery:
            self.log("   GET %s 重新定位失败（尝试 spokes=%s）→ 已阻止后续保存"
                     % (speak_id, sorted(tried)))
        else:
            self.log("   GET %s 打开成其它页；自动纠偏已关闭 → 已阻止后续保存" % speak_id)
        return None, last_text

    # ------------------------------------------------------------------ ③ 摆门店
    @staticmethod
    def _order_link(txt: str, order: str, email: str) -> str:
        """★ 订单查看链接：感谢页里叫 timeSlotData.olssSmartLink，
        形如 https://www.apple.com.cn/xc/wechat/vieworder/<订单号>/<取货人邮箱>
        （访客订单页，不用登录就能看；拿不到就用订单号+邮箱自己拼）"""
        m = re.search(r'"olssSmartLink"\s*:\s*"([^"]+)"', txt or "")
        if m:
            return m.group(1).replace("\\/", "/")
        if order and email:
            return "https://www.apple.com.cn/xc/wechat/vieworder/%s/%s" % (order, email)
        return ""

    @staticmethod
    def _alerts(t: str) -> str:
        """★ 原样引用服务端 alerts 原文，别改写成自己的一句话"""
        t = t or ""
        m = re.search(r'"alerts"\s*:\s*\[\s*\{[^}]*?"message"\s*:\s*"([^"]{4,300})"', t)
        if m:
            return m.group(1)
        m = re.search(r'"alerts"\s*:\s*\[\s*"([^"]{4,300})"', t)
        return m.group(1) if m else ""

    # ------------------------------------------------------------------ ④ 时段
    @staticmethod
    def _slot_from_text(txt: str, slot_index: int = 1) -> Tuple[Optional[dict], Optional[dict]]:
        try:
            ts = (json.loads(txt or "{}").get("timeSlot") or {})
            days = [d for d in (ts.get("daySlots") or []) if d.get("enabled")]
            day = next((d for d in days if d.get("defaultSelection")), days[0] if days else None)
            ss = [s for s in ((day or {}).get("timeSlots") or []) if s.get("enabled")]
            try:
                wanted = max(1, int(slot_index or 1))
            except Exception:
                wanted = 1
            return day, (ss[min(wanted - 1, len(ss) - 1)] if ss else None)
        except Exception:
            return None, None

    def pull_slot(self, slot_index: int = 1) -> Tuple[Optional[dict], Optional[dict], str]:
        """返回 (day, slot, raw)；day/slot 为 None = 这家店这个型号此刻取不了（不是我们请求错了）"""
        idx, txt = self.load_spoke_by_id("timeSlot")     # ★ 按 id 拉，不再猜索引
        self.last_slot_spoke_index = idx
        if idx is None:
            return None, None, ""
        self.last_slot_page = txt
        self.last_slot_kind = ("时段页" if '"timeSlot"' in txt else
                              ("联系人页" if '"pickupContact"' in txt else
                               ("费率/其他页" if txt else "空响应")))
        if idx is None or self.spoke_page_id(txt) != "timeSlot":
            return None, None, txt
        day, slot = self._slot_from_text(txt, slot_index)
        return day, slot, txt

    def save_slot(self, day: dict, slot: dict) -> bool:
        body = urllib.parse.urlencode({
            "checkout.timeSlot.selectedMethod": "INSTORE", "checkout.timeSlot.date": day.get("date"),
            "checkout.timeSlot.timeZone": day.get("timeZone") or "Asia/Shanghai",
            "checkout.timeSlot.startTime": slot.get("startTime"), "checkout.timeSlot.endTime": slot.get("endTime"),
            "checkout.timeSlot.reservationId": slot.get("reservationId"),
            "checkout.timeSlot.timeSlotId": slot.get("timeSlotId"),
            "checkout.timeSlot.selectedTimeSlot": slot.get("id"),
            "checkout.timeSlot.recommendedTimeSlot": "false"})
        r = self.c.post(BASE + "/v2/checkoutx?_a=saveUrl&_m=checkout.timeSlot", body)
        t = (r.text or "") if r is not None else ""
        self.note(t)
        self.refresh_spokes(t)          # ★ 每填一格，服务端会重排 spokes → 刷新
        self._raise_if_store_gone(t, include_quote=False)
        if r is None or r.status_code != 200:
            raise RuntimeError("存时段失败（HTTP %s，%d 字节）"
                               % (r.status_code if r is not None else 0, len(t)))
        self.last_saved_day, self.last_saved_slot = day, slot   # ★ 成单日志/邮件要用
        return True

    # ------------------------------------------------------------------ ④.6 / ⑤ / ⑥
    def write_invoice(self, email: str = "") -> bool:
        # 发票页必须由当前 spokes 决定，不能猜固定 index；页面返回后再提交服务端当前表单。
        if "invoice" in (getattr(self, "last_spokes", {}) or {}):
            idx, page = self.load_spoke_by_id("invoice")
            if idx is None:
                raise RuntimeError("当前 invoice spoke 定位失败，跳过发票写入")
        r0 = self.c.post(BASE + "/v2/checkoutx?_a=selectInvoiceOptionUrl&_m=checkout.invoice",
                         "checkout.invoice.invoiceOptionsAvailableForm=true&checkout.invoice.invoiceOptionSelection=e_personal_fdf")
        if r0 is None or r0.status_code != 200:
            raise RuntimeError("打开发票页失败（HTTP %s）" % (r0.status_code if r0 is not None else 0))
        r = self.c.post(BASE + "/v2/checkoutx?_a=saveUrl&_m=checkout.invoice",
                        "checkout.invoice.selectedInvoiceOption.mobileAddress.addressData.emailAddress=" +
                        urllib.parse.quote(email or "") +
                        "&checkout.invoice.selectedInvoiceOption.taxData.invoiceHeader="
                        "&checkout.invoice.invoiceOptionsAvailableForm=true")
        t = (r.text or "") if r is not None else ""
        self.refresh_spokes(t)
        self._raise_if_store_gone(t, include_quote=False)
        if r is None or r.status_code != 200:
            raise RuntimeError("存发票失败（HTTP %s，%d 字节）"
                               % (r.status_code if r is not None else 0, len(t)))
        return True

    @staticmethod
    def contact_body(contact: dict, last4: str) -> str:
        return urllib.parse.urlencode({
            "checkout.pickupContact.mobileAddress.addressData.lastName": contact.get("lastName", ""),
            "checkout.pickupContact.mobileAddress.addressData.firstName": contact.get("firstName", ""),
            "checkout.pickupContact.mobileAddress.addressData.emailAddress": contact.get("email", ""),
            "checkout.pickupContact.mobileAddress.addressData.fullDaytimePhone": contact.get("phone", ""),
            "checkout.pickupContact.mobileAddress.addressData.keyPath": "checkout.pickupContact.taxFields",
            "checkout.pickupContact.taxFields.nationalId": last4,
            "checkout.pickupContact.pickupContactTerms.terms-accept": "true"})

    def write_contact(self, contact: dict, last4: str, need_get: bool = True,
                      verify_complete: bool = True) -> int:
        """★ 那个 GET 是必须的（把联系人表单装载进会话）；返回 POST 的响应字节数（≈36390 = 健康）

        need_get=False：提速用，省掉这一发（≈0.5s）。
        ★ 安全网：POST 回来后若响应里 3pp 仍是【未完成】→ 抛 FastFillMiss，
          由上层立刻回退成标准 GET+POST（联系人绝不会静默丢）。
        """
        if need_get:
            idx, page = self.load_spoke_by_id("3pp")
            if idx is None or self.spoke_page_id(page) != "3pp":
                raise RuntimeError("联系人页定位失败，已阻止保存")
        r = self.c.post(BASE + "/v2/checkoutx?_a=saveThirdPartyPickupUrl&_m=checkout.pickupContact",
                        self.contact_body(contact, last4))
        t = (r.text or "") if r is not None else ""
        self.note(t)
        self.refresh_spokes(t)
        self._raise_if_store_gone(t, include_quote=False)
        # ★★ 必须检查状态码！实测（0916 00:01）：手机号填成中文时苹果直接回 HTTP 500
        #    + 一整页 HTML 错误页（10663 字节）。以前不看状态码 → 以为存进去了 →
        #    提交被弹回 5 次才报个含糊的"连试5次"。
        if r is None or r.status_code != 200:
            _msg = ("存联系人失败（HTTP %s，%d 字节）→ 重点查【手机号/邮箱】格式"
                    % (r.status_code if r is not None else 0, len(t)))
            # ★ 快速填格（省了前置 GET）被服务端拒 → 不判死，交给上层补 GET+POST 再来一次
            if not need_get:
                raise FastFillMiss(_msg)
            raise RuntimeError(_msg)
        if not need_get and verify_complete:
            sp = (getattr(self, "last_spokes", None) or {}).get("3pp") or {}
            if sp and not sp.get("complete"):
                raise FastFillMiss("响应里 3pp 仍是未完成（%d 字节）" % len(t))
        return len(t)

    def write_billing(self, need_get: bool = True) -> bool:
        # ★★ 付款【必须】前置 GET —— 实测（0916 00:28）去掉之后连续两单都弹回要付款。
        #    我一度以为不需要（老抓包里无 GET 也 200），但那是【联系人还没写】的情况：
        #    一旦写过联系人，付款这一格就必须重新 GET 装载，否则不落库。
        #    老流程之所以"看起来不需要"，是因为它最后总弹回一次付款、补付款时才有 GET ——
        #    那次弹回就是这么来的。省 0.69s 换来 1.76s 的弹回，不划算。
        if need_get:
            idx, page = self.load_spoke_by_id("billing")
            if idx is None or self.spoke_page_id(page) != "billing":
                raise RuntimeError("付款页定位失败，已阻止保存")
        r = self.c.post(BASE + "/checkoutx/billing?_a=selectBillingOptionUrl&_m=checkout.billing",
                        "checkout.billing.paymentOptionsAvailableForm=true&checkout.billing.paymentTypeCategory=WECHAT")
        t = (r.text or "") if r is not None else ""
        self.note(t)
        self.refresh_spokes(t)
        self._raise_if_store_gone(t, include_quote=False)
        if r is None or r.status_code != 200:
            raise RuntimeError("存付款失败（HTTP %s，%d 字节）"
                               % (r.status_code if r is not None else 0, len(t)))
        return True

    # ------------------------------------------------------------------ ⑦ 提交
    @staticmethod
    def terms_body() -> str:
        return "checkout.review.terms.sections.0.termsAccepted=1"

    def _pbody(self) -> str:
        self.last_terms_submitted = True
        return ("mSignCookies=&masConfigKey=placeOrder&sessionID=" + self.sid +
                "&mSignParameters=sessionID&" + self.terms_body() +
                "&checkout.review.transactions.openid=" + (self.openid or "") +
                "&checkout.review.transactions.tzos=-7&checkout.review.transactions.udid=")

    @staticmethod
    def _next_url(r, txt: str) -> Tuple[str, bool]:
        """★ 服务端的"跳转"有两种：HTTP 302 头；或者 body 里 head.data.{status:302,url}
        （真机是小程序 SPA，很多跳转是后者，只认 HTTP 头会把成功当失败）"""
        try:
            loc = (r.headers.get("Location") or "") if r is not None else ""
        except Exception:
            loc = ""
        if loc:
            return loc, True
        try:
            j = json.loads(txt or "")
            d = ((j.get("head") or {}).get("data") or {})
            if str(d.get("status")) in ("301", "302", "303", "307") and d.get("url"):
                return d["url"], True
        except Exception:
            pass
        return "", False

    def _nap(self, secs: float) -> bool:
        """可被"点停止"立刻打断的等待；返回 True = 已经要求停止"""
        ev = getattr(self, "stop_evt", None) or getattr(getattr(self, "c", None), "stop_evt", None)
        if ev is not None:
            try:
                return bool(ev.wait(max(0.0, float(secs))))
            except Exception:
                pass
        time.sleep(max(0.0, float(secs)))
        return False

    def confirm_landed(self) -> Tuple[bool, str]:
        """落库确认（★ 提交后查订单状态）：
           每 status_poll_sec 秒查一次，最多 status_poll_max 次（默认 3 秒 × 20 次）。
           必须走到 thankyou（或正文里出现订单号）才算真建单。"""
        p = self._pbody()
        nxt = ""
        tries = max(1, int(getattr(self, "status_poll_max", 20) or 20))
        gap = float(getattr(self, "status_poll_sec", 3.0) or 3.0)
        t0 = time.time()
        saw_empty_slot = False

        for i in range(tries):
            if i % 5 == 4 and self.log:     # 每 5 发报一次进度，不刷屏
                try:
                    self.log("提交后查订单状态：第 %d/%d 次（已等 %.0f 秒）" % (i + 1, tries, time.time() - t0))
                except Exception:
                    pass
            r = self.c.get(nxt) if nxt else self.c.post(BASE + "/v2/checkout/status", p)
            if self.dbg is not None:
                try:
                    self.dbg("status", i=i + 1, waited=round(time.time() - t0, 2),
                             code=(r.status_code if r is not None else -1),
                             loc=((r.headers.get("Location") or "")[:200] if r is not None else ""),
                             n=(len(r.text or "") if r is not None else 0),
                             resp=((r.text or "")[:4000] if r is not None else ""))
                except Exception:
                    pass
            if r is None:
                ev = getattr(self, "stop_evt", None) or getattr(self.c, "stop_evt", None)
                if ev is not None and ev.is_set():
                    return False, "已停止"
                # status 是幂等查询；代理瞬断或连接复位只影响这一发，不能据此
                # 把已经被 placeOrder 受理的整单判成失败。
                nxt = ""
                if i + 1 >= tries:
                    return False, "status 请求连续失败（已重试 %d 次）" % tries
                if self._nap(gap):
                    return False, "已停止"
                continue
            txt = r.text or ""
            self.refresh_spokes(txt)
            if _store_gone(txt):
                return False, "门店取不了（被抢了）"
            # 感谢页/订单号是最高优先级的成功信号。页面可能同时带着
            # timeSlot 摘要，所以必须在“空时段”判断前先看是否已经成单。
            url, isredir = self._next_url(r, txt)
            blob = (url or "") + " " + txt[:4000]
            if "thankyou" in blob:
                self.landed_text = txt
                return True, "已到感谢页"
            if "quantity_limit_exceeded" in blob:
                return False, "QUOTA"
            m = re.search(r"(W\d{9,})", blob)
            if m:
                self.landed_text = txt
                self.landed_order = m.group(1)
                return True, "已看到订单号"
            if '"timeSlot"' in txt and '"daySlots"' in txt:
                slot_day, slot_choice = self._slot_from_text(txt)
                if not slot_choice:
                    # placeOrder 已经被受理后，status 中途出现空时段页不能立即
                    # 当成整单失败。继续轮询到感谢页/订单号，也避免 worker
                    # 释放额度后马上重复下单。
                    saw_empty_slot = True
                    nxt = ""
                    if self._nap(gap):
                        return False, "已停止"
                    continue
                try:
                    self.save_slot(slot_day, slot_choice)
                except StoreUnavailable:
                    return False, "门店取不了（被抢了）"
                nxt = ""
                time.sleep(0.3)
                continue
            if '"view"' in txt and "mmap-crd-review" in txt:
                if self.last_terms_accepted is False:
                    return False, "服务端退回结算页：条款未接受"
                return False, "服务端退回结算页：条款已提交但交易未落库"
            if isredir and url:
                nxt = url if url.startswith("http") else BASE + url
                # ★★ 中间态（422 字节"还在处理"）：等一个轮询间隔再继续；
                #    以前是 0.6 秒连打，现在按配置的间隔来（默认 3 秒），既省请求也不容易 541
                if self._nap(gap):
                    return False, "已停止"
                continue
            nxt = ""
            if self._nap(gap):
                return False, "已停止"
        suffix = "；期间状态页出现过空时段，已继续跑完确认" if saw_empty_slot else ""
        return False, "未确认（等了 %.0f 秒 / 试了 %d 次：始终没翻出感谢页%s）" % \
            (time.time() - t0, tries, suffix)

    def submit(self, contact: dict, last4: str, retry: int = 5,
               on_bounce: Optional[Callable[[int], None]] = None,
               require_slot: bool = False) -> Tuple[bool, str, dict]:
        """提交订单。★ 被弹回时用【当前 contact】重填 —— 这就是"换成正确取货人"的时机。

        require_slot=True 时：弹回页里的时段根本没得选 → 不再补打一轮，直接当没货。

        返回 (是否成功, 说明, 明细)"""
        p = self._pbody()
        bounces = 0
        empty_slot_retry_done = False
        t_submit = time.perf_counter()
        detail = {"bounces": 0, "needs": [], "order": "", "ack_sec": None,
                  "land_sec": None, "total_sec": None, "empty_slot": False}
        for att in range(1, max(1, retry) + 1):
            r = self.c.post(BASE + "/v2/checkoutx?_a=placeOrderUrl&_m=checkout.review.transactions", p)
            if r is None:
                return False, "网络异常", detail
            txt = r.text or ""
            loc = r.headers.get("Location") or ""
            _u, _isr = self._next_url(r, txt)
            if (r.status_code in (301, 302, 303, 307) and "status" in loc) or \
               (_isr and ("status" in _u or "spinner" in _u or "checkout" in _u)):
                detail["ack_sec"] = round(time.perf_counter() - t_submit, 3)
                t_land = time.perf_counter()
                ok, why = self.confirm_landed()
                detail["land_sec"] = round(time.perf_counter() - t_land, 3)
                detail["total_sec"] = round(time.perf_counter() - t_submit, 3)
                if ok:
                    m = (re.search(r"(W\d{9,})", (getattr(self, "landed_text", "") or "") + txt + loc)
                         or re.search(r"orderNumber\"\s*:\s*\"(W\d+)\"", (getattr(self, "landed_text", "") or "")))
                    detail["order"] = m.group(1) if m else ""
                    detail["link"] = self._order_link(getattr(self, "landed_text", ""),
                                                      detail["order"], contact.get("email", ""))
                    detail["bounces"] = bounces
                    return True, "下单成功", detail
                detail["bounces"] = bounces
                if why == "QUOTA":
                    return False, "QUOTA", detail
                return False, why, detail
            if ("quantity_limit_exceeded" in txt or "超出单件购买数量限制" in txt
                    or ("最多可购买" in txt and "iPhone" in txt)):
                detail["bounces"] = bounces
                detail["last"] = txt[:3000]
                detail["quota_msg"] = self._alerts(txt) or "（没抓到文字，但有 quantity_limit_exceeded）"
                return False, "QUOTA", detail
            # ★★ 实名验证必须【精确】判断 —— 之前用 "realIdVerification" 子串匹配，
            #    结果把普通页面里的翻译标签 "realIdVerificationLabel":"身份验证" 也当成了实名，
            #    误报成"该号需要实名验证"，还把真正的 "门店取不了" 判断给短路了。
            #    只有这三种才是真·实名页：
            #      "view":"mmap-realid-verification"  或  authRequired + idVerificationType
            if (re.search(r'"view"\s*:\s*"mmap-realid-verification"', txt)
                    or ('"authRequired"' in txt and '"idVerificationType"' in txt)):
                detail["bounces"] = bounces
                detail["last"] = txt[:3000]
                return False, "该号需要实名验证（苹果要求手机号短信验证，工具做不了）", detail
            if _store_gone(txt):
                detail["bounces"] = bounces
                detail["last"] = txt[:3000]
                return False, "门店取不了（被抢了）", detail
            slot_day = slot_choice = None
            slot_page = ('"timeSlot"' in txt and ('"daySlots"' in txt or '"timeSlots"' in txt))
            if slot_page:
                slot_day, slot_choice = self._slot_from_text(txt)
            need = []
            if slot_page:
                need.append("时段")
            if "INVOICE_SELECTION" in txt:
                need.append("发票")
            if "pickupContactTerms" in txt or '"terms-accept":false' in txt:
                need.append("联系人")
            if "checkout_payment" in txt or "请选择支付方式" in txt:
                need.append("付款")
            # GET-only 预热测试的兜底：有些弹回页不带旧关键词，只在 spokes
            # 中把对应格标成未完成。照服务端状态补写，避免一次试验直接丢单。
            bounced_spokes = self.parse_spokes(txt)
            bounced_states = self.parse_checkout_states(txt)
            if (bounced_states.get("pickupContactReady") is False or
                    ((bounced_states.get("pickupContactReady") is None) and
                     (bounced_spokes.get("3pp") or {}).get("complete") is False)) and "联系人" not in need:
                need.append("联系人")
            if (bounced_states.get("isPaymentReady") is False or
                    ((bounced_states.get("isPaymentReady") is None) and
                     (bounced_spokes.get("billing") or {}).get("complete") is False)) and "付款" not in need:
                need.append("付款")
            if bounced_states.get("invoiceReady") is False and "发票" not in need:
                need.append("发票")
            if not need:
                detail["bounces"] = bounces
                detail["last"] = txt[:3000]      # ★ 诊断：把服务端这一页留下来
                al = re.search(r'"alerts"\s*:\s*\["([^"]{2,90})"\]', txt)
                return False, "提交未受理（HTTP %s%s）" % (r.status_code, ("：" + al.group(1)) if al else ""), detail
            bounces += 1
            detail["needs"] = need
            if on_bounce:
                on_bounce(bounces)
            self.log("   （第 %d 次：服务端要 %s → 一并补上）" % (att, "+".join(need)))
            stop_after_fill = False
            slot_missing_now = False
            try:
                if "时段" in need:
                    if not slot_choice:
                        slot_missing_now = True
                        detail["empty_slot"] = True
                        if require_slot:
                            # ★ 开关=开：服务端要求补时段但页里没得选 → 当没货，不再补打
                            detail["bounces"] = bounces
                            return False, "没时段（已按【需要时段】判定为没货）", detail
                        if empty_slot_retry_done:
                            stop_after_fill = True
                            self.log("   （时段仍为空：先补完本页其它格，然后结束本次尝试）")
                        else:
                            empty_slot_retry_done = True
                            self.log("   （时段为空：不提前返回，继续补联系人/付款/发票并再提交一次）")
                    else:
                        self.save_slot(slot_day, slot_choice)
            except StoreUnavailable:
                detail["bounces"] = bounces
                return False, "门店取不了（被抢了）", detail
            for _label, _fill in (
                    ("发票", lambda: self.write_invoice(contact.get("email", ""))),
                    ("联系人", lambda: self.write_contact(contact, last4)),
                    ("付款", self.write_billing)):
                if _label not in need:
                    continue
                try:
                    _fill()
                except StoreUnavailable:
                    if not slot_missing_now:
                        detail["bounces"] = bounces
                        return False, "门店取不了（被抢了）", detail
                    self.log("   （空时段兜底：%s 被服务端拒绝，仍继续完整提交）" % _label)
                except Exception as e:
                    if not slot_missing_now:
                        raise
                    self.log("   （空时段兜底：%s 保存未成（%s），仍继续完整提交）"
                             % (_label, e))
            if stop_after_fill:
                detail["bounces"] = bounces
                return False, "没时段/时段页里没有可选的时段（已补齐其它信息并完整提交）", detail
            p = self._pbody()
        detail["bounces"] = bounces
        return False, "连试 %d 次都没成功" % retry, detail
