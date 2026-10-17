"""D4 赛程卡、轮胎策略图、积分榜、数据卡、多面板叠层。全部不联网（GPT 用假 runner，FastF1 / Ergast 用假数据）。"""
import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hermes import (codex_runner, compose, config, edit, episodes, facts, glossary, graphics, narration, overlays,
                    panels, research, steps, trackmap)

REAL_FONTS = Path("/Volumes/张三波个人硬盘/Codex/F1 Hermes V1/assets/fonts")
needs_fonts = pytest.mark.skipif(not (REAL_FONTS / "SourceHanSansSC-Heavy.otf").exists(), reason="字体盘未挂载")


@pytest.fixture(autouse=True)
def real_fonts(monkeypatch):
    monkeypatch.setattr(config, "FONTS_DIR", REAL_FONTS)


def _s(name, bj):
    return {"name": name, "start_utc": None, "start_bj": bj}


CONVENTIONAL = {"season": 2026, "round": 16, "name_zh": "巴林大奖赛", "name_en": "Bahrain Grand Prix",
                "format": "conventional", "location": "Sakhir", "event_date": "2026-10-04",
                "sessions": [_s("Practice 1", "2026-10-02 12:30"), _s("Practice 2", "2026-10-02 16:00"),
                             _s("Practice 3", "2026-10-03 12:30"), _s("Qualifying", "2026-10-03 16:00"),
                             _s("Race", "2026-10-04 15:00")]}
SPRINT = {"season": 2026, "round": 6, "name_zh": "迈阿密大奖赛", "name_en": "Miami Grand Prix",
          "format": "sprint_qualifying", "location": "Miami", "event_date": "2026-05-03",
          "sessions": [_s("Race", "2026-05-04 04:00"), _s("Practice 1", "2026-05-02 00:30"),
                       _s("Sprint Qualifying", "2026-05-02 04:30"), _s("Sprint", "2026-05-03 00:00"),
                       _s("Qualifying", "2026-05-03 04:00")]}
CONV_DAYS = [
    {"date": "10月2日", "weekday": "周五", "sessions": [{"name": "一练", "time": "12:30"}, {"name": "二练", "time": "16:00"}]},
    {"date": "10月3日", "weekday": "周六", "sessions": [{"name": "三练", "time": "12:30"}, {"name": "排位", "time": "16:00"}]},
    {"date": "10月4日", "weekday": "周日", "sessions": [{"name": "正赛", "time": "15:00"}]},
]
SPRINT_NAMES = [["一练", "冲刺排位"], ["冲刺赛", "排位"], ["正赛"]]


def _episode(conn, gp="巴林大奖赛"):
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="前瞻", gp_label=gp, title="巴林前瞻")
    return episodes.get(conn, eid)


# ---------- 赛程卡数据 ----------

def test_schedule_from_facts_conventional(conn):
    ep = _episode(conn)
    research.schedule_facts(conn, ep["id"], CONVENTIONAL)
    sched = overlays.schedule_of(conn, ep, None)
    assert sched["name"] == "巴林大奖赛" and sched["format"] == "常规"
    assert sched["season"] == "2026" and sched["round"] == "16"
    assert sched["days"] == CONV_DAYS and sched["credit"] == "数据：FastF1 赛历"
    assert overlays.race_start(None, sched) == "10月4日 周日 15:00（北京时间）"
    assert overlays.race_start(CONVENTIONAL, sched) == "2026-10-04 15:00（北京时间）"


def test_schedule_sprint_from_facts_and_race_sessions(conn):
    ep = _episode(conn, "迈阿密大奖赛")
    research.schedule_facts(conn, ep["id"], SPRINT)
    from_facts = overlays.schedule_of(conn, ep, None)
    assert from_facts["format"] == "冲刺"
    assert [[s["name"] for s in d["sessions"]] for d in from_facts["days"]] == SPRINT_NAMES
    assert [d["date"] for d in from_facts["days"]] == ["5月2日", "5月3日", "5月4日"]

    other = _episode(conn, "迈阿密大奖赛")                    # 没有事实表 → 后备赛历（乱序也能排好）
    fallback = overlays.schedule_of(conn, other, SPRINT)
    assert fallback["format"] == "冲刺" and fallback["round"] == 6
    assert fallback["days"] == from_facts["days"]
    assert overlays.schedule_of(conn, other, None) is None


SCRIPT = ("## 开场\n今天聊聊这一站。\n\n## 赛程\n周五下午一练，周六排位，周日晚上正赛。\n\n"
          "## 赛道\n这条赛道很长。\n\n还有两条长直道。\n\n"
          "## 轮胎与练习\n倍耐力带来最硬的三款配方，预计一停为主。\n\n## 结尾\n下期见。\n")
SCRIPT_PLAIN = ("今天聊聊这一站。\n\n一练、二练都在周五，周六排位，周日正赛，全是北京时间下午。\n\n"
                "轮胎方面配方偏硬，进站策略大概是一停，软胎很难用。\n\n下期见。\n")


