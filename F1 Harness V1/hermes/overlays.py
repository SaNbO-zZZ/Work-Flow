"""D4 图形与叠层（M6-3）：前瞻在介绍赛道、赛程、轮胎的段落分别叠赛道简图、赛程卡、轮胎策略图；
所有分支在口播明确讲积分、讲数据的段落叠积分榜、数据卡（用户 2026-10-14）。写 05_图形/叠层.json 给剪辑用。"""
import asyncio
import json
import re
import sqlite3
import unicodedata
from datetime import date, datetime
from pathlib import Path

from . import assets, calendar, config, facts, glossary, graphics, narration, panels, research, trackmap
from .news import valid_url

LAYER_FILE = "叠层.json"
TRACK_PNG = "赛道图.png"
SCHEDULE_PNG = "赛程.png"
TYRE_PNG = "轮胎策略.png"
TYRE_JSON = "轮胎信息.json"
MIN_SPAN = 4.0
PANEL_MAX = 8.0                                          # 每张面板从段落开头起最多停留的秒数（用户 2026-10-12 定）
KEYWORDS = ("赛道", "布局", "一圈", "公里", "弯", "直道", "路面", "刹车", "发卡", "连续弯", "回头弯", "海拔", "逆时针", "顺时针")
MIN_HITS = 3
SCHEDULE_KEYWORDS = ("一练", "二练", "三练", "排位", "正赛", "冲刺", "北京时间")
TYRE_KEYWORDS = ("轮胎", "配方", "进站", "策略", "软胎", "中性胎", "硬胎", "一停", "两停")
SCHEDULE_SOURCE = "FastF1 赛历"
WEEKDAY = "一二三四五六日"
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*?)\s*$")
# 积分榜、数据卡（用户 2026-10-14：文案明确提到积分或数据时叠图，规格同赛道简图）
STANDINGS_PNG = {"drivers": "积分榜-车手.png", "teams": "积分榜-车队.png"}
STANDINGS_JSON = "积分榜数据.json"
DATACARD_PNG = "数据卡-{n}.png"
STANDINGS_SOURCE = "FastF1 / Ergast"
MAX_DATA_PANELS = 4                                      # 全片积分榜 + 数据卡最多几张，免得满屏都是图
STANDINGS_TOP = 10                                       # 车手榜列前 10，口播点名的 10 名以外车手补在后面
STANDINGS_MAX_ROWS = 12
STANDINGS_RE = re.compile(r"积分|分差|总冠军争夺|(?:领先|落后)[^，。！？,!?]{0,8}?"
                          r"(?:\d+(?:\.\d+)?|[零一二三四五六七八九十百两]+)\s*分(?!钟)")
TEAM_STANDINGS_RE = re.compile(r"车队积分|车队榜|车队总冠军|车队冠军|制造商")
DATA_WORDS = ("秒", "圈速", "差距", "名", "圈", "公里/时", "公里每小时", "km/h", "次")
DATA_MIN_ROWS, DATA_MAX_ROWS = 2, 4
# Ergast 车队 ID → FastF1 的 TeamName（graphics.TEAM_COLORS 和译名表都按 FastF1 的写法）
ERGAST_TEAMS = {"mercedes": "Mercedes", "red_bull": "Red Bull Racing", "ferrari": "Ferrari", "mclaren": "McLaren",
                "aston_martin": "Aston Martin", "alpine": "Alpine", "williams": "Williams", "rb": "Racing Bulls",
                "haas": "Haas F1 Team", "audi": "Audi", "sauber": "Kick Sauber", "cadillac": "Cadillac"}
_POS_FACT = re.compile(r"^(.*?)第(\d+)名：(.+?)（(.+?)）$")
_BJ_PAREN = re.compile(r"（北京时间[^）]*）")
_UNIT_PAREN = re.compile(r"（([^（）]{1,8})）$")
_CN_NUM = "零〇一二三四五六七八九十百千万两点"
_POS_SAID = re.compile(r"第([\d零〇一二三四五六七八九十两]+)名|(?<![A-Za-z])P(\d{1,2})(?!\d)")
_SESSION_FACT = re.compile(r"^(一练|二练|三练|冲刺排位|排位|冲刺赛|正赛)开始时间（北京时间 (\d{1,2})月(\d{1,2})日 周(.)）$")

