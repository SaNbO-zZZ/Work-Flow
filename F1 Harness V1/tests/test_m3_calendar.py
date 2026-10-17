from datetime import date, datetime, timedelta, timezone

import pytest

from hermes import calendar as cal
from hermes import db

UTC = timezone.utc


def _event(rnd, name, country, location, day, *, fmt="conventional", race_hour=7, sessions=None):
    d = date.fromisoformat(day)
    race = datetime(d.year, d.month, d.day, race_hour, tzinfo=UTC)
    if sessions is None:
        sessions = [("Practice 1", race - timedelta(days=2, hours=3)), ("Practice 2", race - timedelta(days=2)),
                    ("Practice 3", race - timedelta(days=1, hours=3)), ("Qualifying", race - timedelta(days=1)),
                    ("Race", race)]
    return {"round": rnd, "name_en": name, "official_name": f"FORMULA 1 {name.upper()} 2026", "country": country,
            "location": location, "event_date": d, "format": fmt,
            "sessions": [{"idx": i, "name": n, "start_utc": t} for i, (n, t) in enumerate(sessions, 1)]}


def _t(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


# 美国站：正赛 UTC 周日 20:00 = 北京周一 04:00
US_SESSIONS = [("Practice 1", _t("2026-10-23 17:30")), ("Practice 2", _t("2026-10-23 21:00")),
               ("Practice 3", _t("2026-10-24 17:30")), ("Qualifying", _t("2026-10-24 21:00")),
               ("Race", _t("2026-10-25 20:00"))]


def season_2026():
    return [
        _event(1, "Australian Grand Prix", "Australia", "Melbourne", "2026-03-08", race_hour=4),
        _event(2, "Japanese Grand Prix", "Japan", "Suzuka", "2026-03-29", race_hour=5),
        _event(3, "Miami Grand Prix", "United States", "Miami", "2026-05-03", race_hour=20),
        _event(4, "Hungarian Grand Prix", "Hungary", "Budapest", "2026-07-26", race_hour=13),
        _event(5, "Dutch Grand Prix", "Netherlands", "Zandvoort", "2026-08-23", race_hour=13),
        _event(6, "Azerbaijan Grand Prix", "Azerbaijan", "Baku", "2026-09-26", race_hour=11),
        _event(7, "Bahrain Grand Prix", "Bahrain", "Kuala Lumpur", "2026-10-04"),
        _event(8, "United States Grand Prix", "United States", "Austin", "2026-10-25", sessions=US_SESSIONS),
        _event(9, "Abu Dhabi Grand Prix", "United Arab Emirates", "Yas Island", "2026-12-06", race_hour=13),
    ]


def season_2027():
    return [
        _event(1, "Australian Grand Prix", "Australia", "Melbourne", "2027-03-07", race_hour=4),
        _event(2, "Bahrain Grand Prix", "Bahrain", "Sakhir", "2027-03-14", race_hour=15),
    ]


def loader_of(data):
    return lambda season: data.get(season)


def _gloss(conn, original, *, suggested=None, approved=None):
    conn.execute("INSERT INTO glossary(kind, original, suggested, approved, state, source, updated_at) VALUES (?,?,?,?,?,?,?)",
                 ("大奖赛", original, suggested, approved, "approved" if approved else "pending", "测试", db.now()))


@pytest.fixture
def loaded(conn):
    _gloss(conn, "Bahrain Grand Prix", suggested="巴林大奖赛")
    _gloss(conn, "United States Grand Prix", suggested="美国站", approved="美国大奖赛")
    _gloss(conn, "Japanese Grand Prix", suggested="日本大奖赛")
    assert cal.refresh(conn, [2026, 2027], loader=loader_of({2026: season_2026(), 2027: season_2027()})) == []
    return conn


def test_first_load_no_changes_and_stores(loaded):
    conn = loaded
    assert conn.execute("SELECT count(*) FROM calendar_changes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM race_events WHERE season=2026").fetchone()[0] == 9
    assert conn.execute("SELECT value FROM settings WHERE key='calendar.refreshed_at'").fetchone()
    evs = cal.events(conn, 2026)
    bah = evs[6]
    assert (bah["name_zh"], bah["location"], bah["event_date"]) == ("巴林大奖赛", "Kuala Lumpur", "2026-10-04")
    assert bah["sessions"][-1] == {"idx": 5, "name": "Race", "start_utc": "2026-10-04T07:00:00+00:00",
                                   "start_bj": "2026-10-04 15:00"}
    assert evs[0]["name_zh"] is None
    # 相同数据再刷一次不算变动
    assert cal.refresh(conn, [2026], loader=loader_of({2026: season_2026()})) == []


def test_second_refresh_reports_changes(loaded):
    conn = loaded
    data = season_2026()
    data[6] = _event(7, "Bahrain Grand Prix", "Bahrain", "Kuala Lumpur", "2026-10-04", race_hour=8)
    data[3] = _event(4, "Hungarian Grand Prix", "Hungary", "Budapest", "2026-07-26", fmt="sprint_qualifying",
                     race_hour=13, sessions=[("Practice 1", _t("2026-07-24 10:30")),
                                             ("Sprint Qualifying", _t("2026-07-24 14:30")),
                                             ("Sprint", _t("2026-07-25 10:00")), ("Qualifying", _t("2026-07-25 14:00")),
                                             ("Race", _t("2026-07-26 13:00"))])
    del data[2]  # 迈阿密取消，其余轮次号不变
    data.append(_event(10, "Qatar Grand Prix", "Qatar", "Lusail", "2026-11-29", race_hour=16))
    changes = cal.refresh(conn, [2026], loader=loader_of({2026: data}))

    assert "2026 第7站 巴林大奖赛：正赛时间 10月04日 15:00 → 10月04日 16:00（北京时间）" in changes
    assert "2026 第4站 Hungarian Grand Prix：赛制 常规 → 冲刺" in changes
    assert any(c.startswith("2026 第4站 Hungarian Grand Prix：新增冲刺赛") for c in changes)
    assert "2026 第4站 Hungarian Grand Prix：取消三练" in changes
    assert "2026 取消第3站 Miami Grand Prix" in changes
    assert "2026 新增第10站 Qatar Grand Prix（正赛 11月29日）" in changes
    rows = conn.execute("SELECT season, round, detail FROM calendar_changes ORDER BY id").fetchall()
    assert [r["detail"] for r in rows] == changes
    assert conn.execute("SELECT count(*) FROM race_events WHERE season=2026 AND round=3").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM race_sessions WHERE season=2026 AND round=3").fetchone()[0] == 0
    assert next(e for e in cal.events(conn, 2026) if e["round"] == 4)["format"] == "sprint_qualifying"
    # 2027 未参与本次刷新，数据保持不动
    assert len(cal.events(conn, 2027)) == 2


def test_renumbered_rounds_and_rename(loaded):
    conn = loaded
    data = [e for e in season_2026() if e["round"] != 2]
    for e in data:
        if e["round"] > 2:
            e["round"] -= 1
    data[0]["name_en"] = "Australia Grand Prix"
    changes = cal.refresh(conn, [2026], loader=loader_of({2026: data}))
    assert "2026 取消第2站 日本大奖赛" in changes
    assert "2026 第1站 Australia Grand Prix：站名 Australian Grand Prix → Australia Grand Prix" in changes
    assert "2026 第6站 巴林大奖赛：轮次 第7站 → 第6站" in changes
    assert len(cal.events(conn, 2026)) == 8


@pytest.mark.parametrize("result", [None, []])
def test_loader_without_data_keeps_existing(loaded, result):
    conn = loaded
    assert cal.refresh(conn, [2026, 2027], loader=lambda s: result) == []
    assert len(cal.events(conn, 2026)) == 9 and len(cal.events(conn, 2027)) == 2


def test_default_seasons_are_this_and_next_year(conn):
    calls = []
    cal.refresh(conn, loader=lambda s: calls.append(s))
    y = datetime.now(cal.BEIJING).year
    assert calls == [y, y + 1]


@pytest.mark.parametrize("day, kind, season, rnd", [
    ("2026-09-29", "比赛周", 2026, 7),
    ("2026-09-28", "比赛周", 2026, 7),      # 周一
    ("2026-09-27", "比赛周", 2026, 6),      # 阿塞拜疆周六正赛所在周的周日
    ("2026-08-05", "夏休", 2026, None),
    ("2026-07-27", "夏休", 2026, None),     # 空档从 7 月开始，但含 8 月，整段算夏休
    ("2026-04-15", "发车区", 2026, None),
    ("2026-10-12", "发车区", 2026, None),
    ("2026-12-01", "比赛周", 2026, 9),
    ("2026-12-20", "冬歇", 2026, None),
    ("2027-01-10", "冬歇", 2026, None),     # 跨年冬歇
    ("2027-03-01", "比赛周", 2027, 1),
    ("2026-01-10", "冬歇", 2025, None),     # 首站之前，上一季无数据
    ("2027-06-01", "冬歇", 2027, None),     # 最后一站之后下季无数据
])
def test_period(loaded, day, kind, season, rnd):
    p = cal.period(loaded, date.fromisoformat(day))
    assert (p.kind, p.season, p.round) == (kind, season, rnd)


def test_period_without_data(conn):
    assert cal.period(conn, date(2026, 9, 29)) == cal.Period("冬歇", None, None, None)


def test_gp_label(loaded):
    assert cal.gp_label(loaded, date(2026, 9, 29)) == "巴林大奖赛"
    assert cal.gp_label(loaded, date(2026, 9, 22)) == "Azerbaijan Grand Prix"
    assert cal.gp_label(loaded, date(2026, 10, 20)) == "美国大奖赛"  # 已审译名优先于建议
    assert cal.gp_label(loaded, date(2026, 8, 5)) == "夏休"
    assert cal.gp_label(loaded, date(2026, 12, 20)) == "冬歇"


@pytest.mark.parametrize("day, expected", [
    ("2026-10-23", False),  # 当地周五一练 = 北京周六凌晨
    ("2026-10-24", True),
    ("2026-10-25", True),
    ("2026-10-26", True),   # 正赛 UTC 周日 20:00 = 北京周一 04:00
    ("2026-10-27", False),
    ("2026-10-04", True),
    ("2026-10-05", False),
])
def test_is_race_day_beijing(loaded, day, expected):
    assert cal.is_race_day(loaded, date.fromisoformat(day)) is expected


def test_find_event(loaded):
    conn = loaded
    today = date(2026, 9, 29)
    for text in ("巴林", "巴林大奖赛", "巴林站", "Bahrain GP", "bahrain grand prix", "BAHRAIN", "kuala lumpur"):
        e = cal.find_event(conn, text, today=today)
        assert (e["season"], e["round"]) == (2026, 7), text
    assert cal.find_event(conn, "美国大奖赛", today=today)["round"] == 8
    assert cal.find_event(conn, "Hungary", today=today)["name_en"] == "Hungarian Grand Prix"
    assert cal.find_event(conn, "Abu Dhabi", today=today)["round"] == 9
    assert cal.find_event(conn, "日本", today=today)["name_zh"] == "日本大奖赛"
    assert cal.find_event(conn, "Miami GP", today=today)["round"] == 3  # 精确命中 location，不被 United States 干扰
    assert cal.find_event(conn, "火星大奖赛", today=today) is None
    assert cal.find_event(conn, "大奖赛", today=today) is None
    # 两季都有巴林：缺省取离今天最近，指定赛季则只看该季
    assert cal.find_event(conn, "巴林", today=date(2027, 2, 1))["season"] == 2027
    assert cal.find_event(conn, "巴林", 2026, today=date(2027, 2, 1))["season"] == 2026
    assert set(cal.find_event(conn, "巴林", today=today)) >= {"round", "name_en", "name_zh", "event_date", "sessions"}


def test_label_for(loaded):
    conn = loaded
    d = date(2026, 9, 29)
    assert cal.label_for(conn, "前瞻", d, "巴林") == "巴林大奖赛"
    assert cal.label_for(conn, "前瞻", d) == "巴林大奖赛"
    assert cal.label_for(conn, "前瞻", d, "不存在的站") == "巴林大奖赛"
    assert cal.label_for(conn, "复盘", d) == "Azerbaijan Grand Prix"
    assert cal.label_for(conn, "复盘", d, "日本") == "日本大奖赛"
    # 美国站正赛在北京周一凌晨：周日还没办完，周一才算
    assert cal.label_for(conn, "复盘", date(2026, 10, 25)) == "巴林大奖赛"
    assert cal.label_for(conn, "复盘", date(2026, 10, 26)) == "美国大奖赛"
    assert cal.label_for(conn, "资讯", date(2026, 8, 5)) == "夏休"
    assert cal.label_for(conn, "资讯", d, "日本") == "巴林大奖赛"
    assert cal.label_for(conn, "科普", d) == "巴林大奖赛"
    assert cal.label_for(conn, "科普", date(2026, 4, 15)) == "发车区"
    with pytest.raises(ValueError):
        cal.label_for(conn, "闲聊", d)


def test_refresh_rejects_incomplete_backend(conn):
    cal.refresh(conn, [2026], loader=loader_of({2026: season_2026()}))
    assert cal.refresh(conn, [2026], loader=loader_of({2026: season_2026()[:3]})) == []
    assert len(cal.events(conn, 2026)) == 9


def test_find_event_by_circuit_zh(conn):
    cal.refresh(conn, [2026], loader=loader_of({2026: season_2026()}))
    conn.execute("INSERT INTO glossary(kind, original, suggested, state, source, updated_at) VALUES ('赛道','Kuala Lumpur','雪邦','pending','测试',?)",
                 (db.now(),))
    assert cal.find_event(conn, "雪邦", today=date(2026, 9, 29))["round"] == 7


def test_summer_break_follows_fia_shutdown_window(conn):
    # 荷兰 8-23 → 意大利 9-06 之间只有 8-24~8-30 一周，虽然在 8 月也不算夏休
    data = season_2026() + [_event(10, "Italian Grand Prix", "Italy", "Monza", "2026-09-06", race_hour=13)]
    cal.refresh(conn, [2026], loader=loader_of({2026: data}))
    assert cal.summer_break(conn, 2026) == (date(2026, 7, 27), date(2026, 8, 16))
    assert cal.period(conn, date(2026, 7, 27)).kind == "夏休"
    assert cal.period(conn, date(2026, 8, 16)).kind == "夏休"
    assert cal.period(conn, date(2026, 8, 26)).kind == "发车区"
    assert cal.period(conn, date(2026, 4, 15)).kind == "发车区"   # 4 月长空档不是夏休
