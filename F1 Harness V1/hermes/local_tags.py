"""本机打标（零 token）：macOS Vision 读画面文字 + 中文 CLIP 样例原型分类 + 赛历/车手名单规则。

输出与 GPT 打标同一结构（teams/drivers/circuit/session/main/sub/shot_type/has_subtitles/has_logo/logo_note），
由 assets.save_tags 写库。GPT 打标只在用户手动点"用 GPT 重新打标"时使用。
"""
import functools
import json
import logging
import re
import subprocess
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np

from . import assets, config, db

log = logging.getLogger("hermes.local_tags")

OCR_BIN = config.CORE / "tools" / "bin" / "hermes-ocr"
OCR_TIMEOUT_SECONDS = 3600

# 角落：距边 ≤20% 宽、≤15% 高；常驻台标、UP 主水印都在这里
CORNER_X, CORNER_Y = 0.20, 0.15
# 同一文字在同一角出现的镜头比例超过这个就当常驻水印（F1 官方图形会被排除）
LOGO_MIN_SHARE, LOGO_MIN_SHOTS = 0.2, 5
SUB_TOP = 0.72                  # 字幕在画面下方 28% 以内、水平居中
SUB_CENTER = 0.2
MIN_CONF = 0.5
TOWER_CODES = 6                 # 一帧读到 ≥6 个车手缩写就是计时塔，缩写不算出镜车手
TOWER_X = 0.2                   # 计时塔在画面左侧 20% 以内
MAX_CODES = 3

# 其他频道/平台的台标文字（转播方、国内平台）。F1 官方图形不算台标
BROADCASTERS = ("SKY SPORTS", "SKYSPORTS", "SKY F1", "ESPN", "DAZN", "VIAPLAY", "CANAL+", "SERVUSTV", "ORF",
                "RTL", "ZIGGO", "MOVISTAR", "CCTV", "咪咕", "腾讯体育", "五星体育", "广东体育", "爱奇艺", "优酷",
                "BILIBILI", "哔哩哔哩", "抖音", "快手", "西瓜视频", "微博", "小红书")
OFFICIAL = re.compile(r"^(F1|FORMULA ?1|LAP.*|TURN.*|DRS.*|FIA|LIVE|REPLAY|\W*\d[\d\W]*)$")

# 主标签、镜头类型：人工标注样例的 CLIP 向量求均值当原型，取最像的一类。
# 文字提示词零样本实测只有 0.5 左右；原型留一验证：主标签 0.89（334 个样例）、镜头类型 0.73（96 个）
EXAMPLES_FILE = Path(__file__).parent / "data" / "tag_examples.json"
PROTOTYPES_FILE = Path(__file__).parent / "data" / "tag_prototypes.npz"
# 并入用户手改主标签后重算的原型，存在就优先用
LEARNED_FILE = config.CORE / "data" / "tag_prototypes.npz"
MIN_EXAMPLES = 3                # 样例不到 3 个的类别不建原型（实测只有 1 个「采访」样例时会把围观的车队员工都归进去）

# 画面文字决定的主标签，只改写「比赛中/安全车」这类赛道画面；发布会例外
TRACK_MAINS = ("比赛中", "安全车")
RE_PRESS = re.compile(r"PRESS CONFERENCE")
RE_RED = re.compile(r"\bRED FLAG")
RE_INCIDENT = re.compile(r"\b(STOPPED|CRASH(ED)?|COLLISION|STATIONARY|RETIRED)\b")
RE_SC = re.compile(r"\b(SAFETY|VSC)\b")
# 计时塔里的斜杠常被读成 1 或 I（LAP 1/70 → LAP1170）
RE_START = re.compile(r"\bFORMATION LAP\b|\bLAP\s*1\s*[/1I|]\s*\d{2}\b")
RE_BATTLE = re.compile(r"\bBATTLE\b")
RE_PROBE = re.compile(r"\b(INVESTIGATION|PENALTY|STEWARDS|NOTED)\b")
RE_GAP = re.compile(r"\+\s?(\d{1,2}\.\d{3})\b")   # 对战间隔是三位小数；计时塔普通间隔只有一位
# 计时塔里 BATTLE 字样下方这么高的范围是对战框：列着参与缠斗的车手全名和彼此间隔
BATTLE_SPAN = 0.25
# 安全车、起步是一段连续的时间：两个读到字的镜头相隔不超过这么多个，中间的也算
PERIOD_GAP = 8
WHEEL_GAP = 0.2                 # 对战图上两车间隔 ≤0.2 秒（约 14 米）算轮对轮
OVERTAKE_WINDOW = 4             # 同一对车手在 4 个镜头内前后顺序颠倒算超车


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).upper()


