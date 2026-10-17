"""M3-4 联网实测：临时库里造第 15/16 站场次，真实请求 Open-Meteo 预报与历史接口，打印场次天气和生成的事实。
用法：.venv/bin/python tests/live_weather_check.py"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from hermes import db, episodes, facts, weather  # noqa: E402

# 场次时间取自 FastF1 get_event_schedule(2026)
ROUNDS = {
    15: ("Azerbaijan Grand Prix", "Baku", "2026-09-26", [
        (1, "Practice 1", "2026-09-24T08:30:00Z"), (2, "Practice 2", "2026-09-24T12:00:00Z"),
        (3, "Practice 3", "2026-09-25T08:30:00Z"), (4, "Qualifying", "2026-09-25T12:00:00Z"),
        (5, "Race", "2026-09-26T11:00:00Z")]),
    16: ("Bahrain Grand Prix", "Kuala Lumpur", "2026-10-04", [
        (1, "Practice 1", "2026-10-02T04:30:00Z"), (2, "Practice 2", "2026-10-02T08:00:00Z"),
        (3, "Practice 3", "2026-10-03T04:30:00Z"), (4, "Qualifying", "2026-10-03T08:00:00Z"),
        (5, "Race", "2026-10-04T07:00:00Z")]),
}


async def show(conn, rnd: int, client: httpx.AsyncClient) -> None:
    name, loc, day, _ = ROUNDS[rnd]
    print(f"\n== 第 {rnd} 站 {name}（{loc}，坐标 {weather.CIRCUITS[loc]}）", flush=True)
    for w in await weather.session_weather(conn, 2026, rnd, client=client):
        print(" ", w)
    ep = episodes.new_session(conn, f"实测 第{rnd}站")
    tags = await weather.add_weather_facts(conn, ep, 2026, rnd, client=client)
    if not tags:
        print("  （历史接口暂无这几天的数据，未生成事实）")
    print(facts.render(conn, ep))


async def main() -> None:
    conn = db.connect(Path(tempfile.mkdtemp(prefix="hermes-live-")) / "t.db")
    with db.transaction(conn):
        for rnd, (name, loc, day, sessions) in ROUNDS.items():
            conn.execute("INSERT INTO race_events(season, round, name_en, location, event_date, format, updated_at) "
                         "VALUES (2026, ?, ?, ?, ?, 'conventional', ?)", (rnd, name, loc, day, db.now()))
            conn.executemany("INSERT INTO race_sessions(season, round, idx, name, start_utc) VALUES (2026, ?, ?, ?, ?)",
                             [(rnd, *s) for s in sessions])
    print("今天（UTC）：", weather._today())
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": weather.UA}) as client:
        await show(conn, 16, client)
        await show(conn, 15, client)


asyncio.run(main())
