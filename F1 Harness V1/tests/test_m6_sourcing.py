"""M6-3 D2 自动找素材：库内复用、GPT 找链接的校验、下载排队与收尾。GPT、yt-dlp、下载、Drive 全部替身。"""
import asyncio
import json
from pathlib import Path

import pytest

from hermes import codex_runner, config, db, episodes, prompts, sourcing, steps
from hermes.scheduler import StepDeferred, StepNotReady

ORIG_CONNECT = db.connect
SCRIPT = "# 雪邦前瞻\n\n## 赛道\n马来西亚站回来了，雪邦有两条长直道。\n\n## 轮胎\n倍耐力带来2套新配方[F3]，比赛有56圈。\n"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """不查真 DNS：域名里带 intranet 的解析到内网，带 nxdomain 的解析失败，其余给公网地址。"""
    def getaddrinfo(host, port, *a, **kw):
        if "nxdomain" in host:
            raise sourcing.socket.gaierror(8, "nodename nor servname provided")
        ip = "10.0.0.5" if "intranet" in host else "::ffff:192.168.1.2" if "mapped" in host else "93.184.216.34"
        return [(2, 1, 6, "", (ip, 0))]
    monkeypatch.setattr(sourcing.socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def ep(conn, db_path, monkeypatch, tmp_path):
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"{branch}|{step}|{ctx}|{notes}")
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label="马来西亚大奖赛", title="雪邦前瞻")
    row = conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
    (Path(row["folder"]) / "02_文案" / "逐字稿.md").write_text(SCRIPT, encoding="utf-8")
    (Path(row["folder"]) / "04_素材" / sourcing.MANIFEST).unlink(missing_ok=True)  # 各用例的期文件夹同名
    return row


def _asset(conn, tmp_path, shots, *, local=True, drive_path=None, source_url=None, title="素材") -> int:
    path = None
    aid = conn.execute("INSERT INTO assets(source_type, source_url, drive_path, title, tagged_at, created_at) "
                       "VALUES ('测试', ?, ?, ?, ?, ?)", (source_url, drive_path, title, db.now(), db.now())).lastrowid
    if local:
        path = tmp_path / f"a{aid}.mp4"
        path.write_bytes(b"v")
    conn.execute("UPDATE assets SET local_path=? WHERE id=?", (str(path or tmp_path / f"gone{aid}.mp4"), aid))
    for i in range(shots):
        conn.execute("INSERT INTO asset_shots(asset_id, idx, start_s, end_s, phash, clip_vec) VALUES (?,?,?,?,?,?)",
                     (aid, i, i * 3.0, i * 3.0 + 3.0, "ff", b"v"))
    return aid


def _filler(conn, tmp_path, other_ep, n=200) -> int:
    """别的期用过的大素材，撑出"全库前 10%"的分母。"""
    aid = _asset(conn, tmp_path, n, title="别期素材")
    conn.execute("INSERT INTO asset_uses(asset_id, episode_id, src_in, src_out, created_at) VALUES (?,?,0,3,?)",
                 (aid, other_ep, db.now()))
    return aid


def _other_ep(conn):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="复盘", gp_label="巴林大奖赛", title="别的期")
    return eid


def _search(first):
    """把 first 里素材的镜头排在最前面。"""
    def search(conn, query, limit, **kw):
        rows = conn.execute("SELECT asset_id, idx FROM asset_shots WHERE clip_vec IS NOT NULL").fetchall()
        rows = sorted(rows, key=lambda r: (r[0] not in first, r[0], r[1]))
        return [{"asset_id": r[0], "shot_idx": r[1], "score": 0.4} for r in rows][:limit]
    return search


def _runner(items, seen=None):
    async def run(prompt, model, effort, workdir, **kw):
        if seen is not None:
            seen.append({"prompt": prompt, "schema": Path(kw["output_schema"]).name, "web": kw["web_search"],
                         "model": model, "effort": effort})
        if isinstance(items, Exception):
            raise items
        return codex_runner.CodexResult("t", json.dumps({"items": items}, ensure_ascii=False),
                                        {"input_tokens": 10, "output_tokens": 5})
    return run


def _no_gpt():
    async def run(*a, **kw):
        raise AssertionError("不该调 GPT")
    return run


def _probe(durations):
    async def probe(url):
        d = durations.get(url, 120)
        if isinstance(d, Exception):
            raise d
        return {"webpage_url": url, "duration": d, "title": "t", "uploader": "FORMULA 1"}
    return probe


def _manifest(ep):
    return json.loads((Path(ep["folder"]) / "04_素材" / sourcing.MANIFEST).read_text(encoding="utf-8"))


def _run(conn, ep, **kw):
    kw.setdefault("model", "gpt-6-sol")
    kw.setdefault("effort", "high")
    return asyncio.run(sourcing.run(conn, ep, **kw))


# ---------- 分段与链接规范化 ----------

def test_segments_by_heading_and_paragraph():
    segs = sourcing.segments(SCRIPT)
    assert [s["name"] for s in segs] == ["赛道", "轮胎"]
    assert "[F3]" not in segs[1]["text"] and segs[1]["query"].startswith("轮胎：")
    assert all(s["need"] >= 1 for s in segs)
    plain = sourcing.segments("# 标题\n\n第一段话。\n\n第二段话。" + "很长" * 60)
    assert [s["name"] for s in plain] == ["第1段", "第2段"]
    assert plain[1]["need"] > plain[0]["need"]
    assert sourcing.segments("") == []