def _shown(script, paras):
    return "".join(u["show"] for u in narration.prepare(script).units if u["para"] in paras)


def test_section_paras_by_name_and_keywords():
    paras, how = overlays.section_paras(SCRIPT, names=("赛程",), keywords=overlays.SCHEDULE_KEYWORDS)
    assert how == "段名「赛程」" and "周六排位" in _shown(SCRIPT, paras) and "赛道很长" not in _shown(SCRIPT, paras)
    paras, how = overlays.section_paras(SCRIPT, names=("轮胎",), keywords=overlays.TYRE_KEYWORDS)
    assert how == "段名「轮胎与练习」" and "一停" in _shown(SCRIPT, paras)
    paras, how = overlays.section_paras(SCRIPT_PLAIN, names=("赛程",), keywords=overlays.SCHEDULE_KEYWORDS)
    assert list(paras) == [1] and how.startswith("关键词（第 2 段")
    paras, how = overlays.section_paras(SCRIPT_PLAIN, names=("轮胎",), keywords=overlays.TYRE_KEYWORDS)
    assert list(paras) == [2] and how.startswith("关键词（第 3 段")
    assert overlays.section_paras("今天天气很好。\n\n明天也不错。\n", names=("轮胎",),
                                  keywords=overlays.TYRE_KEYWORDS) is None
    assert overlays.track_paras(SCRIPT)[1] == "段名「赛道」"        # 赛道图定位不受影响


# ---------- 渲染 ----------

def _check_panel(png, size=(1920, 1080)):
    img = Image.open(png)
    assert img.size == size and img.mode == "RGBA"
    k = size[0] / 1920
    x1, y1, x2, y2 = trackmap.PANEL_BOX
    l, t, r, b = img.getchannel("A").getbbox()
    assert abs(l - x1 * k) <= 2 and abs(t - y1 * k) <= 2 and abs(r - x2 * k) <= 2 and abs(b - y2 * k) <= 2
    m = round(30 * k)                                       # 面板内部不能有半透明的洞（亮画面会透出来、字看不清）
    inner = np.asarray(img.getchannel("A"))[round(y1 * k) + m:round(y2 * k) - m, round(x1 * k) + m:round(x2 * k) - m]
    assert inner.min() >= 220
    rgb = np.asarray(img.convert("RGB"))[round(y1 * k):round(y2 * k), round(x1 * k):round(x2 * k)]
    return rgb


@needs_fonts
def test_render_schedule_png(tmp_path):
    sched = {"name": "巴林大奖赛", "format": "常规", "season": "2026", "round": "16", "days": CONV_DAYS,
             "credit": "数据：FastF1 赛历"}
    rgb = _check_panel(panels.render_schedule(sched, tmp_path / "赛程.png"))
    assert (rgb.max(axis=2) > 240).sum() > 2000                                     # 有白字
    sprint = dict(sched, format="冲刺", days=[{"date": d, "weekday": "周六", "sessions": [
        {"name": n, "time": "12:00"} for n in names]} for d, names in zip(("5月2日", "5月3日", "5月4日"), SPRINT_NAMES)])
    _check_panel(panels.render_schedule(sprint, tmp_path / "s.png", size=(1280, 720)), (1280, 720))


TYRES = {"name": "巴林大奖赛", "compounds": {"hard": "C1", "medium": "C2", "soft": "C3"}, "race_laps": 57,
         "strategies": [{"name": "一停", "stints": ["medium", "hard"], "pit_laps": ["18-25"]},
                        {"name": "两停", "stints": ["soft", "hard", "medium"], "pit_laps": ["12-16", "34-40"]}],
         "sites": ["Pirelli", "F1 官网"]}


@needs_fonts
def test_render_tyres_png(tmp_path):
    rgb = _check_panel(panels.render_tyres(TYRES, tmp_path / "轮胎策略.png"))
    yellow = (rgb[..., 0] > 230) & (rgb[..., 1] > 180) & (rgb[..., 2] < 60)
    red = (rgb[..., 0] > 190) & (rgb[..., 1] < 70) & (rgb[..., 2] < 70)
    assert yellow.sum() > 500 and red.sum() > 500
    empty = dict(TYRES, strategies=[], race_laps=None, compounds={"hard": "C1", "medium": None, "soft": "C3"})
    _check_panel(panels.render_tyres(empty, tmp_path / "空.png"))


def test_tyre_positions_by_laps_or_even():
    st = TYRES["strategies"][1]
    assert panels._positions(st, 57) == pytest.approx([14 / 57, 37 / 57, 1.0])
    assert panels._positions(st, None) == pytest.approx([1 / 3, 2 / 3, 1.0])
    assert panels._positions(dict(st, pit_laps=["约第 12 圈", "不定"]), 57) == pytest.approx([1 / 3, 2 / 3, 1.0])


# ---------- 轮胎信息校验 ----------

