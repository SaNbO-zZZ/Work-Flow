"""2026-10-14 用户定：比赛实况只用 FORMULA 1 官方 YouTube 频道的本站集锦；车队、F1 官方、举办方的运营视频和航拍
可以来自 YouTube、X、Instagram；关键点、名场面段用比赛画面，其余段用运营、航拍，讲到哪支车队用它自己的运营视频。
打标要列出每个角的台标。"""
import asyncio

import pytest

from hermes import asset_identity, assets, edit, sourcing


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    monkeypatch.setattr(sourcing.socket, "getaddrinfo", lambda host, port, *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 0))])


def test_instruction_sites_and_race_footage():
    text = sourcing.INSTRUCTION.format(max_minutes=15, limit=6, ops_minutes=30)
    assert "1. 比赛实况（练习、排位、冲刺、正赛的赛道画面）只给 YouTube 上 FORMULA 1 官方频道发布的集锦" in text
    assert "可以是 YouTube、X、Instagram 上官方账号发的单个视频或帖子" in text
    assert "不要给 F1 官网（formula1.com）、B 站的链接" in text and "赛道举办方（大奖赛主办方）" in text
    assert "3. 按段的场景找" in text and "讲升级、研发、新零件、空气动力的段，找车队工厂、风洞、模拟器" in text and "其他段讲到哪支车队就找这支车队自己发的运营视频" in text
    assert "5. 单条不超过 15 分钟" in text and "最多 6 条" in text and "只给 YouTube 单个视频页" not in text


def test_race_rules_sites():
    text = sourcing.RACE_RULES.format(season=2026, name_zh="新加坡大奖赛", event="Singapore Grand Prix",
                                      race_minutes=180)
    assert "本期讲的是 2026 年新加坡大奖赛（2026 Singapore Grand Prix）" in text
    assert "比赛实况：YouTube 上 FORMULA 1 官方频道发布的本站集锦，赛段和本期类型对应（正赛复盘找正赛集锦）" in text
    assert "各车队官方在 YouTube、X、Instagram 发的幕后" in text and "新加坡大奖赛举办方官方账号发的赛道航拍" in text
    assert "完整回放" not in text and "180 分钟" not in text


def test_video_key_and_site_problem():
    k = sourcing.video_key
    assert k("https://x.com/F1/status/1845000000000000000/video/1") == k(
        "https://twitter.com/F1/status/1845000000000000000?s=20") == "x:1845000000000000000"
    assert k("https://x.com/F1") is None and k("https://x.com/F1/status/abc") is None
    assert k("https://www.instagram.com/reel/DAbCdEfGh12/?igsh=x") == k("https://instagram.com/p/DAbCdEfGh12/") \
        == k("https://www.instagram.com/scuderiaferrari/reel/DAbCdEfGh12/") == "instagram:DAbCdEfGh12"
    assert k("https://www.instagram.com/scuderiaferrari/") is None
    p = sourcing.site_problem
    for ok in ("https://youtu.be/abcdefghijk", "https://m.youtube.com/watch?v=abcdefghijk", "https://x.com/a/status/12345",
               "https://mobile.twitter.com/a/status/12345", "https://www.instagram.com/reel/DAbCdEfGh12/"):
        assert p(ok) is None
    for bad in ("https://www.formula1.com/en/video/a.6060976994001", "https://f1tv.formula1.com/x",
                "https://www.bilibili.com/video/BV1xx411c7mD", "https://b23.tv/AbCd12"):
        assert p(bad) == "F1 官网、B 站的链接不收"
    assert p("https://www.fia.com/en/video/a.1") == p("https://notyoutube.com/watch?v=abcdefghijk") \
        == "只收 YouTube、X、Instagram 链接"
    assert p("ftp://example.com/v.mp4") is None   # 交给 url_problem