def test_video_key():
    k = sourcing.video_key
    assert k("https://www.youtube.com/watch?v=abcdefghijk&t=30") == k("https://youtu.be/abcdefghijk") \
        == k("https://m.youtube.com/shorts/abcdefghijk") == "youtube:abcdefghijk"
    assert k("https://www.bilibili.com/video/BV1xx411c7mD/?spm_id_from=333") == "bilibili:BV1xx411c7mD"
    assert k("https://b23.tv/AbCd12") == "b23:AbCd12"
    assert k("https://www.youtube.com/@Formula1") is None
    assert k("https://space.bilibili.com/123") is None
    assert k("https://notyoutube.com/watch?v=abcdefghijk") == "url:notyoutube.com/watch?v=abcdefghijk"
    assert k("https://www.fia.com/video/abc/?utm_source=x&id=2&fbclid=z#t=3") \
        == k("http://WWW.FIA.com/video/abc?id=2") == "url:www.fia.com/video/abc?id=2"
    assert k("https://www.fia.com/video/abc?id=3") != k("https://www.fia.com/video/abc?id=2")
    assert k("ftp://example.com/v.mp4") is None and k("not a url") is None


def test_url_problem():
    p = sourcing.url_problem
    assert p("https://www.fia.com/video/1") is None and p("http://cdn.example.com/a.mp4") is None
    assert p("ftp://example.com/v.mp4") == p("file:///etc/passwd") == p("javascript:alert(1)") == "只收 http/https 链接"
    assert p("https://user:pw@example.com/v") == p("https://user@example.com/v") == "链接里带账号密码"
    for url in ("http://127.0.0.1:8000/x", "https://[::1]/x", "http://10.1.2.3/x", "http://93.184.216.34/x"):
        assert p(url) == "链接是 IP 地址，不收", url
    assert p("https://intranet.example.com/v") == p("https://mapped.example.com/v") == "网站指向本机或内网地址，不收"
    assert p("https://nxdomain.example/v") == "网站域名解析不了"
    assert p("https:///v") == "链接里没有网站域名" and p("http://example.com:99999/") == "链接格式不对"


# ---------- 素材够了不调 GPT ----------

def test_enough_material_skips_gpt(conn, ep, tmp_path):
    _filler(conn, tmp_path, _other_ep(conn))
    mine = _asset(conn, tmp_path, 20, title="雪邦车载")
    (Path(ep["folder"]) / "04_素材" / sourcing.MANIFEST).write_text(json.dumps({"asset_ids": [mine, 999]}))
    out = _run(conn, ep, runner=_no_gpt(), search=_search({mine}))
    assert out["asset_ids"] == [mine] and out["existing"] == 1
    assert out["gpt_called"] is False and out["from_library"] == 0
    assert _manifest(ep)["asset_ids"] == [mine]


def test_library_reuse_and_drive_fetch(conn, ep, tmp_path):
    other = _other_ep(conn)
    _filler(conn, tmp_path, other)
    local = _asset(conn, tmp_path, 8, title="没用过的本地素材")
    remote = _asset(conn, tmp_path, 8, local=False, drive_path="Hermes/素材/x.mp4", title="只在 Drive")
    reserved = _asset(conn, tmp_path, 15, title="别期预留")
    conn.execute("INSERT INTO downloads(url, episode_id, state, asset_id, created_at) VALUES ('u', ?, 'done', ?, ?)",
                 (other, reserved, db.now()))
    fetched = []

    async def fetch(conn, aid):
        fetched.append(aid)
        return tmp_path / "x.mp4"

    out = _run(conn, ep, runner=_no_gpt(), search=_search({local, remote, reserved}), fetch=fetch)
    assert set(out["asset_ids"]) == {local, remote} and out["from_library"] == 2
    assert fetched == [remote] and out["gpt_called"] is False


def test_drive_fetch_failure_noted(conn, ep, tmp_path):
    _filler(conn, tmp_path, _other_ep(conn))
    mine = _asset(conn, tmp_path, 5)
    remote = _asset(conn, tmp_path, 15, local=False, drive_path="Hermes/x.mp4")
    (Path(ep["folder"]) / "04_素材" / sourcing.MANIFEST).write_text(json.dumps({"asset_ids": [mine]}))

    async def fetch(conn, aid):
        raise RuntimeError("rclone 连不上")

    out = _run(conn, ep, runner=_runner([]), search=_search({mine, remote}), fetch=fetch, probe=_probe({}))
    assert out["asset_ids"] == [mine]
    assert any("rclone 连不上" in n for n in out["notes"])


# ---------- 不够时让 GPT 找 ----------