GOOD = {"compounds": {"hard": "c1", "medium": " C2", "soft": "C3"}, "race_laps": 57,
        "strategies": [{"name": "一停", "stints": ["medium", "hard"], "pit_laps": ["18-25"]},
                       {"name": "怪", "stints": ["soft"], "pit_laps": []}],
        "sources": [{"url": "https://press.pirelli.com/bahrain-preview", "excerpt": "C1 as the P Zero White hard"},
                    {"url": "https://www.formula1.com/en/latest/x", "excerpt": "one-stop is favoured"}]}


def test_clean_tyres():
    used, warns, ok = overlays.clean_tyres(GOOD)
    assert ok and used["compounds"] == {"hard": "C1", "medium": "C2", "soft": "C3"}
    assert [s["name"] for s in used["strategies"]] == ["一停"] and any("段数不对" in w for w in warns)
    bad_url = dict(GOOD, sources=[{"url": "javascript:alert(1)", "excerpt": "x"}, {"url": "pirelli.com", "excerpt": "y"},
                                  {"url": "https://press.pirelli.com/a", "excerpt": "C1 hard"},
                                  {"url": "ftp://pirelli.com/a", "excerpt": "z"}])
    used, warns, ok = overlays.clean_tyres(bad_url)
    assert ok and [s["url"] for s in used["sources"]] == ["https://press.pirelli.com/a"]
    assert any("丢弃 3 条来源" in w for w in warns)
    used, warns, ok = overlays.clean_tyres(dict(GOOD, sources=[{"url": "not a url", "excerpt": "x"}]))
    assert not ok and "没有合法的来源网址" in warns[-1]
    used, warns, ok = overlays.clean_tyres(dict(GOOD, compounds={"hard": None, "medium": "未公布", "soft": None}))
    assert not ok and "没拿到可用的干胎配方" in warns[-1] and any("「未公布」" in w for w in warns)


# ---------- 多面板不重叠 ----------

def test_resolve_overlaps_trims_or_drops_later_panel():
    items = [{"kind": "赛道图", "start": 10.0, "end": 30.0}, {"kind": "赛程", "start": 2.0, "end": 12.0},
             {"kind": "轮胎策略", "start": 25.0, "end": 31.0}, {"kind": "X", "start": 40.0, "end": 50.0}]
    kept, warns = overlays.resolve_overlaps(items)
    assert [(k["kind"], k["start"], k["end"]) for k in kept] == [("赛程", 2.0, 12.0), ("赛道图", 12.0, 30.0),
                                                                  ("X", 40.0, 50.0)]
    assert "赛道图改从 12.0 秒开始" in warns[0] and "轮胎策略" in warns[1] and "没放进时间线" in warns[1]
    for a, b in zip(kept, kept[1:]):
        assert a["end"] <= b["start"]


# ---------- build 全流程 ----------

def _write_inputs(ep, script=SCRIPT):
    folder = Path(ep["folder"])
    (folder / "02_文案").mkdir(parents=True, exist_ok=True)
    (folder / "03_配音").mkdir(parents=True, exist_ok=True)
    (folder / "02_文案" / "逐字稿.md").write_text(script, encoding="utf-8")
    segs, t = [], 0.0
    for u in narration.prepare(script).units:
        segs.append({"text": u["say"], "show": u["show"], "start": t, "end": t + 3})
        t += 3.5
    (folder / "03_配音" / "对齐.json").write_text(json.dumps({"duration": t, "segments": segs}), encoding="utf-8")
    return folder


@pytest.fixture
def stage(monkeypatch):
    monkeypatch.setattr(overlays.calendar, "find_event", lambda conn, text: CONVENTIONAL if "巴林" in text else None)
    monkeypatch.setattr(trackmap, "outline_for_event", lambda e: {"source": "OpenStreetMap", "length_m": 5412.0})

    def fake_render(outline, out_png, **kw):
        Image.new("RGBA", (1920, 1080)).save(out_png)
        return Path(out_png)
    monkeypatch.setattr(trackmap, "render", fake_render)


def _runner(answer, seen):
    async def run(prompt, model, effort, workdir, **kw):
        seen.append({"prompt": prompt, "model": model, "effort": effort, "kw": kw,
                     "name": Path(kw["output_schema"]).name.split(".")[0]})
        return codex_runner.CodexResult("t", answer if isinstance(answer, str) else json.dumps(answer),
                                        {"input_tokens": 10, "output_tokens": 5})
    return run