# ---------- 文字识别 ----------

def ocr(paths: list) -> dict[str, list[dict]]:
    """返回 {路径: [{t, c, x, y, w, h}]}，坐标 0–1、原点左上。"""
    if not paths:
        return {}
    if not OCR_BIN.exists():
        raise RuntimeError(f"缺少文字识别工具：{OCR_BIN}（swiftc -O -o {OCR_BIN} {config.CORE / 'tools' / 'ocr.swift'}）")
    proc = subprocess.run([str(OCR_BIN)], input="\n".join(str(p) for p in paths) + "\n", capture_output=True,
                          text=True, timeout=OCR_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"文字识别失败：{proc.stderr[-500:]}")
    out = {}
    for line in proc.stdout.splitlines():
        d = json.loads(line)
        out[d["path"]] = d.get("lines") or []
    return out


# ---------- 车手名单、赛道 ----------

def _season(title: str | None) -> int:
    m = re.search(r"20[2-3]\d", title or "")
    return int(m.group()) if m else date.today().year


def _load_roster_fastf1(season: int) -> list[dict]:
    import fastf1
    import pandas as pd

    config.FASTF1_CACHE.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(config.FASTF1_CACHE))
    logging.getLogger("fastf1").setLevel(logging.WARNING)
    sched = fastf1.get_event_schedule(season, include_testing=False)
    done = sched[sched["EventDate"] < pd.Timestamp.now()].sort_values("RoundNumber", ascending=False)
    for rnd in done["RoundNumber"]:
        try:
            s = fastf1.get_session(season, int(rnd), "R")
            s.load(laps=False, telemetry=False, weather=False, messages=False)
            res = s.results
        except Exception as e:  # noqa: BLE001
            log.warning("第 %s 站成绩读取失败：%s", rnd, e)
            continue
        if res is None or res.empty:
            continue
        return [{"code": r["Abbreviation"], "name": r["FullName"], "team": r["TeamName"]}
                for _, r in res.iterrows() if isinstance(r["FullName"], str)]
    return []


def roster(conn, season: int, *, loader=_load_roster_fastf1) -> list[dict]:
    """当季车手名单（缩写、全名、车队），存在设置里，一个赛季只从 FastF1 读一次。"""
    key = f"assets.roster.{season}"
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row:
        return json.loads(row[0])
    try:
        drivers = loader(season)
    except Exception as e:  # noqa: BLE001
        log.warning("%s 赛季车手名单读取失败，只按译名表认姓氏：%s", season, e)
        return []
    if drivers:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
                     (key, json.dumps(drivers, ensure_ascii=False)))
    return drivers


class Names:
    """把画面文字里的缩写、姓氏、车队名对到译名表写法。"""

    def __init__(self, conn, drivers: list[dict]):
        _, self.mapping = assets._approved_names(conn)
        self.codes: dict[str, str] = {}
        self.surnames: dict[str, str] = {}
        self.team_of: dict[str, str] = {}
        for d in drivers:
            zh = self.zh(d["name"])
            self.codes[d["code"].upper()] = zh
            self.team_of[zh] = self.zh(d["team"])
        for r in conn.execute("SELECT original FROM glossary WHERE kind='车手' AND state='approved'"):
            self.surnames[_plain(r[0].split()[-1])] = self.zh(r[0])
        for d in drivers:
            self.surnames.setdefault(_plain(d["name"].split()[-1]), self.zh(d["name"]))
        self.teams = {_plain(r[0]): self.zh(r[0]) for r in conn.execute(
            "SELECT original FROM glossary WHERE kind='车队' AND state='approved'")}
        for d in drivers:
            self.teams.setdefault(_plain(d["team"]), self.zh(d["team"]))

    def zh(self, name: str) -> str:
        return self.mapping.get(name.strip().lower(), name.strip())