def test_screen_sites_and_race_footage_only_f1_youtube(conn):
    yt = "https://www.youtube.com/watch?v="
    meta = {
        yt + "HIGHLIGHTS1": ("2026 Singapore Grand Prix Race Highlights", "FORMULA 1"),
        yt + "FANREUPLOAD": ("Singapore GP 2026 Race Highlights", "F1 Fan Clips"),
        yt + "F1RADIOOPS1": ("Best Team Radio | 2026 Singapore Grand Prix", "FORMULA 1"),
        yt + "GPTSAYSHL01": ("t", "Random"),
        "https://x.com/F1/status/1845000000000000001": ("Race Highlights Singapore", "Formula 1"),
        "https://x.com/ScuderiaFerrari/status/1845000000000000002": ("Behind the scenes in Singapore",
                                                                     "Scuderia Ferrari HP"),
        "https://www.instagram.com/reel/MCLPOLELAP1/": ("Pole lap onboard Singapore qualifying", "McLaren"),
        "https://www.instagram.com/reel/SGPAERIAL01/": ("Marina Bay from above", "singaporegp"),
        "https://youtu.be/TOBILIBILI": None,
    }
    probed = []

    async def probe(url):
        probed.append(url)
        if meta[url] is None:
            return {"webpage_url": "https://www.bilibili.com/video/BV1xx411c7mD", "duration": 60}
        title, who = meta[url]
        return {"webpage_url": url, "duration": 120, "title": title, "uploader": who}

    skipped = ["https://www.formula1.com/en/video/a.6060976994001", "https://www.bilibili.com/video/BV1xx411c7mD",
               "https://www.fia.com/en/video/a.6060976994001"]
    items = [{"url": u, "title": "Race highlights" if "GPTSAYS" in u else "", "channel": "", "segment": "s",
              "reason": "r"} for u in skipped + list(meta)]
    out = {f["url"]: f for f in asyncio.run(sourcing.screen(conn, items, limit=20, max_minutes=15, probe=probe))}
    note = lambda u: (out[u]["state"], out[u].get("note"))
    assert note(skipped[0]) == note(skipped[1]) == ("跳过", "F1 官网、B 站的链接不收")
    assert note(skipped[2]) == ("跳过", "只收 YouTube、X、Instagram 链接")
    assert probed == list(meta)
    race_only = ("跳过", "比赛实况只收 F1 官方 YouTube 频道")
    for u in (yt + "FANREUPLOAD", yt + "GPTSAYSHL01", "https://x.com/F1/status/1845000000000000001",
              "https://www.instagram.com/reel/MCLPOLELAP1/"):
        assert note(u) == race_only, u
    for u in (yt + "HIGHLIGHTS1", yt + "F1RADIOOPS1", "https://x.com/ScuderiaFerrari/status/1845000000000000002",
              "https://www.instagram.com/reel/SGPAERIAL01/"):
        assert out[u]["state"] == "待下载", u
    assert out["https://x.com/ScuderiaFerrari/status/1845000000000000002"]["site"]
    assert note("https://youtu.be/TOBILIBILI") == ("跳过", "F1 官网、B 站的链接不收")


def test_team_of_and_aliases():
    t = asset_identity.team_of
    assert t({"uploader": "Scuderia Ferrari HP"}) == "法拉利" and t({"uploader": "McLaren"}) == "迈凯伦"
    assert t({"uploader": "Mercedes-AMG PETRONAS F1 Team"}) == "梅赛德斯"
    assert t({"uploader": "Oracle Red Bull Racing"}) == "红牛"
    assert t({"uploader": "Visa Cash App Racing Bulls F1 Team"}) == t({"uploader": "Visa Cash App RB F1 Team"}) \
        == "Racing Bulls"
    assert t({"uploader": "Aston Martin Aramco F1 Team"}) == "阿斯顿·马丁" and t({"uploader": "MoneyGram Haas F1 Team"}) == "哈斯"
    assert t({"uploader": "Audi Revolut F1 Team"}) == "奥迪" and t({"uploader": "Stake F1 Team Kick Sauber"}) == "索伯"
    assert t({"uploader": "FORMULA 1"}) is None and t({"uploader": "Ferrari fan club"}) is None and t({}) is None
    assert "木瓜" in asset_identity.team_aliases("迈凯伦") and "梅奔" in asset_identity.team_aliases("梅赛德斯")
    m = asset_identity.team_mentioned
    assert m("迈凯伦", "木瓜这次策略保守") and m("梅赛德斯", "梅奔两车") and m("法拉利", "Ferrari 赛后")
    assert m("Racing Bulls", "小红牛的劳森") and not m("红牛", "小红牛的劳森") and m("红牛", "红牛和小红牛")
    assert not m("法拉利", "迈凯伦一二") and not m(None, "法拉利")


