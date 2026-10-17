"""2026-10-15 用户定：程序先在 YouTube 官方频道搜候选（搜索词按“主体＋赛事＋动作”拼）交给 GPT 挑，
GPT 没给链接就直接取候选；段里讲到的车队没有官方运营视频要在小结里说。搜索全部替身，不联网。"""
import asyncio
import json
from pathlib import Path

import pytest
from test_m6_sourcing import (RACE, _asset, _filler, _manifest, _other_ep, _probe, _run, _runner, _search,  # noqa: F401
                              fake_dns, notes, rep)

from hermes import codex_runner, sourcing
from hermes.scheduler import StepDeferred
from hermes.sourcing import yt_search as REAL_YT_SEARCH   # conftest 把模块里的换成了空结果

SCRIPT2 = ("# 新加坡复盘\n\n## 法拉利这笔策略账\n法拉利让勒克莱尔提前进站，结果被反超。\n\n"
           "## 红牛的单圈\n维斯塔潘在滨海湾的杆位单圈。\n")
SEG1, SEG2 = "法拉利这笔策略账", "红牛的单圈"
KNOWN = "https://www.youtube.com/watch?v=KNOWNKNOWN1"


def _e(vid, title, channel, duration):
    return {"id": vid, "title": title, "channel": channel, "duration": duration,
            "url": f"https://www.youtube.com/watch?v={vid}"}


HL = _e("HLHLHLHLHL1", "Singapore GP 2026 Race Highlights", "FORMULA 1", 480)
RADIO = _e("RADIORADIO1", "Leclerc team radio Singapore 2026", "FORMULA 1", 200)
RB = _e("RBRBRBRBRB1", "Singapore 2026 Behind the Scenes", "Oracle Red Bull Racing", 300)
AERIAL = _e("AERIALAERI1", "Marina Bay aerial 2026", "Singapore GP", 120)
RESULTS = {
    "highlights": [HL, _e("GBGBGBGBGB1", "British GP 2026 Race Highlights", "FORMULA 1", 480),
                   _e("FANFANFANF1", "Singapore 2026 highlights", "F1 Fan Edits", 300),
                   _e("FULLFULLFU1", "2026 Singapore Grand Prix Full Race Replay", "FORMULA 1", 7200)],
    "Red Bull Racing": [RB, _e("RBHLRBHLRB1", "Singapore 2026 race highlights", "Oracle Red Bull Racing", 300)],
    "aerial": [AERIAL],
    "Leclerc": [_e("KNOWNKNOWN1", "Leclerc Singapore 2026", "FORMULA 1", 100), RADIO,
                _e("NODURNODUR1", "Leclerc Singapore 2026 interview", "FORMULA 1", None)],
}


def _fake_search(asked=None):
    async def search(q, n=8):
        if asked is not None:
            asked.append(q)
        return [e for key, es in RESULTS.items() if key in q for e in es]
    return search


@pytest.fixture
def rep2(rep):
    (Path(rep["folder"]) / "02_文案" / "逐字稿.md").write_text(SCRIPT2, encoding="utf-8")
    return rep


def _segs():
    return sourcing.segments(SCRIPT2)


