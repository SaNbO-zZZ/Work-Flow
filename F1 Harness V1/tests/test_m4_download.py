import asyncio
import json
from pathlib import Path

import pytest

from hermes import config, download


@pytest.fixture(autouse=True)
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "素材库")
    monkeypatch.setattr(download.bilibili, "cookie_file_for", lambda url: None)
    return tmp_path / "素材库"


def _out_dir(args):
    return Path(args[args.index("-P") + 1])


def fake_runner(info=None, lines=(), code=0, calls=None):
    info = info or {"id": "BV1xx", "title": "测试视频", "uploader": "某UP主", "extractor": "BiliBili",
                    "extractor_key": "BiliBili", "webpage_url": "https://www.bilibili.com/video/BV1xx"}

    async def run(args, on_line):
        if calls is not None:
            calls.append(args)
        for line in lines:
            on_line(line)
        if code == 0:
            d = _out_dir(args)
            (d / f"{info['id']}.info.json").write_text(json.dumps(info, ensure_ascii=False))
            (d / f"{info['id']}.mp4").write_bytes(b"\0" * 2048)
        return code
    return run


def fake_ingest(record):
    def ingest(conn, path, **kw):
        record.append((Path(path), kw))
        return conn.execute("INSERT INTO assets(source_type, source_url, local_path, credit, created_at) "
                            "VALUES (?,?,?,?,datetime('now'))",
                            (kw["source_type"], kw["source_url"], "/elsewhere/a.mp4", kw["credit"])).lastrowid
    return ingest


