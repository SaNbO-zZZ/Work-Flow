"""B 站扫码登录：只存 cookie（Netscape 格式，0600），供 yt-dlp 下载高清/会员可见素材；不收账号密码。"""
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import segno

from . import config
from .db import now

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV = "https://api.bilibili.com/x/web-interface/nav"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com"}
TIMEOUT = 20
POLL_STATES = {0: "ok", 86101: "waiting", 86090: "scanned", 86038: "expired"}
LOGIN_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")
BILI_HOSTS = ("bilibili.com", "b23.tv")


def account_file() -> Path:
    return config.BILI_COOKIES.parent / "bilibili_account.json"


async def _get(url: str, client: httpx.AsyncClient | None, **kw) -> httpx.Response:
    headers = {**HEADERS, **kw.pop("headers", {})}
    # 走系统代理时 httpx 自身超时可能不生效，外层再兜一层
    async with asyncio.timeout(TIMEOUT):
        if client:
            r = await client.get(url, headers=headers, **kw)
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as c:
                r = await c.get(url, headers=headers, **kw)
    r.raise_for_status()
    return r


def _data(r: httpx.Response) -> dict:
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"B 站接口返回错误 {body.get('code')}：{body.get('message')}")
    return body.get("data") or {}


async def qr_start(*, client: httpx.AsyncClient | None = None) -> dict:
    data = _data(await _get(QR_GENERATE, client))
    url, key = data["url"], data["qrcode_key"]
    return {"qrcode_key": key, "url": url, "svg": segno.make(url, error="m").svg_inline(scale=6)}


def _cookies_from(r: httpx.Response, login_url: str) -> list[Cookie]:
    cookies = [c for c in r.cookies.jar]
    if not any(c.name == "SESSDATA" for c in cookies):
        # 没有 Set-Cookie 时，data.url（跨域登录链接）的查询参数里也带着同一组值；保留原始编码（SESSDATA 含 %2C）
        query = urlsplit(login_url or "").query
        q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        expires = int(q["Expires"]) if q.get("Expires", "").isdigit() else None
        if expires and expires < 10**9:  # 有的版本给的是有效秒数而不是时间戳
            expires += int(time.time())
        cookies = [_cookie(k, q[k], expires) for k in LOGIN_COOKIES if q.get(k)]
    return cookies


def _cookie(name: str, value: str, expires: int | None) -> Cookie:
    return Cookie(0, name, value, None, False, ".bilibili.com", True, True, "/", True, False,
                  expires, False, None, None, {})


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _netscape(cookies: list[Cookie]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# 由 Hermes B 站扫码登录生成，勿外传", ""]
    for c in cookies:
        domain = c.domain if c.domain.startswith(".") or not c.domain_specified else "." + c.domain
        lines.append("\t".join([domain, "TRUE" if domain.startswith(".") else "FALSE", c.path or "/",
                                "TRUE" if c.secure else "FALSE", str(c.expires or 0), c.name, c.value or ""]))
    return "\n".join(lines) + "\n"


def _read_cookies() -> dict[str, tuple[str, int]]:
    """name → (value, expires)。"""
    try:
        text = config.BILI_COOKIES.read_text()
    except FileNotFoundError:
        return {}
    out = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if line.startswith("#") or len(parts) != 7:
            continue
        out[parts[5]] = (parts[6], int(parts[4]) if parts[4].isdigit() else 0)
    return out


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


async def qr_poll(qrcode_key: str, *, client: httpx.AsyncClient | None = None) -> dict:
    r = await _get(QR_POLL, client, params={"qrcode_key": qrcode_key})
    data = _data(r)
    state = POLL_STATES.get(data.get("code"))
    if state is None:
        raise RuntimeError(f"未知的扫码状态 {data.get('code')}：{data.get('message')}")
    if state != "ok":
        return {"state": state}
    cookies = _cookies_from(r, data.get("url", ""))
    if not any(c.name == "SESSDATA" for c in cookies):
        raise RuntimeError("扫码成功但响应里没有 SESSDATA")
    _write_private(config.BILI_COOKIES, _netscape(cookies))
    header = "; ".join(f"{c.name}={c.value}" for c in cookies)
    nav = _data(await _get(NAV, client, headers={"Cookie": header}))
    sess = next(c for c in cookies if c.name == "SESSDATA")
    account = {"uname": nav.get("uname"), "mid": nav.get("mid"), "logged_in_at": now(),
               "expires_at": _iso(sess.expires)}
    _write_private(account_file(), json.dumps(account, ensure_ascii=False))
    return {"state": "ok", "uname": account["uname"]}


def status() -> dict:
    sess = _read_cookies().get("SESSDATA")
    try:
        account = json.loads(account_file().read_text())
    except (FileNotFoundError, ValueError):
        account = {}
    # 过期时间以 cookie 文件为准；0 表示会话 cookie，没有明确过期时间
    logged_in = bool(sess) and (sess[1] == 0 or sess[1] > time.time())
    return {"logged_in": logged_in, "uname": account.get("uname"), "logged_in_at": account.get("logged_in_at"),
            "expires_at": _iso(sess[1]) if sess else None}


def logout() -> None:
    config.BILI_COOKIES.unlink(missing_ok=True)
    account_file().unlink(missing_ok=True)


def cookie_file_for(url: str) -> Path | None:
    host = (urlsplit(url).hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in BILI_HOSTS):
        return None
    return config.BILI_COOKIES if status()["logged_in"] else None