def test_search_queries_team_driver_action_circuit(conn):
    qs = sourcing.search_queries(conn, _segs(), RACE)
    by = {}
    for seg, q in qs:
        by.setdefault(seg, []).append(q)
    # 先本站集锦，再各段车队运营（2026-10-15 用户定：多用车队运营视频），F1 官方的车手反应、技术解读放最后
    assert qs[0] == (SEG1, "2026 Singapore Grand Prix highlights")
    assert qs[1:3] == [(SEG1, "Ferrari Singapore 2026 behind the scenes"),
                       (SEG2, "Red Bull Racing Singapore 2026 behind the scenes")]
    assert qs[-2:] == [(SEG1, "2026 Singapore Grand Prix drivers react"), (SEG1, "2026 Singapore Grand Prix tech talk")]
    assert by[SEG1][1:5] == ["Ferrari Singapore 2026 behind the scenes", "Ferrari 2026 Singapore Grand Prix",
                             "Ferrari Singapore GP", "Ferrari race week"]
    assert {"Leclerc 2026 Singapore Grand Prix", "Leclerc overtake 2026 Singapore Grand Prix"} <= set(by[SEG1])
    assert {"Red Bull Racing Singapore 2026 behind the scenes", "Red Bull Racing 2026 Singapore Grand Prix",
            "Verstappen 2026 Singapore Grand Prix", "Verstappen pole lap 2026 Singapore Grand Prix",
            "Singapore Grand Prix circuit official aerial", "Singapore Grand Prix track guide"} <= set(by[SEG2])
    assert not any("Red Bull" in q for q in by[SEG1]) and not any("aerial" in q for q in by[SEG1])
    assert len({q for _, q in qs}) == len(qs) <= sourcing.SEARCH_MAX_QUERIES
    # 段多时封顶，各段轮流取
    texts = ("法拉利 勒克莱尔 超车", "红牛 维斯塔潘 撞墙", "迈凯伦 诺里斯 进站", "梅赛德斯 拉塞尔 杆位", "威廉姆斯 塞恩斯 赛道")
    many = [{"name": f"段{i}", "text": t} for i, t in enumerate(texts)]
    got = sourcing.search_queries(conn, many, RACE)
    assert len(got) == sourcing.SEARCH_MAX_QUERIES and {seg for seg, _ in got} == {f"段{i}" for i in range(5)}
    assert got[-2:] == [("段0", "2026 Singapore Grand Prix drivers react"), ("段0", "2026 Singapore Grand Prix tech talk")]
    assert {q for _, q in got} >= {f"{t} 2026 Singapore Grand Prix" for t in ("Ferrari", "Red Bull Racing", "McLaren")}


def test_official_channel():
    oc = sourcing.official_channel
    assert oc("FORMULA 1", RACE) == "F1官方" and oc("F1", RACE) == "F1官方"
    assert oc("Oracle Red Bull Racing", RACE) == "车队官方" and oc("Scuderia Ferrari HP", None) == "车队官方"
    assert oc("Singapore GP", RACE) == "举办方" and oc("Singapore Grand Prix", RACE) == "举办方"
    assert oc("F1 Fan Edits", RACE) is None and oc("Singapore Sports Hub", RACE) is None
    assert oc("Singapore GP", None) is None and oc("Monaco GP", RACE) is None and oc("", RACE) is None


def test_yt_search_parses_and_fails_soft(monkeypatch):
    calls = []

    class Proc:
        def __init__(self, out, code):
            self.out, self.returncode = out, code

        async def communicate(self):
            return self.out, b"ERROR: boom"

    def make(out, code):
        async def exec_(*args, **kw):
            calls.append(args)
            return Proc(out, code)
        return exec_

    data = {"entries": [{"id": "HLHLHLHLHL1", "title": "T", "channel": "FORMULA 1", "duration": 60},
                        {"id": "RBRBRBRBRB1", "title": "B", "uploader": "Oracle Red Bull Racing"},
                        {"id": "x"}, "bad"]}
    monkeypatch.setattr(sourcing.asyncio, "create_subprocess_exec", make(json.dumps(data).encode(), 0))
    got = asyncio.run(REAL_YT_SEARCH("2026 Singapore Grand Prix highlights", 5))
    assert calls[0][-1] == "ytsearch5:2026 Singapore Grand Prix highlights" and "--flat-playlist" in calls[0]
    assert got == [{"id": "HLHLHLHLHL1", "title": "T", "channel": "FORMULA 1", "duration": 60,
                    "url": "https://www.youtube.com/watch?v=HLHLHLHLHL1"},
                   {"id": "RBRBRBRBRB1", "title": "B", "channel": "Oracle Red Bull Racing", "duration": None,
                    "url": "https://www.youtube.com/watch?v=RBRBRBRBRB1"}]
    monkeypatch.setattr(sourcing.asyncio, "create_subprocess_exec", make(b"", 1))
    assert asyncio.run(REAL_YT_SEARCH("q")) == []