def circuit_from_title(conn, title: str | None) -> str | None:
    """标题里的大奖赛名（中文或英文）→ 赛历里该站地点 → 赛道译名。"""
    if not title:
        return None
    t = _plain(title)
    best = None
    for r in conn.execute("SELECT original, approved FROM glossary WHERE kind='大奖赛' AND state='approved'"):
        for key in (r["original"], r["approved"]):
            if key and _plain(key) in t and (best is None or len(key) > len(best[0])):
                best = (key, r["original"])
    if best is None:
        return None
    row = conn.execute("SELECT location FROM race_events WHERE name_en=? ORDER BY season DESC LIMIT 1",
                       (best[1],)).fetchone()
    if not row:
        return None
    zh = conn.execute("SELECT approved FROM glossary WHERE kind='赛道' AND original=? AND state='approved'",
                      (row["location"],)).fetchone()
    return zh["approved"] if zh else row["location"]


# ---------- 单个镜头的文字规则 ----------

def _corner(ln: dict) -> str | None:
    left, right = ln["x"] < CORNER_X, ln["x"] + ln["w"] > 1 - CORNER_X
    top, bottom = ln["y"] < CORNER_Y, ln["y"] + ln["h"] > 1 - CORNER_Y
    if (left or right) and (top or bottom):
        return ("左" if left else "右") + ("上" if top else "下")
    return None


def _is_subtitle(ln: dict) -> bool:
    if ln["c"] < MIN_CONF or ln["y"] < SUB_TOP or abs(ln["x"] + ln["w"] / 2 - 0.5) > SUB_CENTER:
        return False
    # 只认中文字幕：F1 官方英文图形也在画面下方居中，按英文认会大量误报
    return len(re.findall(r"[\u4e00-\u9fff]", ln["t"])) >= 4


def _has_tower(lines: list[dict]) -> bool:
    left = [ln for ln in lines if ln["x"] + ln["w"] / 2 < TOWER_X]
    return any(re.search(r"\bLAP\s*\d", _plain(ln["t"])) for ln in left) or len(left) >= TOWER_CODES


FLAG_RES = (("press", RE_PRESS), ("red", RE_RED), ("incident", RE_INCIDENT), ("sc", RE_SC), ("start", RE_START),
            ("battle", RE_BATTLE), ("probe", RE_PROBE))


def _session_hint(t: str) -> str | None:
    if re.search(r"\b(S?Q[123]|QUALIFYING|SHOOTOUT)\b", t):
        return "排位"
    if re.search(r"\b(PRACTICE|FP[123])\b", t):
        return "练习"
    if re.search(r"\bSPRINT\b", t):
        return "冲刺"
    if re.search(r"\bLAP\s*\d", t):
        return "正赛"
    return None


