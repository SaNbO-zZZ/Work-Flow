"""赛道天气（Open-Meteo 免费接口）：今天起 16 天内走预报，已过去的日期走历史；可把关键场次天气写成 [F#] 事实。"""
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import httpx

from . import facts
from .db import transaction

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
UA = "F1Harness/1.0"
BEIJING = ZoneInfo("Asia/Shanghai")
FORECAST_DAYS = 16
HOURLY = ("temperature_2m", "precipitation_probability", "precipitation", "wind_speed_10m", "relative_humidity_2m")
DAILY = ("temperature_2m_max", "temperature_2m_min", "precipitation_sum", "precipitation_probability_max")
# 历史接口没有降水概率
_ARCHIVE_SKIP = {"precipitation_probability", "precipitation_probability_max"}

# FastF1 Location → 赛道本身的经纬度（维基百科赛道条目与 OpenStreetMap 交叉核对）
CIRCUITS: dict[str, tuple[float, float]] = {
    "Melbourne": (-37.8497, 144.9680),         # Albert Park
    "Shanghai": (31.3389, 121.2197),           # 上海国际赛车场
    "Suzuka": (34.8431, 136.5407),
    "Miami Gardens": (25.9575, -80.2379),      # Miami International Autodrome
    "Montréal": (45.5004, -73.5276),           # Circuit Gilles Villeneuve
    "Monte Carlo": (43.7347, 7.4206),
    "Barcelona": (41.5700, 2.2611),            # Circuit de Barcelona-Catalunya（Montmeló）
    "Spielberg": (47.2197, 14.7647),           # Red Bull Ring
    "Silverstone": (52.0786, -1.0169),
    "Spa-Francorchamps": (50.4372, 5.9714),
    "Budapest": (47.5789, 19.2486),            # Hungaroring（Mogyoród）
    "Zandvoort": (52.3888, 4.5409),
    "Monza": (45.6156, 9.2811),
    "Madrid": (40.4736, -3.6235),              # Madring（IFEMA 与 Valdebebas 之间）
    "Baku": (40.3729, 49.8530),                # Baku City Circuit
    "Kuala Lumpur": (2.7608, 101.7382),        # Sepang International Circuit
    "Marina Bay": (1.2914, 103.8640),
    "Austin": (30.1328, -97.6411),             # Circuit of the Americas
    "Mexico City": (19.4042, -99.0907),        # Autódromo Hermanos Rodríguez
    "São Paulo": (-23.7036, -46.6997),         # Interlagos
    "Las Vegas": (36.1109, -115.1650),         # Las Vegas Strip Circuit
    "Lusail": (25.4900, 51.4542),
    "Yas Marina": (24.4672, 54.6031),
    "Sakhir": (26.0325, 50.5106),
    "Jeddah": (21.6319, 39.1044),              # Jeddah Corniche Circuit
    "Imola": (44.3439, 11.7167),
}


def _today() -> date:
    return datetime.now(timezone.utc).date()


@asynccontextmanager
async def _client(client: httpx.AsyncClient | None):
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}) as c:
        yield c


async def _fetch(client, archive: bool, lat, lon, start: date, end: date) -> dict | None:
    skip = _ARCHIVE_SKIP if archive else set()
    params = {
        "latitude": lat, "longitude": lon, "timezone": "UTC",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": ",".join(v for v in HOURLY if v not in skip),
        "daily": ",".join(v for v in DAILY if v not in skip),
    }
    # 实测经系统代理的 TLS 握手曾卡住远超 httpx 的 30 秒超时，再加一道总时限
    async with asyncio.timeout(60):
        r = await client.get(ARCHIVE_URL if archive else FORECAST_URL, params=params)
    # 历史接口对尚未入库的日期可能直接回 400，按"没有数据"处理
    if archive and r.status_code == 400:
        return None
    r.raise_for_status()
    return r.json()


def _merge(parts: list[dict | None]) -> dict:
    out = {k: v for p in parts if p for k, v in p.items() if k not in ("hourly", "daily")}
    for section, names in (("hourly", HOURLY), ("daily", DAILY)):
        block = out[section] = {"time": [], **{n: [] for n in names}}
        for p in parts:
            src = (p or {}).get(section) or {}
            for i, t in enumerate(src.get("time", [])):
                row = {n: src[n][i] if src.get(n) else None for n in names}
                # 历史接口延迟几天，未入库的小时全是 null，直接丢掉
                if all(v is None for v in row.values()):
                    continue
                block["time"].append(t)
                for n in names:
                    block[n].append(row[n])
    return out