@needs_fonts
def test_build_three_panels(conn, stage):
    ep = _episode(conn)
    research.schedule_facts(conn, ep["id"], CONVENTIONAL)
    folder = _write_inputs(ep)
    seen = []
    res = overlays.build(conn, ep, task_id=None, runner=_runner(GOOD, seen))
    layer = json.loads((folder / "05_图形" / "叠层.json").read_text(encoding="utf-8"))["overlays"]
    assert [o["kind"] for o in layer] == ["赛程", "赛道图", "轮胎策略"] and res["overlays"] == 3
    assert [o["path"] for o in layer] == ["05_图形/赛程.png", "05_图形/赛道图.png", "05_图形/轮胎策略.png"]
    for a, b in zip(layer, layer[1:]):
        assert a["end"] <= b["start"]
    assert res["section"]["how"] == "段名「赛道」" and res["schedule"]["how"] == "段名「赛程」"
    assert res["tyres"]["how"] == "段名「轮胎与练习」"
    assert Image.open(folder / "05_图形" / "赛程.png").size == (1920, 1080)
    assert Image.open(folder / "05_图形" / "轮胎策略.png").size == (1920, 1080)
    # 只问一次 GPT：图形角色、联网、指令原文在最前，带轮胎段、事实表、截止时间
    assert len(seen) == 1 and seen[0]["name"] == "轮胎" and seen[0]["kw"]["web_search"] is True
    assert (seen[0]["model"], seen[0]["effort"]) == config.ROLE_MODELS["图形"]
    p = seen[0]["prompt"]
    assert p.startswith(overlays.TYRE_INSTRUCTION) and "预计一停为主" in p and "一练开始时间" in p
    assert "2026-10-04 15:00（北京时间）" in p and "## 本期信息" in p
    saved = json.loads((folder / "05_图形" / "轮胎信息.json").read_text(encoding="utf-8"))
    assert saved["raw"] == GOOD and saved["ok"] and saved["used"]["compounds"]["hard"] == "C1"
    assert (folder / "05_图形" / "_轮胎" / "轮胎.schema.json").exists()
    assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1


@needs_fonts
def test_build_no_compounds_or_bad_urls_no_tyre_chart(conn, stage):
    ep = _episode(conn)
    research.schedule_facts(conn, ep["id"], CONVENTIONAL)
    folder = _write_inputs(ep)
    res = overlays.build(conn, ep, runner=_runner(dict(GOOD, compounds={"hard": None, "medium": None, "soft": None}),
                                                   []))
    assert "tyres" not in res and any("不出图" in w for w in res["warnings"])
    assert not (folder / "05_图形" / "轮胎策略.png").exists()
    assert [o["kind"] for o in json.loads((folder / "05_图形" / "叠层.json").read_text(encoding="utf-8"))["overlays"]] \
        == ["赛程", "赛道图"]
    res = overlays.build(conn, ep, runner=_runner(dict(GOOD, sources=[{"url": "javascript:x", "excerpt": "a"}]), []))
    assert "tyres" not in res and any("丢弃 1 条来源" in w for w in res["warnings"])
    assert any("没有合法的来源网址" in w for w in res["warnings"])
    res = overlays.build(conn, ep, runner=_runner("不是 JSON", []))
    assert "tyres" not in res and any("读不出来" in w for w in res["warnings"])


@needs_fonts
def test_build_without_tyre_section_skips_gpt(conn, stage):
    ep = _episode(conn)
    research.schedule_facts(conn, ep["id"], CONVENTIONAL)
    _write_inputs(ep, "## 开场\n今天聊聊这一站。\n\n## 赛道\n这条赛道很长。\n\n还有两条长直道。\n")

    async def boom(*a, **kw):
        raise AssertionError("不该调 GPT")
    res = overlays.build(conn, ep, runner=boom)
    assert res["overlays"] == 1 and not res["warnings"] and "schedule" not in res and "tyres" not in res
    assert res["section"]["how"] == "段名「赛道」"


def test_graphics_step_card(tmp_path, monkeypatch):
    """一轮一张图形卡片（第 3 批）：图存进 09_版本/图形-r<轮次>，卡片指向存下的图。"""
    cards, got = [], {}
    g = tmp_path / "05_图形"
    g.mkdir()
    for name in ("赛程.png", "赛道图.png", "轮胎策略.png"):
        Image.new("RGBA", (8, 8)).save(g / name)
    (g / "叠层.json").write_text(json.dumps({"overlays": [
        {"path": "05_图形/赛道图.png", "start": 10.0, "end": 20.0, "kind": "赛道图"},
        {"path": "05_图形/赛程.png", "start": 2.0, "end": 8.0, "kind": "赛程"},
        {"path": "05_图形/轮胎策略.png", "start": 30.0, "end": 40.0, "kind": "轮胎策略"}]}), encoding="utf-8")

    def fake_build(conn, ep, feedback, **kw):
        got.update(kw)
        return {"warnings": ["轮胎图：丢弃 1 条来源"], "section": {"how": "段名「赛道」", "start": 10.0, "end": 20.0},
                "schedule": {"how": "段名「赛程」", "start": 2.0, "end": 8.0},
                "tyres": {"how": "关键词（第 5 段，命中 4 次）", "start": 30.0, "end": 40.0}}
    monkeypatch.setattr(overlays, "build", fake_build)
    monkeypatch.setattr(steps, "_feedback", lambda conn, eid, gates, step: [])
    monkeypatch.setattr(steps, "_card", lambda conn, eid, card, text: cards.append((card, text)))
    asyncio.run(steps.graphics_step({"id": 7, "model": None, "effort": None},
                                    {"id": 1, "folder": str(tmp_path), "stage_run": 3}))
    (card, text), = cards
    assert card["type"] == "overlays" and card["run"] == 3 and card["warnings"] == ["轮胎图：丢弃 1 条来源"]
    assert [(i["name"], i["path"]) for i in card["items"]] == [
        ("赛道简图", "09_版本/图形-r3/05_图形/赛道图.png"), ("赛程卡", "09_版本/图形-r3/05_图形/赛程.png"),
        ("轮胎策略图", "09_版本/图形-r3/05_图形/轮胎策略.png")]
    assert all((tmp_path / i["path"]).is_file() for i in card["items"])
    assert "赛程卡叠在 2.0–8.0 秒" in text and "提醒：轮胎图" in text
    assert (got["model"], got["effort"]) == config.ROLE_MODELS["图形"] and got["task_id"] == 7


