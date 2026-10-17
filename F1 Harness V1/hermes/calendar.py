"""赛历：从 FastF1 拉官方赛程入库、比对变动，并判断某天属于比赛周/发车区/夏休/冬歇。"""
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db, glossary

log = logging.getLogger("hermes.calendar")

BEIJING = ZoneInfo("Asia/Shanghai")
SESSION_ZH = {
    "Practice 1": "一练", "Practice 2": "二练", "Practice 3": "三练",
    "Qualifying": "排位", "Sprint Qualifying": "冲刺排位", "Sprint Shootout": "冲刺排位",
    "Sprint": "冲刺赛", "Race": "正赛",
}
FORMAT_ZH = {"conventional": "常规", "sprint_qualifying": "冲刺", "sprint_shootout": "冲刺", "sprint": "冲刺"}
BRANCHES = ("资讯", "前瞻", "复盘", "科普")
_SUFFIX = re.compile(r"(大奖赛|grandprix|gp|分站|站)$")


@dataclass(frozen=True)
class Period:
    kind: str            # '比赛周' | '发车区' | '夏休' | '冬歇'
    season: int | None   # 冬歇时为刚结束的赛季
    round: int | None
    name_en: str | None


def _today() -> date:
    return datetime.now(BEIJING).date()


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _iso_utc(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _bj(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso).astimezone(BEIJING) if iso else None


def load_fastf1(season: int) -> list[dict] | None:
    import fastf1
    import pandas as pd

    logging.getLogger("fastf1").setLevel(logging.WARNING)
    config.FASTF1_CACHE.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(config.FASTF1_CACHE))
    try:
        df = fastf1.get_event_schedule(season, include_testing=False)
    except Exception as e:  # 未公布的赛季会抛各种异常，一律视为没有数据
        log.info("FastF1 %s 赛季无数据：%s", season, e)
        return None
    if df is None or df.empty:
        return None
    out = []
    for _, r in df.iterrows():
        if pd.isna(r["RoundNumber"]) or int(r["RoundNumber"]) <= 0 or pd.isna(r["EventDate"]):
            continue
        sessions = []
        for i in range(1, 6):
            name = r.get(f"Session{i}")
            if not isinstance(name, str) or name in ("", "None"):
                continue
            ts = r.get(f"Session{i}DateUtc")
            start = None if ts is None or pd.isna(ts) else ts.to_pydatetime().replace(tzinfo=timezone.utc)
            sessions.append({"idx": i, "name": name, "start_utc": start})
        out.append({
            "round": int(r["RoundNumber"]),
            "name_en": str(r["EventName"]),
            "official_name": str(r["OfficialEventName"]) if not pd.isna(r["OfficialEventName"]) else None,
            "country": str(r["Country"]) if not pd.isna(r["Country"]) else None,
            "location": str(r["Location"]) if not pd.isna(r["Location"]) else None,
            "event_date": r["EventDate"].date(),
            "format": str(r["EventFormat"]),
            "sessions": sessions,
        })
    return out or None


def _normalize(e: dict) -> dict:
    """把 loader 结果转成与库中一致的字符串形式，便于比对。"""
    d = e["event_date"]
    return {
        "round": int(e["round"]), "name_en": e["name_en"], "official_name": e.get("official_name"),
        "country": e.get("country"), "location": e.get("location"),
        "event_date": d.isoformat() if isinstance(d, date) else str(d), "format": e["format"],
        "sessions": [{"idx": s["idx"], "name": s["name"],
                      "start_utc": s["start_utc"] if isinstance(s["start_utc"], str | None) else _iso_utc(s["start_utc"])}
                     for s in e["sessions"]],
    }


def _name(conn, e: dict) -> str:
    return glossary.zh(conn, "大奖赛", e["name_en"]) or e["name_en"]


def _fmt(iso: str | None) -> str:
    return f"{_bj(iso):%m月%d日 %H:%M}" if iso else "待定"


def _md(d: str) -> str:
    return f"{date.fromisoformat(d):%m月%d日}"


def _session(name: str) -> str:
    return SESSION_ZH.get(name, name)