def test_enqueue_validation_and_dedupe(conn):
    for bad in ("", "not a url", "ftp://example.com/a", "http://localhost/x", "https:///path", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            download.enqueue(conn, bad)
    a = download.enqueue(conn, " https://www.bilibili.com/video/BV1xx ")
    assert download.enqueue(conn, "https://www.bilibili.com/video/BV1xx") == a
    conn.execute("UPDATE downloads SET state='running' WHERE id=?", (a,))
    assert download.enqueue(conn, "https://www.bilibili.com/video/BV1xx") == a
    conn.execute("UPDATE downloads SET state='done' WHERE id=?", (a,))
    b = download.enqueue(conn, "https://www.bilibili.com/video/BV1xx")
    assert b != a
    assert [r["id"] for r in download.items(conn)] == [b, a]
    assert download.items(conn)[0]["state"] == "queued"


def test_progress_single_and_merged():
    p = download.Progress()
    assert p.feed("[info] abc: Downloading 1 format(s): 18") is None
    assert p.feed("[download] Destination: /x/abc.mp4") == 0
    assert p.feed("[download]  45.5% of    1.00MiB at  1.00MiB/s ETA 00:01") == pytest.approx(0.455)
    assert p.feed("[download] 100% of    1.00MiB in 00:00:01") == 0.99
    assert p.feed("随便一行") is None

    p = download.Progress()
    p.feed("[info] BV1: Downloading 1 format(s): 30080+30280")
    p.feed("[download] Destination: /x/BV1.f30080.mp4")
    assert p.feed("[download]  50.0% of 10MiB") == pytest.approx(0.25)
    assert p.feed("[download] 100.0% of 10MiB") == pytest.approx(0.5)
    p.feed("[download] Destination: /x/BV1.f30280.m4a")
    assert p.feed("\x1b[0;94m[download]\x1b[0m  50.0% of 1MiB") == pytest.approx(0.75)
    assert p.feed("[download] /x/BV1.mp4 has already been downloaded") == 0.99


def test_credit_text():
    assert download.credit_text({"extractor_key": "BiliBili", "uploader": "某UP主"}) == "来源：B站 @某UP主"
    assert download.credit_text({"extractor_key": "Youtube", "extractor": "youtube", "uploader": "jawed"}) \
        == "来源：YouTube @jawed"
    assert download.credit_text({"extractor": "youtube", "channel": "F1"}) == "来源：YouTube @F1"
    assert download.credit_text({"extractor_key": "Vimeo"}) == "来源：Vimeo"
    assert download.credit_text({"extractor_key": "Generic", "uploader": "someone",
                                 "webpage_url": "https://v.example.com/a"}) == "来源：v.example.com @someone"
    assert download.credit_text({"extractor": "twitch:vod", "uploader": "a"}) == "来源：twitch @a"
    assert download.credit_text({"webpage_url": "https://video.example.com/x"}) == "来源：video.example.com"


def test_credit_text_f1_site():
    bc = {"extractor_key": "BrightcoveNew", "extractor": "brightcove:new", "uploader_id": "6057949432001",
          "webpage_url": "https://players.brightcove.net/6057949432001/default_default/index.html?videoId=1"}
    want = "来源：F1 官网 @FORMULA 1"
    assert download.credit_text(bc) == want
    assert download.credit_text({**bc, "uploader_id": None},
                                "https://www.formula1.com/en/video/a.6060976994001") == want
    assert download.credit_text({**bc, "uploader_id": None,
                                 "original_url": "https://www.formula1.com/en/latest/video.a.1.html"}) == want
    assert download.uploader_name(bc) == "FORMULA 1"


def test_run_one_f1_rewrites_url_keeps_original(conn):
    url = "https://www.formula1.com/en/video/race-highlights-malaysia-2016.6060976994001"
    info = {"id": "6060976994001", "title": "Race highlights - Malaysia 2016", "extractor_key": "BrightcoveNew",
            "uploader_id": "6057949432001",
            "webpage_url": "https://players.brightcove.net/6057949432001/default_default/index.html?videoId=6060976994001"}
    calls, record = [], []
    did = download.enqueue(conn, url)
    asyncio.run(download.run_one(conn, did, runner=fake_runner(info, calls=calls), ingest=fake_ingest(record)))
    assert calls[0][-1] == "https://www.formula1.com/en/latest/video.race-highlights-malaysia-2016.6060976994001.html"
    kw = record[0][1]
    assert (kw["source_url"], kw["uploader"], kw["credit"]) == (url, "FORMULA 1", "来源：F1 官网 @FORMULA 1")
    assert conn.execute("SELECT url FROM downloads WHERE id=?", (did,)).fetchone()[0] == url
    from hermes import assets
    assert assets.credit_line({"source_url": url, "title": info["title"], **kw}) \
        == "画面：F1 官网 · FORMULA 1《Race highlights - Ma…》"


def test_run_one_success(conn, library):
    did = download.enqueue(conn, "https://www.bilibili.com/video/BV1xx")
    seen, record, calls = [], [], []
    lines = ["[info] BV1xx: Downloading 1 format(s): 30080+30280", "[download] Destination: a",
             "[download]  50.0% of 1MiB", "[download] 100% of 1MiB", "[download] Destination: b",
             "[download]  50.0% of 1MiB"]
    base = fake_runner(lines=lines, calls=calls)

    async def runner(args, on_line):
        def spy(line):
            on_line(line)
            seen.append(conn.execute("SELECT state, progress FROM downloads WHERE id=?", (did,)).fetchone())
        return await base(args, spy)

    asset_id = asyncio.run(download.run_one(conn, did, runner=runner, ingest=fake_ingest(record)))
    args = calls[0]
    assert args[0] == str(config.YTDLP) and args[-1] == "https://www.bilibili.com/video/BV1xx"
    for flag in ("--no-playlist", "--newline", "--write-info-json"):
        assert flag in args
    assert args[args.index("-f") + 1] == download.FORMAT
    assert args[args.index("--merge-output-format") + 1] == "mp4"
    assert args[args.index("-o") + 1] == "%(id)s.%(ext)s"
    assert _out_dir(args) == library / "_incoming" / str(did)
    assert "--cookies" not in args
    assert all(s["state"] == "running" for s in seen)
    assert [s["progress"] for s in seen][-1] == pytest.approx(0.75)

    path, kw = record[0]
    assert path.name == "BV1xx.mp4"
    assert kw == {"source_type": "用户链接", "source_url": "https://www.bilibili.com/video/BV1xx",
                  "title": "测试视频", "uploader": "某UP主", "credit": "来源：B站 @某UP主"}
    row = conn.execute("SELECT * FROM downloads WHERE id=?", (did,)).fetchone()
    assert (row["state"], row["progress"], row["asset_id"], row["error"]) == ("done", 1, asset_id, None)
    assert row["finished_at"]
    # 入库已把原片放到别处，临时下载目录应清掉
    assert not (library / "_incoming" / str(did)).exists()


def test_run_one_keeps_incoming_when_asset_points_there(conn, library):
    did = download.enqueue(conn, "https://youtu.be/abc")

    async def ingest(conn, path, **kw):
        return conn.execute("INSERT INTO assets(source_type, local_path, created_at) VALUES ('用户链接', ?, 'x')",
                            (str(path),)).lastrowid

    asyncio.run(download.run_one(conn, did, runner=fake_runner(), ingest=ingest))
    assert (library / "_incoming" / str(did) / "BV1xx.mp4").exists()


def test_run_one_adds_bilibili_cookies(conn, monkeypatch, tmp_path):
    cookie = tmp_path / "c.txt"
    monkeypatch.setattr(download.bilibili, "cookie_file_for", lambda url: cookie if "bilibili" in url else None)
    calls = []
    did = download.enqueue(conn, "https://www.bilibili.com/video/BV1xx")
    asyncio.run(download.run_one(conn, did, runner=fake_runner(calls=calls), ingest=fake_ingest([])))
    assert calls[0][calls[0].index("--cookies") + 1] == str(cookie)


def test_run_one_failure(conn):
    did = download.enqueue(conn, "https://www.youtube.com/watch?v=x")
    lines = ["x" * 3000, "ERROR: [youtube] x: Video unavailable"]
    with pytest.raises(download.DownloadError):
        asyncio.run(download.run_one(conn, did, runner=fake_runner(lines=lines, code=1), ingest=fake_ingest([])))
    row = conn.execute("SELECT * FROM downloads WHERE id=?", (did,)).fetchone()
    assert row["state"] == "failed" and row["finished_at"] and row["asset_id"] is None
    assert len(row["error"]) == 2000 and row["error"].endswith("Video unavailable")


def test_run_one_no_file_and_ingest_error(conn):
    did = download.enqueue(conn, "https://example.com/v")

    async def empty(args, on_line):
        return 0
    with pytest.raises(download.DownloadError, match="没有找到"):
        asyncio.run(download.run_one(conn, did, runner=empty, ingest=fake_ingest([])))

    def broken(conn, path, **kw):
        raise OSError("磁盘满")
    with pytest.raises(download.DownloadError, match="磁盘满"):
        asyncio.run(download.run_one(conn, did, runner=fake_runner(), ingest=broken))
    assert conn.execute("SELECT state FROM downloads WHERE id=?", (did,)).fetchone()[0] == "failed"


async def _run_worker(conn, until, **kw):
    stop = asyncio.Event()
    task = asyncio.create_task(download.worker(conn, stop=stop, retry_delays=(0.01, 0.01), idle_seconds=0.01, **kw))
    for _ in range(500):
        if until():
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)


