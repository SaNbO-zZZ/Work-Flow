"""X / Instagram 登录：打开本机 Chrome（独立资料夹）让用户自己登录，经 DevTools 只取 cookie（Netscape 格式，0600）供 yt-dlp 用；不收账号密码。"""
import asyncio
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

import websockets

from . import config
from .bilibili import _iso, _write_private
from .db import now

log = logging.getLogger("hermes.social_login")
SITES = {
    "x": {"name": "X", "login_url": "https://x.com/i/flow/login", "domains": ("x.com", "twitter.com"),
          "cookie": "auth_token"},
    "instagram": {"name": "Instagram", "login_url": "https://www.instagram.com/accounts/login/",
                  "domains": ("instagram.com",), "cookie": "sessionid"},
}
BROWSERS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
POLL_SECONDS = 2
LOGIN_TIMEOUT = 600
CLOSE_WAIT = 5          # 发 Browser.close 后等它自己退出的秒数，超时再 terminate


@dataclass
class Job:
    state: str = "waiting"    # waiting / ok / cancelled / expired / error
    error: str | None = None
    task: asyncio.Task | None = None
    proc: subprocess.Popen | None = None


_jobs: dict[str, Job] = {}


def site_info(site: str) -> dict:
    if site not in SITES:
        raise ValueError(f"不支持的网站：{site}")
    return SITES[site]


def cookie_file(site: str) -> Path:
    return config.SECRETS_DIR / f"{site}_cookies.txt"


def account_file(site: str) -> Path:
    return config.SECRETS_DIR / f"{site}_account.json"


def profile_dir(site: str) -> Path:
    # 独立资料夹：Chrome 不许在默认资料夹上开远程调试；保留下来，下次登录不用重输
    return config.SECRETS_DIR / "browser" / site


def _match(domain: str, domains) -> bool:
    d = (domain or "").lstrip(".").lower()
    return any(d == h or d.endswith("." + h) for h in domains)


def _netscape(cookies: list[dict], name: str) -> str:
    lines = ["# Netscape HTTP Cookie File", f"# 由 Hermes {name} 登录生成，勿外传", ""]
    for c in cookies:
        domain = c["domain"]
        exp = int(c.get("expires") or 0)
        lines.append("\t".join([domain, "TRUE" if domain.startswith(".") else "FALSE", c.get("path") or "/",
                                "TRUE" if c.get("secure") else "FALSE", str(max(exp, 0)), c["name"], c.get("value") or ""]))
    return "\n".join(lines) + "\n"


def _read_cookies(site: str) -> dict[str, tuple[str, int]]:
    """name → (value, expires)；yt-dlp 会回写这个文件，兼容 #HttpOnly_ 前缀和空过期时间。"""
    try:
        text = cookie_file(site).read_text()
    except FileNotFoundError:
        return {}
    out = {}
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        parts = line.split("\t")
        if line.startswith("#") or len(parts) != 7:
            continue
        out[parts[5]] = (parts[6], int(parts[4]) if parts[4].isdigit() else 0)
    return out


def _user_id(site: str, cookies: dict[str, str]) -> str | None:
    if site == "x":   # twid = "u=123456"（URL 编码）
        v = unquote(cookies.get("twid", ""))
        return v[2:] if v.startswith("u=") else None
    return cookies.get("ds_user_id")


def save_cookies(site: str, cookies: list[dict]) -> bool:
    """浏览器里已有登录 cookie 就落盘并返回 True。"""
    info = site_info(site)
    mine = [c for c in cookies if _match(c.get("domain", ""), info["domains"])]
    key = next((c for c in mine if c["name"] == info["cookie"] and c.get("value")), None)
    if not key:
        return False
    _write_private(cookie_file(site), _netscape(mine, info["name"]))
    exp = int(key.get("expires") or 0)
    account = {"logged_in_at": now(), "expires_at": _iso(exp) if exp > 0 else None}
    if uid := _user_id(site, {c["name"]: c.get("value") or "" for c in mine}):
        account["user_id"] = uid
    _write_private(account_file(site), json.dumps(account, ensure_ascii=False))
    return True


def status(site: str) -> dict:
    site_info(site)
    key = _read_cookies(site).get(SITES[site]["cookie"])
    try:
        account = json.loads(account_file(site).read_text())
    except (FileNotFoundError, ValueError):
        account = {}
    job = _jobs.get(site)
    logged_in = bool(key and key[0]) and (key[1] == 0 or key[1] > time.time())
    return {"logged_in": logged_in, "expires_at": _iso(key[1]) if key and key[1] else None,
            "at": account.get("logged_in_at"), "user_id": account.get("user_id"),
            "state": job.state if job else None, "error": job.error if job else None}


