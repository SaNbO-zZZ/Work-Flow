import asyncio
from datetime import date, datetime

import httpx
import pytest

from hermes import db, episodes, facts, weather

LOCATIONS_2026 = ["Melbourne", "Shanghai", "Suzuka", "Miami Gardens", "Montréal", "Monte Carlo", "Barcelona",
                  "Spielberg", "Silverstone", "Spa-Francorchamps", "Budapest", "Zandvoort", "Monza", "Madrid", "Baku",
                  "Kuala Lumpur", "Marina Bay", "Austin", "Mexico City", "São Paulo", "Las Vegas", "Lusail",
                  "Yas Marina", "Sakhir", "Jeddah", "Imola"]


def test_circuits_cover_2026():
    assert set(LOCATIONS_2026) <= set(weather.CIRCUITS)
    for lat, lon in weather.CIRCUITS.values():
        assert -90 <= lat <= 90 and -180 <= lon <= 180
        assert round(lat, 4) == lat and round(lon, 4) == lon


def fake_api(archive_status=200):
    """按请求日期逐小时造数据：气温 25+h/10，降水概率 h*5，降水 h/10，风速 10.4，湿度 80（h 为 UTC 小时）。"""
    calls = []

    def handler(req: httpx.Request):
        p = dict(req.url.params)
        archive = req.url.host == "archive-api.open-meteo.com"
        calls.append(("archive" if archive else "forecast", p))
        if archive and archive_status != 200:
            return httpx.Response(archive_status, json={"error": True, "reason": "out of range"})
        start, end = date.fromisoformat(p["start_date"]), date.fromisoformat(p["end_date"])
        days = [date.fromordinal(d).isoformat() for d in range(start.toordinal(), end.toordinal() + 1)]
        hours = [(d, h) for d in days for h in range(24)]
        names = p["hourly"].split(",")
        gen = {"temperature_2m": lambda h: 25 + h / 10, "precipitation_probability": lambda h: h * 5,
               "precipitation": lambda h: h / 10, "wind_speed_10m": lambda h: 10.4, "relative_humidity_2m": lambda h: 80}
        hourly = {"time": [f"{d}T{h:02d}:00" for d, h in hours], **{n: [gen[n](h) for _, h in hours] for n in names}}
        daily = {"time": days, **{n: [1.0] * len(days) for n in p["daily"].split(",")}}
        return httpx.Response(200, json={"latitude": float(p["latitude"]), "hourly": hourly, "daily": daily})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(weather, "_today", lambda: date(2026, 10, 1))


def seed(conn, location="Kuala Lumpur", sessions=None):
    sessions = sessions or [
        (1, "Practice 1", "2026-09-30T03:30:00+00:00"),
        (2, "Practice 2", None),
        (4, "Qualifying", "2026-10-03T08:00:00Z"),
        (5, "Race", "2026-10-04T07:00:00"),
    ]
    with db.transaction(conn):
        conn.execute("INSERT INTO race_events(season, round, name_en, location, event_date, format, updated_at) "
                     "VALUES (2026, 16, 'Bahrain Grand Prix', ?, '2026-10-04', 'conventional', ?)", (location, db.now()))
        conn.executemany("INSERT INTO race_sessions(season, round, idx, name, start_utc) VALUES (2026, 16, ?, ?, ?)", sessions)


def test_forecast_splits_past_and_future(today):
    client, calls = fake_api()
    data = asyncio.run(weather.forecast(2.76, 101.74, date(2026, 9, 30), date(2026, 10, 2), client=client))
    assert [(k, p["start_date"], p["end_date"]) for k, p in calls] == [
        ("archive", "2026-09-30", "2026-09-30"), ("forecast", "2026-10-01", "2026-10-02")]
    archive_params, forecast_params = calls[0][1], calls[1][1]
    assert "precipitation_probability" not in archive_params["hourly"]
    assert "precipitation_probability_max" not in archive_params["daily"]
    assert forecast_params["hourly"].split(",") == list(weather.HOURLY) and forecast_params["timezone"] == "UTC"
    assert data["source"] == "mixed" and len(data["hourly"]["time"]) == 72 and len(data["daily"]["time"]) == 3
    assert data["hourly"]["precipitation_probability"][0] is None and data["hourly"]["precipitation_probability"][24] == 0


def test_forecast_out_of_range_and_archive_missing(today):
    client, calls = fake_api(archive_status=400)
    far = asyncio.run(weather.forecast(0, 0, date(2026, 10, 17), date(2026, 10, 20), client=client))
    assert calls == [] and far["hourly"]["time"] == [] and far["source"] is None
    past = asyncio.run(weather.forecast(0, 0, date(2026, 9, 26), date(2026, 9, 27), client=client))
    assert past["source"] == "archive" and past["hourly"]["time"] == [] and past["daily"]["time"] == []