# ---------- 积分榜、数据卡（所有分支） ----------

def test_data_panel_triggers():
    R = overlays.STANDINGS_RE
    for s in ("安东内利的积分还在涨", "两人分差缩小到两位数", "总冠军争夺基本没悬念", "他领先拉塞尔 102 分",
              "落后榜首一百零二分"):
        assert R.search(s), s
    for s in ("他领先了 5 分钟", "他领先拉塞尔两秒", "落后第二名 3 秒"):
        assert not R.search(s), s
    assert overlays.TEAM_STANDINGS_RE.search("梅赛德斯在车队积分上一骑绝尘")
    assert not overlays.TEAM_STANDINGS_RE.search("安东内利的积分")
    assert overlays.is_data_para("他比第二名快了 1.2 秒。") and overlays.is_data_para("全程一共 55 圈。")
    assert not overlays.is_data_para("比赛很精彩，他跑得很快。")       # 没有数字
    assert not overlays.is_data_para("今年的 3 位新人都很稳。")        # 有数字没数据词


def test_said_and_pos_said():
    assert overlays.said("55", "全程 55 圈[F3]") and overlays.said("55", "全程五十五圈")
    assert not overlays.said("55", "全程 155 圈") and not overlays.said("55", "全程 56 圈")
    assert overlays.said("1.2", "快了 1.2 秒") and not overlays.said("1.2", "快了 1.25 秒")
    assert overlays.said("2", "一共两次进站")
    assert overlays._pos_said(1, "维斯塔潘拿下第 1 名") is True and overlays._pos_said(1, "维斯塔潘拿下第一名") is True
    assert overlays._pos_said(2, "安东内利第 3 名") is False
    assert overlays._pos_said(1, "维斯塔潘赢了") is None


def test_standings_round(conn, monkeypatch):
    def ep(branch, code):
        return {"branch": branch, "code": code, "created_at": None}
    ev = CONVENTIONAL                                       # 第 16 站，正赛 2026-10-04
    assert overlays.standings_round(conn, ep("前瞻", "20261001-前瞻-巴林-01"), ev)[:2] == (2026, 15)
    assert overlays.standings_round(conn, ep("复盘", "20261005-复盘-巴林-01"), ev)[:2] == (2026, 16)
    # 复盘出得早、本站还没跑完：取本期日期前已跑完的最近一站
    prev = dict(CONVENTIONAL, round=15, event_date="2026-09-27", name_zh="阿塞拜疆大奖赛", sessions=[])
    monkeypatch.setattr(overlays.calendar, "_all_events", lambda conn: [prev, CONVENTIONAL])
    assert overlays.standings_round(conn, ep("复盘", "20261003-复盘-巴林-01"), ev)[:2] == (2026, 15)
    assert overlays.standings_round(conn, ep("前瞻", "20260301-前瞻-澳大利亚-01"), dict(ev, round=1)) is None
    monkeypatch.setattr(overlays.calendar, "_all_events", lambda conn: [CONVENTIONAL])
    assert overlays.standings_round(conn, ep("资讯", "20260901-资讯-x-01"), None) is None


def _standing_rows(n=12):
    return [{"pos": i + 1, "name": f"车手{i + 1}", "team": "梅赛德斯", "color": graphics.team_color("Mercedes"),
             "points": 340 - i * 21.5, "hl": i in (0, 10)} for i in range(n)]


