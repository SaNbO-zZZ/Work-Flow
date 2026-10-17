import asyncio
import json
import stat
import time

import pytest
import websockets
from fastapi.testclient import TestClient

from hermes import bilibili, config, db, download, server, social_login

FUTURE = int(time.time()) + 365 * 86400
H = {"Host": "127.0.0.1:8790"}
W = {**H, "X-Hermes": "1"}


def ck(name, value, domain, expires=FUTURE, **kw):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires, "secure": True, **kw}


X_COOKIES = [ck("auth_token", "tok123", ".x.com"), ck("twid", "u%3D987654", ".x.com"), ck("ct0", "c", ".x.com"),
             ck("guest", "g", "x.com", expires=-1), ck("auth_token", "evil", ".notx.com"),
             ck("SID", "google", ".google.com")]


@pytest.fixture(autouse=True)
def secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(config, "BILI_COOKIES", tmp_path / "secrets" / "bilibili_cookies.txt")
    monkeypatch.setattr(social_login, "_jobs", {})
    return tmp_path / "secrets"


def test_save_cookies_format_and_permissions(secrets):
    assert social_login.save_cookies("x", [ck("ct0", "c", ".x.com")]) is False
    assert not social_login.cookie_file("x").exists()
    assert social_login.save_cookies("x", X_COOKIES) is True
    f = social_login.cookie_file("x")
    assert f == secrets / "x_cookies.txt" and stat.S_IMODE(f.stat().st_mode) == 0o600
    lines = f.read_text().splitlines()
    assert lines[0] == "# Netscape HTTP Cookie File"
    rows = [l.split("\t") for l in lines if l and not l.startswith("#")]
    assert {r[5] for r in rows} == {"auth_token", "twid", "ct0", "guest"}       # 别的网站的不带
    assert [".x.com", "TRUE", "/", "TRUE", str(FUTURE), "auth_token", "tok123"] in rows
    assert ["x.com", "FALSE", "/", "TRUE", "0", "guest", "g"] in rows             # 仅本域、会话 cookie
    acct = json.loads(social_login.account_file("x").read_text())
    assert acct["user_id"] == "987654" and acct["expires_at"].startswith(time.strftime("%Y", time.gmtime(FUTURE)))
    assert stat.S_IMODE(social_login.account_file("x").stat().st_mode) == 0o600
    # yt-dlp 能读
    import http.cookiejar
    jar = http.cookiejar.MozillaCookieJar(str(f))
    jar.load(ignore_discard=True)
    assert {c.name for c in jar} >= {"auth_token", "twid"}


def test_status_and_logout(secrets):
    st = social_login.status("instagram")
    assert st["logged_in"] is False and st["state"] is None
    social_login.save_cookies("instagram", [ck("sessionid", "s1", ".instagram.com"), ck("ds_user_id", "55", ".instagram.com")])
    social_login.profile_dir("instagram").mkdir(parents=True)
    st = social_login.status("instagram")
    assert st["logged_in"] and st["user_id"] == "55" and st["at"] and st["expires_at"]
    # yt-dlp 回写时可能带 #HttpOnly_ 前缀
    f = social_login.cookie_file("instagram")
    f.write_text(f.read_text().replace(".instagram.com\t", "#HttpOnly_.instagram.com\t"))
    assert social_login.status("instagram")["logged_in"]
    social_login.logout("instagram")
    assert not f.exists() and not social_login.account_file("instagram").exists()
    assert not social_login.profile_dir("instagram").exists()
    assert social_login.status("instagram")["logged_in"] is False
    # 过期的不算
    social_login.save_cookies("x", [ck("auth_token", "t", ".x.com", expires=int(time.time()) - 10)])
    assert social_login.status("x")["logged_in"] is False
    with pytest.raises(ValueError):
        social_login.status("tiktok")


def test_ytdlp_args_cookies(secrets, tmp_path):
    def cookies_of(url):
        a = download.ytdlp_args(url, tmp_path)
        return a[a.index("--cookies") + 1] if "--cookies" in a else None
    assert cookies_of("https://x.com/F1/status/1") is None
    social_login.save_cookies("x", X_COOKIES)
    social_login.save_cookies("instagram", [ck("sessionid", "s1", ".instagram.com")])
    assert cookies_of("https://x.com/F1/status/1") == str(secrets / "x_cookies.txt")
    assert cookies_of("https://twitter.com/F1/status/1") == str(secrets / "x_cookies.txt")
    assert cookies_of("https://mobile.twitter.com/F1/status/1") == str(secrets / "x_cookies.txt")
    assert cookies_of("https://www.instagram.com/reel/abc/") == str(secrets / "instagram_cookies.txt")
    assert cookies_of("https://notx.com/a") is None and cookies_of("https://www.youtube.com/watch?v=1") is None
    bilibili._write_private(config.BILI_COOKIES, bilibili._netscape([bilibili._cookie("SESSDATA", "s", FUTURE)]))
    assert cookies_of("https://www.bilibili.com/video/BV1") == str(config.BILI_COOKIES)


class FakeProc:
    def __init__(self):
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, -15