def test_worker_retries_then_fails(conn):
    did = download.enqueue(conn, "https://example.com/bad")
    calls = []
    state = lambda: conn.execute("SELECT state FROM downloads WHERE id=?", (did,)).fetchone()[0]  # noqa: E731
    asyncio.run(_run_worker(conn, lambda: state() == "failed" and len(calls) >= 3,
                            runner=fake_runner(code=1, lines=["ERROR: boom"], calls=calls), ingest=fake_ingest([])))
    assert len(calls) == 3 and state() == "failed"


def test_worker_order_and_retry_success(conn):
    first = download.enqueue(conn, "https://example.com/1")
    second = download.enqueue(conn, "https://example.com/2")
    conn.execute("UPDATE downloads SET state='running' WHERE id=?", (second,))  # 模拟上次中途退出
    order, fails = [], {"n": 0}
    ok = fake_runner()

    async def runner(args, on_line):
        order.append(args[-1])
        if args[-1].endswith("/1") and fails["n"] == 0:
            fails["n"] += 1
            return 1
        return await ok(args, on_line)

    done = lambda: conn.execute("SELECT count(*) FROM downloads WHERE state='done'").fetchone()[0] == 2  # noqa: E731
    asyncio.run(_run_worker(conn, done, runner=runner, ingest=fake_ingest([])))
    assert done()
    assert order == ["https://example.com/1", "https://example.com/1", "https://example.com/2"]
    assert first < second


def test_find_outputs_skips_appledouble(tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"x" * 100)
    (tmp_path / "v.info.json").write_text('{"title": "t"}')
    (tmp_path / "._v.info.json").write_bytes(b"\x00\x05\x16\x07\xb0\xff")
    (tmp_path / "._v.mp4").write_bytes(b"\x00" * 4096)
    video, info = download._find_outputs(tmp_path)
    assert video.name == "v.mp4" and info == {"title": "t"}