BASE = {"names": [], "main": [], "has_subtitles": False, "has_logo": False, "duration": 3.0, "cross_used": False}


def test_score_team_ops_and_race_footage():
    ferrari = {**BASE, "category": "车队运营", "team": "法拉利"}
    mclaren = {**BASE, "category": "车队运营", "team": "迈凯伦"}
    text = "法拉利这一站的问题出在策略上"
    assert edit._score(ferrari, text, 0.0) - edit._score(mclaren, text, 0.0) == pytest.approx(0.25)
    assert edit._score(ferrari, text, 0.0, True) == edit._score(mclaren, text, 0.0, True) == 0   # 关键段不加
    hl = {**BASE, "category": "集锦"}
    racing = {**BASE, "main": ["比赛中"]}
    grid = {**BASE, "main": ["发车格"]}
    for s in (hl, racing, grid, {**BASE, "category": "比赛回放"}):
        assert edit._score(s, text, 0.0) == pytest.approx(-0.15) and edit._score(s, text, 0.0, True) == 0
    assert edit._score({**BASE, "main": ["维修区车库"]}, text, 0.0) == 0
    assert edit.key_segment("诺里斯在 7 号弯完成超车") and edit.key_segment("赛车故障") and not edit.key_segment(text)


def test_team_marker_and_candidates(conn, monkeypatch):
    assert edit.team_of({"category": "车队运营", "uploader": "Scuderia Ferrari HP"}) == "法拉利"
    assert edit.team_of({"category": "集锦", "uploader": "Scuderia Ferrari HP"}) is None
    line = dict(BASE, asset_id=1, shot_idx=0, start=0.0, end=3.0, shot_type=None, key=False, race="未知",
                category="车队运营")
    assert "车队运营:法拉利" in edit._shot_line(dict(line, team="法拉利"))
    assert edit._shot_line(dict(line, team=None)).endswith("车队运营")
    assert "车队运营:某车队=这支车队自己发的运营视频" in edit.STEP_INSTRUCTION
    shots = [dict(line, team="法拉利", names=[]), dict(line, shot_idx=1, category="集锦", team=None, names=[],
                                                     main=["比赛中"])]
    monkeypatch.setattr(edit.visual_search, "search", lambda *a, **kw: [])
    segs = [{"i": 0, "start": 0, "end": 3, "text": "法拉利赛后很沮丧"},
            {"i": 1, "start": 3, "end": 6, "text": "最后一圈的超车"}]
    res = edit.candidates(conn, [1], segs, shots=shots)
    score = [{c["shot_idx"]: c["score"] for c in seg["candidates"]} for seg in res["segments"]]
    assert score[0] == {0: pytest.approx(0.35), 1: pytest.approx(-0.15)}    # 讲法拉利：法拉利运营 +0.1+0.25，实况 -0.15
    assert score[1] == {0: 0, 1: 0}     # 超车段算关键段：运营不加分，比赛画面不减分


def test_tag_prompt_lists_every_logo_corner():
    p = assets.TAG_PROMPT
    assert "bilibili 字样）也算" in p
    assert "每一个台标水印都列出来" in p and "左上角、右上角、左下角、右下角" in p
    assert "右上角「AEchihiro」徽标；右下角「AEchihiro bilibili」字样" in p and "没有台标写空字符串" in p
