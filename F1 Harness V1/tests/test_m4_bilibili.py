import asyncio
import json
import stat
import time

import httpx
import pytest

from hermes import bilibili, config

FUTURE = int(time.time()) + 180 * 86400


@pytest.fixture(autouse=True)
def secrets(tmp_path, monkeypatch):
    path = tmp_path / "secrets" / "bilibili_cookies.txt"
    monkeypatch.setattr(config, "BILI_COOKIES", path)
    return path


def client_for(poll_code=86101, set_cookie=True, login_url="", uname="车迷小张"):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/qrcode/generate"):
            return httpx.Response(200, json={"code": 0, "message": "0", "data": {
                "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=k123",
                "qrcode_key": "k123"}})
        if req.url.path.endswith("/qrcode/poll"):
            headers = []
            if poll_code == 0 and set_cookie:
                exp = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(FUTURE))
                headers = [("set-cookie", f"SESSDATA=sess%2Cabc; Path=/; Domain=bilibili.com; Expires={exp}; HttpOnly; Secure"),
                           ("set-cookie", f"bili_jct=jct123; Path=/; Domain=bilibili.com; Expires={exp}"),
                           ("set-cookie", f"DedeUserID=42; Path=/; Domain=bilibili.com; Expires={exp}")]
            return httpx.Response(200, headers=headers, json={"code": 0, "message": "0", "data": {
                "url": login_url, "refresh_token": "r", "timestamp": 0, "code": poll_code, "message": ""}})
        if req.url.host == "api.bilibili.com":
            assert "SESSDATA=" in req.headers["cookie"]
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "uname": uname, "mid": 42}})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def test_qr_start():
    client, seen = client_for()
    res = asyncio.run(bilibili.qr_start(client=client))
    assert res["qrcode_key"] == "k123" and "qrcode_key=k123" in res["url"]
    assert res["svg"].startswith("<svg") and len(res["svg"]) > 500
    assert "Mozilla" in seen[0].headers["user-agent"]
    assert seen[0].headers["referer"] == "https://www.bilibili.com"


@pytest.mark.parametrize("code,state", [(86101, "waiting"), (86090, "scanned"), (86038, "expired")])
def test_qr_poll_pending_states(code, state, secrets):
    client, seen = client_for(poll_code=code)
    assert asyncio.run(bilibili.qr_poll("k123", client=client)) == {"state": state}
    assert seen[0].url.params["qrcode_key"] == "k123"
    assert not secrets.exists()


def test_qr_poll_unknown_code():
    client, _ = client_for(poll_code=12345)
    with pytest.raises(RuntimeError):
        asyncio.run(bilibili.qr_poll("k123", client=client))


def test_qr_poll_ok_writes_private_netscape_cookies(secrets):
    client, _ = client_for(poll_code=0)
    assert asyncio.run(bilibili.qr_poll("k123", client=client)) == {"state": "ok", "uname": "车迷小张"}

    text = secrets.read_text()
    assert text.startswith("# Netscape HTTP Cookie File")
    rows = [line.split("\t") for line in text.splitlines() if line and not line.startswith("#")]
    assert all(len(r) == 7 for r in rows)
    sess = next(r for r in rows if r[5] == "SESSDATA")
    assert sess[0] == ".bilibili.com" and sess[1] == "TRUE" and sess[2] == "/" and sess[3] == "TRUE"
    assert sess[4] == str(FUTURE) and sess[6] == "sess%2Cabc"
    assert {r[5] for r in rows} == {"SESSDATA", "bili_jct", "DedeUserID"}
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets.parent.stat().st_mode) == 0o700
    acc_file = bilibili.account_file()
    assert stat.S_IMODE(acc_file.stat().st_mode) == 0o600
    acc = json.loads(acc_file.read_text())
    assert acc["uname"] == "车迷小张" and acc["mid"] == 42 and acc["logged_in_at"]

    st = bilibili.status()
    assert st["logged_in"] and st["uname"] == "车迷小张" and st["expires_at"] == acc["expires_at"]
    assert not list(secrets.parent.glob("*.tmp"))


def test_qr_poll_ok_from_login_url(secrets):
    url = (f"https://passport.biligame.com/x/passport-login/web/crossDomain?DedeUserID=42&DedeUserID__ckMd5=md5"
           f"&Expires={FUTURE}&SESSDATA=s2%2Cxyz&bili_jct=j2&gourl=https%3A%2F%2Fwww.bilibili.com")
    client, _ = client_for(poll_code=0, set_cookie=False, login_url=url)
    assert asyncio.run(bilibili.qr_poll("k", client=client))["state"] == "ok"
    rows = {r.split("\t")[5]: r.split("\t") for r in secrets.read_text().splitlines() if r and r[0] != "#"}
    assert rows["SESSDATA"][6] == "s2%2Cxyz" and rows["SESSDATA"][4] == str(FUTURE)
    assert set(rows) == {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"}
    assert bilibili.status()["logged_in"]


def _write_cookie(path, expires):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t{expires}\tSESSDATA\tx\n")


def test_status_and_expiry(secrets):
    assert bilibili.status() == {"logged_in": False, "uname": None, "logged_in_at": None, "expires_at": None}
    _write_cookie(secrets, int(time.time()) - 10)
    st = bilibili.status()
    assert not st["logged_in"] and st["expires_at"]
    _write_cookie(secrets, FUTURE)
    assert bilibili.status()["logged_in"]


def test_logout(secrets):
    _write_cookie(secrets, FUTURE)
    bilibili.account_file().write_text('{"uname": "a"}')
    bilibili.logout()
    assert not secrets.exists() and not bilibili.account_file().exists()
    bilibili.logout()  # 重复登出不报错
    assert not bilibili.status()["logged_in"]


def test_cookie_file_for(secrets):
    assert bilibili.cookie_file_for("https://www.bilibili.com/video/BV1xx") is None  # 未登录
    _write_cookie(secrets, FUTURE)
    for url in ("https://www.bilibili.com/video/BV1xx", "https://bilibili.com/x", "https://b23.tv/abc",
                "https://m.bilibili.com/video/BV1", "https://WWW.BILIBILI.COM/video/BV1"):
        assert bilibili.cookie_file_for(url) == secrets
    for url in ("https://www.youtube.com/watch?v=x", "https://bilibili.com.evil.com/x", "https://notbilibili.com/x",
                "https://evil.com/?u=bilibili.com", "not a url"):
        assert bilibili.cookie_file_for(url) is None
    _write_cookie(secrets, int(time.time()) - 1)
    assert bilibili.cookie_file_for("https://www.bilibili.com/video/BV1xx") is None