def test_candidates_filter_rank_and_team_gaps(conn, rep2, tmp_path):
    _asset(conn, tmp_path, 1, source_url=KNOWN, title="已有")
    cands, gaps = asyncio.run(sourcing.candidates(conn, _segs(), RACE, search=_fake_search()))
    by = {}
    for c in cands:
        by.setdefault(c["segment"], []).append(c["url"])
    # 非官方、别站、太长、没时长、库里已有、车队频道发的比赛集锦都不要；讲关键点的段比赛集锦在前
    assert by == {SEG1: [HL["url"], RADIO["url"]], SEG2: [RB["url"], AERIAL["url"]]}
    hl = cands[0]
    assert hl["kind"] == "F1官方" and hl["minutes"] == 8.0 and hl["same_race"] is True
    assert {c["url"]: c["kind"] for c in cands}[AERIAL["url"]] == "举办方"
    assert {c["url"]: c["kind"] for c in cands}[RB["url"]] == "车队官方"
    assert gaps == [{"segment": SEG1, "team": "法拉利"}]
    # 每段上限、时长上限
    cands, _ = asyncio.run(sourcing.candidates(conn, _segs(), RACE, search=_fake_search(), limit_per_seg=1,
                                               max_minutes=4, ops_minutes=4))
    assert [c["url"] for c in cands] == [RADIO["url"], AERIAL["url"]]
    # 车队官方运营视频的时长上限另算（2026-10-16 放宽到 30 分钟）
    cands, _ = asyncio.run(sourcing.candidates(conn, _segs(), RACE, search=_fake_search(), limit_per_seg=1,
                                               max_minutes=4))
    assert [c["url"] for c in cands] == [RADIO["url"], RB["url"]]


def test_run_falls_back_to_candidates_when_gpt_gives_none(conn, rep2, tmp_path, notes):
    _filler(conn, tmp_path, _other_ep(conn))
    _asset(conn, tmp_path, 1, source_url=KNOWN, title="已有")
    seen, asked = [], []
    with pytest.raises(StepDeferred, match="下载"):
        _run(conn, rep2, runner=_runner([], seen), search=_search(set()), probe=_probe({}), yt=_fake_search(asked))
    assert asked and seen
    p = seen[0]["prompt"]
    assert "4. 下面“程序在 YouTube 官方频道搜到的候选”是程序在本机联网搜的" in p and "网址照抄候选的" in p and "5. 单条不超过" in p
    assert "## 程序在 YouTube 官方频道搜到的候选（按段）" in p and f"### {SEG1}" in p
    assert f"- [F1官方] Singapore GP 2026 Race Highlights | FORMULA 1 | 8.0 分钟 | {HL['url']}" in p
    reply = Path(rep2["folder"]) / "04_素材" / "_找素材" / "回复.json"
    assert json.loads(reply.read_text(encoding="utf-8")) == {"items": []}
    m = _manifest(rep2)
    s = m["summary"]
    assert s["gpt_found"] == 0 and s["candidates"] == 4 and s["fallback"] == 4
    assert s["team_gaps"] == [{"segment": SEG1, "team": "法拉利"}]
    assert {f["url"]: (f["segment"], f["state"]) for f in m["found"]} == {
        HL["url"]: (SEG1, "下载中"), RADIO["url"]: (SEG1, "下载中"), RB["url"]: (SEG2, "下载中"),
        AERIAL["url"]: (SEG2, "下载中")}
    assert {f["reason"] for f in m["found"]} == {"程序搜到的官方候选（GPT 没给）"}


def test_run_gpt_error_with_candidates_continues(conn, rep2, tmp_path, notes):
    _filler(conn, tmp_path, _other_ep(conn))
    with pytest.raises(StepDeferred, match="下载"):
        _run(conn, rep2, runner=_runner(RuntimeError("断网")), search=_search(set()), probe=_probe({}),
             yt=_fake_search())
    reply = Path(rep2["folder"]) / "04_素材" / "_找素材" / "回复.json"
    assert json.loads(reply.read_text(encoding="utf-8")) == {"error": "断网"}
    s = _manifest(rep2)["summary"]
    assert s["gpt_error"] == "断网" and s["fallback"] == 4