def read_shot(lines: list[dict], names: Names) -> dict:
    found: dict[str, tuple] = {}            # 车手 → 画面位置，判断对战图上谁在前
    battle: dict[str, tuple] = {}
    teams, codes, gaps, flags = [], {}, [], set()
    session = None
    subtitles = False
    # 左侧计时塔列着全部车手，不代表他们出镜；只认塔里对战框中的车手
    tower = _has_tower(lines)
    battle_y = min((ln["y"] for ln in lines if RE_BATTLE.search(_plain(ln["t"]))), default=None)
    for ln in lines:
        t = _plain(ln["t"])
        hint = _session_hint(t)
        if hint and (session is None or session == "正赛"):
            session = hint
        flags.update(name for name, rx in FLAG_RES if rx.search(t))
        subtitles = subtitles or _is_subtitle(ln)
        pos = (round(ln["y"] * 20), ln["x"])   # 同一行（高度差 <5%）按左右排
        words = re.findall(r"[A-Z]+", t)
        if tower and ln["x"] + ln["w"] / 2 < TOWER_X:
            if battle_y is not None and battle_y <= ln["y"] <= battle_y + BATTLE_SPAN:
                gaps += [float(g) for g in RE_GAP.findall(t)]
                for w in words:
                    if len(w) >= 4 and w in names.surnames:
                        battle.setdefault(names.surnames[w], pos)
            continue
        gaps += [float(g) for g in RE_GAP.findall(t)]
        if words and words[0] in names.codes and len(words[0]) == 3:
            codes.setdefault(words[0], pos)
        for w in words:
            if len(w) >= 4 and w in names.surnames:
                found.setdefault(names.surnames[w], pos)
        for key, zh in names.teams.items():
            if len(key) >= 4 and re.search(rf"\b{re.escape(key)}\b", t):
                teams.append(zh)
    if len(codes) <= MAX_CODES:
        for c, pos in codes.items():
            found.setdefault(names.codes[c], pos)
    drivers = list(dict.fromkeys([*found, *battle]))
    teams = list(dict.fromkeys(teams + [names.team_of[d] for d in drivers if d in names.team_of]))
    ranked = battle or found
    return {"drivers": drivers, "teams": teams, "session_hint": session, "flags": flags, "gaps": gaps,
            "order": sorted(ranked, key=ranked.get), "has_subtitles": subtitles}


def find_logos(per_shot: dict[int, list[dict]], uploader: str | None) -> dict[int, str]:
    """返回 {镜头号: 台标说明}。认法：已知转播方/平台名、上传者名，或同一文字常驻同一角。"""
    seen: dict[tuple[str, str], set[int]] = {}
    raw: dict[tuple[str, str], str] = {}
    up = _plain(uploader or "")
    hits: dict[int, str] = {}
    for idx, lines in per_shot.items():
        for ln in lines:
            corner = _corner(ln)
            if corner is None or ln["c"] < MIN_CONF:
                continue
            t = _plain(ln["t"]).strip()
            key = re.sub(r"\s+", " ", t)
            if not key or OFFICIAL.match(key):
                continue
            if any(b in key for b in BROADCASTERS) or (len(up) >= 3 and up in key) or key.startswith("@"):
                hits.setdefault(idx, f"{corner}角「{ln['t'].strip()}」字样")
                continue
            seen.setdefault((corner, key), set()).add(idx)
            raw.setdefault((corner, key), ln["t"].strip())
    n = max(len(per_shot), 1)
    for (corner, key), idxs in seen.items():
        if len(idxs) >= LOGO_MIN_SHOTS and len(idxs) / n >= LOGO_MIN_SHARE:
            for i in idxs:
                hits.setdefault(i, f"{corner}角常驻「{raw[(corner, key)]}」字样")
    return hits


# ---------- 画面分类 ----------

def _centroids(vecs: np.ndarray, labels: list[str]) -> tuple[list[str], np.ndarray]:
    counts = Counter(labels)
    names = sorted(c for c in counts if counts[c] >= MIN_EXAMPLES)
    mat = np.stack([vecs[[i for i, t in enumerate(labels) if t == c]].mean(axis=0) for c in names])
    return names, mat / np.linalg.norm(mat, axis=1, keepdims=True)