def cookie_file_for(url: str) -> Path | None:
    host = (urlsplit(url).hostname or "").lower()
    for site, info in SITES.items():
        if _match(host, info["domains"]):
            return cookie_file(site) if status(site)["logged_in"] else None
    return None


def _browser() -> str:
    for b in BROWSERS:
        if Path(b).exists():
            return b
    raise RuntimeError("没找到 Google Chrome 或 Microsoft Edge，请先安装 Chrome")


def _launch(site: str) -> subprocess.Popen:
    prof = profile_dir(site)
    prof.mkdir(parents=True, exist_ok=True)
    prof.chmod(0o700)
    (prof / "DevToolsActivePort").unlink(missing_ok=True)   # 端口 0 让 Chrome 自选空闲端口，再从这个文件读
    args = [_browser(), f"--user-data-dir={prof}", "--remote-debugging-port=0", "--no-first-run",
            "--no-default-browser-check", "--new-window", SITES[site]["login_url"]]
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def _ws_url(site: str) -> str | None:
    try:
        port, path = (profile_dir(site) / "DevToolsActivePort").read_text().split()[:2]
    except (FileNotFoundError, ValueError):
        return None
    return f"ws://127.0.0.1:{port}{path}"


async def _cdp(ws_url: str, *calls: tuple[str, dict]) -> list[dict]:
    """在浏览器级 DevTools 连接上依次调用；本机连接，不走系统代理。"""
    out = []
    async with websockets.connect(ws_url, max_size=None, open_timeout=5, proxy=None) as ws:
        for i, (method, params) in enumerate(calls, 1):
            await ws.send(json.dumps({"id": i, "method": method, "params": params}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if msg.get("id") == i:
                    break
            if "error" in msg:
                raise RuntimeError(f"{method} 失败：{msg['error'].get('message')}")
            out.append(msg.get("result") or {})
    return out


async def _shutdown(site: str, proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if ws := _ws_url(site):
        try:
            await _cdp(ws, ("Browser.close", {}))
        except Exception:
            pass   # 浏览器关得快，回复常常来不及
    for _ in range(int(CLOSE_WAIT / 0.2)):
        if proc.poll() is not None:
            return
        await asyncio.sleep(0.2)
    proc.terminate()


async def _watch(site: str, job: Job, on_done: Callable[[], None] | None) -> None:
    deadline = time.monotonic() + LOGIN_TIMEOUT
    seen_page = False
    try:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            if job.proc.poll() is not None:
                job.state = "cancelled"     # 用户 ⌘Q 退出了浏览器
                break
            if time.monotonic() > deadline:
                job.state = "expired"
                break
            if not (ws := _ws_url(site)):
                continue
            try:
                targets, got = await _cdp(ws, ("Target.getTargets", {}), ("Storage.getCookies", {}))
            except Exception as e:
                log.debug("读取浏览器 cookie 失败：%s", e)
                continue
            if save_cookies(site, got.get("cookies") or []):
                job.state = "ok"
                break
            # macOS 关掉最后一个窗口时 Chrome 进程还在，只能看还有没有网页
            if any(t.get("type") == "page" for t in targets.get("targetInfos") or []):
                seen_page = True
            elif seen_page:
                job.state = "cancelled"
                break
    except Exception as e:
        log.exception("%s 登录失败", site)
        job.state, job.error = "error", str(e)[:200]
    finally:
        try:
            await _shutdown(site, job.proc)
        except Exception:
            log.exception("关闭登录浏览器失败")
        if on_done:
            on_done()


async def start(site: str, on_done: Callable[[], None] | None = None) -> dict:
    site_info(site)
    job = _jobs.get(site)
    if job and job.task and not job.task.done():
        return {"started": False, "state": job.state}
    job = Job(proc=_launch(site))
    job.task = asyncio.create_task(_watch(site, job, on_done))
    _jobs[site] = job
    return {"started": True, "state": job.state}


def logout(site: str) -> None:
    site_info(site)
    job = _jobs.pop(site, None)
    if job and job.task and not job.task.done():
        job.task.cancel()
        if job.proc and job.proc.poll() is None:
            job.proc.terminate()
    cookie_file(site).unlink(missing_ok=True)
    account_file(site).unlink(missing_ok=True)
    shutil.rmtree(profile_dir(site), ignore_errors=True)   # 浏览器资料夹里也有登录状态，一并删掉才算真退出