def test_run_gpt_items_no_fallback(conn, rep2, tmp_path, notes):
    _filler(conn, tmp_path, _other_ep(conn))
    items = [{"url": RB["url"], "title": RB["title"], "channel": RB["channel"], "segment": SEG2, "reason": "r"}]
    with pytest.raises(StepDeferred, match="下载"):
        _run(conn, rep2, runner=_runner(items), search=_search(set()), probe=_probe({}), yt=_fake_search())
    m = _manifest(rep2)
    assert "fallback" not in m["summary"] and [f["url"] for f in m["found"]] == [RB["url"]]


def test_summary_text_candidates_fallback_and_team_gap():
    out = {"existing": 0, "from_library": 0, "gpt_called": True, "forced_by_feedback": False, "gpt_found": 0,
           "downloaded": 3, "failed": [], "skipped": [], "candidates": 5, "fallback": 4, "short_segments": [],
           "notes": [], "team_gaps": [{"segment": SEG1, "team": "法拉利"}, {"segment": "结尾", "team": "法拉利"}]}
    text = sourcing.summary_text(out)
    assert "程序在 YouTube 官方频道搜到候选 5 条" in text
    assert "GPT 没给链接，程序直接取了 4 条官方候选，下载成功 3 条" in text
    assert "法拉利段（法拉利这笔策略账、结尾）没有车队官方运营视频，用 F1 官方采访和维修区画面顶上。" in text
    assert "查过的官方账号" not in text
    out["searched"] = ["Instagram @scuderiaferrari", "X @ScuderiaFerrari"]
    assert "GPT 查过的官方账号：Instagram @scuderiaferrari、X @ScuderiaFerrari。" in sourcing.summary_text(out)
    out.update(candidates=None, fallback=0, gpt_found=2, team_gaps=[])
    text = sourcing.summary_text(out)
    assert "候选" not in text and "GPT 找到 2 条" in text and "车队官方运营" not in text


def test_open_gaps_drop_team_with_downloaded_video(conn, tmp_path):
    gaps = [{"segment": SEG1, "team": "法拉利"}, {"segment": SEG2, "team": "红牛"}]
    found = [{"state": "已下载", "channel": "Scuderia Ferrari HP"}, {"state": "跳过", "channel": "Oracle Red Bull Racing"}]
    assert sourcing._open_gaps(conn, gaps, [], found) == [{"segment": SEG2, "team": "红牛"}]


def test_candidates_unsure_title_checked_by_description(conn, rep2):
    """标题认不出哪站的车队 Vlog：简介写本站算本站；写别站但今年发的也留（算别站，排后面，2026-10-16 用户定）；
    往年发的不要。"""
    sg = _e("VLOGSGSGSG1", "Our Night Race | Behind The Charge", "Oracle Red Bull Racing", 400)
    my = _e("VLOGMYMYMY1", "Our First Win Of 2026!!! | Behind The Charge", "Oracle Red Bull Racing", 400)
    old = _e("VLOGOLDOLD1", "Our Night Race | Behind The Charge", "Oracle Red Bull Racing", 400)
    info = {sg["url"]: {"description": "All the behind-the-scenes from Singapore under the lights", "upload_date": "20261012"},
            my["url"]: {"description": "Max's incredible win in Malaysia", "upload_date": "20261009"},
            old["url"]: {"description": "All the behind-the-scenes from Singapore", "upload_date": "20251006"}}

    async def search(q, n=8):
        return [my, sg, old] if "Red Bull Racing" in q else []

    async def probe(url):
        return info[url]

    cands, _ = asyncio.run(sourcing.candidates(conn, _segs(), RACE, search=search, probe=probe))
    assert [c["url"] for c in cands] == [sg["url"], my["url"]]
    assert cands[0]["same_race"] is True and cands[1]["same_race"] is False