TYRE_INSTRUCTION = """你是 F1 视频的图形资料员。根据本期逐字稿里讲轮胎的那段、事实表和给出的来源，整理出本站的轮胎信息，用来做一张叠在画面上的轮胎策略图。
1. 只用比赛开始前已公开的信息（倍耐力赛前预告、F1 官网、车队官网等），不得使用赛果或赛后数据。
2. 写清本站三种干胎配方（硬/中/软各对应 C 几）；倍耐力或官方给出了预计策略（几停、每段用什么胎、大约第几圈进站）就写上，没有就留空，不要自己推算。
3. 每一项都要有来源网址和原文摘录；没有可靠来源的项不写，宁缺毋滥。
4. 按给定 JSON 格式输出：配方、策略、来源。"""
TYRE_FIELDS = """- compounds：hard/medium/soft 分别填 "C1"…"C6"，没公布的填 null；
- race_laps：正赛总圈数，没有可靠来源填 null；
- strategies：每条写 name（如“一停：中→硬”）、stints（依次用的胎，取 hard/medium/soft）、pit_laps（每次进站的大约圈数，如“18-25”，个数比 stints 少 1）；官方没给就输出空数组；
- sources：每条写 url 和 excerpt（原文摘录，照抄原文语言）。"""
_TYRE_KEYS = ("hard", "medium", "soft")
TYRE_SCHEMA = research._obj({
    "compounds": research._obj({k: {"type": ["string", "null"]} for k in _TYRE_KEYS}),
    "race_laps": {"type": ["integer", "null"]},
    "strategies": {"type": "array", "items": research._obj({
        "name": research.STR,
        "stints": {"type": "array", "items": {"type": "string", "enum": list(_TYRE_KEYS)}},
        "pit_laps": {"type": "array", "items": research.STR}})},
    "sources": {"type": "array", "items": research._obj({"url": research.STR, "excerpt": research.STR})},
})


def _paras(units: list[dict]) -> int:
    return max((u["para"] for u in units), default=-1) + 1


def section_paras(script: str, conn: sqlite3.Connection | None = None, *, names: tuple[str, ...] = ("赛道",),
                  keywords: tuple[str, ...] = KEYWORDS, min_hits: int = MIN_HITS) -> tuple[range, str] | None:
    """某个主题的段落（配音稿段号范围）：先找段名含 names 任一词的段，找不到再按关键词挑最像的一段。"""
    lines = script.splitlines()
    heads = [(k, m[1]) for k, l in enumerate(lines) if (m := _HEADING.match(l))]
    full = narration.prepare(script, conn).units
    total = _paras(full)
    for j, (k, name) in enumerate(heads):
        if not any(n in name for n in names):
            continue
        a = _paras(narration.prepare("\n".join(lines[:k]), conn).units)
        nxt = heads[j + 1][0] if j + 1 < len(heads) else len(lines)
        b = _paras(narration.prepare("\n".join(lines[:nxt]), conn).units) if nxt < len(lines) else total
        if b > a:
            return range(a, b), f"段名「{name}」"
    best, hits = None, 0
    for p in range(total):
        text = "".join(u["show"] for u in full if u["para"] == p)
        n = sum(text.count(k) for k in keywords)
        if n > hits:
            best, hits = p, n
    if best is not None and hits >= min_hits:
        return range(best, best + 1), f"关键词（第 {best + 1} 段，命中 {hits} 次）"
    return None


def track_paras(script: str, conn: sqlite3.Connection | None = None) -> tuple[range, str] | None:
    """赛道段落：段名含“赛道”，后备按赛道关键词。"""
    return section_paras(script, conn)


def span_of(units: list[dict], paras: range, align: dict) -> tuple[float, float]:
    """段落 → 成片时间：按字数偏移把配音稿单元对到对齐结果的分句上。"""
    pos, c0, c1 = 0, None, None
    for u in units:
        n = len(u["say"])
        if u["para"] in paras:
            c0 = pos if c0 is None else c0
            c1 = pos + n
        pos += n
    if c0 is None:
        raise ValueError("配音稿里没有这几段")
    segs = align["segments"]
    total = sum(len(s["text"]) for s in segs) or 1
    k = total / (pos or 1)
    a, b = c0 * k, c1 * k
    picked, acc = [], 0
    for s in segs:
        n = len(s["text"])
        if a <= acc + n / 2 < b:
            picked.append(s)
        acc += n
    if not picked:
        raise ValueError("对齐结果里找不到赛道段的分句")
    start, end = float(picked[0]["start"]), float(picked[-1]["end"])
    duration = float(align.get("duration") or end)
    if end - start < MIN_SPAN:
        end = min(duration, start + MIN_SPAN)
    return round(start, 3), round(end, 3)



def panel_span(units: list[dict], paras: range, align: dict) -> tuple[float, float]:
    """面板从段落开头出现，最多停 PANEL_MAX 秒，段落更短就到段落结尾。"""
    start, end = span_of(units, paras, align)
    return start, round(min(end, start + PANEL_MAX), 3)

def _title(conn, ev: dict) -> str:
    zh = glossary.zh(conn, "赛道", ev.get("location") or "") or ev.get("location") or "赛道"
    return zh if zh.endswith("赛道") else zh + "赛道"