def test_gpt_links_filtered_then_downloaded(conn, ep, tmp_path):
    _filler(conn, tmp_path, _other_ep(conn))
    mine = _asset(conn, tmp_path, 1, source_url="https://www.youtube.com/watch?v=AAAAAAAAAAA&t=3")
    (Path(ep["folder"]) / "04_素材" / sourcing.MANIFEST).write_text(json.dumps({"asset_ids": [mine]}))
    conn.execute("INSERT INTO settings(key, value) VALUES ('assets.auto_max', '2')")
    conn.execute("INSERT INTO settings(key, value) VALUES ('assets.auto_max_minutes', '10')")
    items = [
        {"url": "ftp://example.com/v/1", "title": "外站", "channel": "x", "segment": "赛道", "reason": "r"},
        {"url": "https://www.youtube.com/@Formula1", "title": "频道页", "channel": "F1", "segment": "赛道", "reason": "r"},
        {"url": "https://youtu.be/AAAAAAAAAAA", "title": "库里有", "channel": "F1", "segment": "赛道", "reason": "r"},
        {"url": "https://www.youtube.com/watch?v=LONGLONGLON", "title": "太长", "channel": "F1", "segment": "赛道",
         "reason": "r"},
        {"url": "https://www.youtube.com/watch?v=BBBBBBBBBBB", "title": "车载", "channel": "F1", "segment": "赛道",
         "reason": "r"},
        {"url": "https://youtu.be/BBBBBBBBBBB", "title": "重复", "channel": "F1", "segment": "赛道", "reason": "r"},
        {"url": "https://www.bilibili.com/video/BV1xx411c7mD", "title": "B站", "channel": "倍耐力",
         "segment": "轮胎", "reason": "r"},
        {"url": "https://www.youtube.com/watch?v=TYRETYRETYR", "title": "轮胎", "channel": "倍耐力",
         "segment": "轮胎", "reason": "r"},
        {"url": "https://www.youtube.com/watch?v=CCCCCCCCCCC", "title": "多余", "channel": "F1", "segment": "轮胎",
         "reason": "r"},
    ]
    seen, probed = [], []
    base = _probe({"https://www.youtube.com/watch?v=LONGLONGLON": 3600})

    async def probe(url):
        probed.append(url)
        return await base(url)

    with pytest.raises(StepDeferred):
        _run(conn, ep, runner=_runner(items, seen), search=_search({mine}), probe=probe,
             feedback=["画面太少，多给车载"])
    assert seen[0]["schema"] == "找素材.schema.json" and seen[0]["web"] is True
    assert seen[0]["model"] == "gpt-6-sol" and seen[0]["effort"] == "high"
    assert "画面太少，多给车载" in seen[0]["prompt"] and "G2/G3" in seen[0]["prompt"]
    assert "AAAAAAAAAAA" in seen[0]["prompt"]
    assert "CCCCCCCCCCC" not in "".join(probed)

    notes = {f["title"]: (f["state"], f.get("note", "")) for f in _manifest(ep)["found"]}
    assert notes["外站"][1] == "只收 http/https 链接"
    assert notes["频道页"][1] == "不是单个视频页"
    assert notes["库里有"][1] == "素材库里已有或已在下载"
    assert "超过上限 10 分钟" in notes["太长"][1]
    assert notes["重复"][1] == "素材库里已有或已在下载"
    assert notes["多余"][1] == "超过数量上限 2 条"
    assert notes["B站"][1] == "F1 官网、B 站的链接不收"
    assert "bilibili" not in "".join(probed)
    assert notes["车载"][0] == notes["轮胎"][0] == "下载中"
    rows = conn.execute("SELECT id, url, episode_id FROM downloads ORDER BY id").fetchall()
    assert [(r["url"], r["episode_id"]) for r in rows] == [
        ("https://www.youtube.com/watch?v=BBBBBBBBBBB", ep["id"]),
        ("https://www.youtube.com/watch?v=TYRETYRETYR", ep["id"])]

    # 还在排队：再进来不重复调 GPT，继续等
    with pytest.raises(StepDeferred):
        _run(conn, ep, runner=_no_gpt(), search=_search({mine}))

    # 一条下好入库，一条失败
    new = _asset(conn, tmp_path, 10, source_url="https://www.youtube.com/watch?v=BBBBBBBBBBB")
    conn.execute("UPDATE downloads SET state='done', asset_id=?, finished_at=? WHERE id=?", (new, db.now(), rows[0]["id"]))
    conn.execute("UPDATE downloads SET state='failed', error='ERROR: HTTP 403\n更多' WHERE id=?", (rows[1]["id"],))
    out = _run(conn, ep, runner=_no_gpt(), search=_search({mine}))
    assert out["asset_ids"] == [mine, new]
    assert out["gpt_called"] and out["gpt_found"] == len(items)
    assert out["accepted"] == 2 and out["downloaded"] == 1
    assert out["failed"] == [{"url": "https://www.youtube.com/watch?v=TYRETYRETYR", "reason": "ERROR: HTTP 403"}]
    assert len(out["skipped"]) == 7
    assert _manifest(ep)["asset_ids"] == [mine, new]
    assert "下载成功 1 条" in sourcing.summary_text(out)


def test_download_wait_times_out(conn, ep, tmp_path, monkeypatch):
    mine = _asset(conn, tmp_path, 1)
    (Path(ep["folder"]) / "04_素材" / sourcing.MANIFEST).write_text(json.dumps({"asset_ids": [mine]}))
    items = [{"url": "https://www.youtube.com/watch?v=BBBBBBBBBBB", "title": "车载", "channel": "F1",
              "segment": "赛道", "reason": "r"}]
    with pytest.raises(StepDeferred):
        _run(conn, ep, runner=_runner(items), search=_search({mine}), probe=_probe({}))
    monkeypatch.setattr(sourcing, "WAIT_LIMIT", -1)
    out = _run(conn, ep, runner=_no_gpt(), search=_search({mine}))
    assert out["asset_ids"] == [mine] and out["failed"][0]["reason"].startswith("等太久")


