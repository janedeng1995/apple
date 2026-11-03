# -*- coding: utf-8 -*-
"""在线账号自动登录（对接本地微信协议服务）。

链路：
    1) GET  {service}/accounts                 -> 在线账号列表
    2) POST {service}/wx/getCode {openId,app_id} -> 由协议服务产出 wx.login 的 code
    3) GET  小程序 bootstrap init               -> 取 diwaFetchURL / openIdFetchURL
    4) GET  {diwaFetchURL}?code=<code>          -> 用 code 兑换小程序会话 Cookie（as_dima 等）
    5) GET  {openIdFetchURL}                    -> 校验并拿小程序内 openID

只依赖标准库；产出的 Cookie 直接写成 AppleMonitorPro 可用的“身份文件”。
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request

# 本地微信协议账号管理平台
DEFAULT_SERVICE = os.environ.get("APPLE_ACCOUNT_SERVICE", "http://127.0.0.1:5050")
# 目标小程序（Apple 官方）
DEFAULT_APP_ID = "wxbc25a7df24ae0e6a"
DEFAULT_INIT = "https://web.mmap.apple.com/miniprogram-merch/p/wechat/init"
DEFAULT_VERSION = "26.07.0-MAIN"
# 与 client.UA_ANDROID 一致的微信小程序 UA
DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 16; PJD110 Build/BP2A.250605.015; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 Mobile Safari/537.36 "
    "XWEB/1500117 MMWEBSDK/20260604 MMWEBID/9105 MicroMessenger/8.0.77.3160(0x28004D30) "
    "WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)
_ONLINE_STATES = {"alive", "online", "ok", "1", "true", "正常", "在线"}


class LoginError(RuntimeError):
    pass


def _headers(token=None, extra=None):
    h = {"content-type": "application/json", "accept": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if extra:
        h.update(extra)
    return h


def _service_call(base, method, path, body=None, token=None, timeout=15):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(token), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(raw)
        except ValueError:
            j = {}
        raise LoginError("HTTP %s %s：%s" % (e.code, path, j.get("msg") or raw[:160]))
    try:
        j = json.loads(raw)
    except ValueError:
        raise LoginError("服务返回非 JSON：%s" % raw[:160])
    if isinstance(j, dict) and j.get("code", 0) not in (0, None):
        raise LoginError("%s -> %s" % (path, j.get("msg") or j))
    return j


def list_online_accounts(base=DEFAULT_SERVICE, channel=None, token=None, timeout=15):
    """返回在线账号列表（status 为 alive/online 等）。channel 为 None 表示不过滤渠道。"""
    data = _service_call(base, "GET", "/accounts", token=token, timeout=timeout).get("data", []) or []
    out = []
    for a in data:
        if str(a.get("status", "")).lower() not in _ONLINE_STATES:
            continue
        if channel and (a.get("channel") or channel) != channel:
            continue
        out.append(a)
    return out


def get_wx_code(base, openid, app_id, token=None, timeout=25):
    """调用协议服务产出 wx.login 的 code。返回 (code, data)。"""
    data = _service_call(base, "POST", "/wx/getCode",
                         {"openId": openid, "app_id": app_id}, token=token, timeout=timeout).get("data") or {}
    code = (data.get("getCode") or {}).get("code")
    if not code:
        raise LoginError("getCode 未返回 code：%s" % json.dumps(data, ensure_ascii=False)[:200])
    return code, data


def _http_get(opener, url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def fetch_cookie_for_account(openid, base=DEFAULT_SERVICE, app_id=DEFAULT_APP_ID,
                             init_url=DEFAULT_INIT, version=DEFAULT_VERSION,
                             token=None, ua=DEFAULT_UA, timeout=20):
    """用在线账号产出小程序 Cookie。返回 {cookie, cookies, code, mini_openid, ...}。"""
    code, code_data = get_wx_code(base, openid, app_id, token=token, timeout=timeout)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    st, body = _http_get(opener, init_url,
                         {"x-ma-version": version, "content-type": "application/json", "User-Agent": ua},
                         timeout=timeout)
    try:
        cfg = json.loads(body)
    except ValueError:
        raise LoginError("bootstrap init 非 JSON（HTTP %s）：%s" % (st, body[:160]))
    urls = cfg.get("urls") or {}
    diwa_url = urls.get("diwaFetchURL")
    openid_url = urls.get("openIdFetchURL")
    if not diwa_url:
        raise LoginError("bootstrap init 缺少 diwaFetchURL")

    fetch_url = diwa_url + ("&" if "?" in diwa_url else "?") + "code=" + urllib.parse.quote(code)
    st2, body2 = _http_get(opener, fetch_url,
                           {"content-type": "application/json", "Referer": init_url, "User-Agent": ua},
                           timeout=timeout)
    if st2 >= 400:
        raise LoginError("diwaFetch 失败（HTTP %s）：%s" % (st2, body2[:160]))

    mini_openid = None
    if openid_url:
        _, b3 = _http_get(opener, openid_url, {"Referer": init_url, "User-Agent": ua}, timeout=timeout)
        try:
            mini_openid = json.loads(b3).get("openID")
        except ValueError:
            mini_openid = None

    cookies = {c.name: c.value for c in jar}
    if not cookies:
        raise LoginError("未拿到任何 Cookie（diwaFetch 返回：%s）" % body2[:160])
    header = "; ".join("%s=%s" % (k, v) for k, v in cookies.items()) + ";"
    return {
        "cookie": header,
        "cookies": cookies,
        "code": code,
        "mini_openid": mini_openid,
        "diwa_cookie_name": cfg.get("diwaCookieName", ""),
        "uin": code_data.get("uin"),
    }


def write_identity(path, cookie, ua=""):
    """把 Cookie 写成身份文件。ua 为空 -> 写纯 cookie（worker 用默认手机档 UA）。"""
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if ua:
            json.dump({"cookie": cookie, "ua": ua}, f, ensure_ascii=False, indent=2)
        else:
            f.write(cookie)
    return path


def login_to_identity(openid, dest, base=DEFAULT_SERVICE, app_id=DEFAULT_APP_ID, token=None, ua=""):
    """一步到位：登录并写出身份文件。返回 fetch_cookie_for_account 的结果。"""
    res = fetch_cookie_for_account(openid, base=base, app_id=app_id, token=token)
    write_identity(dest, res["cookie"], ua=ua)
    res["path"] = dest
    return res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="在线账号 -> 小程序 Cookie 身份文件")
    ap.add_argument("--base", default=DEFAULT_SERVICE)
    ap.add_argument("--app-id", default=DEFAULT_APP_ID)
    ap.add_argument("--openid", default=None)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    accts = list_online_accounts(base=a.base)
    for x in accts:
        print(x.get("openid"), x.get("nickname") or x.get("alias"), x.get("uin"))
    if a.openid and a.out:
        r = login_to_identity(a.openid, a.out, base=a.base, app_id=a.app_id)
        print("OK", os.path.basename(a.out), "len=%d" % len(r["cookie"]), "openID=%s" % r["mini_openid"])