def schedule_of(conn: sqlite3.Connection, ep, ev: dict | None) -> dict | None:
    """赛程卡数据（北京时间，按日期分组）：优先事实表里程序写的赛历事实，没有再用赛历库 race_sessions。"""
    name = ep["gp_label"] or (ev or {}).get("name_zh") or ""
    fmt = season = rnd = None
    rows = []
    for f in facts.items(conn, ep["id"], "F"):
        if f["source"] != SCHEDULE_SOURCE:
            continue
        c = f["content"]
        if m := re.match(r"^本站（(.+)）所属赛季$", c):
            name, season = m[1], f["value"]
        elif c == "本站是本赛季第几站":
            rnd = f["value"]
        elif c.startswith("本站赛制："):
            fmt = c.split("：", 1)[1]
        elif (m := _SESSION_FACT.match(c)) and f["value"]:
            rows.append((f"{int(m[2])}月{int(m[3])}日", f"周{m[4]}", m[1], f["value"]))
    if not rows and ev:
        for s in sorted((s for s in ev.get("sessions") or [] if s.get("start_bj")), key=lambda s: s["start_bj"]):
            dt = datetime.strptime(s["start_bj"], "%Y-%m-%d %H:%M")
            rows.append((f"{dt.month}月{dt.day}日", f"周{WEEKDAY[dt.weekday()]}",
                         calendar.SESSION_ZH.get(s["name"], s["name"]), f"{dt:%H:%M}"))
        name = ev.get("name_zh") or name
        fmt = calendar.FORMAT_ZH.get(ev.get("format"), ev.get("format") or "常规")
        season, rnd = ev.get("season"), ev.get("round")
    if not rows:
        return None
    rows.sort(key=lambda r: (*map(int, re.findall(r"\d+", r[0])), r[3]))
    days = []
    for date, weekday, session, time in rows:
        if not days or days[-1]["date"] != date:
            days.append({"date": date, "weekday": weekday, "sessions": []})
        days[-1]["sessions"].append({"name": session, "time": time})
    return {"name": name, "format": fmt or "常规", "season": season, "round": rnd, "days": days,
            "credit": f"数据：{SCHEDULE_SOURCE}"}


def race_start(ev: dict | None, sched: dict | None) -> str | None:
    """正赛开始时间（北京时间），轮胎资料的信息截止点。"""
    for s in (ev or {}).get("sessions") or []:
        if s.get("name") == "Race" and s.get("start_bj"):
            return f"{s['start_bj']}（北京时间）"
    for day in (sched or {}).get("days") or []:
        for s in day["sessions"]:
            if s["name"] == "正赛":
                return f"{day['date']} {day['weekday']} {s['time']}（北京时间）"
    return None


def _section_text(units: list[dict], paras: range) -> str:
    return "\n".join("".join(u["show"] for u in units if u["para"] == p) for p in paras)


def tyre_prompt(conn: sqlite3.Connection, ep, section: str, cutoff: str | None) -> str:
    return "\n\n".join([
        TYRE_INSTRUCTION,
        "# 本期上下文\n" + research.episode_context(ep),
        "## 逐字稿里讲轮胎的那段\n" + section,
        "## 事实表（程序已取）\n" + (facts.render(conn, ep["id"]) or "（空）"),
        f"## 信息截止点\n正赛开始时间：{cutoff or '未知'}。只用这之前已公开的信息。",
        "## 输出字段\n" + TYRE_FIELDS,
    ])


def clean_tyres(data: dict) -> tuple[dict, list[str], bool]:
    """程序校验 GPT 给的轮胎信息 → (清洗后的数据, 提醒, 能否出图)。要有配方、有合法来源网址才出图。"""
    warns = []
    raw = data.get("compounds") if isinstance(data.get("compounds"), dict) else {}
    compounds = {}
    for k in _TYRE_KEYS:
        v = raw.get(k)
        v = re.sub(r"\s+", "", v).upper() if isinstance(v, str) else ""
        compounds[k] = v if re.fullmatch(r"C\d", v) else None
        if v and not compounds[k]:
            warns.append(f"轮胎图：{panels.TYRE_ZH[k]}配方「{v}」不是 C 几，已丢弃")
    sources, dropped = [], 0
    for s in data.get("sources") or []:
        url = research._text(s.get("url")) if isinstance(s, dict) else ""
        excerpt = research._text(s.get("excerpt")) if isinstance(s, dict) else ""
        if valid_url(url) and excerpt:
            sources.append({"url": url, "excerpt": excerpt})
        else:
            dropped += 1
    if dropped:
        warns.append(f"轮胎图：丢弃 {dropped} 条来源（网址不是合法的 http/https 链接或没有原文摘录）")
    laps = data.get("race_laps")
    laps = laps if isinstance(laps, int) and not isinstance(laps, bool) and 20 <= laps <= 100 else None
    strategies = []
    for st in data.get("strategies") or []:
        if not isinstance(st, dict):
            continue
        stints = [x for x in st.get("stints") or [] if x in _TYRE_KEYS]
        if not 2 <= len(stints) <= 4:
            warns.append(f"轮胎图：策略「{research._text(st.get('name'))}」的轮胎段数不对，已丢弃")
            continue
        pits = [research._text(p) for p in st.get("pit_laps") or []]
        pits = (pits + [""] * len(stints))[:len(stints) - 1]
        strategies.append({"name": research._text(st.get("name")) or f"{len(stints) - 1}停", "stints": stints,
                           "pit_laps": pits})
    used = {"compounds": compounds, "race_laps": laps, "strategies": strategies, "sources": sources}
    if not any(compounds.values()):
        return used, warns + ["轮胎图：没拿到可用的干胎配方（C 几），不出图"], False
    if not sources:
        return used, warns + ["轮胎图：没有合法的来源网址，不出图"], False
    return used, warns, True