def test_probe_failure_skips(conn, ep, tmp_path):
    mine = _asset(conn, tmp_path, 1)
    (Path(ep["folder"]) / "04_素材" / sourcing.MANIFEST).write_text(json.dumps({"asset_ids": [mine]}))
    url = "https://www.youtube.com/watch?v=BBBBBBBBBBB"
    items = [{"url": url, "title": "车载", "channel": "F1", "segment": "赛道", "reason": "r"}]
    out = _run(conn, ep, runner=_runner(items), search=_search({mine}),
               probe=_probe({url: RuntimeError("Video unavailable")}))
    assert out["asset_ids"] == [mine] and out["downloaded"] == 0
    assert "Video unavailable" in out["skipped"][0]["reason"]
    assert conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 0


# ---------- 一个都没有 ----------

def test_nothing_found_not_ready(conn, ep, tmp_path):
    _filler(conn, tmp_path, _other_ep(conn))
    with pytest.raises(StepNotReady, match="链接"):
        _run(conn, ep, runner=_runner([]), search=_search(set()), probe=_probe({}))


def test_all_downloads_failed_not_ready(conn, ep, tmp_path):
    items = [{"url": "https://www.youtube.com/watch?v=BBBBBBBBBBB", "title": "车载", "channel": "F1",
              "segment": "赛道", "reason": "r"}]
    with pytest.raises(StepDeferred):
        _run(conn, ep, runner=_runner(items), search=_search(set()), probe=_probe({}))
    conn.execute("UPDATE downloads SET state='failed', error='boom'")
    with pytest.raises(StepNotReady):
        _run(conn, ep, runner=_no_gpt(), search=_search(set()))


def test_gpt_error_with_empty_pool_raises_and_with_pool_continues(conn, ep, tmp_path):
    with pytest.raises(RuntimeError, match="断网"):
        _run(conn, ep, runner=_runner(RuntimeError("断网")), search=_search(set()))
    with pytest.raises(codex_runner.QuotaExhausted):
        _run(conn, ep, runner=_runner(codex_runner.QuotaExhausted("没额度")), search=_search(set()))
    mine = _asset(conn, tmp_path, 1)
    (Path(ep["folder"]) / "04_素材" / sourcing.MANIFEST).write_text(json.dumps({"asset_ids": [mine]}))
    out = _run(conn, ep, runner=_runner(RuntimeError("断网")), search=_search({mine}))
    assert out["asset_ids"] == [mine] and any("断网" in n for n in out["notes"])


# ---------- 步骤接线 ----------

def test_step_passes_gate_feedback_and_model(conn, ep, monkeypatch):
    conn.execute("INSERT INTO gates(episode_id, gate, state, feedback, created_at) VALUES (?, 'G2', 'rejected', ?, ?)",
                 (ep["id"], "开头画面不对", db.now()))
    conn.execute("INSERT INTO gates(episode_id, gate, state, feedback, created_at) VALUES (?, 'G1', 'rejected', ?, ?)",
                 (ep["id"], "文案太长", db.now()))
    got = {}

    async def fake_run(conn, ep, **kw):
        got.update(kw)
        return {"asset_ids": [1], "existing": 1, "from_library": 0, "gpt_called": False, "gpt_found": 0,
                "accepted": 0, "downloaded": 0, "failed": [], "skipped": [], "notes": [], "short_segments": []}

    monkeypatch.setattr(sourcing, "run", fake_run)
    out = asyncio.run(steps.find_assets({"id": 7, "model": "程序", "effort": None}, ep))
    assert out["asset_ids"] == [1]
    assert got["feedback"] == ["开头画面不对"] and got["task_id"] == 7 and got["redo"] == "开头画面不对"
    assert (got["model"], got["effort"]) == config.ROLE_MODELS["找素材"]
    msgs = [r[0] for r in conn.execute("SELECT content FROM messages WHERE episode_id=?", (ep["id"],))]
    assert any(m.startswith("找素材：") for m in msgs)


# ---------- 只收 YouTube、X、Instagram ----------

def test_sites_screened_by_safety_and_playlist(conn, ep, tmp_path):
    """只收 YouTube、X、Instagram（2026-10-14）：别的网站不解析、不取信息直接跳过；收的链接照样过安全检查、去重、合集。"""
    mine = _asset(conn, tmp_path, 1, source_url="https://www.youtube.com/watch?v=OLDOLDOLDOL&utm_source=x")
    (Path(ep["folder"]) / "04_素材" / sourcing.MANIFEST).write_text(json.dumps({"asset_ids": [mine]}))
    onboard = "https://www.youtube.com/watch?v=ONBOARDONBO"
    items = [{"url": u, "title": t, "channel": "c", "segment": "赛道", "reason": "r"} for u, t in [
        (onboard, "车载"),
        (onboard + "&utm_campaign=y#top", "跟踪参数重复"),
        ("https://youtu.be/OLDOLDOLDOL", "库里有"),
        ("https://www.youtube.com/watch?v=PLAYLISTPLA", "合集"),
        ("https://user:pw@www.youtube.com/watch?v=PASSWORDPAS", "带密码"),
        ("https://youtu.be/SHORTSHORTS", "跳内网"),
        ("https://www.fia.com/video/sepang-onboard", "FIA"),
        ("https://intranet.example.org/v/1", "内网"),
        ("http://192.168.0.8/v.mp4", "IP"),
        ("https://x.com/F1/status/1845000000000000000", "X 视频"),
    ]]
    probed = []

    async def probe(url):
        probed.append(url)
        if "PLAYLIST" in url:
            return {"_type": "playlist", "webpage_url": url, "entries": [{}, {}]}
        if "SHORT" in url:
            return {"webpage_url": "https://intranet.example.org/real", "duration": 60}
        return {"webpage_url": url, "duration": 90, "title": "t", "uploader": "FORMULA 1"}

    with pytest.raises(StepDeferred):
        _run(conn, ep, runner=_runner(items), search=_search({mine}), probe=probe)
    found = {f["title"]: f for f in _manifest(ep)["found"]}
    assert found["车载"]["state"] == "下载中" and found["车载"]["site"] == "YouTube"
    assert found["跟踪参数重复"]["note"] == found["库里有"]["note"] == "素材库里已有或已在下载"
    assert found["合集"]["note"].startswith("不是单个视频页")
    assert found["带密码"]["note"] == "链接里带账号密码"
    assert found["跳内网"]["note"].startswith("跳转后的地址不收")
    for t in ("FIA", "内网", "IP"):
        assert found[t]["state"] == "跳过" and found[t]["note"] == "只收 YouTube、X、Instagram 链接"
    x = "https://x.com/F1/status/1845000000000000000"
    assert found["X 视频"]["state"] == "下载中"
    assert probed == [onboard, "https://www.youtube.com/watch?v=PLAYLISTPLA", "https://youtu.be/SHORTSHORTS", x]
    assert [r[0] for r in conn.execute("SELECT url FROM downloads ORDER BY id")] == [onboard, x]