async def forecast(lat: float, lon: float, start: date, end: date, *, client: httpx.AsyncClient | None = None) -> dict:
    """返回 Open-Meteo 解析后的 dict；跨今天时两段拼接，超出 16 天的部分没有数据。source 标 forecast/archive/mixed。"""
    today = _today()
    last = today + timedelta(days=FORECAST_DAYS - 1)
    parts, sources = [], []
    async with _client(client) as c:
        if start < today:
            parts.append(await _fetch(c, True, lat, lon, start, min(end, today - timedelta(days=1))))
            sources.append("archive")
        if end >= today and start <= last:
            parts.append(await _fetch(c, False, lat, lon, max(start, today), min(end, last)))
            sources.append("forecast")
    data = _merge(parts)
    data["source"] = sources[0] if len(sources) == 1 else ("mixed" if sources else None)
    return data


def _utc(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


async def session_weather(conn: sqlite3.Connection, season: int, round: int, *,
                          client: httpx.AsyncClient | None = None) -> list[dict]:
    ev = conn.execute("SELECT location FROM race_events WHERE season=? AND round=?", (season, round)).fetchone()
    if ev is None:
        raise KeyError(f"赛历里没有 {season} 赛季第 {round} 站")
    if ev["location"] not in CIRCUITS:
        raise KeyError(f"CIRCUITS 没有收录赛道坐标：{ev['location']}")
    lat, lon = CIRCUITS[ev["location"]]
    rows = conn.execute("SELECT name, start_utc FROM race_sessions WHERE season=? AND round=? AND start_utc IS NOT NULL "
                        "ORDER BY idx", (season, round)).fetchall()
    sessions = [(r["name"], _utc(r["start_utc"])) for r in rows]
    if not sessions:
        return []
    days = [s.date() for _, s in sessions]
    hourly = (await forecast(lat, lon, min(days), max(days), client=client))["hourly"]
    index = {t: i for i, t in enumerate(hourly["time"])}
    today = _today()
    out = []
    for name, start in sessions:
        i = index.get(start.strftime("%Y-%m-%dT%H:00"))

        def pick(key, i=i):
            return None if i is None else hourly[key][i]

        out.append({
            "session": name,
            "start_bj": start.astimezone(BEIJING).strftime("%m-%d %H:%M"),
            "temp": pick("temperature_2m"),
            "precip_prob": pick("precipitation_probability"),
            "precip_mm": pick("precipitation"),
            "wind_kmh": pick("wind_speed_10m"),
            "humidity": pick("relative_humidity_2m"),
            "source": "archive" if start.date() < today else "forecast",
        })
    return out


_FACT_SESSIONS = {"Qualifying": "排位", "Sprint": "冲刺", "Race": "正赛"}
_FACT_FIELDS = (("temp", "气温", "1", "°C"), ("precip_prob", "降水概率", "1", "%"), ("precip_mm", "降水量", "0.1", "mm"),
                ("wind_kmh", "风速", "1", "km/h"), ("humidity", "相对湿度", "1", "%"))


def _fmt(v: float, step: str) -> str:
    # format 是银行家舍入（24.5→24），口播数字按四舍五入
    return str(Decimal(str(v)).quantize(Decimal(step), ROUND_HALF_UP))


async def add_weather_facts(conn: sqlite3.Connection, episode_id: int, season: int, round: int, *,
                            client: httpx.AsyncClient | None = None) -> list[str]:
    rows = await session_weather(conn, season, round, client=client)
    fetched = datetime.now(BEIJING).date().isoformat()
    tags = []
    with transaction(conn):
        for w in rows:
            label = _FACT_SESSIONS.get(w["session"])
            if not label:
                continue
            source = "Open-Meteo 历史数据" if w["source"] == "archive" else f"Open-Meteo 预报 {fetched} 取数"
            for key, name, step, unit in _FACT_FIELDS:
                if w[key] is not None:
                    # 值只放数字，逐字稿写"28度"或"28°C"都能对上；单位写在内容里
                    tags.append(facts._insert(conn, episode_id, "F", f"{label}开始时（北京时间 {w['start_bj']}）{name}（{unit}）",
                                              source=source, value=_fmt(w[key], step)))
    return tags