async def fake_cdp(script):
    """本机假 DevTools：按 script 依次回 Target.getTargets / Storage.getCookies；收到 Browser.close 就记下。"""
    seen = []

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            seen.append(msg["method"])
            if msg["method"] == "Browser.close":
                await ws.send(json.dumps({"id": msg["id"], "result": {}}))
                continue
            step = script[min(seen.count("Storage.getCookies"), len(script) - 1)] if msg["method"] == "Storage.getCookies" \
                else script[min(seen.count("Target.getTargets") - 1, len(script) - 1)]
            res = {"targetInfos": [{"type": "page"}] * step[0]} if msg["method"] == "Target.getTargets" \
                else {"cookies": step[1]}
            await ws.send(json.dumps({"id": msg["id"], "result": res}))
    srv = await websockets.serve(handler, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], seen


def run_login(monkeypatch, script, proc=None, timeout=60):
    proc = proc or FakeProc()
    monkeypatch.setattr(social_login, "_launch", lambda site: proc)
    monkeypatch.setattr(social_login, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(social_login, "LOGIN_TIMEOUT", timeout)
    monkeypatch.setattr(social_login, "CLOSE_WAIT", 0.2)
    done = []

    async def main():
        srv, port, seen = await fake_cdp(script)
        prof = social_login.profile_dir("x")
        prof.mkdir(parents=True, exist_ok=True)
        (prof / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/abc\n")
        r = await social_login.start("x", on_done=lambda: done.append(1))
        assert r == {"started": True, "state": "waiting"}
        assert (await social_login.start("x"))["started"] is False
        await asyncio.wait_for(social_login._jobs["x"].task, 10)
        srv.close()
        return seen
    seen = asyncio.run(main())
    return social_login.status("x"), seen, proc, done


def test_login_flow_ok(monkeypatch):
    st, seen, proc, done = run_login(monkeypatch, [(1, []), (1, [ck("ct0", "c", ".x.com")]), (1, X_COOKIES)])
    assert st["state"] == "ok" and st["logged_in"] and st["user_id"] == "987654"
    assert "Browser.close" in seen and done == [1]
    assert proc.terminated     # 假进程不会自己退出，兜底 terminate


def test_login_window_closed_is_cancelled(monkeypatch):
    st, seen, _, _ = run_login(monkeypatch, [(1, []), (1, []), (0, [])])
    assert st["state"] == "cancelled" and not st["logged_in"]


def test_login_browser_quit_and_timeout(monkeypatch):
    proc = FakeProc()
    proc.code = 0
    st, _, _, _ = run_login(monkeypatch, [(1, [])], proc=proc)
    assert st["state"] == "cancelled"
    social_login._jobs.clear()
    st, _, _, _ = run_login(monkeypatch, [(1, [])], timeout=0.05)
    assert st["state"] == "expired"


def test_launch_args(monkeypatch, secrets):
    got = {}
    monkeypatch.setattr(social_login, "_browser", lambda: "/fake/Chrome")
    monkeypatch.setattr(social_login.subprocess, "Popen", lambda args, **kw: got.setdefault("args", args))
    prof = social_login.profile_dir("instagram")
    prof.mkdir(parents=True)
    (prof / "DevToolsActivePort").write_text("1\n/x\n")
    social_login._launch("instagram")
    a = got["args"]
    assert a[0] == "/fake/Chrome" and f"--user-data-dir={prof}" in a and "--remote-debugging-port=0" in a
    assert a[-1] == "https://www.instagram.com/accounts/login/" and "--new-window" in a
    assert not (prof / "DevToolsActivePort").exists() and stat.S_IMODE(prof.stat().st_mode) == 0o700


db._orig_connect = getattr(db, "_orig_connect", db.connect)


@pytest.fixture
def client(monkeypatch, db_path):
    from tests.test_m2_server import FakeAppServer
    monkeypatch.setattr(server.db, "connect", lambda path=None: db._orig_connect(db_path))
    monkeypatch.setattr(server, "AppServer", FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        yield c


def test_endpoints(client, monkeypatch):
    calls = []

    async def fake_start(site, on_done=None):
        calls.append(site)
        return {"started": True, "state": "waiting"}
    monkeypatch.setattr(social_login, "start", fake_start)
    assert client.post("/api/social/x/login", headers=H).status_code == 403     # 写操作要 X-Hermes
    assert client.post("/api/social/x/login", headers=W).json() == {"started": True, "state": "waiting"}
    assert client.post("/api/social/tiktok/login", headers=W).status_code == 404
    assert calls == ["x"]
    a = client.get("/api/accounts", headers=H).json()
    assert a["x"]["logged_in"] is False and a["instagram"]["logged_in"] is False and "bilibili" in a
    social_login.save_cookies("instagram", [ck("sessionid", "s1", ".instagram.com")])
    assert client.get("/api/social/instagram/status", headers=H).json()["logged_in"] is True
    assert client.post("/api/social/instagram/logout", headers=W).json() == {"ok": True}
    assert client.get("/api/social/instagram/status", headers=H).json()["logged_in"] is False

    async def no_browser(site, on_done=None):
        raise RuntimeError("没找到 Google Chrome")
    monkeypatch.setattr(social_login, "start", no_browser)
    r = client.post("/api/social/x/login", headers=W)
    assert r.status_code == 502 and "Chrome" in r.json()["detail"]