def build_prototypes(conn, examples_file=EXAMPLES_FILE, out_file=PROTOTYPES_FILE, *, manual=False) -> dict:
    """按样例文件（素材 sha256 + 镜头号 + 人工标签）从库里取向量，重算原型。
    manual=True 时再并入用户在网页上手改的主标签（同一镜头以手改为准）。"""
    examples = json.loads(examples_file.read_text())["examples"]
    items: dict[tuple, dict] = {}
    for e in examples:
        row = conn.execute("SELECT s.clip_vec FROM asset_shots s JOIN assets a ON a.id = s.asset_id "
                           "WHERE a.sha256=? AND s.idx=?", (e["sha256"], e["idx"])).fetchone()
        if row and row[0]:
            items[(e["sha256"], e["idx"])] = {"vec": row[0], "main": [e["main"]] if e.get("main") else [],
                                              "type": e.get("shot_type")}
    missing = len(examples) - len(items)
    if manual:
        for r in conn.execute("SELECT a.sha256, s.idx, s.clip_vec, s.main_manual FROM asset_shots s "
                              "JOIN assets a ON a.id = s.asset_id "
                              "WHERE s.main_manual IS NOT NULL AND s.clip_vec IS NOT NULL"):
            it = items.setdefault((r["sha256"], r["idx"]), {"vec": r["clip_vec"], "type": None})
            it["main"] = json.loads(r["main_manual"])
    vec = {k: np.frombuffer(v["vec"], dtype=np.float16).astype(np.float32) for k, v in items.items()}
    mains = [(k, m) for k, v in items.items() for m in v["main"]]
    types = [(k, v["type"]) for k, v in items.items() if v["type"]]
    if not mains or not types:
        raise ValueError("样例镜头在库里都找不到向量")
    m_names, m_mat = _centroids(np.stack([vec[k] for k, _ in mains]), [m for _, m in mains])
    t_names, t_mat = _centroids(np.stack([vec[k] for k, _ in types]), [t for _, t in types])
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_file.with_name(out_file.stem + ".tmp.npz")
    np.savez(tmp, main_names=np.array(m_names), main=m_mat, type_names=np.array(t_names), type=t_mat)
    tmp.replace(out_file)
    return {"examples": len(items), "missing": missing, "main": dict(Counter(m for _, m in mains)),
            "shot_type": dict(Counter(t for _, t in types))}


def learn(conn) -> dict:
    """用户改了主标签后调用：样例 + 手改一起重算，存到数据目录，之后打标优先用它。"""
    return build_prototypes(conn, out_file=LEARNED_FILE, manual=True)


@functools.lru_cache(maxsize=4)
def _load(path: str, mtime: float):
    d = np.load(path)
    return [str(x) for x in d["main_names"]], d["main"], [str(x) for x in d["type_names"]], d["type"]


def prototypes():
    p = LEARNED_FILE if LEARNED_FILE.exists() else PROTOTYPES_FILE
    return _load(str(p), p.stat().st_mtime)


def classify(vecs: np.ndarray) -> list[tuple[str, str, str]]:
    """每个镜头返回 (主标签, 镜头类型, 次像的主标签)。vecs：(n, d) 已归一化的关键帧向量。"""
    m_names, m_mat, t_names, t_mat = prototypes()
    order = np.argsort(-(vecs @ m_mat.T), axis=1)
    t = (vecs @ t_mat.T).argmax(axis=1)
    return [(m_names[o[0]], t_names[b], m_names[o[1]] if len(o) > 1 else m_names[o[0]])
            for o, b in zip(order, t)]


def _title_session(title: str | None) -> str | None:
    raw, t = title or "", _plain(title or "")
    if "排位" in raw or re.search(r"QUALIFYING|SHOOTOUT", t):
        return "排位"
    if "冲刺" in raw or re.search(r"\bSPRINT\b", t):
        return "冲刺"
    if "练习" in raw or re.search(r"\b(PRACTICE|FP[123])\b", t):
        return "练习"
    if any(k in raw for k in ("正赛", "大奖赛回放", "比赛回放")) or re.search(r"\bRACE\b", t):
        return "正赛"
    return None


def _spans(marked: list[int], gap: int = PERIOD_GAP) -> set[int]:
    out = set(marked)
    for a, b in zip(marked, marked[1:]):
        if b - a <= gap:
            out.update(range(a, b + 1))
    return out


def _overtakes(reads: dict[int, dict]) -> set[int]:
    """对战图上同一对车手前后顺序颠倒的镜头。"""
    seen: list[tuple[int, list]] = []
    out = set()
    for i in sorted(reads):
        rd = reads[i]
        if "battle" not in rd["flags"] or len(rd["order"]) < 2:
            continue
        cur = rd["order"]
        for j, prev in reversed(seen):
            if i - j > OVERTAKE_WINDOW:
                break
            both = [d for d in prev if d in cur]
            if any(cur.index(a) > cur.index(b) for a, b in zip(both, both[1:])):
                out.add(i)
                break
        seen.append((i, cur))
    return out


