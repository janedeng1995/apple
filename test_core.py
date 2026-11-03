import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from apple_monitor.config import Config, STORE_SOURCE_BUYER, STORE_SOURCE_STORE
from apple_monitor.proxy_pool import ProxyPool
from apple_monitor.quota import Ledger
from apple_monitor.scan import parse_verdict, scan
from apple_monitor.session import CheckoutSession
from apple_monitor.worker import CookieWorker


def _buyer(skus):
    return {
        "id": "buyer-1", "name": "测试取货人", "phone": "17000000000",
        "email": "example@example.com", "last4": "0000", "want": 1,
        "skus": skus, "cities": ["北京"], "enabled": True,
    }


class ConfigTests(unittest.TestCase):
    def _config(self, skus=("MJTL4CH/A", "MJY64CH/A"), task2_sku="MJY64CH/A"):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p1, p2 = Path(td.name) / "a.txt", Path(td.name) / "b.txt"
        p1.write_text("cookie=a", encoding="utf-8")
        p2.write_text("cookie=b", encoding="utf-8")
        data = {
            "cookies": [
                {"id": "task-1", "name": "任务1", "cookie_file": str(p1),
                 "sku": "MJTL4CH/A", "buyer": "测试取货人", "enabled": True},
                {"id": "task-2", "name": "任务2", "cookie_file": str(p2),
                 "sku": task2_sku, "buyer": "测试取货人", "enabled": True},
                {"id": "task-off", "name": "禁用任务", "cookie_file": "/not/exist",
                 "sku": "", "enabled": False},
            ],
            "buyers": [_buyer(list(skus))],
            "cities": {"北京": ["R1"]},
            "settings": {"dry_run": True, "auto_submit": False},
        }
        return Config(data)

    def test_multiple_accounts_can_share_one_buyer(self):
        self.assertEqual([], self._config().validate())

    def test_bound_buyer_must_accept_task_sku(self):
        errs = self._config(skus=("MJTL4CH/A",)).validate()
        self.assertTrue(any("要抢型号" in e for e in errs))

    def test_running_worker_rebinds_latest_task_config(self):
        cfg = self._config()
        logs = []
        worker = CookieWorker(cfg, cfg.cookies[0], logs.append)
        self.addCleanup(lambda: worker._prefetch_executor.shutdown(wait=False))
        self.assertTrue(worker._refresh_runtime_config())

        cfg.raw["buyers"].append({
            "id": "buyer-2", "name": "新取货人", "phone": "17100000000",
            "email": "new@example.com", "last4": "1111", "want": 2,
            "skus": ["MJY64CH/A"], "cities": ["上海"], "enabled": True,
        })
        cfg.raw["cities"]["上海"] = ["R2"]
        cfg.raw["cookies"][0].update({
            "sku": "MJY64CH/A", "buyer": "新取货人", "per_order": 2, "want": 2,
        })
        cfg.raw["settings"].update({
            "warmup_mode": "first", "dry_run": False, "auto_submit": True,
            "status_poll_sec": 1.5, "status_poll_max": 7,
        })
        cfg.rebuild()

        self.assertTrue(worker._refresh_runtime_config())
        self.assertEqual("MJY64CH/A", worker.sku)
        self.assertEqual("新取货人", worker._bound_buyer().name)
        self.assertEqual(2, worker._order_qty())
        self.assertFalse(worker.dry_run)
        self.assertTrue(worker.auto_submit)
        self.assertEqual("first", worker.warmup_mode)
        self.assertEqual(7, worker.sess.status_poll_max)
        self.assertEqual(["R2"], worker.target_stores())
        self.assertTrue(any("热配置生效" in x for x in logs))