def test_candidates_unsure_kept_by_upload_date(conn, rep2):
    """2026-10-15：标题、简介都没写哪站的车队官方 Vlog，上传日期在本站正赛（10-11）前后 3 天内算本站；
    今年别的时候发的也留但排后面（2026-10-16）。"""
    near = _e("VLOGNEARNE1", "Lights, camera, action | Race Week", "Scuderia Ferrari HP", 400)
    far = _e("VLOGFARFAR1", "Lights, camera, action | Race Week", "Scuderia Ferrari HP", 400)
    info = {near["url"]: {"description": "Another week with the team", "upload_date": "20261013"},
            far["url"]: {"description": "Another week with the team", "upload_date": "20261001"}}
    asked = []

    async def search(q, n=8):
        asked.append(q)
        return [near, far] if "Ferrari" in q else []

    async def probe(url):
        return info[url]

    cands, gaps = asyncio.run(sourcing.candidates(conn, _segs(), RACE, search=search, probe=probe))
    assert [c["url"] for c in cands] == [near["url"], far["url"]] and gaps == [{"segment": SEG2, "team": "红牛"}]
    assert [c["same_race"] for c in cands] == [True, False]
    assert "Ferrari race week" in asked and "Ferrari Singapore GP" in asked
    assert sourcing.race_date(conn, RACE).isoformat() == "2026-10-11"
    assert sourcing.race_date(conn, {**RACE, "event": "Monaco Grand Prix"}) is None


def test_asks_new_material_user_wording():
    ask = sourcing.asks_new_material
    assert ask("第 1 项打回，减少比赛画面。如果没有比赛画面可以使用的话，你可以去使用车队运营画面。")
    assert ask("法拉利那段换车队运营画面") and ask("红牛段补点镜头") and ask("这段画面不对")
    assert not ask("画面不错") and not ask("节奏太慢")


def test_run_team_gaps_in_prompt_and_searched_recorded(conn, rep2, tmp_path, notes):
    _filler(conn, tmp_path, _other_ep(conn))
    seen = []

    async def runner(prompt, model, effort, workdir, **kw):
        seen.append(prompt)
        return codex_runner.CodexResult("t", json.dumps({"items": [], "searched": ["Instagram @scuderiaferrari"]}),
                                        {"input_tokens": 1, "output_tokens": 1})
    with pytest.raises(StepDeferred, match="下载"):
        _run(conn, rep2, runner=runner, search=_search(set()), probe=_probe({}), yt=_fake_search())
    assert "## 还缺官方运营视频的车队（程序没搜到、本期素材里也没有）\n- 法拉利：法拉利这笔策略账" in seen[0]
    assert "9. 用户意见要运营画面" in seen[0]
    assert _manifest(rep2)["summary"]["searched"] == ["Instagram @scuderiaferrari"]


def test_run_team_in_pool_not_a_gap(conn, rep2, tmp_path, notes):
    """之前下好的法拉利官方视频在本期素材里：搜索时被当成已知去掉，也不算缺口。"""
    from test_m6_sourcing import _rasset
    _filler(conn, tmp_path, _other_ep(conn))
    _rasset(conn, tmp_path, 3, "Singapore GP 2026 | Behind the scenes", uploader="Scuderia Ferrari HP")
    seen = []
    with pytest.raises(StepDeferred, match="下载"):
        _run(conn, rep2, runner=_runner([], seen), search=_search(set()), probe=_probe({}), yt=_fake_search())
    assert _manifest(rep2)["summary"]["team_gaps"] == []
    assert "## 还缺官方运营视频的车队" not in seen[0]["prompt"]


# ---------- 2026-10-16 用户定：车队官方运营视频不必是本站，今年各站、日常、开发宣传的都能用 ----------

def test_team_channels_and_channel_page_listing(monkeypatch):
    assert sourcing.team_channels(_segs(), RACE) == [(SEG1, "https://www.youtube.com/@Ferrari/videos"),
                                                     (SEG2, "https://www.youtube.com/@redbullracing/videos")]
    calls = []

    class Proc:
        returncode = 0

        async def communicate(self):
            data = {"channel": "Ferrari", "entries": [{"id": "CHANCHANCH1", "title": "SF-26 shakedown", "duration": 90}]}
            return json.dumps(data).encode(), b""

    async def exec_(*args, **kw):
        calls.append(args)
        return Proc()
    monkeypatch.setattr(sourcing.asyncio, "create_subprocess_exec", exec_)
    got = asyncio.run(REAL_YT_SEARCH("https://www.youtube.com/@Ferrari/videos"))
    assert calls[0][-1] == "https://www.youtube.com/@Ferrari/videos" and "--playlist-end" in calls[0]
    assert got == [{"id": "CHANCHANCH1", "title": "SF-26 shakedown", "channel": "Ferrari", "duration": 90,
                    "url": "https://www.youtube.com/watch?v=CHANCHANCH1"}]