# ---------- 打回后要不要重新找素材 ----------

def test_asks_new_material_and_named_segments():
    ask = sourcing.asks_new_material
    assert ask("赛道这段换成车载画面") and ask("轮胎段素材太少") and ask("别用那个镜头")
    assert not ask("节奏太慢") and not ask("画面不错") and not ask("换个开头") and not ask(None)
    segs = [{"name": "赛道"}, {"name": "赛道历史"}, {"name": "轮胎"}]
    assert sourcing.named_segments("## 赛道历史 这段画面换掉", segs) == ["赛道历史"]
    assert sourcing.named_segments("赛道和轮胎的画面都换", segs) == ["赛道", "轮胎"]
    assert sourcing.named_segments("画面整体换一批", segs) == []


@pytest.mark.parametrize("redo", [None, "节奏太慢，开头再紧凑点"])
def test_enough_material_redo_without_ask_skips_gpt(conn, ep, tmp_path, redo):
    _filler(conn, tmp_path, _other_ep(conn))
    mine = _asset(conn, tmp_path, 20, title="雪邦车载")
    (Path(ep["folder"]) / "04_素材" / sourcing.MANIFEST).write_text(json.dumps({"asset_ids": [mine]}))
    out = _run(conn, ep, runner=_no_gpt(), search=_search({mine}), redo=redo)
    assert out["gpt_called"] is False and out["forced_by_feedback"] is False and out["forced_segments"] == []


def test_enough_material_redo_asking_swap_forces_gpt(conn, ep, tmp_path):
    _filler(conn, tmp_path, _other_ep(conn))
    mine = _asset(conn, tmp_path, 20, title="雪邦车载")
    (Path(ep["folder"]) / "04_素材" / sourcing.MANIFEST).write_text(json.dumps({"asset_ids": [mine]}))
    seen = []
    out = _run(conn, ep, runner=_runner([], seen), search=_search({mine}), probe=_probe({}),
               redo="赛道这段换成车载画面")
    assert out["gpt_called"] and out["forced_by_feedback"] and out["forced_segments"] == ["赛道"]
    prompt = seen[0]["prompt"]
    assert "缺（用户要求换素材）" in prompt.split("- 赛道")[1].split("\n")[0]
    assert "缺（用户要求换素材）" not in prompt.split("- 轮胎")[1].split("\n")[0]
    assert "## 用户打回时要求换素材" in prompt and "赛道这段换成车载画面" in prompt
    assert "按打回意见重找（赛道）" in sourcing.summary_text(out)

    seen.clear()
    out = _run(conn, ep, runner=_runner([], seen), search=_search({mine}), probe=_probe({}), redo="画面整体太少")
    assert out["forced_segments"] == ["全片"]
    assert seen[0]["prompt"].count("缺（用户要求换素材）") == 2


def _gate(conn, ep, gate, feedback, at):
    conn.execute("INSERT INTO gates(episode_id, gate, state, feedback, created_at, decided_at) "
                 "VALUES (?, ?, 'rejected', ?, ?, ?)", (ep["id"], gate, feedback, at, at))


def _d2_done(conn, ep, at, output=None):
    conn.execute("INSERT INTO tasks(episode_id, stage, step, stage_run, phase, role, model, effort, state, "
                 "created_at, finished_at, output) VALUES (?, '配素材', 'D2找素材', 1, 1, '找素材', '程序', '', "
                 "'succeeded', ?, ?, ?)", (ep["id"], at, at, None if output is None else json.dumps(output)))