@needs_fonts
@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720)])
def test_render_standings_and_datacard_png(tmp_path, size):
    table = {"kind": "drivers", "title": "车手积分榜", "subtitle": "2026 赛季 · 第 17 站新加坡大奖赛后",
             "rows": _standing_rows(), "gap_after": 10, "credit": "数据：FastF1 / Ergast"}
    rgb = _check_panel(panels.render_standings(table, tmp_path / "s.png", size=size), size)
    assert rgb.std() > 10                                   # 不是一块空底板
    teams = {"kind": "teams", "title": "车队积分榜", "subtitle": None, "gap_after": None, "credit": "数据：FastF1 / Ergast",
             "rows": [{"pos": 1, "name": "梅赛德斯", "color": graphics.team_color("Mercedes"), "points": 578, "hl": True}]}
    _check_panel(panels.render_standings(teams, tmp_path / "t.png", size=size), size)
    card = {"title": "比赛数据", "subtitle": "巴林大奖赛", "credit": "数据：FastF1",
            "rows": [{"label": "正赛第 1 名", "value": "维斯塔潘", "bar": None},
                     {"label": "最快圈", "value": "1:32.1", "bar": None},
                     {"label": "进站", "value": "2 次", "bar": 1.0}, {"label": "安全车", "value": "1 次", "bar": 0.5},
                     {"label": "多余的第五行", "value": "9", "bar": None}]}
    _check_panel(panels.render_datacard(card, tmp_path / "d.png", size=size), size)


FAKE_STANDINGS = {
    "season": 2026, "round": 16, "source": "FastF1 / Ergast",
    "drivers": [{"pos": i + 1, "given": g, "family": f, "code": "", "team": t, "points": p, "wins": 0}
                for i, (g, f, t, p) in enumerate([
                    ("Andrea Kimi", "Antonelli", "Mercedes", 340), ("George", "Russell", "Mercedes", 238),
                    ("Lewis", "Hamilton", "Ferrari", 229), ("Max", "Verstappen", "Red Bull Racing", 221),
                    ("Lando", "Norris", "McLaren", 180), ("Oscar", "Piastri", "McLaren", 170),
                    ("Charles", "Leclerc", "Ferrari", 160), ("Isack", "Hadjar", "Red Bull Racing", 60),
                    ("Alexander", "Albon", "Williams", 40), ("Carlos", "Sainz", "Williams", 30),
                    ("Nico", "Hülkenberg", "Kick Sauber", 20), ("Esteban", "Ocon", "Haas F1 Team", 10)])],
    "teams": [{"pos": 1, "team": "Mercedes", "points": 578, "wins": 10}, {"pos": 2, "team": "Ferrari", "points": 389, "wins": 2}],
}

DATA_SCRIPT = """## 开场
今天复盘巴林站。

## 积分形势
咱们先看看积分。

安东内利现在积分 340 分，领先拉塞尔 102 分，霍肯伯格也拿到了积分，总冠军争夺基本没悬念了。

## 比赛数据
维斯塔潘拿下第 1 名[F1]，安东内利第 2 名[F2]。全程 55 圈[F3]。

冠军一共跑了 56 圈[F3]，汉密尔顿第 3 名[F4]。
"""


def _review(conn):
    for kind, orig, zh in (("车手", "Kimi Antonelli", "安东内利"), ("车手", "Max Verstappen", "维斯塔潘"),
                           ("车手", "Nico Hulkenberg", "霍肯伯格"), ("车手", "George Russell", "拉塞尔"),
                           ("车手", "Lewis Hamilton", "汉密尔顿"), ("车队", "Mercedes", "梅赛德斯"),
                           ("车队", "Red Bull Racing", "红牛")):
        glossary.add_manual(conn, kind, orig, zh)
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="复盘", gp_label="巴林大奖赛", title="巴林复盘")
    src = "FastF1 2026 第 16 站成绩"
    facts.add(conn, eid, "F", "正赛第1名：Max Verstappen（Red Bull Racing）", source=src, value="1")
    facts.add(conn, eid, "F", "正赛第2名：Kimi Antonelli（Mercedes）", source=src, value="2")
    facts.add(conn, eid, "F", "正赛冠军完成圈数", source=src, value="55")
    facts.add(conn, eid, "F", "正赛第3名：Lewis Hamilton（Ferrari）", source=src, value="3")
    return episodes.get(conn, eid)


@pytest.fixture
def standings(monkeypatch):
    calls = []

    def fake(season, rnd):
        calls.append((season, rnd))
        return FAKE_STANDINGS
    monkeypatch.setattr(overlays, "standings_round", lambda conn, ep, ev: (2026, 16, "巴林大奖赛"))
    monkeypatch.setattr(overlays, "standings_fastf1", fake)
    return calls