class LedgerTests(unittest.TestCase):
    def test_shared_buyer_reservation_prevents_double_order(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(str(Path(td) / "ledger.json"))
            tok1, n1 = led.reserve("task-1", "buyer-1", 1, 1, 1)
            tok2, n2 = led.reserve("task-2", "buyer-1", 1, 1, 1)
            self.assertTrue(tok1)
            self.assertEqual(1, n1)
            self.assertEqual(("", 0), (tok2, n2))
            led.commit(tok1, "MJTL4CH/A", "任务1", "取货人")
            self.assertEqual(1, led.task_total("task-1"))
            self.assertEqual(1, led.buyer_total("buyer-1"))


class ProxyTests(unittest.TestCase):
    def test_provider_url_is_not_rewritten(self):
        url = "https://proxy.invalid/get?number=7&token=secret"
        self.assertEqual(url, ProxyPool(url)._url_for(1))

    def test_fetch_failure_falls_back_without_format_exception(self):
        logs = []
        pool = ProxyPool("https://proxy.invalid", logger=logs.append)
        pool.fetch_tries = 1
        pool._fetch_once = lambda _n: ([], "simulated")
        self.assertEqual([], pool.fetch_batch(force=True))
        self.assertTrue(any("暂时直连" in x for x in logs))


class ScanTests(unittest.TestCase):
    def test_missing_date_is_still_checkout_candidate(self):
        raw = ('{"saveStoreEnabled":true,"availabilityInfo":"暂无日期",'
               '"encodedUpperDateString":null}')
        verdict, info = parse_verdict(raw)
        self.assertIs(verdict, True)
        self.assertTrue(info["date_missing"])

    def test_scan_collects_all_hits(self):
        def fake_quote(_cli, _sku, store, info, **_kw):
            info.update(allow=True, date="20260924", text="有货")
            return store != "R2"

        with mock.patch("apple_monitor.scan.quote_one", side_effect=fake_quote):
            hits, _detail = scan(object(), "MJTL4CH/A", ["R1", "R2", "R3"], concurrency=3,
                                 recheck=False)
        self.assertEqual({"R1", "R3"}, set(hits))


class _Response:
    def __init__(self, text, status=200, headers=None):
        self.text = text
        self.status_code = status
        self.headers = headers or {}


class _SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.stop_evt = None

    def post(self, url, body="", **_kwargs):
        self.posts.append((url, body))
        return self.responses.pop(0)

    def get(self, url, **_kwargs):
        return self.post(url, "")


class CheckoutFlowTests(unittest.TestCase):
    def test_empty_slot_bounce_still_fills_other_fields_and_retries_place_order(self):
        empty_slot = ('{"timeSlot":{"daySlots":[]},'
                      '"pickupContactTerms":true,"checkout_payment":true}')
        cli = _SequenceClient([_Response(empty_slot),
                               _Response('{"alerts":["still unavailable"]}')])
        sess = CheckoutSession(cli, "MJTL4CH/A")
        calls = []
        sess.write_contact = lambda *_args, **_kw: calls.append("联系人")
        def fail_billing(*_args, **_kw):
            calls.append("付款")
            raise RuntimeError("时段未就绪")
        sess.write_billing = fail_billing

        done, _why, detail = sess.submit({"email": "x@example.com"}, "0000")

        self.assertFalse(done)
        self.assertTrue(detail["empty_slot"])
        self.assertEqual(["联系人", "付款"], calls)
        place_orders = [url for url, _body in cli.posts if "placeOrderUrl" in url]
        self.assertEqual(2, len(place_orders))

    def test_empty_slot_during_status_poll_does_not_end_confirmation(self):
        empty_slot = '{"timeSlot":{"daySlots":[]}}'
        thankyou = ('{"head":{"data":{"status":302,"url":"/thankyou"}},'
                    '"timeSlot":{"daySlots":[]}}')
        cli = _SequenceClient([_Response(empty_slot), _Response(thankyou)])
        sess = CheckoutSession(cli, "MJTL4CH/A")
        sess.status_poll_sec = 0.001
        sess.status_poll_max = 2

        ok, why = sess.confirm_landed()

        self.assertTrue(ok)
        self.assertEqual("已到感谢页", why)
        self.assertEqual(2, len(cli.posts))



class StoreSourceTests(unittest.TestCase):
    """任务级「门店来源」：按取货人意向地址 / 按门店自定义（可多选）。"""

    def _cfg(self, task_extra=None, cities=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p1 = Path(td.name) / "a.txt"
        p1.write_text("cookie=a", encoding="utf-8")
        task = {"id": "task-1", "name": "任务1", "cookie_file": str(p1),
                "sku": "MJTL4CH/A", "buyer": "测试取货人", "enabled": True}
        task.update(task_extra or {})
        data = {
            "cookies": [task],
            "buyers": [_buyer(["MJTL4CH/A"])],
            "cities": cities or {"北京": ["R1", "R2"]},
            "settings": {"dry_run": True},
        }
        return Config(data)

    def test_default_source_is_buyer_cities(self):
        cfg = self._cfg()
        self.assertEqual(STORE_SOURCE_BUYER, cfg.cookies[0].store_source)
        self.assertEqual([], cfg.cookies[0].stores)
        self.assertEqual([], cfg.validate())

    def test_legacy_single_store_migrates_to_custom(self):
        cc = self._cfg({"store": "r678"}).cookies[0]
        self.assertEqual(STORE_SOURCE_STORE, cc.store_source)
        self.assertEqual(["R678"], cc.stores)

    def test_multi_custom_stores_normalized(self):
        cc = self._cfg({"store_source": "store",
                        "stores": [" r575 ", "R678", "r575", ""]}).cookies[0]
        self.assertEqual(STORE_SOURCE_STORE, cc.store_source)
        self.assertEqual(["R575", "R678"], cc.stores)

    def test_buyer_source_drops_leftover_stores(self):
        cc = self._cfg({"store_source": "buyer", "stores": ["R678"]}).cookies[0]
        self.assertEqual(STORE_SOURCE_BUYER, cc.store_source)
        self.assertEqual([], cc.stores)

    def test_validate_flags_empty_custom_stores(self):
        errs = self._cfg({"store_source": "store"}).validate()
        self.assertTrue(any("自定义门店" in e for e in errs), errs)

    def test_validate_flags_unknown_store_code(self):
        names = {"R1": "Apple 测试一", "R2": "Apple 测试二"}
        with mock.patch.object(Config, "_load_store_names", staticmethod(lambda: names)):
            errs = self._cfg({"store_source": "store", "stores": ["R999"]}).validate()
        self.assertTrue(any("R999" in e for e in errs), errs)

    def test_validate_accepts_known_custom_stores(self):
        names = {"R1": "Apple 测试一", "R678": "Apple 静安"}
        with mock.patch.object(Config, "_load_store_names", staticmethod(lambda: names)):
            cfg = self._cfg({"store_source": "store", "stores": ["R1", "R678"]})
            self.assertEqual([], cfg.validate())


class WorkerStoreSourceTests(unittest.TestCase):
    """worker 侧：两种门店来源的取店/放行逻辑。"""

    def _worker(self, store_source="", stores=None, store=""):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p1 = Path(td.name) / "a.txt"
        p1.write_text("cookie=a", encoding="utf-8")
        data = {
            "cookies": [{"id": "task-1", "name": "任务1", "cookie_file": str(p1),
                         "sku": "MJTL4CH/A", "buyer": "测试取货人", "enabled": True,
                         "store_source": store_source, "stores": stores or [], "store": store}],
            "buyers": [_buyer(["MJTL4CH/A"])],
            "cities": {"北京": ["R1", "R2"]},
            "settings": {"dry_run": True},
        }
        cfg = Config(data)
        w = CookieWorker(cfg, cfg.cookies[0], [].append)
        self.addCleanup(lambda: w._prefetch_executor.shutdown(wait=False))
        return w

    def test_buyer_source_scans_buyer_cities(self):
        w = self._worker()
        self.assertEqual(["R1", "R2"], w.target_stores())
        self.assertEqual("", w.store_pin())
        self.assertIsNone(w.buyer_for_store("R678"))      # 北京外的店不买

    def test_custom_multi_stores_used_verbatim(self):
        w = self._worker(STORE_SOURCE_STORE, ["R678", "R575"])
        self.assertEqual(["R678", "R575"], w.target_stores())
        self.assertEqual("", w.store_pin())               # 多家 → 不钉死，命中要换店

    def test_custom_single_store_pins(self):
        w = self._worker(STORE_SOURCE_STORE, ["R678"])
        self.assertEqual(["R678"], w.target_stores())
        self.assertEqual("R678", w.store_pin())           # 单家 → 直接建会话、命中不换店

    def test_custom_store_outside_buyer_city_still_buys(self):
        w = self._worker(STORE_SOURCE_STORE, ["R678", "R575"])
        self.assertIsNotNone(w.buyer_for_store("R678"))   # 用户点名要的店，绕过城市校验
        self.assertIsNotNone(w.buyer_for_store("R575"))
        self.assertIsNone(w.buyer_for_store("R999"))      # 列表外的店不买

    def test_legacy_store_field_still_pins(self):
        w = self._worker(store="r678")
        self.assertEqual(["R678"], w.target_stores())
        self.assertEqual("R678", w.store_pin())
        self.assertIsNotNone(w.buyer_for_store("R678"))


class GuiStoreHelperTests(unittest.TestCase):
    """GUI 行的门店来源展示/回写helper（不起窗口，只测纯函数）。"""

    @classmethod
    def setUpClass(cls):
        try:
            import apple_monitor.gui as gui  # noqa: F401
        except Exception as e:  # pragma: no cover - 无显示环境时跳过
            raise unittest.SkipTest("gui 不可导入：%s" % e)
        cls.gui = gui

    def test_source_label_roundtrip(self):
        self.assertEqual(STORE_SOURCE_STORE, self.gui._store_source_of(self.gui.STORE_SRC_STORE))
        self.assertEqual(STORE_SOURCE_BUYER, self.gui._store_source_of(self.gui.STORE_SRC_BUYER))

    def test_summary_for_buyer_source(self):
        self.assertEqual("按取货人城市", self.gui._store_summary({}, ["R678｜Apple 静安"]))

    def test_summary_for_single_and_multi_custom(self):
        items = ["R575｜Apple 武汉", "R678｜Apple 静安"]
        self.assertEqual("R678｜Apple 静安",
                         self.gui._store_summary({"store": "R678"}, items))
        self.assertEqual("R678 等 2 家",
                         self.gui._store_summary({"store_source": "store",
                                                  "stores": ["R678", "R575"]}, items))
        self.assertEqual("（未选门店）",
                         self.gui._store_summary({"store_source": "store", "stores": []}, items))


if __name__ == "__main__":
    unittest.main()


class RequireSlotAndCooldownTests(unittest.TestCase):
    """「需要时段」开关 + 门店失败固定冷却间隔。"""

    def _cfg(self, settings=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p1 = Path(td.name) / "a.txt"
        p1.write_text("cookie=a", encoding="utf-8")
        data = {
            "cookies": [{"id": "task-1", "name": "任务1", "cookie_file": str(p1),
                         "sku": "MJTL4CH/A", "buyer": "测试取货人", "enabled": True}],
            "buyers": [_buyer(["MJTL4CH/A"])],
            "cities": {"北京": ["R1", "R2"]},
            "settings": dict({"dry_run": True}, **(settings or {})),
        }
        return Config(data)

    def test_defaults_are_off_and_15s(self):
        st = self._cfg().settings
        self.assertFalse(st.require_time_slot)      # 默认不要求时段（没时段也照样提交）
        self.assertEqual(15, st.store_cool_sec)     # 默认冷却 15 秒

    def test_settings_roundtrip(self):
        cfg = self._cfg({"require_time_slot": True, "store_cool_sec": 42})
        st = cfg.settings
        self.assertTrue(st.require_time_slot)
        self.assertEqual(42, st.store_cool_sec)
        # 0 = 不冷却（用户显式选择，不应该被后台改成 15）
        self.assertEqual(0, self._cfg({"store_cool_sec": 0}).settings.store_cool_sec)
        self.assertEqual(15, self._cfg({}).settings.store_cool_sec)

    def test_cool_store_uses_fixed_interval(self):
        cfg = self._cfg({"store_cool_sec": 30})
        w = CookieWorker(cfg, cfg.cookies[0], [].append)
        self.addCleanup(lambda: w._prefetch_executor.shutdown(wait=False))
        now = time.time()
        w.settle_until.clear()
        for _ in range(4):                       # 每一轮间隔必须一样
            w._cool_store("R678", reason="测试")
        for st in w.settle_until.values():
            self.assertAlmostEqual(30.0, st - now, delta=1.0)

    def test_cool_store_respects_configured_value(self):
        cfg = self._cfg({"store_cool_sec": 5})
        w = CookieWorker(cfg, cfg.cookies[0], [].append)
        self.addCleanup(lambda: w._prefetch_executor.shutdown(wait=False))
        before = time.time()
        w._cool_store("R678")
        self.assertAlmostEqual(5.0, w.settle_until["R678"] - before, delta=1.0)

    def test_cool_store_zero_means_no_cooldown(self):
        cfg = self._cfg({"store_cool_sec": 0})
        w = CookieWorker(cfg, cfg.cookies[0], [].append)
        self.addCleanup(lambda: w._prefetch_executor.shutdown(wait=False))
        before = time.time()
        w._cool_store("R678")
        self.assertLessEqual(w.settle_until["R678"], before + 0.001)

    def test_require_slot_stops_before_any_fill_or_resubmit(self):
        empty_slot = ('{"timeSlot":{"daySlots":[]},'
                      '"pickupContactTerms":true,"checkout_payment":true}')
        cli = _SequenceClient([_Response(empty_slot),
                               _Response('{"alerts":["still unavailable"]}')])
        sess = CheckoutSession(cli, "MJTL4CH/A")
        calls = []
        sess.write_contact = lambda *_a, **_k: calls.append("联系人")
        sess.write_billing = lambda *_a, **_k: calls.append("付款")
        sess.write_invoice = lambda *_a, **_k: calls.append("发票")

        done, why, detail = sess.submit({"email": "x@example.com"}, "0000",
                                        require_slot=True)

        self.assertFalse(done)
        self.assertIn("没时段", why)
        self.assertTrue(detail["empty_slot"])
        self.assertEqual([], calls)                       # 一格都不补
        self.assertEqual(1, len([u for u, _ in cli.posts if "placeOrderUrl" in u]))

    def test_without_require_slot_keeps_full_submit(self):
        """开关关着时保持老行为：补完其它格再提交一次。"""
        empty_slot = ('{"timeSlot":{"daySlots":[]},'
                      '"pickupContactTerms":true,"checkout_payment":true}')
        cli = _SequenceClient([_Response(empty_slot),
                               _Response('{"alerts":["still unavailable"]}')])
        sess = CheckoutSession(cli, "MJTL4CH/A")
        calls = []
        sess.write_contact = lambda *_a, **_k: calls.append("联系人")
        sess.write_billing = lambda *_a, **_k: calls.append("付款")

        done, _why, _detail = sess.submit({"email": "x@example.com"}, "0000")

        self.assertFalse(done)
        self.assertEqual(["联系人", "付款"], calls)
        self.assertEqual(2, len([u for u, _ in cli.posts if "placeOrderUrl" in u]))


class _FakeSess:
    """只覆盖 worker 用到的部分，用于验证占位店/退回逻辑。"""

    def __init__(self, store="", sid="sid-abc", spokes=None):
        self.store = store
        self.sid = sid
        self.last_spokes = spokes if spokes is not None else {}
        self.last_checkout_states = {}
        self.switched_to = []
        self.fail_switch = False
        self.expired = False
        self.dbg = None
        self.lids = ["lid-1"]
        self.qty = 1

    def prepare_cart(self, _qty=1):
        return True

    def switch_store(self, store, _name=""):
        if self.fail_switch:
            raise RuntimeError("selectStoreUrl 未受理")
        self.switched_to.append(store)
        self.store = store
        return "ok"


class PrewarmStoreTests(unittest.TestCase):
    """固定/自定义门店也要用占位店预热，失败后退回占位店。"""

    def _worker(self, store_source="", stores=None, store="", warmup_store="R390"):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p1 = Path(td.name) / "a.txt"
        p1.write_text("cookie=a", encoding="utf-8")
        data = {
            "cookies": [{"id": "task-1", "name": "任务1", "cookie_file": str(p1),
                         "sku": "MJTL4CH/A", "buyer": "测试取货人", "enabled": True,
                         "store_source": store_source, "stores": stores or [], "store": store}],
            "buyers": [_buyer(["MJTL4CH/A"])],
            "cities": {"北京": ["R1", "R2"]},
            "settings": {"dry_run": True, "warmup_store": warmup_store},
        }
        cfg = Config(data)
        w = CookieWorker(cfg, cfg.cookies[0], [].append)
        self.addCleanup(lambda: w._prefetch_executor.shutdown(wait=False))
        return w

    def test_fixed_store_prewarms_on_placeholder_not_target(self):
        w = self._worker(STORE_SOURCE_STORE, ["R678"])
        self.assertEqual("R678", w.store_pin())
        self.assertEqual("R390", w._prewarm_store())     # 占位店，不是目标店

    def test_multi_custom_stores_avoid_all_targets(self):
        w = self._worker(STORE_SOURCE_STORE, ["R678", "R575"])
        ph = w._prewarm_store()
        self.assertNotIn(ph, ("R678", "R575"))
        self.assertEqual("R390", ph)

    def test_buyer_cities_mode_keeps_old_placeholder(self):
        w = self._worker()
        self.assertEqual("R390", w._prewarm_store())     # 旧行为不变

    def test_placeholder_skips_store_equal_to_target(self):
        w = self._worker(STORE_SOURCE_STORE, ["R678"], warmup_store="R678")
        # warmup_store 必不能等于目标店，否则又回到旧 bug
        self.assertEqual("R1", w._prewarm_store())       # 取货人城市里第一家

    def test_restore_switches_back_to_placeholder(self):
        w = self._worker(STORE_SOURCE_STORE, ["R678"])
        w.sess = _FakeSess(store="R678")
        w._restore_prewarm_store()
        self.assertEqual(["R390"], w.sess.switched_to)   # 退回占位店
        self.assertEqual("R390", w.sess.store)

    def test_restore_noop_when_already_on_placeholder(self):
        w = self._worker(STORE_SOURCE_STORE, ["R678"])
        w.sess = _FakeSess(store="R390")
        w._restore_prewarm_store()
        self.assertEqual([], w.sess.switched_to)         # 一发都不多发

    def test_restore_destroys_session_when_switch_fails(self):
        w = self._worker(STORE_SOURCE_STORE, ["R678"])
        w.sess = _FakeSess(store="R678")
        w.sess.fail_switch = True
        w._full_ready = True
        w._restore_prewarm_store()
        self.assertEqual("", w.sess.sid)                 # 退不过去就销毁，下一轮重建
        self.assertFalse(w._full_ready)


class FixedStoreHitPathTests(unittest.TestCase):
    """固定门店命中时必须走换店，才能拿到目标店最新的 spokes（时段格）。"""

    def _worker(self, stores):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p1 = Path(td.name) / "a.txt"
        p1.write_text("cookie=a", encoding="utf-8")
        data = {
            "cookies": [{"id": "task-1", "name": "任务1", "cookie_file": str(p1),
                         "sku": "MJTL4CH/A", "buyer": "测试取货人", "enabled": True,
                         "store_source": STORE_SOURCE_STORE, "stores": stores}],
            "buyers": [_buyer(["MJTL4CH/A"])],
            "cities": {"北京": ["R1", "R2"]},
            "settings": {"dry_run": True, "warmup_store": "R390"},
        }
        cfg = Config(data)
        w = CookieWorker(cfg, cfg.cookies[0], [].append)
        self.addCleanup(lambda: w._prefetch_executor.shutdown(wait=False))
        w._full_ready = True
        w.dry_run = True                       # 干跑：填完格就马上停，不发 placeOrder
        w._hit_jsonl = lambda rec: None        # 不写事件文件
        return w

    def test_hit_switches_from_placeholder_to_target(self):
        w = self._worker(["R678"])
        fake = _FakeSess(store="R390")          # 预热建在占位店
        w.sess = fake
        w.try_buy("R678")
        self.assertEqual(["R678"], fake.switched_to)   # 命中切到目标店，拿到新 spokes
        self.assertEqual("R678", fake.store)

    def test_stale_session_on_target_refreshes_spokes(self):
        """会话残留在目标店、spokes 里没有 timeSlot → 必须补选店刷新，不能直接复用。"""
        w = self._worker(["R678"])
        fake = _FakeSess(store="R678")          # 上次失败后没退回来
        w.sess = fake
        w.try_buy("R678")
        self.assertIn("R678", fake.switched_to)  # 自愂：补了一发 selectStoreUrl


class BugFixRegressionTests(unittest.TestCase):
    """两个修好的 bug 的回归测试。"""

    def _worker(self, stores):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p1 = Path(td.name) / "a.txt"
        p1.write_text("cookie=a", encoding="utf-8")
        data = {
            "cookies": [{"id": "task-1", "name": "任务1", "cookie_file": str(p1),
                         "sku": "MJTL4CH/A", "buyer": "测试取货人", "enabled": True,
                         "store_source": STORE_SOURCE_STORE, "stores": stores}],
            "buyers": [_buyer(["MJTL4CH/A"])],
            "cities": {"北京": ["R1", "R2"]},
            "settings": {"dry_run": True, "warmup_store": "R390"},
        }
        cfg = Config(data)
        w = CookieWorker(cfg, cfg.cookies[0], [].append)
        self.addCleanup(lambda: w._prefetch_executor.shutdown(wait=False))
        return w

    def test_restore_skips_when_another_account_is_buying(self):
        """别的号正在抢单时，退回占位店那一发必须让路（否则打扰到关键窗口）。"""
        w = self._worker(["R678"])
        w.sess = _FakeSess(store="R678")
        w.busy.set()                       # 占位入口（默认上限 1）→ 已满
        w._restore_prewarm_store()
        self.assertEqual([], w.sess.switched_to)     # 一发都不发
        self.assertEqual("R678", w.sess.store)       # 会话原样留在目标店

    def test_restore_still_runs_when_nobody_is_buying(self):
        w = self._worker(["R678"])
        w.sess = _FakeSess(store="R678")
        w._restore_prewarm_store()
        self.assertEqual(["R390"], w.sess.switched_to)

    def test_multipick_restores_to_readonly_not_editable(self):
        """置灰再恢复后必须回到 readonly，不能变成可编辑（否则用户打字不生效会被误导）。"""
        import tkinter as tk
        import ttkbootstrap as tb
        from apple_monitor.gui import MultiPick
        root = tb.Window()
        self.addCleanup(root.destroy)
        mp = MultiPick(tb.Frame(root), ["R678｜Apple 静安", "R575｜Apple 武汉"], [])
        mp.set_enabled(False)
        self.assertEqual("disabled", str(mp.entry.cget("state")))
        mp.set_enabled(True)
        self.assertEqual("readonly", str(mp.entry.cget("state")))