def test_archive_null_hours_dropped(today):
    def handler(req):
        return httpx.Response(200, json={"hourly": {"time": ["2026-09-28T00:00", "2026-09-28T01:00"],
                                                    "temperature_2m": [30.1, None], "precipitation": [0.0, None]},
                                         "daily": {"time": ["2026-09-28"], "temperature_2m_max": [None]}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    data = asyncio.run(weather.forecast(0, 0, date(2026, 9, 28), date(2026, 9, 28), client=client))
    assert data["hourly"]["time"] == ["2026-09-28T00:00"] and data["hourly"]["temperature_2m"] == [30.1]
    assert data["daily"]["time"] == []


def test_session_weather_picks_start_hour(conn, today):
    seed(conn)
    client, calls = fake_api()
    rows = asyncio.run(weather.session_weather(conn, 2026, 16, client=client))
    assert calls[0][1]["latitude"] == str(weather.CIRCUITS["Kuala Lumpur"][0])
    assert [r["session"] for r in rows] == ["Practice 1", "Qualifying", "Race"]
    p1, q, race = rows
    assert p1["start_bj"] == "09-30 11:30" and p1["temp"] == 25.3 and p1["precip_prob"] is None
    assert p1["source"] == "archive"
    assert q["start_bj"] == "10-03 16:00" and q["precip_prob"] == 40 and q["source"] == "forecast"
    assert race == {"session": "Race", "start_bj": "10-04 15:00", "temp": 25.7, "precip_prob": 35, "precip_mm": 0.7,
                    "wind_kmh": 10.4, "humidity": 80, "source": "forecast"}


def test_session_weather_errors(conn, today):
    client, _ = fake_api()
    with pytest.raises(KeyError):
        asyncio.run(weather.session_weather(conn, 2026, 16, client=client))
    seed(conn, location="Atlantis")
    with pytest.raises(KeyError, match="Atlantis"):
        asyncio.run(weather.session_weather(conn, 2026, 16, client=client))


def test_add_weather_facts(conn, today):
    seed(conn, sessions=[(1, "Practice 1", "2026-10-02T03:30:00Z"), (2, "Sprint Qualifying", "2026-10-02T07:30:00Z"),
                         (3, "Sprint", "2026-10-03T03:00:00Z"), (4, "Qualifying", "2026-10-03T08:00:00Z"),
                         (5, "Race", "2026-10-04T07:00:00Z")])
    ep = episodes.new_session(conn, "天气")
    client, _ = fake_api()
    tags = asyncio.run(weather.add_weather_facts(conn, ep, 2026, 16, client=client))
    assert tags == [f"F{i}" for i in range(1, 16)]
    fs = facts.items(conn, ep)
    assert fs[0]["content"] == "冲刺开始时（北京时间 10-03 11:00）气温（°C）"
    rain = next(f for f in fs if f["content"] == "正赛开始时（北京时间 10-04 15:00）降水概率（%）")
    assert rain["value"] == "35" and rain["kind"] == "F"
    today_bj = datetime.now(weather.BEIJING).date().isoformat()
    assert rain["source"] == f"Open-Meteo 预报 {today_bj} 取数"
    by_content = {f["content"]: f["value"] for f in fs}
    assert by_content["排位开始时（北京时间 10-03 16:00）气温（°C）"] == "26"
    assert by_content["正赛开始时（北京时间 10-04 15:00）降水量（mm）"] == "0.7"
    assert by_content["正赛开始时（北京时间 10-04 15:00）风速（km/h）"] == "10"
    assert by_content["正赛开始时（北京时间 10-04 15:00）相对湿度（%）"] == "80"
    # 写入的事实能直接被逐字稿核对使用，口语写法也能对上
    assert facts.check_text(conn, ep, f"正赛开始时降水概率 35%[{rain['tag']}]") == []
    assert facts.check_text(conn, ep, f"正赛开始时降水概率百分之35[{rain['tag']}]") == []


def test_fact_value_rounds_half_up():
    assert weather._fmt(24.5, "1") == "25" and weather._fmt(23.4, "1") == "23"
    assert weather._fmt(0.25, "0.1") == "0.3" and weather._fmt(0, "0.1") == "0.0"


def test_add_weather_facts_archive(conn, monkeypatch):
    monkeypatch.setattr(weather, "_today", lambda: date(2026, 10, 10))
    seed(conn, sessions=[(4, "Qualifying", "2026-10-03T08:00:00Z"), (5, "Race", "2026-10-04T07:00:00Z")])
    ep = episodes.new_session(conn, "天气")
    client, _ = fake_api()
    tags = asyncio.run(weather.add_weather_facts(conn, ep, 2026, 16, client=client))
    fs = facts.items(conn, ep)
    assert len(tags) == 8 and all(f["source"] == "Open-Meteo 历史数据" for f in fs)
    assert not any("降水概率" in f["content"] for f in fs)
