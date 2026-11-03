# -*- coding: utf-8 -*-
"""入口：--gui（默认）图形界面 / --headless 命令行跑 / --dry 干跑（不提交）"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from .config import Config
from .worker import BusyGate, CookieWorker, WarmupGate

APP = "AppleMonitorPro"


def start_workers(cfg: Config, log, on_order=None, dry_run=None, auto_submit=None):
    """起所有 cookie 的 worker（并行）。返回 (workers, stop_event)"""
    errs = cfg.validate()
    if errs:
        for e in errs:
            log("[配置错误] " + e)
        return [], None
    stop = threading.Event()
    # ★★ 全局"有人正在抢单"计数标志：任何一个号进了抢单关键路径，
    #    其它号立刻【暂停探测】——把带宽/连接池/Apple 的限流窗口全让给抢单的号。
    #    （引用计数：两个号同时在抢时，先结束的那个不会把另一个的让路也解除。）
    max_parallel = max(1, int(getattr(cfg.settings, "max_parallel_buy", 2) or 2))
    busy = BusyGate(max_parallel)
    warmup_gate = WarmupGate(getattr(cfg.settings, "prewarm_interval_sec", 0.0))
    # ★★ 代理：默认「一号一池」——每个账号各用一来自己的 IP 队列（一个号被风控不影响其它号）。
    #    proxy_mode=global 时退化为旧的全局共用一个 IP。手机扫描始终直连。
    from . import proxy_pool as _pp
    enabled = [c for c in cfg.cookies if c.enabled]
    proxy_on = bool(getattr(cfg.settings, "proxy_enabled", False))
    per_account = proxy_on and str(getattr(cfg.settings, "proxy_mode", "per_account")).lower() != "global"
    pools = []
    if per_account:
        for cc in enabled:
            pl = _pp.make_pool(cfg, log, name=cc.name)
            pl.start(lazy=True)          # 轮换线程先起；IP 由下面的错峰预取挂上
            pools.append(pl)
        # ★ 启动即给每个号挂上代理 IP（错峰预取，避免同一秒打爆 API）并打日志；
        #   预取失败也不卡，provider 会在首次请求时兑底重试。
        for _i, _pl in enumerate(pools):
            def _pre(_pl=_pl, _idx=_i):
                if _idx and _pl._stop.wait(min(_idx, 12) * 0.5):
                    return
                _ips = _pl.fetch_batch(force=True)
                if _ips:
                    with _pl._lock:
                        if not _pl._cur and _pl._q:
                            _pl._cur = _pl._q.popleft()
                            _pl._t_cur = _pl._t_got
                    log("[代理%s] 已挂代理 IP：%s" % (_pl._tag(), _pl.shortcut() or "(获取中)"))
            threading.Thread(target=_pre, name="proxy-pre-%s" % (_pl.name or "?"), daemon=True).start()
        log("[代理] 一号一池：%d 个账号各自一个 IP（不改写服务商提取数、每 %d 秒前换新、遇 403/541 异步更换）"
            % (len(pools), int(getattr(cfg.settings, "proxy_rotate_sec", 60) or 60)))
    elif proxy_on:
        pl = _pp.get_pool(cfg, log)
        pl.start()
        pools.append(pl)
    if pools:
        def _stop_pools():
            stop.wait()
            for pl in pools:
                try:
                    pl.stop()
                except Exception:
                    pass
        threading.Thread(target=_stop_pools, name="proxy-stop", daemon=True).start()
    ws = []
    for i, cc in enumerate(enabled):
        prov = pools[i].provider() if per_account else (pools[0].provider() if pools else None)
        w = CookieWorker(cfg, cc, log, on_order=on_order, stop_evt=stop,
                         dry_run=dry_run, auto_submit=auto_submit, busy_evt=busy,
                         proxy_provider=prov, warmup_gate=warmup_gate)
        ws.append(w)
    for w in ws:
        w.start()
    mode_names = {"none": "首发模式（不预加购物袋）", "first": "首发模式（不预加购物袋）",
                 "cart": "只预热购物袋"}
    log("[启动] 预热模式=%s ｜ 同时抢单上限=%d"
        % (mode_names.get(getattr(cfg.settings, "warmup_mode", "cart"), "只预热购物袋"), max_parallel))
    log("[启动] 订单确认固定走 status：每 %s 秒一次，最多 %s 次"
        % (int(getattr(cfg.settings, "status_poll_sec", 3)),
           int(getattr(cfg.settings, "status_poll_max", 20))))
    log("[启动] %d 个 cookie 并行（干跑=%s ｜ 自动提交=%s）"
        % (len(ws), cfg.settings.dry_run if dry_run is None else dry_run,
           cfg.settings.auto_submit if auto_submit is None else auto_submit))
    return ws, stop


def main(argv=None):
    ap = argparse.ArgumentParser(prog=APP, description="苹果小程序抢购（工程版）")
    ap.add_argument("--config", default="", help="配置文件路径（默认同目录 config.json）")
    ap.add_argument("--gui", action="store_true", help="打开图形界面（默认）")
    ap.add_argument("--headless", action="store_true", help="命令行跑，不开界面")
    ap.add_argument("--dry", action="store_true", help="干跑：跑到提交前就停（不建单）")
    ap.add_argument("--auto-submit", action="store_true", help="自动提交（配置里也可开）")
    a = ap.parse_args(argv)

    if a.headless:
        cfg = Config.load(a.config)
        lines = []

        def log(m):
            line = "[%s] %s" % (time.strftime("%H:%M:%S"), m)
            print(line, flush=True)
            lines.append(line)
            if len(lines) > 2000:
                del lines[:1000]

        ws, stop = start_workers(cfg, log, dry_run=(True if a.dry else None),
                                 auto_submit=(True if a.auto_submit else None))
        if not ws:
            return 2
        try:
            while any(w.is_alive() for w in ws):
                time.sleep(1)
        except KeyboardInterrupt:
            stop.set()
            log("已中断")
        return 0

    from .gui import run_gui
    return run_gui(a.config)


if __name__ == "__main__":
    sys.exit(main())