def _diff(conn, season: int, old: list[dict], new: list[dict]) -> list[tuple[int, str]]:
    # 先按英文站名配对，轮次重排时不会误报成整季改名；剩下的再按轮次配对
    left = {e["name_en"]: e for e in old}
    pairs, added = [], []
    for n in new:
        o = left.pop(n["name_en"], None)
        (pairs.append((o, n)) if o else added.append(n))
    by_round = {e["round"]: e for e in left.values()}
    for n in list(added):
        if o := by_round.pop(n["round"], None):
            pairs.append((o, n))
            added.remove(n)

    out = []
    for o, n in sorted(pairs, key=lambda p: p[1]["round"]):
        items = []
        if o["round"] != n["round"]:
            items.append(f"轮次 第{o['round']}站 → 第{n['round']}站")
        if o["name_en"] != n["name_en"]:
            items.append(f"站名 {_name(conn, o)} → {_name(conn, n)}")
        if o["location"] != n["location"]:
            items.append(f"举办地 {o['location']} → {n['location']}")
        if o["event_date"] != n["event_date"]:
            items.append(f"正赛日期 {_md(o['event_date'])} → {_md(n['event_date'])}")
        if o["format"] != n["format"]:
            items.append(f"赛制 {FORMAT_ZH.get(o['format'], o['format'])} → {FORMAT_ZH.get(n['format'], n['format'])}")
        os_ = {s["name"]: s["start_utc"] for s in o["sessions"]}
        ns = {s["name"]: s["start_utc"] for s in n["sessions"]}
        for name, t in ns.items():
            if name not in os_:
                items.append(f"新增{_session(name)} {_fmt(t)}（北京时间）")
            elif os_[name] != t:
                items.append(f"{_session(name)}时间 {_fmt(os_[name])} → {_fmt(t)}（北京时间）")
        items += [f"取消{_session(name)}" for name in os_ if name not in ns]
        out += [(n["round"], f"{season} 第{n['round']}站 {_name(conn, n)}：{i}") for i in items]
    out += [(n["round"], f"{season} 新增第{n['round']}站 {_name(conn, n)}（正赛 {_md(n['event_date'])}）") for n in added]
    out += [(o["round"], f"{season} 取消第{o['round']}站 {_name(conn, o)}") for o in sorted(by_round.values(), key=lambda e: e["round"])]
    return out


def _suspicious(old: list[dict], new: list[dict]) -> bool:
    untimed = lambda evs: sum(1 for e in evs for s in e["sessions"] if not s["start_utc"])  # noqa: E731
    return len(new) < len(old) * 0.7 or untimed(new) > untimed(old) + 10


def refresh(conn, seasons: Iterable[int] | None = None, *,
            loader: Callable[[int], list[dict] | None] = load_fastf1) -> list[str]:
    if seasons is None:
        y = _today().year
        seasons = (y, y + 1)
    changes: list[str] = []
    for season in seasons:
        data = loader(season)
        if not data:
            continue
        new = sorted((_normalize(e) for e in data), key=lambda e: e["round"])
        old = events(conn, season)
        if old and _suspicious(old, new):
            # FastF1 主数据源失败时会降级到不完整的备用源，不能拿它覆盖已有赛历
            log.warning("%s 赛季新数据不完整（%d 站），保留原赛历", season, len(new))
            continue
        diffs = _diff(conn, season, old, new) if old else []
        ts = db.now()
        with db.transaction(conn):
            conn.execute("DELETE FROM race_events WHERE season=? AND round NOT IN (SELECT value FROM json_each(?))",
                         (season, json.dumps([e["round"] for e in new])))
            for e in new:
                conn.execute(
                    """INSERT INTO race_events(season, round, name_en, official_name, country, location, event_date, format, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(season, round) DO UPDATE SET name_en=excluded.name_en, official_name=excluded.official_name,
                         country=excluded.country, location=excluded.location, event_date=excluded.event_date,
                         format=excluded.format, updated_at=excluded.updated_at""",
                    (season, e["round"], e["name_en"], e["official_name"], e["country"], e["location"],
                     e["event_date"], e["format"], ts))
                conn.execute("DELETE FROM race_sessions WHERE season=? AND round=?", (season, e["round"]))
                conn.executemany("INSERT INTO race_sessions(season, round, idx, name, start_utc) VALUES (?,?,?,?,?)",
                                 [(season, e["round"], s["idx"], s["name"], s["start_utc"]) for s in e["sessions"]])
            conn.executemany("INSERT INTO calendar_changes(detected_at, season, round, detail) VALUES (?,?,?,?)",
                             [(ts, season, rnd, text) for rnd, text in diffs])
        changes += [text for _, text in diffs]
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('calendar.refreshed_at', ?)", (db.now(),))
    return changes


def events(conn, season: int) -> list[dict]:
    sessions: dict[int, list] = {}
    for s in conn.execute("SELECT round, idx, name, start_utc FROM race_sessions WHERE season=? ORDER BY round, idx", (season,)):
        sessions.setdefault(s["round"], []).append({
            "idx": s["idx"], "name": s["name"], "start_utc": s["start_utc"],
            "start_bj": f"{_bj(s['start_utc']):%Y-%m-%d %H:%M}" if s["start_utc"] else None,
        })
    return [
        {"season": season, "round": r["round"], "name_en": r["name_en"],
         "name_zh": glossary.zh(conn, "大奖赛", r["name_en"]), "official_name": r["official_name"],
         "country": r["country"], "location": r["location"], "event_date": r["event_date"], "format": r["format"],
         "sessions": sessions.get(r["round"], [])}
        for r in conn.execute("SELECT * FROM race_events WHERE season=? ORDER BY round", (season,))
    ]


def _all_events(conn) -> list[dict]:
    seasons = [r[0] for r in conn.execute("SELECT DISTINCT season FROM race_events ORDER BY season")]
    return sorted((e for s in seasons for e in events(conn, s)), key=lambda e: e["event_date"])