def tyre_chart(conn: sqlite3.Connection, ep, out_dir: Path, section: str, cutoff: str | None, title: str, *,
               model: str, effort: str, task_id=None, runner=None) -> tuple[Path | None, list[str]]:
    """问一次 GPT（图形，联网）拿本站轮胎信息，校验后画轮胎策略图；原始 JSON 存 05_图形/轮胎信息.json。"""
    try:
        raw = asyncio.run(research.ask(conn, ep, tyre_prompt(conn, ep, section, cutoff), sub="05_图形", name="轮胎",
                                       schema=TYRE_SCHEMA, model=model, effort=effort, task_id=task_id,
                                       web_search=True, runner=runner))
    except ValueError as e:
        return None, [f"轮胎图：GPT 输出读不出来（{e}），不出图"]
    used, warns, ok = clean_tyres(raw)
    (out_dir / TYRE_JSON).write_text(json.dumps({"model": model, "cutoff": cutoff, "raw": raw, "used": used, "ok": ok},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    if not ok:
        return None, warns
    sites = list(dict.fromkeys(assets.site_name(s["url"]) for s in used["sources"]))
    png = panels.render_tyres({**used, "name": title, "sites": sites}, out_dir / TYRE_PNG)
    return png, warns


def resolve_overlaps(items: list[dict]) -> tuple[list[dict], list[str]]:
    """多张面板时间不能重叠：按开始时间排，后出现的让位——截短后还够 MIN_SPAN 就截短，否则不放。"""
    kept, warns = [], []
    for it in sorted(items, key=lambda x: x["start"]):
        if kept and it["start"] < kept[-1]["end"]:
            prev = kept[-1]
            if it["end"] - prev["end"] >= MIN_SPAN:
                warns.append(f"{it['kind']}和{prev['kind']}时间重叠，{it['kind']}改从 {prev['end']:.1f} 秒开始"
                             f"（原定 {it['start']:.1f} 秒）")
                it = {**it, "start": prev["end"]}
            else:
                warns.append(f"{it['kind']}（{it['start']:.1f}–{it['end']:.1f} 秒）和{prev['kind']}时间重叠，"
                             f"让开后不足 {MIN_SPAN:.0f} 秒，没放进时间线")
                continue
        kept.append(it)
    return kept, warns


def build(conn: sqlite3.Connection, ep, feedback: list[str] | None = None, *, model: str | None = None,
          effort: str | None = None, task_id=None, runner=None) -> dict:
    model = model or config.ROLE_MODELS["图形"][0]
    effort = effort or config.ROLE_MODELS["图形"][1]
    folder = Path(ep["folder"])
    out_dir = folder / "05_图形"
    out_dir.mkdir(parents=True, exist_ok=True)
    layer_path = out_dir / LAYER_FILE
    result = {"overlays": 0, "warnings": [], "feedback": list(feedback or [])}
    preview = ep["branch"] == "前瞻"
    for old in (SCHEDULE_PNG, TYRE_PNG, STANDINGS_JSON, *STANDINGS_PNG.values()):   # 上一轮生成、这一轮可能不再出的面板
        (out_dir / old).unlink(missing_ok=True)
    for old in out_dir.glob(DATACARD_PNG.format(n="*")):
        old.unlink(missing_ok=True)

    script_path = folder / "02_文案" / "逐字稿.md"
    align_path = folder / "03_配音" / "对齐.json"
    if not preview:
        result["note"] = f"{ep['branch']}分支不加赛道简图"
    if not script_path.exists() or not align_path.exists():
        if preview:
            raise RuntimeError("缺 02_文案/逐字稿.md 或 03_配音/对齐.json，先完成文案和配音")
        layer_path.write_text(json.dumps({"overlays": []}, ensure_ascii=False, indent=1), encoding="utf-8")
        result["warnings"].append("缺 02_文案/逐字稿.md 或 03_配音/对齐.json，积分榜和数据卡没做")
        return result
    script = script_path.read_text(encoding="utf-8")
    align = json.loads(align_path.read_text(encoding="utf-8"))

    labels = [x for x in (ep["gp_label"], ep["title"]) if x]
    ev = next((e for e in (calendar.find_event(conn, x) for x in labels) if e), None)
    units = narration.prepare(script, conn).units
    cands, used = [], set()
    if preview:
        cands, used, outline = _preview_panels(conn, ep, ev, labels, script, units, align, out_dir, result,
                                               model=model, effort=effort, task_id=task_id, runner=runner)
    more, warns = data_panels(conn, ep, ev, script, units, align, out_dir, used)
    cands += more
    result["warnings"] += warns

    kept, warns = resolve_overlaps(cands)
    result["warnings"] += warns
    overlays = []
    for c in kept:
        c = dict(c)
        key, how, name = c.pop("_key"), c.pop("_how"), c.pop("_name", None)
        if key == "data_panels":
            result.setdefault(key, []).append({"name": name, "how": how, "start": c["start"], "end": c["end"],
                                               "path": c["path"]})
        else:
            result[key] = {"how": how, "start": c["start"], "end": c["end"]}
        overlays.append(c)
    layer_path.write_text(json.dumps({"overlays": overlays}, ensure_ascii=False, indent=1), encoding="utf-8")
    result["overlays"] = len(overlays)
    if preview:
        result["track"] = {"source": outline.get("source"), "length_m": round(outline["length_m"])}
    return result


def _preview_panels(conn, ep, ev, labels, script, units, align, out_dir, result, *, model, effort, task_id,
                    runner) -> tuple[list[dict], set[int], dict]:
    """前瞻专属：赛道简图、赛程卡、轮胎策略图。返回 (候选面板, 已占用的段号, 赛道轮廓)。"""
    if ev is None:
        raise RuntimeError(f"赛历里找不到「{'」「'.join(labels)}」对应的一站，没法确定赛道")
    outline = trackmap.outline_for_event(ev)
    png = trackmap.render(outline, out_dir / TRACK_PNG, title=_title(conn, ev), subtitle=ev.get("name_zh"),
                          length_km=trackmap.official_km(ev.get("location")))
    cands, used = [], set()
    found = track_paras(script, conn)
    if found is None:
        result["warnings"].append("逐字稿里没找到介绍赛道的段落（没有“## 赛道”段名，关键词也不够），"
                                  "赛道图已生成但没放进时间线")
    else:
        paras, how = found
        start, end = panel_span(units, paras, align)
        cands.append({"path": f"05_图形/{png.name}", "start": start, "end": end, "kind": "赛道图",
                      "_key": "section", "_how": how})
        used.update(paras)

    sched = schedule_of(conn, ep, ev)
    found = section_paras(script, conn, names=("赛程",), keywords=SCHEDULE_KEYWORDS)
    if found is not None:
        paras, how = found
        if sched is None:
            result["warnings"].append("逐字稿有讲赛程的段落，但事实表和赛历里都没有场次时间，赛程卡没做")
        else:
            card = panels.render_schedule(sched, out_dir / SCHEDULE_PNG)
            start, end = panel_span(units, paras, align)
            cands.append({"path": f"05_图形/{card.name}", "start": start, "end": end, "kind": "赛程",
                          "_key": "schedule", "_how": how})
            used.update(paras)

    found = section_paras(script, conn, names=("轮胎",), keywords=TYRE_KEYWORDS)
    if found is not None:
        paras, how = found
        chart, warns = tyre_chart(conn, ep, out_dir, _section_text(units, paras), race_start(ev, sched),
                                  ev.get("name_zh") or ep["gp_label"] or "", model=model, effort=effort,
                                  task_id=task_id, runner=runner)
        result["warnings"] += warns
        if chart is not None:
            start, end = panel_span(units, paras, align)
            cands.append({"path": f"05_图形/{chart.name}", "start": start, "end": end, "kind": "轮胎策略",
                          "_key": "tyres", "_how": how})
            used.update(paras)
    return cands, used, outline


# ---------- 积分榜、数据卡（所有分支） ----------

def para_lines(script: str) -> tuple[list[str], list[str | None]]:
    """配音稿每一段对应的逐字稿原文行（保留 [F#] 标记和阿拉伯数字）及其所在段名，下标就是配音稿段号。"""
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m[0].count("\n"), script, flags=re.S)
    lines, heads, head = [], [], None
    for line in text.splitlines():
        if m := _HEADING.match(line):
            head = m[1]
            continue
        if narration.prepare(line).units:
            lines.append(line)
            heads.append(head)
    return lines, heads


def _field(ep, key: str):
    try:
        return ep[key]
    except (KeyError, IndexError):
        return None


def episode_day(ep) -> date:
    """本期日期：期号开头的 YYYYMMDD，没有就用创建时间。"""
    for v in (_field(ep, "code"), _field(ep, "created_at")):
        if m := re.match(r"(\d{4})-?(\d{2})-?(\d{2})", v or ""):
            return date(int(m[1]), int(m[2]), int(m[3]))
    return calendar._today()


def _done_on(e: dict) -> date:
    """一站跑完的日期：最后一个场次的北京时间日期，没有场次时间用正赛当地日期。"""
    starts = [s["start_utc"] for s in e.get("sessions") or [] if s.get("start_utc")]
    return calendar._bj(max(starts)).date() if starts else date.fromisoformat(e["event_date"])


def standings_round(conn: sqlite3.Connection, ep, ev: dict | None) -> tuple[int, int, str | None] | None:
    """积分榜取哪一站结束后的：前瞻取本站开赛前（上一站后）；复盘等取本站（本期日期前已跑完时），
    否则取本期日期前已跑完的最近一站。返回 (赛季, 站次, 那站的中文名)，赛季首站前返回 None。"""
    day = episode_day(ep)
    season = rnd = None
    if ev and ev.get("season") and ev.get("round"):
        if ep["branch"] == "前瞻":
            season, rnd = int(ev["season"]), int(ev["round"]) - 1
        elif _done_on(ev) <= day:
            season, rnd = int(ev["season"]), int(ev["round"])
    if season is None:
        if ep["branch"] == "前瞻":
            return None
        done = [e for e in calendar._all_events(conn) if _done_on(e) <= day]
        if not done:
            return None
        season, rnd = int(done[-1]["season"]), int(done[-1]["round"])
    if rnd < 1:
        return None
    name = next((e.get("name_zh") or e.get("name_en") for e in calendar.events(conn, season) if e["round"] == rnd),
                None)
    if name is None and ev and ev.get("round") == rnd:
        name = ev.get("name_zh")
    return season, rnd, name


def standings_fastf1(season: int, rnd: int) -> dict:
    """FastF1 的 Ergast 接口取某站结束后的车手、车队积分榜（含冲刺赛积分）。先只读缓存，没有再联网。"""
    fastf1 = trackmap._fastf1()
    from fastf1.ergast import Ergast

    last: Exception | None = None
    for offline in (True, False):
        fastf1.Cache.offline_mode(offline)
        try:
            api = Ergast()
            dr = api.get_driver_standings(season=season, round=rnd)
            cr = api.get_constructor_standings(season=season, round=rnd)
            if not dr.content or dr.content[0].empty:
                raise ValueError("没有车手积分")
            got = int(dr.description.iloc[0]["round"])
            if got != rnd:
                raise ValueError(f"返回的是第 {got} 站后的积分")
            drivers = []
            for i, r in enumerate(dr.content[0].to_dict("records")):
                ids, names = list(r.get("constructorIds") or []), list(r.get("constructorNames") or [])
                team = ERGAST_TEAMS.get(ids[-1], names[-1]) if ids else (names[-1] if names else "")
                pos = r.get("position")
                drivers.append({"pos": int(pos) if pos == pos and pos is not None else i + 1,
                                "given": str(r["givenName"]), "family": str(r["familyName"]),
                                "code": str(r.get("driverCode") or ""), "team": team,
                                "points": float(r["points"]), "wins": int(r.get("wins") or 0)})
            teams = []
            if cr.content and not cr.content[0].empty:
                for i, r in enumerate(cr.content[0].to_dict("records")):
                    pos = r.get("position")
                    teams.append({"pos": int(pos) if pos == pos and pos is not None else i + 1,
                                  "team": ERGAST_TEAMS.get(r["constructorId"], str(r["constructorName"])),
                                  "points": float(r["points"]), "wins": int(r.get("wins") or 0)})
            return {"season": season, "round": rnd, "source": STANDINGS_SOURCE, "drivers": drivers, "teams": teams}
        except Exception as e:  # noqa: BLE001
            last = e
        finally:
            fastf1.Cache.offline_mode(False)
    raise RuntimeError(f"FastF1 / Ergast 读取 {season} 赛季第 {rnd} 站后积分榜失败：{last}") from last


def _plain(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def driver_zh(conn: sqlite3.Connection, given: str, family: str) -> str | None:
    """车手中文译名：先按全名查译名表，再按姓氏（Ergast 写 Hülkenberg、Andrea Kimi，译名表是 FastF1 写法）。"""
    if zh := glossary.zh(conn, "车手", f"{given} {family}"):
        return zh
    key = _plain(family.split()[-1] if family.split() else family)
    for r in conn.execute("SELECT original, approved, suggested FROM glossary WHERE kind='车手'"):
        if _plain(r["original"].split()[-1]) == key and (r["approved"] or r["suggested"]):
            return r["approved"] or r["suggested"]
    return None


def standings_table(conn: sqlite3.Connection, data: dict, kind: str, text: str,
                    subtitle: str | None) -> tuple[dict | None, list[str]]:
    """积分榜面板数据。车手榜：前 10 名，口播点名的 10 名以外车手按名次补在后面（最多 12 行）；
    车队榜：全部车队。口播里点到名的行高亮。"""
    warns = []
    if kind == "teams":
        if not data.get("teams"):
            return None, ["积分榜：Ergast 没给车队积分，车队积分榜没做"]
        rows = []
        for t in data["teams"][:STANDINGS_MAX_ROWS]:
            zh = glossary.zh(conn, "车队", t["team"])
            if not zh:
                warns.append(f"积分榜：车队 {t['team']} 没有译名，图上用英文")
            rows.append({"pos": t["pos"], "name": zh or t["team"], "color": graphics.team_color(t["team"]),
                         "points": t["points"], "hl": bool(zh) and zh in text})
        return {"kind": "teams", "title": "车队积分榜", "subtitle": subtitle, "rows": rows, "gap_after": None,
                "credit": f"数据：{STANDINGS_SOURCE}"}, warns
    rows = []
    for d in data["drivers"]:
        zh = driver_zh(conn, d["given"], d["family"])
        rows.append({"pos": d["pos"], "name": zh or d["family"], "_zh": zh,
                     "team": glossary.zh(conn, "车队", d["team"]) or d["team"], "color": graphics.team_color(d["team"]),
                     "points": d["points"], "hl": bool(zh) and zh in text})
    top = rows[:STANDINGS_TOP]
    extra = [r for r in rows[STANDINGS_TOP:] if r["hl"]][:STANDINGS_MAX_ROWS - STANDINGS_TOP]
    for r in top + extra:
        if not r.pop("_zh"):
            warns.append(f"积分榜：车手 {r['name']} 没有译名，图上用英文")
    return {"kind": "drivers", "title": "车手积分榜", "subtitle": subtitle, "rows": top + extra,
            "gap_after": len(top) if extra else None, "credit": f"数据：{STANDINGS_SOURCE}"}, warns


def _zh_names(conn: sqlite3.Connection, text: str) -> str:
    """事实表里的英文车手/车队名换成译名。"""
    rows = conn.execute("SELECT original, approved, suggested FROM glossary WHERE kind IN ('车手','车队','大奖赛')")
    for r in sorted(rows, key=lambda r: -len(r["original"])):
        zh = r["approved"] or r["suggested"]
        if zh and r["original"] in text:
            text = text.replace(r["original"], zh)
    return text


def said(value: str, sentence: str) -> bool:
    """事实表的值在这句口播里原样出现（阿拉伯数字，或按配音稿读法转成的中文数字），前后不能接着别的数字。"""
    v = facts._norm(value or "")
    plain = facts._norm(facts.strip_tags(sentence))
    if not v:
        return False
    if re.search(rf"(?<![\d.]){re.escape(v)}(?!\.?\d)", plain):
        return True

    def quiet(msg):
        return None
    spoken_v = narration._numbers(v, quiet)
    if not re.search(f"[{_CN_NUM}]", spoken_v):
        return False
    spoken = narration._numbers(plain, quiet)
    alts = {spoken_v, "两"} if spoken_v == "二" else {spoken_v}
    return any(re.search(rf"(?<![{_CN_NUM}]){re.escape(a)}(?![{_CN_NUM}])", spoken) for a in alts)


def _pos_said(n: int, sentence: str) -> bool | None:
    """名次事实：口播说了“第 n 名”/Pn → True；说了别的名次 → False；没说名次 → None（这句不拿来做卡）。"""
    plain = facts._norm(facts.strip_tags(sentence))
    hits = _POS_SAID.findall(plain)
    if not hits:
        return None
    want = {str(n), narration.num_int(n), narration.num_int(n).replace("二", "两")}
    return any(a in want or b == str(n) for a, b in hits)


def fact_row(conn: sqlite3.Connection, f: dict) -> dict | None:
    """事实 → 数据卡的一行 {"label", "value", "num", "unit"}；不是数据的事实返回 None。"""
    c = _BJ_PAREN.sub("", f["content"]).strip()
    if m := _POS_FACT.match(c):
        return {"label": f"{m[1].removeprefix('本站')}第 {int(m[2])} 名", "value": _zh_names(conn, m[3]),
                "num": None, "unit": "", "pos": int(m[2])}
    if f["value"] is None or not re.search(r"\d", f["value"]):
        return None
    unit = ""
    if m := _UNIT_PAREN.search(c):
        unit, c = m[1], c[:m.start()]
    label = _zh_names(conn, c.removeprefix("本站")).strip("：: ")
    if not unit and (m := re.search(r"(圈|次|站)数$", label)):         # “完成圈数 55” → “55 圈”
        unit = m[1]
    value = f["value"].strip()
    try:
        num = float(value)
    except ValueError:
        num = None
    shown = value + (unit if unit in ("%", "°C") else f" {unit}" if unit else "")
    return {"label": label, "value": shown, "num": num, "unit": unit}


def is_data_para(text: str) -> bool:
    """段内有一句同时带数字和数据类词（秒、圈速、差距、名、圈、公里/时、次）。"""
    return any(facts.has_number(s) and any(w in s for w in DATA_WORDS) for s in facts._sentences(text))


def data_card(conn: sqlite3.Connection, line: str, title: str | None, subtitle: str | None,
              fact_map: dict) -> tuple[dict | None, list[str]]:
    """这段口播带 [F#] 标记、且数字和事实表一致的数据做成数据卡；有一个数字对不上整张不做。"""
    rows = []
    for sent in facts._sentences(line):
        if not facts.has_number(sent):
            continue
        tags = [t for body in facts.TAG_RE.findall(sent) for t in re.split(r"\s*[,，、]\s*", body)]
        for tag in tags:
            f = fact_map.get(tag)
            if f is None or _SESSION_FACT.match(f["content"]):
                continue
            row = fact_row(conn, f)
            if row is None:
                continue
            ok = _pos_said(row["pos"], sent) if "pos" in row else said(f["value"], sent)
            if ok is None:
                continue
            if not ok:
                return None, [f"数据卡：「{facts.strip_tags(sent).strip()[:30]}」里的数字和事实表 [{tag}]"
                              f"（{f['value'] or f['content']}）对不上，这段不做数据卡"]
            if not any(r["label"] == row["label"] and r["value"] == row["value"] for r in rows):
                rows.append({**row, "source": f["source"]})
    if len(rows) < DATA_MIN_ROWS:
        return None, []
    rows = rows[:DATA_MAX_ROWS]
    nums = [r["num"] for r in rows]
    units = {r["unit"] for r in rows}
    bars = all(n is not None and n >= 0 for n in nums) and len(units) == 1 and "" not in units and max(nums) > 0
    sources = list(dict.fromkeys(r["source"] for r in rows))
    return {"title": title or "本段数据", "subtitle": subtitle,
            "rows": [{"label": r["label"], "value": r["value"], "bar": r["num"] / max(nums) if bars else None}
                     for r in rows],
            "credit": "数据：" + "、".join(sources)}, []


def data_panels(conn: sqlite3.Connection, ep, ev: dict | None, script: str, units: list[dict], align: dict,
                out_dir: Path, used: set[int]) -> tuple[list[dict], list[str]]:
    """逐段找“讲积分”“讲数据”的段落，每段最多一张；跳过已放赛道图/赛程/轮胎面板的段；全片最多 MAX_DATA_PANELS 张。
    积分榜车手榜、车队榜各最多一张（同一张表放两次没意义），放在积分关键词命中最多的那段（同样多取靠前的），
    免得开场一句“看看积分形势”就把榜单抢过去。"""
    lines, heads = para_lines(script)
    total = _paras(units)
    if len(lines) != total:
        return [], [f"逐字稿分段和配音稿对不上（{len(lines)}/{total} 段），积分榜和数据卡没做"]
    fact_map = {f["tag"]: f for f in facts.items(conn, ep["id"], "F")}
    subtitle = (ev or {}).get("name_zh") or ep["gp_label"] or None
    cands, warns, made_kinds = [], [], set()
    standings, standings_sub = None, None                   # None 还没取；False 取不到
    best: dict[str, tuple[int, int]] = {}                    # 榜单种类 → (命中次数, 段号)
    for p, line in enumerate(lines):
        plain = facts.strip_tags(line)
        n = len(STANDINGS_RE.findall(plain))
        kind = "teams" if TEAM_STANDINGS_RE.search(plain) else "drivers"
        if n and p not in used and n > best.get(kind, (0, 0))[0]:
            best[kind] = (n, p)
    for p, (line, head) in enumerate(zip(lines, heads)):
        if p in used:
            continue
        plain = facts.strip_tags(line)
        hit = STANDINGS_RE.search(plain)
        kind = "teams" if TEAM_STANDINGS_RE.search(plain) else "drivers"
        want_table = bool(hit) and best.get(kind, (0, -1))[1] == p and kind not in made_kinds \
            and standings is not False
        want_card = is_data_para(plain)
        if not (want_table or want_card):
            continue
        if len(cands) >= MAX_DATA_PANELS:
            warns.append(f"积分榜和数据卡已到 {MAX_DATA_PANELS} 张上限，第 {p + 1} 段起讲积分、数据的段落不再加图")
            break
        made = None
        if want_table:
            if standings is None:
                found = standings_round(conn, ep, ev)
                if found is None:
                    standings = False
                    warns.append("积分榜：确定不了本期该用哪一站结束后的积分（赛季首站前或赛历里找不到），没做")
                else:
                    season, rnd, name = found
                    try:
                        standings = standings_fastf1(season, rnd)
                    except Exception as e:  # noqa: BLE001
                        standings = False
                        warns.append(f"积分榜：{season} 赛季第 {rnd} 站后的积分取不到，没做（{e}）")
                    else:
                        standings_sub = f"{season} 赛季 · 第 {rnd} 站{name or ''}后"
                        (out_dir / STANDINGS_JSON).write_text(json.dumps(standings, ensure_ascii=False, indent=1),
                                                              encoding="utf-8")
            if standings:
                table, w = standings_table(conn, standings, kind, plain + "\n" + _section_text(units, range(p, p + 1)),
                                           standings_sub)
                warns += w
                if table:
                    png = panels.render_standings(table, out_dir / STANDINGS_PNG[kind])
                    made = {"path": f"05_图形/{png.name}", "kind": "积分榜", "_name": table["title"],
                            "_how": f"关键词（第 {p + 1} 段「{hit[0]}」）"}
                    made_kinds.add(kind)
        if made is None and want_card:
            card, w = data_card(conn, line, head, subtitle, fact_map)
            warns += w
            if card:
                png = panels.render_datacard(card, out_dir / DATACARD_PNG.format(
                    n=sum(c["kind"] == "数据卡" for c in cands) + 1))
                made = {"path": f"05_图形/{png.name}", "kind": "数据卡", "_name": "数据卡",
                        "_how": f"数据（第 {p + 1} 段，{len(card['rows'])} 项，已和事实表核对）"}
        if made:
            try:
                start, end = panel_span(units, range(p, p + 1), align)
            except ValueError as e:
                warns.append(f"{made['_name']}（第 {p + 1} 段）对不上时间：{e}，没放进时间线")
                continue
            cands.append({**made, "start": start, "end": end, "_key": "data_panels"})
    return cands, warns
