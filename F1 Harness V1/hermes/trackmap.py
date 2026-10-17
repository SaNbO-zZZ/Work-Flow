"""M6-3 赛道简图：取赛道线（2018 年后的赛道用 FastF1，其余用 OpenStreetMap），渲染立体 F1 风格透明叠层。

赛道线统一成 outline 字典，JSON 缓存在 data/track_maps/，只第一次联网：
  points   归一化点列 [[x, y], ...]：最长边 = 1，y 向下；第一个点是起终点，按行驶方向排列
  corners  [{"n": "1", "x", "y", "dx", "dy"}]：弯道编号位置和标注方向（OSM 没有，留空）
  length_m 赛道线长度（米）；source "FastF1" / "OpenStreetMap"；name_en 英文名；clockwise 是否顺时针
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import config, graphics

log = logging.getLogger(__name__)

N_POINTS = 720
UA = "F1Hermes/1.0 (local F1 video tool)"
HTTP_TIMEOUT = httpx.Timeout(60, connect=15)
HARD_TIMEOUT = 100      # 系统代理偶尔让 httpx 自己的超时失效，外层再兜一次
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter")
OSM_MAP = "https://api.openstreetmap.org/api/0.6/map"
OSM_CREDIT = "地图数据 © OpenStreetMap 贡献者"
FASTF1_CREDIT = "赛道数据：F1 官方计时（FastF1）"

# 赛历地点 → (OSM 检索名, 官方全长/公里, 是否顺时针)。全长取 F1 官网赛道页；方向只在 OSM 没有单行标记时兜底用
CIRCUITS: dict[str, tuple[str | None, float | None, bool | None]] = {
    "kuala lumpur": ("Sepang International Circuit", 5.543, True),
    "madrid": ("Madring", None, None),
    "melbourne": ("Albert Park Circuit", 5.278, True),
    "shanghai": ("Shanghai International Circuit", 5.451, True),
    "suzuka": ("Suzuka Circuit", 5.807, None),
    "sakhir": ("Bahrain International Circuit", 5.412, True),
    "jeddah": ("Jeddah Corniche Circuit", 6.174, False),
    "miami": ("Miami International Autodrome", 5.412, False),
    "montreal": ("Circuit Gilles Villeneuve", 4.361, False),
    "monaco": ("Circuit de Monaco", 3.337, True),
    "barcelona": ("Circuit de Barcelona-Catalunya", 4.657, True),
    "spielberg": ("Red Bull Ring", 4.318, True),
    "silverstone": ("Silverstone Circuit", 5.891, True),
    "spa francorchamps": ("Circuit de Spa-Francorchamps", 7.004, True),
    "budapest": ("Hungaroring", 4.381, True),
    "zandvoort": ("Circuit Zandvoort", 4.259, True),
    "monza": ("Autodromo Nazionale Monza", 5.793, True),
    "imola": ("Autodromo Enzo e Dino Ferrari", 4.909, False),
    "baku": ("Baku City Circuit", 6.003, False),
    "marina bay": ("Marina Bay Street Circuit", 4.940, False),
    "austin": ("Circuit of the Americas", 5.513, False),
    "mexico city": ("Autódromo Hermanos Rodríguez", 4.304, True),
    "sao paulo": ("Autódromo José Carlos Pace", 4.309, False),
    "las vegas": ("Las Vegas Strip Circuit", 6.201, False),
    "lusail": ("Lusail International Circuit", 5.419, True),
    "yas island": ("Yas Marina Circuit", 5.281, False),
}
ALIASES = {"sepang": "kuala lumpur", "miami gardens": "miami", "monte carlo": "monaco", "yas marina": "yas island",
           "abu dhabi": "yas island", "spa": "spa francorchamps", "singapore": "marina bay"}


def _norm(text: str | None) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return ALIASES.get(s, s)


def _slug(text: str) -> str:
    return _norm(text).replace(" ", "_") or "unknown"


def circuit(location: str | None) -> tuple[str | None, float | None, bool | None]:
    return CIRCUITS.get(_norm(location), (None, None, None))


def official_km(location: str | None) -> float | None:
    return circuit(location)[1]


def cache_dir() -> Path:
    return config.DB_PATH.parent / "track_maps"


def _cached(name: str) -> dict | None:
    p = cache_dir() / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _store(name: str, outline: dict) -> dict:
    p = cache_dir() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
    return outline


# ---------- 联网 ----------

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="trackmap-http")


def _brief(text: str) -> str:
    t = re.sub(r"<[^>]+>", " ", text)
    return " ".join(t.split())[:160]


def _fetch(method: str, url: str, **kw) -> bytes:
    def call():
        with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": UA}, follow_redirects=True) as c:
            r = c.request(method, url, **kw)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {_brief(r.text)}")
        return r.content
    try:
        return _POOL.submit(call).result(timeout=HARD_TIMEOUT)
    except concurrent.futures.TimeoutError:
        raise RuntimeError(f"{HARD_TIMEOUT} 秒没有响应") from None
    except httpx.HTTPError as e:
        raise RuntimeError(f"{type(e).__name__}：{e}") from e


def geocode(name: str) -> tuple[float, float]:
    try:
        data = json.loads(_fetch("GET", NOMINATIM, params={"q": name, "format": "jsonv2", "limit": 1}))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"OpenStreetMap 地名检索（Nominatim）查「{name}」失败：{e}") from e
    if not data:
        raise RuntimeError(f"OpenStreetMap 地名检索找不到「{name}」")
    return float(data[0]["lat"]), float(data[0]["lon"])


def _parse_overpass(data: dict) -> tuple[list[dict], dict]:
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in data.get("elements", []) if e["type"] == "node"}
    ways = [{"id": e["id"], "nodes": e["nodes"], "tags": e.get("tags", {})}
            for e in data.get("elements", []) if e["type"] == "way"]
    return ways, nodes


def _parse_osm_xml(raw: bytes) -> tuple[list[dict], dict]:
    root = ET.fromstring(raw)
    nodes = {int(n.get("id")): (float(n.get("lat")), float(n.get("lon"))) for n in root.iter("node")}
    ways = []
    for w in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in w.iter("tag")}
        if tags.get("highway") == "raceway":
            ways.append({"id": int(w.get("id")), "nodes": [int(n.get("ref")) for n in w.iter("nd")], "tags": tags})
    return ways, nodes


def overpass_query(lat: float, lon: float, radius: int) -> str:
    return (f'[out:json][timeout:60];way["highway"="raceway"](around:{radius},{lat:.6f},{lon:.6f});'
            "out body;>;out skel qt;")


def _osm_ways(lat: float, lon: float, radius: int) -> tuple[list[dict], dict]:
    errors = []
    q = overpass_query(lat, lon, radius)
    for _ in range(2):
        for url in OVERPASS:
            try:
                return _parse_overpass(json.loads(_fetch("POST", url, data={"data": q})))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{httpx.URL(url).host}：{e}")
    # Overpass 都忙时兜底：OSM 主站 API 按矩形取全部数据，本地筛 raceway
    dlat = radius / 111_320
    dlon = dlat / max(math.cos(math.radians(lat)), 0.2)
    try:
        raw = _fetch("GET", OSM_MAP, params={"bbox": f"{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}"})
        return _parse_osm_xml(raw)
    except Exception as e:  # noqa: BLE001
        errors.append(f"api.openstreetmap.org：{e}")
    raise RuntimeError("OpenStreetMap 赛道数据下载失败：" + "；".join(errors[-4:]))


# ---------- OSM 拼环线 ----------

_PIT = re.compile(r"\bpit|boxen", re.I)
_OTHER_SPORT = re.compile(r"kart|motocross|quad|drag|cycl|bmx|horse|rally|dirt", re.I)


def _way_kind(tags: dict) -> str | None:
    name = " ".join(str(tags.get(k, "")) for k in ("name", "name:en"))
    if (tags.get("raceway") in ("pit_lane", "pitlane", "pits") or tags.get("service") == "pit_lane"
            or tags.get("pitlane") == "yes" or _PIT.search(name)):
        return "pit"
    if (_OTHER_SPORT.search(tags.get("sport", "")) or tags.get("raceway") in ("karting", "drag_strip")
            or tags.get("area") == "yes" or tags.get("surface") in ("unpaved", "dirt", "gravel", "grass", "ground", "sand")):
        return None
    return "track"


def _meters(nodes: dict) -> callable:
    lat0 = sum(v[0] for v in nodes.values()) / len(nodes)
    lon0 = sum(v[1] for v in nodes.values()) / len(nodes)
    kx = 111_320 * math.cos(math.radians(lat0))
    return lambda n: ((nodes[n][1] - lon0) * kx, (nodes[n][0] - lat0) * 110_574)


def _cycles(edges: list[dict], limit: int = 20000) -> list[list[int]]:
    inc: dict[int, list[tuple[int, int]]] = defaultdict(list)
    found: dict[frozenset, list[int]] = {}
    for i, e in enumerate(edges):
        if e["u"] == e["v"]:
            found[frozenset([i])] = [i]
            continue
        inc[e["u"]].append((i, e["v"]))
        inc[e["v"]].append((i, e["u"]))

    def dfs(start, cur, used, seen):
        if len(found) >= limit:
            return
        for i, other in inc[cur]:
            if i in used:
                continue
            if other == start:
                found.setdefault(frozenset(used + [i]), used + [i])
            elif other > start and other not in seen:
                dfs(start, other, used + [i], seen | {other})

    for s in sorted(inc):
        dfs(s, s, [], {s})
    return list(found.values())


def build_loop(ways: list[dict], nodes: dict, *, expected_m: float | None = None,
               clockwise: bool | None = None) -> dict:
    """raceway 各段 → 去掉维修区/卡丁车等 → 收缩成路口图 → 枚举闭合环 → 取最接近官方全长（不知道就取最长）的一个。"""
    track = [w for w in ways if _way_kind(w["tags"]) == "track" and all(n in nodes for n in w["nodes"])]
    pits = [w for w in ways if _way_kind(w["tags"]) == "pit" and all(n in nodes for n in w["nodes"])]
    if not track:
        raise RuntimeError("附近没有 highway=raceway 的赛道线")
    xy = _meters({n: nodes[n] for w in track + pits for n in w["nodes"]})
    adj: dict[int, list[tuple[int, float, int]]] = defaultdict(list)
    for w in track:
        ow = str(w["tags"].get("oneway", "")).lower()
        sign = 1 if ow in ("yes", "true", "1") else -1 if ow == "-1" else 0
        for a, b in zip(w["nodes"], w["nodes"][1:]):
            if a != b:
                d = math.dist(xy(a), xy(b))
                adj[a].append((b, d, sign))
                adj[b].append((a, d, -sign))
    junction = {n for n, nb in adj.items() if len(nb) != 2}
    edges, seen_paths, visited = [], set(), set()

    def walk(u, first):
        path, length, signed = [u], 0.0, 0.0
        prev, (cur, d, s) = u, first
        while True:
            path.append(cur)
            length += d
            signed += d * s
            visited.add(cur)
            if cur in junction or cur == u:
                return path, length, signed
            nxt = [x for x in adj[cur] if x[0] != prev] or adj[cur][:1]
            prev, (cur, d, s) = cur, nxt[0]

    def add(path, length, signed):
        key = tuple(path) if (path[0], path[1]) <= (path[-1], path[-2]) else tuple(reversed(path))
        if key not in seen_paths:
            seen_paths.add(key)
            edges.append({"u": path[0], "v": path[-1], "path": path, "len": length, "sign": signed})

    for u in junction:
        visited.add(u)
        for first in adj[u]:
            add(*walk(u, first))
    for n in list(adj):           # 没有路口的独立闭环（整条 way 自成一圈）
        if n not in visited:
            visited.add(n)
            add(*walk(n, adj[n][0]))

    cycles = _cycles(edges)
    if not cycles:
        raise RuntimeError("赛道线拼不成闭合环线")
    total = lambda c: sum(edges[i]["len"] for i in c)  # noqa: E731
    best = (min(cycles, key=lambda c: abs(total(c) - expected_m)) if expected_m
            else max(cycles, key=total))

    # 沿环把各段首尾接起来
    left = list(best)
    e0 = edges[left.pop(0)]
    seq, signed, cur = list(e0["path"]), e0["sign"], e0["v"]
    while left:
        for k, i in enumerate(left):
            e = edges[i]
            if e["u"] == cur:
                seq += e["path"][1:]
                signed += e["sign"]
                cur = e["v"]
            elif e["v"] == cur:
                seq += list(reversed(e["path"]))[1:]
                signed -= e["sign"]
                cur = e["u"]
            else:
                continue
            left.pop(k)
            break
        else:
            raise RuntimeError("赛道线拼接中断")
    pts = np.array([xy(n) for n in seq[:-1]], float)
    cw = _signed_area(pts) < 0
    if signed < 0 or (signed == 0 and cw != (True if clockwise is None else clockwise)):
        pts = pts[::-1]
    pit_pts = [np.array([xy(n) for n in w["nodes"]], float) for w in pits]
    start = _start_index(pts, pit_pts)
    pts = np.roll(pts, -start, axis=0)
    return {"xy": pts, "length_m": _length(pts), "nodes": len(seq) - 1, "direction_from": "oneway" if signed else
            ("table" if clockwise is not None else "default"), "start_from": "pit" if pit_pts else "straight"}


def _signed_area(p: np.ndarray) -> float:
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _length(p: np.ndarray) -> float:
    return float(np.sum(np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1)))


def _resample(p: np.ndarray, n: int) -> np.ndarray:
    closed = np.vstack([p, p[:1]])
    seg = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    t = np.linspace(0, s[-1], n, endpoint=False)
    return np.stack([np.interp(t, s, closed[:, 0]), np.interp(t, s, closed[:, 1])], axis=1)


def _densify(p: np.ndarray, step: float) -> np.ndarray:
    if len(p) < 2:
        return p
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    t = np.linspace(0, s[-1], max(2, int(s[-1] / step) + 1))
    return np.stack([np.interp(t, s, p[:, 0]), np.interp(t, s, p[:, 1])], axis=1)


def _start_index(pts: np.ndarray, pit_pts: list[np.ndarray]) -> int:
    """起终点：赛道上与维修区通道并行（80 米内）最长的一段取中点；没有维修区数据就取最长直道中点。"""
    dense = _resample(pts, N_POINTS)
    if pit_pts:
        pit = np.vstack([_densify(p, 10) for p in pit_pts])
        near = np.min(np.linalg.norm(dense[:, None, :] - pit[None, :, :], axis=2), axis=1) < 80
    else:
        d = np.roll(dense, -2, axis=0) - np.roll(dense, 2, axis=0)
        ang = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
        near = np.abs(np.roll(ang, -3) - np.roll(ang, 3)) < math.radians(6)
    if not near.any() or near.all():
        k = 0
    else:
        shift = int(np.argmin(near))
        run, best, cur0, cur = (0, 0), 0, None, 0
        for j in range(N_POINTS + 1):
            idx = (shift + j) % N_POINTS
            if j < N_POINTS and near[idx]:
                cur0 = j if cur0 is None else cur0
                cur += 1
            elif cur0 is not None:
                if cur > best:
                    best, run = cur, (cur0, cur)
                cur0, cur = None, 0
        k = (shift + run[0] + run[1] // 2) % N_POINTS
    target = dense[k]
    return int(np.argmin(np.linalg.norm(pts - target, axis=1)))


def _rotate(p: np.ndarray, deg: float) -> np.ndarray:
    a = math.radians(deg)
    m = np.array([[math.cos(a), math.sin(a)], [-math.sin(a), math.cos(a)]])
    return p @ m


def _finish(xy: np.ndarray, *, rotation: float, corners: list[dict] | None = None) -> dict:
    """米制坐标（y 向上）→ 按 rotation（与 FastF1 的 CircuitInfo.rotation 同义）转好 → 归一化、y 向下。"""
    length = _length(xy)
    pts = _resample(xy, N_POINTS)
    pts = (np.roll(pts, 1, axis=0) + pts + np.roll(pts, -1, axis=0)) / 3
    pts[0] = xy[0]
    cw = _signed_area(xy) < 0
    r = _rotate(pts, rotation) * [1, -1]
    lo = r.min(axis=0)
    scale = float((r.max(axis=0) - lo).max())
    norm = (r - lo) / scale
    out_corners = []
    for c in corners or []:
        p = (_rotate(np.array([[c["x"], c["y"]]]), rotation) * [1, -1] - lo) / scale
        a = math.radians(c.get("angle", 0.0))
        dv = _rotate(np.array([[math.cos(a), math.sin(a)]]), rotation) * [1, -1]
        out_corners.append({"n": c["n"], "x": round(float(p[0, 0]), 5), "y": round(float(p[0, 1]), 5),
                            "dx": round(float(dv[0, 0]), 4), "dy": round(float(dv[0, 1]), 4)})
    ext = r.max(axis=0) - lo
    return {"points": [[round(float(x), 5), round(float(y), 5)] for x, y in norm],
            "corners": out_corners, "length_m": round(length, 1), "clockwise": cw,
            "aspect": round(float(ext[0] / ext[1]), 4), "rotation": rotation}


def _wide_rotation(xy: np.ndarray) -> float:
    """OSM 没有官方朝向：取让赛道最"扁"的角度，放进横版面板最省地方。"""
    return max(range(0, 180, 3), key=lambda d: (lambda e: e[0] / e[1])(np.ptp(_rotate(xy, d), axis=0)))


def osm_outline(name: str, lat: float | None = None, lon: float | None = None, *, expected_km: float | None = None,
                clockwise: bool | None = None, radius: int = 3500) -> dict:
    key = f"osm_{_slug(name)}.json"
    if (hit := _cached(key)) is not None:
        return hit
    if lat is None or lon is None:
        lat, lon = geocode(name)
    ways, nodes = _osm_ways(lat, lon, radius)
    try:
        loop = build_loop(ways, nodes, expected_m=expected_km * 1000 if expected_km else None, clockwise=clockwise)
    except RuntimeError as e:
        raise RuntimeError(f"OpenStreetMap「{name}」：{e}") from e
    out = _finish(loop["xy"], rotation=_wide_rotation(loop["xy"]))
    out.update(source="OpenStreetMap", name_en=name, credit=OSM_CREDIT, lat=lat, lon=lon,
               osm_nodes=loop["nodes"], direction_from=loop["direction_from"], start_from=loop["start_from"])
    if expected_km:
        out["expected_m"] = expected_km * 1000
    return _store(key, out)


# ---------- FastF1 ----------

def _fastf1():
    import fastf1

    config.FASTF1_CACHE.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(config.FASTF1_CACHE))
    logging.getLogger("fastf1").setLevel(logging.ERROR)
    return fastf1


def _ff1_lap(season: int, rnd: int, session: str):
    fastf1 = _fastf1()
    last: Exception | None = None
    for offline in (True, False):
        fastf1.Cache.offline_mode(offline)
        try:
            s = fastf1.get_session(season, rnd, session)
            s.load(laps=True, telemetry=True, weather=False, messages=False)
            lap = s.laps.pick_fastest()
            if lap is None or lap.empty:
                raise ValueError("没有最快圈")
            pos = lap.get_pos_data()
            if len(pos) < 50:
                raise ValueError("最快圈没有位置数据")
            return s, pos, s.get_circuit_info()
        except Exception as e:  # noqa: BLE001
            last = e
        finally:
            fastf1.Cache.offline_mode(False)
    raise RuntimeError(f"FastF1 读取 {season} 第 {rnd} 站 {session} 位置数据失败：{last}") from last


def fastf1_outline(season: int, rnd: int, session: str = "Q") -> dict:
    key = f"fastf1_{season}_{rnd:02d}_{session}.json"
    if (hit := _cached(key)) is not None:
        return hit
    s, pos, ci = _ff1_lap(season, rnd, session)
    xy = pos[["X", "Y"]].to_numpy(float) / 10           # FastF1 位置单位是 0.1 米
    keep = np.concatenate([[True], np.linalg.norm(np.diff(xy, axis=0), axis=1) > 0.5])
    xy = xy[keep]
    corners = []
    if ci is not None and ci.corners is not None:
        for r in ci.corners.itertuples():
            letter = r.Letter if isinstance(r.Letter, str) else ""
            corners.append({"n": f"{int(r.Number)}{letter}", "x": float(r.X) / 10, "y": float(r.Y) / 10,
                            "angle": float(r.Angle)})
    out = _finish(xy, rotation=float(ci.rotation) if ci is not None else 0.0, corners=corners)
    ev = s.event
    out.update(source="FastF1", name_en=str(ev["Location"]), credit=FASTF1_CREDIT,
               session=f"{season} {ev['EventName']} {session}")
    return _store(key, out)


def _schedule(season: int):
    fastf1 = _fastf1()
    last: Exception | None = None
    for offline in (True, False):
        fastf1.Cache.offline_mode(offline)
        try:
            return fastf1.get_event_schedule(season, include_testing=False)
        except Exception as e:  # noqa: BLE001
            last = e
        finally:
            fastf1.Cache.offline_mode(False)
    raise RuntimeError(f"FastF1 赛历 {season} 读取失败：{last}") from last


def fastf1_event(location: str, before: date) -> tuple[int, int] | None:
    """同一地点、比 before 早、2018 年以后最近的一站（2018 年起 FastF1 才有位置数据）。"""
    key = _norm(location)
    for year in range(before.year, 2017, -1):
        try:
            sch = _schedule(year)
        except RuntimeError as e:
            log.warning("%s", e)
            continue
        hits = [r for r in sch.itertuples() if _norm(r.Location) == key and r.RoundNumber > 0
                and r.EventDate.date() < before]
        if hits:
            r = max(hits, key=lambda r: r.EventDate)
            return year, int(r.RoundNumber)
    return None


def outline_for_event(ev: dict) -> dict:
    """赛历里的一站 → 赛道线：FastF1 有往年数据就用，否则 OSM。结果按地点缓存。"""
    loc = ev.get("location") or ev.get("name_en") or ""
    key = f"event_{_slug(loc)}.json"
    if (hit := _cached(key)) is not None:
        return hit
    errors = []
    out = None
    try:
        hit = fastf1_event(loc, date.fromisoformat(ev["event_date"]))
        if hit:
            out = fastf1_outline(*hit)
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
    if out is None:
        name, km, cw = circuit(loc)
        try:
            out = osm_outline(name or f"{loc} circuit", expected_km=km, clockwise=cw)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{loc} 的赛道简图取不到：" + "；".join(errors + [str(e)])) from e
    return _store(key, out)


# ---------- 渲染 ----------

BASE_W, BASE_H = 728, 476          # 面板版式的设计尺寸，整体按 ZOOM 放大
ZOOM = 1.62                         # 底栏文字刚好在字幕上方；左上水印、右下出处都不碰
PANEL_W, PANEL_H = round(BASE_W * ZOOM), round(BASE_H * ZOOM)
PANEL_BOX = ((1920 - PANEL_W) // 2, (1080 - PANEL_H) // 2,
             (1920 - PANEL_W) // 2 + PANEL_W, (1080 - PANEL_H) // 2 + PANEL_H)   # 1920×1080 基准，画面正中
SS = 2                              # 超采样倍数
TILT = math.radians(40)             # 赛道平面向后倒的角度
PERSP = 3.4                         # 透视强度：越小近大远小越明显
TRACK_W = 10
WALL_H = 0.05
BG_TOP, BG_BOTTOM = (34, 35, 46), (12, 12, 18)
WALL_DARK, WALL_LIGHT = (72, 2, 2), (168, 14, 8)
TOP = (246, 246, 250)
RED = graphics.F1_RED
GREY = (150, 154, 166)


def _v(a: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(a)
    return a / n if n else a


def panel_base(s: float, grid: Image.Image | None = None) -> Image.Image:
    """面板底板（渐变底、左上红条、圆角、细边框），s = 设计单位到像素的倍数。赛程卡、轮胎图共用。"""
    W, H = round(BASE_W * s), round(BASE_H * s)
    g = np.linspace(0, 1, H)[:, None, None]
    bg = (np.array(BG_TOP) * (1 - g) + np.array(BG_BOTTOM) * g).repeat(W, axis=1)
    img = Image.fromarray(np.dstack([bg, np.full((H, W, 1), 232)]).astype(np.uint8), "RGBA")
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), radius=16 * s, fill=255)
    if grid is not None:
        img.alpha_composite(grid)
    d = ImageDraw.Draw(img)
    d.polygon([(0, 0), (W * 0.36, 0), (W * 0.36 - 6 * s, 6 * s), (0, 6 * s)], fill=RED + (255,))
    img.putalpha(Image.fromarray(np.minimum(np.asarray(img.getchannel("A")), np.asarray(mask))))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=16 * s, outline=(255, 255, 255, 34), width=max(1, round(s)))
    return img


def place_panel(panel: Image.Image, size: tuple[int, int], *, alpha: float = 1.0, lift: float = 0.0) -> Image.Image:
    """超采样面板 → 缩到 PANEL_BOX 大小，贴进整帧透明画布。"""
    k = size[0] / 1920
    small = panel.resize((round(PANEL_W * k), round(PANEL_H * k)), Image.LANCZOS)
    if alpha < 1:
        small.putalpha(small.getchannel("A").point(lambda v: int(v * alpha)))
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    canvas.alpha_composite(small, (round(PANEL_BOX[0] * k), round((PANEL_BOX[1] + lift * ZOOM) * k)))
    return canvas


class _Scene:
    def __init__(self, outline: dict, *, title: str, subtitle: str | None, length_km: float | None,
                 credit: str | None, size: tuple[int, int]):
        self.k = size[0] / 1920
        self.pw, self.ph = BASE_W, BASE_H
        self.s = SS * self.k * ZOOM
        self.size = size
        self.outline = outline
        pts = np.asarray(outline["points"], float)
        self.n = len(pts)
        self.top, self.scale = self._project(pts)
        self.base = self.top + np.stack([np.zeros(self.n), WALL_H * self.scale], axis=1) * self.fit
        self.corners = self._corner_badges()
        self.panel = self._panel(title, subtitle, length_km, credit)

    def _tilt(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        x, z = p[:, 0] - self.c[0], p[:, 1] - self.c[1]
        sc = PERSP / (PERSP - z * math.sin(TILT))
        return np.stack([x * sc, z * math.cos(TILT) * sc], axis=1), sc

    def _project(self, pts: np.ndarray):
        self.c = (pts.min(axis=0) + pts.max(axis=0)) / 2
        t, sc = self._tilt(pts)
        b = t + np.stack([np.zeros(len(t)), WALL_H * sc], axis=1)
        allp = [t, b]
        cs = self.outline.get("corners") or []
        if cs:   # 弯道号牌也要落在面板里
            allp.append(self._tilt(np.array([[c["x"] + c["dx"] * 0.1, c["y"] + c["dy"] * 0.1] for c in cs]))[0])
        allp = np.vstack(allp)
        lo, hi = allp.min(axis=0), allp.max(axis=0)
        s = self.s
        m = 34
        ax1, ay1, ax2, ay2 = 24 + m, 132 + m * 0.6, self.pw - 24 - m, self.ph - 52 - m * 0.4
        self.fit = min((ax2 - ax1) / (hi - lo)[0], (ay2 - ay1) / (hi - lo)[1]) * s
        off = np.array([(ax1 + ax2) / 2, (ay1 + ay2) / 2]) * s - (lo + hi) / 2 * self.fit
        self.off = off
        return t * self.fit + off, sc

    def _to_screen(self, p: np.ndarray) -> np.ndarray:
        t, _ = self._tilt(p)
        return t * self.fit + self.off

    def _corner_badges(self) -> list[tuple[str, float, float, int]]:
        out: list[tuple[str, float, float, int]] = []
        r = 11 * self.s
        pts = np.asarray(self.outline["points"], float)
        for c in self.outline.get("corners") or []:
            near = int(np.argmin(np.linalg.norm(pts - [c["x"], c["y"]], axis=1)))
            for dist in (0.05, 0.075, 0.1):
                q = self._to_screen(np.array([[c["x"] + c["dx"] * dist, c["y"] + c["dy"] * dist]]))[0]
                if all(math.dist(q, (x, y)) > 2.1 * r for _, x, y, _ in out):
                    break
            out.append((c["n"], float(q[0]), float(q[1]), near))
        return out

    # 面板底板 + 文字（动画里不变）
    def _panel(self, title, subtitle, length_km, credit) -> Image.Image:
        s, W, H = self.s, round(self.pw * self.s), round(self.ph * self.s)
        img = panel_base(s, self._grid(W, H))
        d = ImageDraw.Draw(img)

        f = graphics.font
        x, y = 28 * s, 24 * s
        if subtitle:
            d.text((x, y), subtitle, font=f("Bold", round(18 * s)), fill=RED + (255,), anchor="la")
            y += 28 * s
        d.text((x, y), title, font=f("Heavy", round(42 * s)), fill=(255, 255, 255, 255), anchor="la")
        y += 58 * s
        if self.outline.get("name_en"):
            d.text((x, y), self.outline["name_en"].upper(), font=f("Medium", round(15 * s)), fill=GREY + (255,),
                   anchor="la")

        km = length_km if length_km else self.outline.get("length_m", 0) / 1000
        val = f"{km:.3f}" if length_km else f"约 {km:.1f}"
        stats = [("赛道长度", val, "公里")]
        n_corners = len({re.sub(r"\D", "", c["n"]) for c in self.outline.get("corners") or []})
        if n_corners:
            stats.insert(0, ("弯道", str(n_corners), "个"))
        rx = W - 28 * s
        for label, value, unit in reversed(stats):
            fu, fv, fl = f("Medium", round(17 * s)), f("Heavy", round(40 * s)), f("Medium", round(15 * s))
            uw, vw = fu.getlength(unit), fv.getlength(value)
            d.text((rx, 34 * s), label, font=fl, fill=GREY + (255,), anchor="ra")
            d.text((rx, 102 * s), unit, font=fu, fill=GREY + (255,), anchor="rs")
            d.text((rx - uw - 6 * s, 102 * s), value, font=fv, fill=(255, 255, 255, 255), anchor="rs")
            rx -= max(uw + vw + 6 * s, fl.getlength(label)) + 30 * s

        credit = credit or self.outline.get("credit") or ""
        fy = H - 22 * s
        d.text((28 * s, fy), credit, font=f("Regular", round(14 * s)), fill=GREY + (220,), anchor="ls")
        fl = f("Medium", round(14 * s))
        lx = W - 28 * s
        d.text((lx, fy), "行驶方向", font=fl, fill=GREY + (230,), anchor="rs")
        lx -= fl.getlength("行驶方向") + 8 * s
        self._arrow(d, np.array([lx - 11 * s, fy - 6 * s]), np.array([1.0, 0.0]), 22 * s, outline=False)
        lx -= 22 * s + 20 * s
        d.text((lx, fy), "起终点", font=fl, fill=GREY + (230,), anchor="rs")
        lx -= fl.getlength("起终点") + 8 * s
        self._checker(d, np.array([lx - 12 * s, fy - 6 * s]), np.array([0.0, 1.0]), 24 * s, 9 * s, cols=6, rows=2)
        return img

    def _grid(self, W: int, H: int) -> Image.Image:
        """赛道平面上的透视网格，往上、往两边渐隐，不进文字区。"""
        s = self.s
        pts = np.asarray(self.outline["points"], float)
        lo, hi = pts.min(axis=0) - 0.3, pts.max(axis=0) + 0.3
        step = 0.1
        layer = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(layer)
        for gx in np.arange(math.floor(lo[0] / step) * step, hi[0] + 1e-6, step):
            p = self._to_screen(np.array([[gx, lo[1]], [gx, hi[1]]]))
            d.line([tuple(p[0]), tuple(p[1])], fill=255, width=max(1, round(s)))
        for gy in np.arange(math.floor(lo[1] / step) * step, hi[1] + 1e-6, step):
            p = self._to_screen(np.array([[lo[0], gy], [hi[0], gy]]))
            d.line([tuple(p[0]), tuple(p[1])], fill=255, width=max(1, round(s)))
        y = np.arange(H)[:, None]
        x = np.arange(W)[None, :]
        fade = (np.clip((y - 130 * s) / (90 * s), 0, 1) * np.clip(x / (90 * s), 0, 1) * np.clip((W - x) / (90 * s), 0, 1)
                * np.clip((H - 40 * s - y) / (60 * s), 0, 1))
        a = (np.asarray(layer, float) * fade * 0.075).astype(np.uint8)
        return Image.merge("RGBA", [Image.new("L", (W, H), 255)] * 3 + [Image.fromarray(a, "L")])

    def _checker(self, d, c, t, length, thick, *, cols=5, rows=2):
        t = _v(t)
        nrm = np.array([-t[1], t[0]])
        for i in range(rows):
            for j in range(cols):
                a = c + nrm * (-length / 2 + j * length / cols) + t * (-thick / 2 + i * thick / rows)
                q = [a, a + nrm * length / cols, a + nrm * length / cols + t * thick / rows, a + t * thick / rows]
                color = (255, 255, 255, 255) if (i + j) % 2 else (20, 20, 24, 255)
                d.polygon([tuple(p) for p in q], fill=color)

    def _arrow(self, d, c, t, length, *, outline=True):
        t = _v(t)
        nrm = np.array([-t[1], t[0]])
        head, hw, sw = length * 0.45, length * 0.52, length * 0.2
        tail, tip = c - t * length / 2, c + t * length / 2
        neck = tip - t * head
        poly = [tail + nrm * sw / 2, neck + nrm * sw / 2, neck + nrm * hw / 2, tip,
                neck - nrm * hw / 2, neck - nrm * sw / 2, tail - nrm * sw / 2]
        d.polygon([tuple(p) for p in poly], fill=RED + (255,),
                  outline=(255, 255, 255, 255) if outline else None, width=max(1, round(1.6 * self.s)) if outline else 0)

    def _line(self, d, pts: np.ndarray, color, width: float, closed: bool):
        seq = [tuple(p) for p in pts]
        if closed:
            seq += seq[:2]
        if len(seq) >= 2:
            d.line(seq, fill=color, width=max(1, round(width)), joint="curve")

    def track(self, progress: float = 1.0) -> Image.Image:
        s, W, H = self.s, round(self.pw * self.s), round(self.ph * self.s)
        m = self.n if progress >= 1 else max(2, int(self.n * progress))
        closed = m >= self.n
        top, base = self.top[:m], self.base[:m]
        w = TRACK_W * s
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))

        glow = Image.new("L", (W, H), 0)
        self._line(ImageDraw.Draw(glow), base, 255, w * 2.6, closed)
        glow = glow.filter(ImageFilter.GaussianBlur(12 * s))
        layer.alpha_composite(Image.merge("RGBA", [Image.new("L", (W, H), RED[0]), Image.new("L", (W, H), RED[1]),
                                                    Image.new("L", (W, H), RED[2]), glow.point(lambda v: int(v * 0.55))]))
        shadow = Image.new("L", (W, H), 0)
        self._line(ImageDraw.Draw(shadow), base + [3 * s, 9 * s], 255, w * 1.3, closed)
        shadow = shadow.filter(ImageFilter.GaussianBlur(6 * s))
        layer.alpha_composite(Image.merge("RGBA", [Image.new("L", (W, H), 0)] * 3 + [shadow.point(lambda v: int(v * 0.7))]))

        d = ImageDraw.Draw(layer)
        steps = max(2, int(np.max(base[:, 1] - top[:, 1])) + 1)
        for i in range(steps, -1, -1):
            f = i / steps
            col = tuple(int(a + (b - a) * (1 - f)) for a, b in zip(WALL_DARK, WALL_LIGHT)) + (255,)
            self._line(d, top + (base - top) * f, col, w, closed)
        self._line(d, top, (255, 40, 30, 255), w + 2.6 * s, closed)
        self._line(d, top, TOP + (255,), w - 1.2 * s, closed)

        if not closed:
            hx, hy = top[-1]
            r = w * 0.9
            d.ellipse((hx - r, hy - r, hx + r, hy + r), fill=(255, 255, 255, 255), outline=RED + (255,),
                      width=max(1, round(2 * s)))
            return layer

        tan = self.top[2] - self.top[-2]
        self._checker(d, self.top[0], tan, w * 2.1, w * 0.8)
        i = int(self.n * 0.06)
        tan = _v(self.top[(i + 6) % self.n] - self.top[i - 6])
        nrm = np.array([-tan[1], tan[0]])
        centre = self.top.mean(axis=0)
        side = 1 if np.dot(nrm, self.top[i] - centre) > 0 else -1
        self._arrow(d, self.top[i] + nrm * side * w * 2.4, tan, 34 * s)

        r = 11 * s
        for label, x, y, _ in self.corners:
            d.ellipse((x - r, y - r, x + r, y + r), fill=(18, 18, 26, 240), outline=(255, 255, 255, 120),
                      width=max(1, round(1.3 * s)))
            fz = 13 if len(label) <= 2 else 11
            d.text((x, y + 0.5 * s), label, font=graphics.font("Bold", round(fz * s)), fill=(255, 255, 255, 255),
                   anchor="mm")
        return layer

    def frame(self, progress: float = 1.0, alpha: float = 1.0, lift: float = 0.0) -> Image.Image:
        img = self.panel.copy()
        img.alpha_composite(self.track(progress))
        return place_panel(img, self.size, alpha=alpha, lift=lift)


def render(outline: dict, out_png: Path, *, title: str, length_km: float | None = None, credit: str | None = None,
           subtitle: str | None = None, size: tuple[int, int] = (1920, 1080)) -> Path:
    """整帧透明 PNG（面板在画面正中）。length_km 给官方全长；不给就用赛道线长度并标“约”。"""
    scene = _Scene(outline, title=title, subtitle=subtitle, length_km=length_km, credit=credit, size=size)
    return graphics._save_png(scene.frame(), out_png)


def animate(outline: dict, out_mov: Path, *, title: str, length_km: float | None = None, credit: str | None = None,
            subtitle: str | None = None, size: tuple[int, int] = (1920, 1080), draw: float = 1.8,
            hold: float = 1.0, fps: int = graphics.FPS) -> Path:
    """面板淡入上浮 → 赛道从起终点按行驶方向画出一圈 → 停住。ProRes 4444 透明 MOV。"""
    scene = _Scene(outline, title=title, subtitle=subtitle, length_km=length_km, credit=credit, size=size)
    intro = 0.35
    total = intro + draw + hold

    def frames() -> Iterator[bytes]:
        for i in range(round(total * fps)):
            t = i / fps
            a = graphics._ease_out(t / intro)
            p = graphics._ease_out((t - intro) / draw) if t > intro else 0.0
            yield scene.frame(min(1.0, max(p, 0.003)) if p < 0.999 else 1.0, a, (1 - a) * 18).tobytes()
    return graphics._write_mov(frames(), out_mov, size, fps)