@needs_fonts
def test_build_review_standings_and_datacard(conn, stage, standings):
    ep = _review(conn)
    folder = _write_inputs(ep, DATA_SCRIPT)

    async def boom(*a, **kw):
        raise AssertionError("不该调 GPT")
    res = overlays.build(conn, ep, runner=boom)
    g = folder / "05_图形"
    layer = json.loads((g / "叠层.json").read_text(encoding="utf-8"))["overlays"]
    assert [(o["kind"], o["path"]) for o in layer] == [("积分榜", "05_图形/积分榜-车手.png"),
                                                       ("数据卡", "05_图形/数据卡-1.png")]
    assert res["note"] == "复盘分支不加赛道简图" and "section" not in res and "track" not in res
    assert not (g / "赛道图.png").exists() and not (g / "赛程.png").exists()
    assert standings == [(2026, 16)]                        # 只取一次
    # 积分榜放在命中最多的那段（第 3 段），不放在开场那句“看看积分”
    st, dc = res["data_panels"]
    assert st["name"] == "车手积分榜" and st["how"] == "关键词（第 3 段「积分」）"
    assert dc["name"] == "数据卡" and dc["how"] == "数据（第 4 段，3 项，已和事实表核对）"
    for o in layer:
        assert o["end"] - o["start"] <= overlays.PANEL_MAX + 1e-6
        _check_panel(folder / o["path"])
    assert json.loads((g / "积分榜数据.json").read_text(encoding="utf-8"))["drivers"][0]["family"] == "Antonelli"
    # 第 5 段 56 圈和事实表 55 圈对不上：不做卡、有提醒
    assert not (g / "数据卡-2.png").exists()
    assert any("对不上" in w and "[F3]" in w for w in res["warnings"])


def test_standings_table_and_datacard_rows(conn):
    ep = _review(conn)
    table, warns = overlays.standings_table(conn, FAKE_STANDINGS, "drivers", "安东内利领先，霍肯伯格也拿到积分",
                                            "2026 赛季 · 第 16 站巴林大奖赛后")
    names = [r["name"] for r in table["rows"]]
    assert names[:2] == ["安东内利", "拉塞尔"] and names[-1] == "霍肯伯格" and len(names) == 11
    assert table["gap_after"] == 10 and table["credit"] == "数据：FastF1 / Ergast"
    assert [r["hl"] for r in table["rows"]].count(True) == 2
    assert table["rows"][0]["color"] == graphics.team_color("Mercedes") and table["rows"][0]["team"] == "梅赛德斯"
    assert any("Norris" in w and "没有译名" in w for w in warns)
    teams, _ = overlays.standings_table(conn, FAKE_STANDINGS, "teams", "梅赛德斯", None)
    assert [r["name"] for r in teams["rows"]] == ["梅赛德斯", "Ferrari"] and teams["rows"][0]["hl"]

    fmap = {f["tag"]: f for f in facts.items(conn, ep["id"], "F")}
    card, warns = overlays.data_card(conn, "维斯塔潘拿下第 1 名[F1]，安东内利第 2 名[F2]。全程 55 圈[F3]。",
                                     "比赛数据", "巴林大奖赛", fmap)
    assert [(r["label"], r["value"]) for r in card["rows"]] == [
        ("正赛第 1 名", "维斯塔潘"), ("正赛第 2 名", "安东内利"), ("正赛冠军完成圈数", "55 圈")]
    assert card["credit"] == "数据：FastF1 2026 第 16 站成绩" and not warns
    # 数字对不上整张不做；名次说错也不做；只有一项不成卡
    for line in ("维斯塔潘第 1 名[F1]，全程 56 圈[F3]。", "维斯塔潘第 2 名[F1]，全程 55 圈[F3]。"):
        card, warns = overlays.data_card(conn, line, None, None, fmap)
        assert card is None and "对不上" in warns[0]
    assert overlays.data_card(conn, "全程 55 圈[F3]。", None, None, fmap) == (None, [])
    assert overlays.data_card(conn, "全程 55 圈。", None, None, fmap) == (None, [])      # 没标 [F#] 不认


@needs_fonts
def test_build_review_standings_unavailable_skips(conn, stage, monkeypatch):
    ep = _review(conn)
    folder = _write_inputs(ep, DATA_SCRIPT)
    calls = []

    def fail(season, rnd):
        calls.append(rnd)
        raise RuntimeError("FastF1 / Ergast 读取失败：离线")
    monkeypatch.setattr(overlays, "standings_round", lambda conn, ep, ev: (2026, 16, "巴林大奖赛"))
    monkeypatch.setattr(overlays, "standings_fastf1", fail)
    res = overlays.build(conn, ep)
    layer = json.loads((folder / "05_图形" / "叠层.json").read_text(encoding="utf-8"))["overlays"]
    assert [o["kind"] for o in layer] == ["数据卡"] and calls == [16]
    assert any("积分取不到，没做" in w for w in res["warnings"])
    assert not (folder / "05_图形" / "积分榜-车手.png").exists()
    assert not (folder / "05_图形" / "积分榜数据.json").exists()
    # 赛季首站前：不联网、不做、不报错
    monkeypatch.setattr(overlays, "standings_round", lambda conn, ep, ev: None)
    res = overlays.build(conn, ep)
    assert calls == [16] and any("确定不了" in w for w in res["warnings"]) and res["overlays"] == 1