def test_redo_feedback_only_latest_after_last_d2(conn, ep):
    assert steps._redo_feedback(conn, ep["id"]) is None
    _gate(conn, ep, "G2", "赛道这段换成车载画面", "2026-10-01T10:00:00+00:00")
    _gate(conn, ep, "G1", "文案素材太少", "2026-10-01T10:30:00+00:00")
    assert steps._redo_feedback(conn, ep["id"]) == "赛道这段换成车载画面"
    _d2_done(conn, ep, "2026-10-01T11:00:00+00:00")
    assert steps._redo_feedback(conn, ep["id"]) is None          # 旧意见已经按它找过了
    _gate(conn, ep, "G3", "节奏太慢", "2026-10-01T12:00:00+00:00")
    assert steps._redo_feedback(conn, ep["id"]) == "节奏太慢"     # 只看最近一次，交给 sourcing 判断不触发
    assert steps._gate_feedback(conn, ep["id"], ("G2", "G3")) == ["赛道这段换成车载画面", "节奏太慢"]


def test_redo_feedback_survives_forced_d2_without_new_material(conn, ep):
    _gate(conn, ep, "G2", "名场面段换成对应比赛的画面", "2026-10-01T10:00:00+00:00")
    _d2_done(conn, ep, "2026-10-01T11:00:00+00:00",
             {"forced_by_feedback": True, "forced_segments": ["名场面"], "gpt_found": 5, "accepted": 5, "downloaded": 0})
    assert steps._redo_feedback(conn, ep["id"]) == "名场面段换成对应比赛的画面"   # 一条都没下到，意见还要用
    _d2_done(conn, ep, "2026-10-01T12:00:00+00:00",
             {"forced_by_feedback": True, "forced_segments": ["名场面"], "gpt_found": 3, "accepted": 3, "downloaded": 1})
    assert steps._redo_feedback(conn, ep["id"]) is None


def test_redo_feedback_unforced_d2_still_clears_older_feedback(conn, ep):
    _gate(conn, ep, "G2", "名场面段换成对应比赛的画面", "2026-10-01T10:00:00+00:00")
    _d2_done(conn, ep, "2026-10-01T11:00:00+00:00",
             {"forced_by_feedback": False, "forced_segments": [], "gpt_found": 0, "accepted": 0, "downloaded": 0})
    assert steps._redo_feedback(conn, ep["id"]) is None
    _gate(conn, ep, "G3", "节奏太慢", "2026-10-01T12:00:00+00:00")
    _d2_done(conn, ep, "2026-10-01T11:30:00+00:00", {"forced_by_feedback": True, "downloaded": 0})
    assert steps._redo_feedback(conn, ep["id"]) == "节奏太慢"
    _d2_done(conn, ep, "2026-10-01T13:00:00+00:00")
    conn.execute("UPDATE tasks SET output='不是 JSON' WHERE episode_id=? AND finished_at=?",
                 (ep["id"], "2026-10-01T13:00:00+00:00"))
    assert steps._redo_feedback(conn, ep["id"]) is None             # 解析不了的照旧算完成


# ---------- F1 官网视频页 ----------

F1_NEW = "https://www.formula1.com/en/video/race-highlights-malaysia-2016.6060976994001"
F1_OLD = "https://www.formula1.com/en/latest/video.race-highlights-malaysia-2016.6060976994001.html"
F1_NEW_PLATFORM = "https://www.formula1.com/en/video/pierre-gasly-in-bahrain-testing-day-2.1856945568392700356"


def test_f1_url_rewrite_and_video_key():
    from hermes import download
    assert download.ytdlp_url(F1_NEW) == F1_OLD
    assert download.ytdlp_url("https://formula1.com/en/video/race-highlights-malaysia-2016.6060976994001/") == F1_OLD
    assert download.ytdlp_url(F1_OLD) == F1_OLD
    assert download.ytdlp_url(F1_NEW_PLATFORM) == F1_NEW_PLATFORM
    for other in ("https://www.youtube.com/watch?v=abcdefghijk", "https://www.fia.com/en/video/a.6060976994001",
                  "https://f1tv.formula1.com/en/video/a.6060976994001", "https://www.formula1.com/en/video"):
        assert download.ytdlp_url(other) == other
    assert sourcing.video_key(F1_NEW) == sourcing.video_key(F1_OLD) == "formula1:6060976994001"
    assert sourcing.video_key(F1_NEW_PLATFORM) == "formula1:1856945568392700356"


# ---------- 复盘期先用本站画面（2026-10-14 用户定） ----------

RACE = {"event": "Singapore Grand Prix", "season": 2026, "name_zh": "新加坡大奖赛"}


@pytest.fixture
def rep(conn, db_path, monkeypatch):
    """2026 新加坡站复盘；赛历只放新加坡和英国两站。"""
    from datetime import datetime, timezone
    from hermes import glossary
    monkeypatch.setattr(db, "connect", lambda path=None: ORIG_CONNECT(db_path))
    monkeypatch.setattr(prompts, "assemble", lambda branch, step, ctx="", notes="": f"{branch}|{step}|{ctx}|{notes}")
    for rnd, en, loc, day, zh in ((12, "British Grand Prix", "Silverstone", "2026-07-05", "英国大奖赛"),
                                  (17, "Singapore Grand Prix", "Marina Bay", "2026-10-11", "新加坡大奖赛")):
        conn.execute("INSERT INTO race_events(season, round, name_en, location, event_date, format, updated_at) "
                     "VALUES (2026, ?, ?, ?, ?, 'conventional', ?)", (rnd, en, loc, day, db.now()))
        glossary.add_manual(conn, "大奖赛", en, zh)
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="复盘", gp_label="新加坡大奖赛", title="新加坡复盘",
                   when=datetime(2026, 10, 13, 4, tzinfo=timezone.utc))
    row = conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
    (Path(row["folder"]) / "02_文案" / "逐字稿.md").write_text(SCRIPT, encoding="utf-8")
    (Path(row["folder"]) / "04_素材" / sourcing.MANIFEST).unlink(missing_ok=True)
    return row