def period(conn, day: date) -> Period:
    week = _monday(day)
    prev = nxt = None
    for r in conn.execute("SELECT season, round, name_en, event_date FROM race_events ORDER BY event_date, season, round"):
        w = _monday(date.fromisoformat(r["event_date"]))
        if w == week:
            return Period("比赛周", r["season"], r["round"], r["name_en"])
        if w < week:
            prev = r
        elif nxt is None:
            nxt = r
    if prev and nxt and prev["season"] == nxt["season"]:
        start = _monday(date.fromisoformat(prev["event_date"])) + timedelta(days=7)
        summer = summer_break(conn, prev["season"])
        return Period("夏休" if summer and summer[0] == start else "发车区", prev["season"], None, None)
    season = prev["season"] if prev else (nxt["season"] - 1 if nxt else None)
    return Period("冬歇", season, None, None)


SUMMER_MIN_OVERLAP = 13  # FIA Section F 第 F3.1.1a 条：7–8 月内连续 14 天停工，两站只隔 17 天时为 13 天


def summer_break(conn, season: int) -> tuple[date, date] | None:
    """夏休 = 与当年 7–8 月重叠最多、且至少能放下 FIA 夏季停工期的那段两站间空档（周一到周日）。
    官方文章只说"匈牙利站之后、荷兰站之前"，不给日期，所以按赛历推算。"""
    weeks = sorted({_monday(date.fromisoformat(r[0])) for r in
                    conn.execute("SELECT event_date FROM race_events WHERE season=?", (season,))})
    jul, aug_end = date(season, 7, 1), date(season, 8, 31)
    best, best_overlap = None, 0
    for a, b in zip(weeks, weeks[1:]):
        start, end = a + timedelta(days=7), b - timedelta(days=1)
        overlap = (min(end, aug_end) - max(start, jul)).days + 1
        if start <= end and overlap >= SUMMER_MIN_OVERLAP and overlap > best_overlap:
            best, best_overlap = (start, end), overlap
    return best


def gp_label(conn, day: date) -> str:
    p = period(conn, day)
    if p.kind != "比赛周":
        return p.kind
    return glossary.zh(conn, "大奖赛", p.name_en) or p.name_en


def is_race_day(conn, day: date) -> bool:
    lo = datetime.combine(day, time(), BEIJING)
    row = conn.execute("SELECT 1 FROM race_sessions WHERE start_utc >= ? AND start_utc < ? LIMIT 1",
                       (_iso_utc(lo), _iso_utc(lo + timedelta(days=1)))).fetchone()
    return row is not None


def _key(text: str | None) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[\s\-_'’·.,，。]", "", s)
    s = re.sub(r"^(f1|\d{4})+", "", s)
    while (t := _SUFFIX.sub("", s)) != s:
        s = t
    return s


def _long(k: str) -> bool:
    # 太短的英文片段（如 us）会误中 Austria/Australia，不参与包含匹配
    return len(k) >= (3 if k.isascii() else 2)


def find_event(conn, text: str, season: int | None = None, *, today: date | None = None) -> dict | None:
    """精确匹配优先于包含匹配；同档里取离 today（默认北京时间今天）最近的一站。"""
    q = _key(text)
    if not q:
        return None
    today = today or _today()
    pool = events(conn, season) if season is not None else _all_events(conn)
    best: list[tuple[int, int, dict]] = []
    for e in pool:
        keys = [k for k in map(_key, (e["name_zh"], e["name_en"], e["country"], e["location"],
                                      glossary.zh(conn, "赛道", e["location"] or ""))) if k]
        if q in keys:
            tier = 0
        elif any((k in q and _long(k)) or (q in k and _long(q)) for k in keys):
            tier = 1
        else:
            continue
        best.append((tier, abs((date.fromisoformat(e["event_date"]) - today).days), e))
    return min(best, key=lambda b: (b[0], b[1]))[2] if best else None


def _finished_on(e: dict) -> date:
    """最后一个有时间的场次按北京时间所在的日期；没有场次时间就用正赛当地日期。"""
    starts = [s["start_utc"] for s in e["sessions"] if s["start_utc"]]
    return _bj(max(starts)).date() if starts else date.fromisoformat(e["event_date"])


def label_for(conn, branch: str, day: date, gp_text: str | None = None) -> str:
    if branch not in BRANCHES:
        raise ValueError(f"未知分支：{branch}")
    if branch in ("资讯", "科普"):
        return gp_label(conn, day)
    ev = find_event(conn, gp_text, today=day) if gp_text else None
    if ev is None:
        evs = _all_events(conn)
        if branch == "前瞻":
            ev = next((e for e in evs if date.fromisoformat(e["event_date"]) >= day), None)
        else:
            ev = next((e for e in reversed(evs) if _finished_on(e) <= day), None)
    if ev is None:
        return gp_label(conn, day)
    return ev["name_zh"] or ev["name_en"]