@needs_fonts
def test_data_panels_cap_and_used_paras(conn, stage, standings, monkeypatch):
    ep = _review(conn)
    folder = _write_inputs(ep, DATA_SCRIPT)
    script = (folder / "02_文案" / "逐字稿.md").read_text(encoding="utf-8")
    units = narration.prepare(script, conn).units
    align = json.loads((folder / "03_配音" / "对齐.json").read_text(encoding="utf-8"))
    out = folder / "05_图形"
    out.mkdir(exist_ok=True)
    # 第 3 段已放了别的面板：积分榜退到下一个讲积分的段（第 2 段），不重复占段
    cands, _ = overlays.data_panels(conn, ep, CONVENTIONAL, script, units, align, out, {2})
    assert [(c["kind"], c["_how"]) for c in cands] == [("积分榜", "关键词（第 2 段「积分」）"),
                                                       ("数据卡", "数据（第 4 段，3 项，已和事实表核对）")]
    cands, _ = overlays.data_panels(conn, ep, CONVENTIONAL, script, units, align, out, {1, 2, 3})
    assert cands == []
    # 全片上限
    monkeypatch.setattr(overlays, "MAX_DATA_PANELS", 1)
    cands, warns = overlays.data_panels(conn, ep, CONVENTIONAL, script, units, align, out, set())
    assert [c["kind"] for c in cands] == ["积分榜"] and any("已到 1 张上限，第 4 段起" in w for w in warns)


def test_graphics_step_card_with_data_panels(tmp_path, monkeypatch):
    cards = []
    g = tmp_path / "05_图形"
    g.mkdir()
    for name in ("积分榜-车手.png", "数据卡-1.png"):
        Image.new("RGBA", (8, 8)).save(g / name)
    (g / "叠层.json").write_text(json.dumps({"overlays": [
        {"path": "05_图形/积分榜-车手.png", "start": 5.0, "end": 13.0, "kind": "积分榜"},
        {"path": "05_图形/数据卡-1.png", "start": 20.0, "end": 26.0, "kind": "数据卡"}]}), encoding="utf-8")

    def fake_build(conn, ep, feedback, **kw):
        return {"warnings": [], "note": "复盘分支不加赛道简图", "data_panels": [
            {"name": "车手积分榜", "how": "关键词（第 3 段「积分」）", "start": 5.0, "end": 13.0,
             "path": "05_图形/积分榜-车手.png"},
            {"name": "数据卡", "how": "数据（第 4 段，3 项，已和事实表核对）", "start": 20.0, "end": 26.0,
             "path": "05_图形/数据卡-1.png"}]}
    monkeypatch.setattr(overlays, "build", fake_build)
    monkeypatch.setattr(steps, "_feedback", lambda conn, eid, gates, step: [])
    monkeypatch.setattr(steps, "_card", lambda conn, eid, card, text: cards.append((card, text)))
    asyncio.run(steps.graphics_step({"id": 7, "model": None, "effort": None},
                                    {"id": 1, "folder": str(tmp_path), "stage_run": 2}))
    (card, text), = cards
    assert [(i["name"], i["path"]) for i in card["items"]] == [
        ("车手积分榜", "09_版本/图形-r2/05_图形/积分榜-车手.png"), ("数据卡", "09_版本/图形-r2/05_图形/数据卡-1.png")]
    assert all((tmp_path / i["path"]).is_file() for i in card["items"])
    assert "车手积分榜叠在 5.0–13.0 秒" in text and "数据卡叠在 20.0–26.0 秒" in text


# ---------- 合成：多张面板 ----------

def test_edit_and_compose_handle_multiple_panels(tmp_path, monkeypatch):
    g = tmp_path / "05_图形"
    g.mkdir()
    for name, color in (("赛程.png", (255, 0, 0, 255)), ("赛道图.png", (0, 0, 255, 255))):
        Image.new("RGBA", (64, 36), color).save(g / name)
    (g / "叠层.json").write_text(json.dumps({"overlays": [
        {"path": "05_图形/赛程.png", "start": 0.0, "end": 1.0, "kind": "赛程"},
        {"path": "05_图形/赛道图.png", "start": 1.0, "end": 2.0, "kind": "赛道图"}]}), encoding="utf-8")
    items = edit._overlays(tmp_path, 3.0, [])
    assert [o["kind"] for o in items] == ["赛程", "赛道图"]

    monkeypatch.setattr(compose, "_watermark", lambda size: (Image.new("RGBA", (1, 1)), (0, 0)))
    layers = [{"f0": compose._frame(o["start"], 10), "f1": compose._frame(o["end"], 10), "order": 0,
               "path": tmp_path / o["path"]} for o in items]
    listing = compose._overlay_track(layers, 30, (64, 36), 10, tmp_path / "work")
    files = [l.split("'")[1] for l in listing.read_text().splitlines() if l.startswith("file")]
    px = [Image.open(f).getpixel((32, 18)) for f in files[:3]]
    assert px == [(255, 0, 0, 255), (0, 0, 255, 255), (0, 0, 0, 0)]