@pytest.fixture
def notes(monkeypatch):
    got = []
    monkeypatch.setattr(sourcing, "notify", lambda title, text, eid=None: got.append(text))
    return got


def _rasset(conn, tmp_path, shots, title, uploader=None) -> int:
    from hermes import asset_identity
    aid = _asset(conn, tmp_path, shots, title=title)
    conn.execute("UPDATE assets SET uploader=? WHERE id=?", (uploader, aid))
    asset_identity.apply(conn, aid)
    return aid


def _card(conn, ep):
    from hermes import versions
    rows = [json.loads(r[0]) for r in conn.execute(
        "SELECT card FROM messages WHERE episode_id=? AND role='card'", (ep["id"],))]
    return [c for c in rows if c.get("type") == "other_library"], versions


def test_race_same_race_first_other_race_not_taken(conn, rep, tmp_path, notes):
    from hermes import asset_identity
    assert asset_identity.episode_race(conn, rep) == RACE
    _filler(conn, tmp_path, _other_ep(conn))
    sg = _rasset(conn, tmp_path, 20, "Singapore GP 2026 highlights", "FORMULA 1")
    sg2 = _rasset(conn, tmp_path, 2, "2026 新加坡大奖赛 排位 Norris 车载")
    gb = _rasset(conn, tmp_path, 30, "F12026-英国大奖赛回放")
    # 上一轮清单里带着英国站：这次不再带
    (Path(rep["folder"]) / "04_素材" / sourcing.MANIFEST).write_text(json.dumps({"asset_ids": [gb]}))
    out = _run(conn, rep, runner=_no_gpt(), search=_search({gb, sg, sg2}))
    assert set(out["asset_ids"]) == {sg, sg2} and gb not in out["asset_ids"]
    assert out["own_race"] == 2 and out["dropped_other"] == [gb] and out["gpt_called"] is False
    assert out["other_library"] is None and notes == []
    m = _manifest(rep)
    assert m["race"] == RACE
    assert {a["id"]: (a["gp_event"], a["season"], a["same_race"]) for a in m["assets"]} == {
        sg: ("Singapore Grand Prix", 2026, True), sg2: ("Singapore Grand Prix", 2026, True)}
    text = sourcing.summary_text(out)
    assert "2026 新加坡大奖赛" in text and "本站素材 2 条" in text and "其他站素材 1 条" in text


def test_race_ask_then_allow(conn, rep, tmp_path, notes):
    _filler(conn, tmp_path, _other_ep(conn))
    gb = _rasset(conn, tmp_path, 30, "F12026-英国大奖赛回放")
    items = [{"url": "https://www.youtube.com/watch?v=SGSGSGSGSGS", "title": "Singapore GP 2026 highlights",
              "channel": "FORMULA 1", "segment": "赛道", "reason": "r"},
             {"url": "https://www.youtube.com/watch?v=GBGBGBGBGBG", "title": "British GP 2026 highlights",
              "channel": "FORMULA 1", "segment": "赛道", "reason": "r"}]
    seen = []
    with pytest.raises(StepDeferred, match="下载"):
        _run(conn, rep, runner=_runner(items, seen), search=_search({gb}), probe=_probe({}))
    assert "2026 年新加坡大奖赛" in seen[0]["prompt"] and "新加坡大奖赛举办方官方账号" in seen[0]["prompt"]
    found = {f["title"]: f for f in _manifest(rep)["found"]}
    assert found["Singapore GP 2026 highlights"]["state"] == "下载中"
    assert found["Singapore GP 2026 highlights"]["same_race"] is True
    assert found["British GP 2026 highlights"]["note"] == "标题看不是本站本年的画面"

    # 本站那条没下成：不自己拿英国站，发卡片问
    conn.execute("UPDATE downloads SET state='failed', error='HTTP 403'")
    with pytest.raises(StepDeferred, match="其他站"):
        _run(conn, rep, runner=_no_gpt(), search=_search({gb}))
    cards, versions = _card(conn, rep)
    assert len(cards) == 1 and cards[0]["state"] == "ask" and cards[0]["own"] == 0
    assert cards[0]["race"] == "2026 新加坡大奖赛" and len(notes) == 1 and "其他站" in notes[0]
    assert _manifest(rep)["other_library"] == "ask"
    # 同一轮只问一次：没决定前回来看还是等，不再发卡片、不再调 GPT
    with pytest.raises(StepDeferred, match="其他站"):
        _run(conn, rep, runner=_no_gpt(), search=_search({gb}))
    assert len(_card(conn, rep)[0]) == 1 and len(notes) == 1

    with pytest.raises(ValueError):
        sourcing.decide_other_library(conn, rep, "maybe")
    assert sourcing.decide_other_library(conn, rep, "allow") == {"other_library": "allow"}
    with pytest.raises(LookupError):
        sourcing.decide_other_library(conn, rep, "deny")
    assert _card(conn, rep)[0][0]["state"] == "allow"
    out = _run(conn, rep, runner=_no_gpt(), search=_search({gb}))
    assert out["asset_ids"] == [gb] and out["other_added"] == [gb] and out["other_library"] == "allow"
    assert out["gpt_same_race"] == 0 and _manifest(rep)["assets"][0]["same_race"] is False
    assert "同意用其他站画面" in sourcing.summary_text(out)
    # 再回来不重复补
    assert _run(conn, rep, runner=_no_gpt(), search=_search({gb}))["asset_ids"] == [gb]