def _main(base: str, flags: set, i: int, sc: set, start: set) -> str:
    if "press" in flags:
        return "发布会"
    if base == "发车格" and "start" in flags:
        return "起步"
    if base not in TRACK_MAINS:
        return base
    if "red" in flags:
        return "红旗"
    if "incident" in flags:
        return "事故"
    if i in sc:
        return "安全车"
    if i in start:
        return "起步"
    return base


def _subs(rd: dict, overtaken: bool) -> list[str]:
    f, out = rd["flags"], []
    if overtaken:
        out.append("超车")
    if "battle" in f and any(g <= WHEEL_GAP for g in rd["gaps"]):
        out.append("轮对轮")
    if "battle" in f:
        out.append("缠斗")
    if "probe" in f:
        out.append("调查处罚")
    return out


# ---------- 主流程 ----------

def tag_asset(conn, asset_id: int, *, ocr_fn=ocr, classify_fn=classify, roster_loader=_load_roster_fastf1) -> dict:
    asset = assets._asset(conn, asset_id)
    if asset["state"] != "ready":
        raise ValueError(f"素材 {asset_id} 状态是 {asset['state']}，不能打标")
    # 只打原镜头（前后顺序按编号推断赛段），切细出来的段由 save_tags 跟着原镜头写
    rows = conn.execute("SELECT idx, keyframe, clip_vec FROM asset_shots WHERE asset_id=? AND parent_idx IS NULL "
                        "ORDER BY idx", (asset_id,)).fetchall()
    if not rows:
        raise ValueError(f"素材 {asset_id} 没有可打标的镜头")
    names = Names(conn, roster(conn, _season(asset["title"]), loader=roster_loader))
    circuit = circuit_from_title(conn, asset["title"])

    texts = ocr_fn([r["keyframe"] for r in rows if r["keyframe"]])
    per_shot = {r["idx"]: texts.get(r["keyframe"], []) for r in rows}
    reads = {i: read_shot(lines, names) for i, lines in per_shot.items()}
    logos = find_logos(per_shot, asset["uploader"])

    with_vec = [r for r in rows if r["clip_vec"]]
    classes = {}
    if with_vec:
        vecs = np.stack([np.frombuffer(r["clip_vec"], dtype=np.float16) for r in with_vec]).astype(np.float32)
        classes = dict(zip((r["idx"] for r in with_vec), classify_fn(vecs)))

    # 整段素材一般属于同一个赛段：先看标题，再看全片计时图形多数；单个镜头读到 Q1–Q3 / 练习字样的以它为准
    hints = Counter(r["session_hint"] for r in reads.values() if r["session_hint"])
    default = _title_session(asset["title"]) or (hints.most_common(1)[0][0] if hints else "其他")
    sc = _spans([i for i, r in reads.items() if "sc" in r["flags"]])
    start = _spans([i for i, r in reads.items() if "start" in r["flags"]])
    overtakes = _overtakes(reads)
    # 开赛后（起步段之后）停着的车和人群多半是停车区、进站，不算发车格，改取次像的一类
    race_on = max(start) + PERIOD_GAP if start else None

    shots = {}
    for r in rows:
        i = r["idx"]
        rd = reads[i]
        cls = classes.get(i, ("比赛中", None))
        base, shot_type = cls[0], cls[1]
        if base == "发车格" and race_on is not None and i > race_on and len(cls) > 2:
            base = cls[2]
        shots[i] = {
            "teams": rd["teams"], "drivers": rd["drivers"], "circuit": circuit,
            "session": rd["session_hint"] if rd["session_hint"] in ("排位", "练习") else default,
            "main": [_main(base, rd["flags"], i, sc, start)], "sub": _subs(rd, i in overtakes),
            "shot_type": shot_type, "has_subtitles": rd["has_subtitles"],
            "has_logo": i in logos, "logo_note": logos.get(i, ""),
        }
    result = assets.save_tags(conn, asset_id, shots)
    return result | {"engine": "local"}
