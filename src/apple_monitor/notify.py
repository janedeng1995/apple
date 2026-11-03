# -*- coding: utf-8 -*-
"""通知：响铃 + 邮件（可选）"""
from __future__ import annotations

import glob
import io
import json
import os
import re
import smtplib
import threading
import time
from email.header import Header
from email.utils import formataddr
from email.mime.text import MIMEText

from .config import HERE          # 日志目录跟着 exe/项目根目录走

_MAIL_LOCK = threading.Lock()
_last_mail_t = [0.0]


def _rate_gate(min_gap: float = 2.0):
    """★ 发信节流：两次发信至少隔 min_gap 秒（QQ 邮箱密集发信会断连接 → 就是 22:27 那批失败的原因）"""
    with _MAIL_LOCK:
        left = _last_mail_t[0] + min_gap - time.time()
        if left > 0:
            time.sleep(left)
        _last_mail_t[0] = time.time()


def beep(seconds: int = 3):
    """短提示音（命中/异常用）"""
    def _run():
        try:
            import os
            import sys
            if os.name == "nt":
                import winsound
                for _ in range(3):
                    winsound.Beep(1650, 150)
                    winsound.Beep(1650, 150)
                    winsound.Beep(1650, 220)
                    time.sleep(0.25)
            elif sys.platform == "darwin":
                for _ in range(3):
                    os.system("afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1")
            else:
                print("\a", end="", flush=True)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def alarm(seconds: int = 180):
    """起床铃：连响 seconds 秒（Windows 用系统闹钟音循环）"""
    def _run():
        try:
            import os
            import sys
            if os.name == "nt":
                import winsound
                f = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Media", "Alarm01.wav")
                if os.path.isfile(f):
                    winsound.PlaySound(f, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
                    time.sleep(seconds)
                    winsound.PlaySound(None, winsound.SND_PURGE)
                    return
                t0 = time.time()
                while time.time() - t0 < seconds:
                    winsound.Beep(2100, 350)
                    winsound.Beep(1300, 350)
            elif sys.platform == "darwin":
                t0 = time.time()
                while time.time() - t0 < seconds:
                    os.system("afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1")
            else:
                t0 = time.time()
                while time.time() - t0 < seconds:
                    print("\a", end="", flush=True)
                    time.sleep(0.3)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def send_mail(cfg: dict, subject: str, body: str, logger=None) -> str:
    """cfg = {enabled, host, port, user, pass, to:[...]}
    ★ 成功/失败都留日志；失败先重试 2 次，仍失败就把整封邮件【存盘】到
      logs\\邮件-没发出去\\<时间>.json，并在日志里写清文件路径（可用「补发没发出去的邮件」重发）"""
    if not cfg or not cfg.get("enabled"):
        if logger:
            try: logger("[邮件] 邮件未开启（不发送）")
            except Exception: pass
        return "邮件未开启"
    to = [x for x in (cfg.get("to") or []) if x]
    if not to:
        res = "发送失败：收件人是空的"
        if logger:
            try: logger("[邮件] " + res)
            except Exception: pass
        return res
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("苹果抢购助手", cfg.get("user", "")))
    msg["To"] = ", ".join(to)
    raw = msg.as_string()
    err = ""
    waits = (0.0, 3.0, 8.0)           # ★ 失败重试：立刻 → 3 秒 → 8 秒（避开 QQ 限流的瞬时窗口）
    for i, w in enumerate(waits, 1):
        if w:
            time.sleep(w)
        _rate_gate()                  # ★ 发信节流：两次发信至少隔 2 秒，别再触发限流
        try:
            s = smtplib.SMTP_SSL(cfg.get("host", "smtp.qq.com"), int(cfg.get("port", 465)), timeout=20)
            try:
                s.login(cfg.get("user", ""), cfg.get("pass", ""))
                s.sendmail(cfg.get("user", ""), to, raw)
            finally:
                try: s.quit()
                except Exception: pass
            res = "已发送到 " + ",".join(to) + ("（第 %d 次尝试成功）" % i if i > 1 else "")
            if logger:
                try: logger("[邮件] " + res)
                except Exception: pass
            return res
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
    # ★ 三次都失败 → 存盘，别丢
    try:
        d = os.path.join(HERE, "logs", "邮件-没发出去")
        os.makedirs(d, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|]', "_", subject)[:60]
        fp = os.path.join(d, "%s-%s.json" % (time.strftime("%Y%m%d-%H%M%S"), safe))
        io.open(fp, "w", encoding="utf-8").write(json.dumps(
            {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "subject": subject, "body": body,
             "to": to, "user": cfg.get("user", ""), "err": err}, ensure_ascii=False, indent=1))
        res = "❌ 邮件没发出去（重试 3 次都失败）：%s ｜ 已存盘：logs\\邮件-没发出去\\%s" % (err, os.path.basename(fp))
    except Exception as e2:
        res = "❌ 邮件没发出去：%s（存盘也失败：%s）" % (err, e2)
    if logger:
        try: logger("[邮件] " + res)
        except Exception: pass
    return res


def resend_pending(cfg: dict, logger=None) -> str:
    """把 logs\\邮件-没发出去\\ 里积压的邮件重发一遍；成功的删掉，失败的留下"""
    d = os.path.join(HERE, "logs", "邮件-没发出去")
    if not os.path.isdir(d):
        return "没有积压的邮件"
    files = sorted(glob.glob(os.path.join(d, "*.json")))
    if not files:
        return "没有积压的邮件"
    ok = bad = 0
    for f in files:
        try:
            j = json.load(io.open(f, encoding="utf-8"))
            r = send_mail(cfg, j.get("subject", "(无主题)"), j.get("body", ""), logger=logger)
            if r.startswith("已发送"):
                os.remove(f); ok += 1
            else:
                bad += 1
        except Exception as e:
            bad += 1
            if logger:
                try: logger("[邮件] 补发失败 %s：%s" % (os.path.basename(f), e))
                except Exception: pass
    return "补发完成：成功 %d 封，仍失败 %d 封（还在 logs\邮件-没发出去\）" % (ok, bad)