def test_race_no_race_footage_ask_then_deny(conn, rep, tmp_path, notes):
    _filler(conn, tmp_path, _other_ep(conn))
    onboard = _rasset(conn, tmp_path, 20, "2026 新加坡大奖赛 排位 Norris 车载")
    gb = _rasset(conn, tmp_path, 30, "F12026-英国大奖赛回放")
    seen = []
    # 本站只有排位车载，没有正赛回放或集锦：素材数够也要让 GPT 找
    with pytest.raises(StepDeferred, match="其他站"):
        _run(conn, rep, runner=_runner([], seen), search=_search({gb, onboard}), probe=_probe({}))
    assert seen
    cards, _ = _card(conn, rep)
    assert cards[0]["own"] == 1 and "没有正赛的回放或集锦" in cards[0]["why"] and cards[0]["short"] == []
    sourcing.decide_other_library(conn, rep, "deny")
    out = _run(conn, rep, runner=_no_gpt(), search=_search({gb, onboard}))
    assert out["asset_ids"] == [onboard] and out["other_library"] == "deny" and out["other_added"] == []
    assert "不用其他站画面" in sourcing.summary_text(out)


def test_race_mode_off_for_preview(conn, ep, tmp_path):
    from hermes import asset_identity
    assert asset_identity.episode_race(conn, ep) is None
    _filler(conn, tmp_path, _other_ep(conn))
    other = _asset(conn, tmp_path, 20, title="F12026-英国大奖赛回放")
    out = _run(conn, ep, runner=_no_gpt(), search=_search({other}))
    assert out["asset_ids"] == [other] and "race" not in out and "race" not in _manifest(ep)


def test_screen_same_race_replay_cap(conn):
    from hermes import glossary
    conn.execute("INSERT INTO race_events(season, round, name_en, location, event_date, format, updated_at) "
                 "VALUES (2026, 17, 'Singapore Grand Prix', 'Marina Bay', '2026-10-11', 'conventional', ?)", (db.now(),))
    glossary.add_manual(conn, "大奖赛", "Singapore Grand Prix", "新加坡大奖赛")
    urls = {"full": "https://www.youtube.com/watch?v=FULLFULLFUL", "toolong": "https://www.youtube.com/watch?v=LONGLONGLON",
            "plain": "https://www.youtube.com/watch?v=PLAINPLAINP", "yt": "https://www.youtube.com/watch?v=YTTITLEYTTI"}
    items = [{"url": urls["full"], "title": "F12026-新加坡大奖赛回放", "channel": "", "segment": "", "reason": ""},
             {"url": urls["toolong"], "title": "F12026-新加坡大奖赛回放 加长版", "channel": "", "segment": "", "reason": ""},
             {"url": urls["plain"], "title": "车载合集", "channel": "", "segment": "", "reason": ""},
             {"url": urls["yt"], "title": "", "channel": "", "segment": "", "reason": ""}]

    async def probe(url):
        title = "2026 Singapore Grand Prix full race replay" if url == urls["yt"] else "t"
        return {"webpage_url": url, "title": title, "uploader": "FORMULA 1",
                "duration": {urls["toolong"]: 200 * 60}.get(url, 150 * 60)}

    out = asyncio.run(sourcing.screen(conn, items, limit=5, max_minutes=15, probe=probe, race=RACE, race_minutes=180))
    full, toolong, plain, yt = out
    assert full["state"] == "待下载" and full["same_race"] is True
    assert toolong["state"] == "跳过" and "超过上限 180 分钟" in toolong["note"]
    assert plain["state"] == "跳过" and plain["same_race"] is None and "超过上限 15 分钟" in plain["note"]
    assert yt["state"] == "待下载" and yt["same_race"] is True   # GPT 没给标题，用 yt-dlp 拿到的标题认


def test_other_library_endpoint(conn, rep, monkeypatch):
    from fastapi.testclient import TestClient
    from hermes import server

    class FakeAppServer:
        async def read_rate_limits(self):
            return None

        async def close(self):
            pass

    monkeypatch.setattr(server, "AppServer", FakeAppServer)
    monkeypatch.setattr(server.pricing, "refresh_fx", lambda conn, force=False: {})
    path = Path(rep["folder"]) / "04_素材" / sourcing.MANIFEST
    path.write_text(json.dumps({"stage_run": rep["stage_run"], "asset_ids": [], "other_library": "ask"}))
    conn.execute("UPDATE episodes SET state='暂停' WHERE id=?", (rep["id"],))   # 别让调度器把它排进工作阶段
    H = {"Host": "127.0.0.1:8790"}
    W = {**H, "X-Hermes": "1"}
    url = f"/api/episodes/{rep['id']}/sourcing/other_library"
    with TestClient(server.app, base_url="http://127.0.0.1:8790") as c:
        assert c.post(f"{url}/deny", headers=H).status_code == 403
        assert c.post(f"{url}/maybe", headers=W).status_code == 404
        r = c.post(f"{url}/deny", headers=W)
        assert r.status_code == 200 and r.json()["other_library"] == "deny"
        assert c.post(f"{url}/allow", headers=W).status_code == 409
        assert c.post("/api/episodes/9999/sourcing/other_library/allow", headers=W).status_code == 404
    assert _manifest(rep)["other_library"] == "deny"