def test_candidates_team_ops_other_race_this_season(conn, rep2):
    gb26 = _e("RBGB26GB261", "British GP 2026 Behind the Scenes", "Oracle Red Bull Racing", 600)
    gb25 = _e("RBGB25GB251", "British GP 2025 Behind the Scenes", "Oracle Red Bull Racing", 600)
    f1gb = _e("F1GBF1GBF11", "British GP 2026 Team Radio", "FORMULA 1", 300)
    dev = _e("RBDEVRBDEV1", "Building the RB22", "Oracle Red Bull Racing", 1500)
    asked = []

    async def search(q, n=8):
        asked.append(q)
        return [gb26, gb25, f1gb] if "Red Bull Racing" in q else [dev] if "@redbullracing" in q else []

    async def probe(url):
        return {"description": "Inside the factory", "upload_date": "20260301"}

    cands, gaps = asyncio.run(sourcing.candidates(conn, _segs(), RACE, search=search, probe=probe))
    got = {c["url"]: c for c in cands}
    # 别站的 F1 官方运营、往年的车队视频不要；今年别站的、开发宣传的（25 分钟）车队视频要
    assert set(got) == {gb26["url"], dev["url"]} and "https://www.youtube.com/@redbullracing/videos" in asked
    assert got[gb26["url"]]["same_race"] is False and got[dev["url"]]["same_race"] is False
    assert gaps == [{"segment": SEG1, "team": "法拉利"}]


def test_screen_team_ops_other_race_this_season(conn, rep):
    u = {k: f"https://www.youtube.com/watch?v={v}" for k, v in
         (("gb", "GBGBGBGBGB2"), ("old", "OLDOLDOLDO1"), ("f1", "F1F1F1F1F11"), ("long", "LONGLONGLO1"))}
    items = [{"url": u["gb"], "title": "British GP 2026 Behind the Scenes", "channel": "Oracle Red Bull Racing"},
             {"url": u["old"], "title": "Our Night Race | Behind The Charge", "channel": "Oracle Red Bull Racing"},
             {"url": u["f1"], "title": "British GP 2026 Team Radio", "channel": "FORMULA 1"},
             {"url": u["long"], "title": "Building the RB22", "channel": "Oracle Red Bull Racing"}]
    info = {u["gb"]: ("Oracle Red Bull Racing", 20, "20260706"), u["old"]: ("Oracle Red Bull Racing", 8, "20251006"),
            u["f1"]: ("FORMULA 1", 5, "20260706"), u["long"]: ("Oracle Red Bull Racing", 40, "20260301")}

    async def probe(url):
        who, minutes, day = info[url]
        return {"webpage_url": url, "title": "t", "uploader": who, "duration": minutes * 60, "upload_date": day}

    out = asyncio.run(sourcing.screen(conn, items, limit=5, max_minutes=15, probe=probe, race=RACE))
    gb, old, f1, long = out
    assert gb["state"] == "待下载" and gb["same_race"] is False   # 20 分钟，车队运营上限 30
    assert old["state"] == "跳过" and "2025 年发的" in old["note"]
    assert f1["state"] == "跳过" and "不是本站" in f1["note"]
    assert long["state"] == "跳过" and "超过上限 30 分钟" in long["note"]


def test_library_team_ops_this_season_usable(conn, rep, tmp_path):
    from test_m6_sourcing import _rasset
    gb26 = _rasset(conn, tmp_path, 3, "British GP 2026 Behind the Scenes", uploader="Oracle Red Bull Racing")
    gb25 = _rasset(conn, tmp_path, 3, "British GP 2025 Behind the Scenes", uploader="Oracle Red Bull Racing")
    f1gb = _rasset(conn, tmp_path, 3, "British GP 2026 Team Radio", uploader="FORMULA 1")
    assert [sourcing._usable(conn, a, RACE) for a in (gb26, gb25, f1gb)] == [True, False, False]
