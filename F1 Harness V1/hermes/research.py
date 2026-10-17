"""选题与资料（M6-1）：D1 资料与事实、D5 梗与出处、总控汇总选题。"""
import asyncio
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from . import calendar, codex_runner, facts, fia, prompts, weather
from .db import transaction
from .news import parse_json, valid_url

log = logging.getLogger(__name__)

TIMEOUT = 1800
OFFICIAL_HOSTS = ("formula1.com", "fia.com")
WEEKDAY = "一二三四五六日"

D1_INSTRUCTION = """你是资料员。为本期视频联网查资料，补充事实表里没有的内容。
1. 上下文里的事实表是程序从 FastF1、Open-Meteo 取到的数据，已经可靠，不要重复，也不要改。
2. 你要找的是非数字说法：车队和车手动态、赛道特点、规则变化、名场面、技术升级、合同传闻等。每条：
   - content：一句中文概括；传闻要写明“据某某报道”，不要写成定论；
   - source：来源名，如 The Race；
   - url：原文链接，必须是你打开过的页面；找不到链接的不要写；
   - excerpt：原文摘录，照抄原文语言，一两句。
3. 只用一线来源：F1 官网、FIA、车队官网、Motorsport.com、Autosport、The Race、BBC Sport、Sky Sports F1、RaceFans、Auto Motor und Sport、Formu1a.uno。
4. 说法里尽量不带数字。确实需要的数字（赛道长度、圈数、圈速纪录等）只能取自 formula1.com 或 fia.com：content 写这个数字是什么（如“雪邦赛道单圈长度（公里）”），number 只填数字本身（如 5.543）。其他来源的数字一律不要。没有数字时 number 填 null。
5. 找 8 到 20 条，宁缺毋滥。
6. summary：200 到 400 字中文资料摘要，写本期最值得讲的 3 到 5 个点，给写稿人看。
7. 只输出符合 schema 的 JSON。"""

D5_INSTRUCTION = """你负责找梗。围绕本期主题，联网找车迷常用的梗、网络说法、热词（B站、微博、虎扑、Reddit、X 等）。
每条：
- text：梗原文；
- meaning：意思和来历，一两句；
- url：能看到这个梗被车迷使用或解释的页面链接；
- verified：你亲自打开链接、确认意思和来历属实才填 true；只是印象、找不到出处、意思有争议的填 false；
- note：适合用在哪一段、怎么用（如“开头钩子”“讲某车手失误时”）；有冒犯、地域攻击、拿事故伤亡开玩笑风险的写明“不要用”，verified 填 false。
找 5 到 12 条，宁缺毋滥。没核实的梗写稿时不会用。只输出符合 schema 的 JSON。"""

TOPIC_INSTRUCTION = """你是总控。读上下文里的资料摘要、事实表和梗，定下本期怎么讲。
- angle：选题角度，一两句，说清这期最吸引人的点；
- titles：3 个标题方向；传闻不能用肯定语气；科普不写“第N期”；
- outline：按本分支规定的结构写大纲，每段写 part（段名）、points（要讲什么）、seconds（预计秒数）、tags（这一段要用的事实标签，如 F3、S2，只能用事实表里已有的）；
- seconds：全片预计时长，要落在本分支规定的范围里；
- memes：准备用的梗原文，只能从已核实的梗里选，可以为空；
- risks：需要用户注意的地方（传闻未证实、资料不足等），没有就写“无”。
只输出符合 schema 的 JSON。"""


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}


STR = {"type": "string"}
SOURCES_SCHEMA = _obj({
    "summary": STR,
    "items": {"type": "array", "items": _obj({
        "content": STR, "source": STR, "url": STR, "excerpt": STR, "number": {"type": ["string", "null"]}})},
})
MEMES_SCHEMA = _obj({
    "items": {"type": "array", "items": _obj({
        "text": STR, "meaning": STR, "url": STR, "verified": {"type": "boolean"}, "note": STR})},
})
TOPIC_SCHEMA = _obj({
    "angle": STR,
    "titles": {"type": "array", "items": STR},
    "outline": {"type": "array", "items": _obj({
        "part": STR, "points": STR, "seconds": {"type": "integer"}, "tags": {"type": "array", "items": STR}})},
    "seconds": {"type": "integer"},
    "memes": {"type": "array", "items": STR},
    "risks": STR,
})


# ---------- 公用 ----------

def lesson_notes(conn: sqlite3.Connection, branch: str | None, step: str | None = None) -> str:
    """已批准的笔记：分支为空（通用）或等于本期分支；给了 step 时只要适用步骤为空或等于 step 的。"""
    sql = "SELECT content FROM lessons WHERE state='approved' AND (branch IS NULL OR branch='' OR branch=?)"
    args = [branch or ""]
    if step is not None:
        sql += " AND (step IS NULL OR step='' OR step=?)"
        args.append(step)
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    return "\n".join(f"- {r[0]}" for r in rows)


def episode_context(ep) -> str:
    lines = [f"- 分支：{ep['branch']}"]
    for key, name in (("kind", "期型"), ("gp_label", "大奖赛/时期"), ("title", "工作标题"),
                      ("target_seconds", "目标时长（秒）"), ("ad_info", "广告信息")):
        if ep[key] not in (None, ""):
            lines.append(f"- {name}：{ep[key]}")
        elif key == "ad_info":
            lines.append("- 广告信息：无（本期没有登记广告，不留广告位）")
    return "## 本期信息\n" + "\n".join(lines)


def with_feedback(prompt: str, gates: str, feedback) -> str:
    if not feedback:
        return prompt
    return prompt + f"\n\n---\n\n# 用户在 {gates} 打回的意见（这一版必须改正）\n" + "\n".join(f"- {f}" for f in feedback)


def workdir(ep, sub: str, name: str, schema: dict) -> tuple[Path, Path]:
    d = Path(ep["folder"]) / sub / f"_{name}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.schema.json"
    path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    return d, path


async def ask(conn, ep, prompt: str, *, sub: str, name: str, schema: dict, model: str, effort: str,
              task_id=None, web_search: bool = False, runner=None) -> dict:
    d, schema_path = workdir(ep, sub, name, schema)
    run = runner or codex_runner.run
    result = await run(prompt, model, effort, d, sandbox="read-only", ephemeral=True,
                       output_schema=schema_path, web_search=web_search, timeout=TIMEOUT)
    codex_runner.record_usage(conn, result, model=model, effort=effort, episode_id=ep["id"], task_id=task_id)
    data = parse_json(result.message)
    if not isinstance(data, dict):
        raise ValueError(f"{name}：GPT 输出不是 JSON 对象")
    return data


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


# ---------- D1 程序取数 ----------

def find_event(conn, ep) -> dict | None:
    label = ep["gp_label"] or ""
    return calendar.find_event(conn, label) if label else None


def _results(season: int, rnd: int, session: str, top: int) -> list[dict]:
    from .graphics import _load_session
    res = _load_session(season, rnd, session, laps=False).results
    rows = []
    for _, x in res.head(top).iterrows():
        laps = x.get("Laps")
        rows.append({"pos": int(x["Position"]), "name": str(x["FullName"]), "team": str(x["TeamName"]),
                     "laps": int(laps) if laps == laps and laps is not None else None})
    return rows


def _schedule_round(season: int, location: str) -> int | None:
    try:
        import fastf1
        sched = fastf1.get_event_schedule(season, include_testing=False)
        hit = sched[sched["Location"] == location]
        return int(hit.iloc[0]["RoundNumber"]) if len(hit) else None
    except Exception as e:  # noqa: BLE001
        log.warning("读 %s 赛历失败：%s", season, e)
        return None


async def past_round(conn, season: int, location: str, *, years: int = 3, schedule_fn=None) -> tuple[int, int] | None:
    """往前找最近一次在同一地点办的分站（同名大奖赛可能换了赛道，按地点找）。"""
    for y in range(season - 1, season - 1 - years, -1):
        row = conn.execute("SELECT round FROM race_events WHERE season=? AND location=?", (y, location)).fetchone()
        if row:
            return y, row[0]
        rnd = await asyncio.to_thread(schedule_fn or _schedule_round, y, location)
        if rnd:
            return y, rnd
    return None


def schedule_facts(conn, episode_id: int, e: dict) -> list[str]:
    src = "FastF1 赛历"
    total = conn.execute("SELECT COUNT(*) FROM race_events WHERE season=?", (e["season"],)).fetchone()[0]
    tags = []
    with transaction(conn):
        add = lambda content, value: tags.append(facts._insert(conn, episode_id, "F", content, source=src, value=value))  # noqa: E731
        add(f"本站（{e['name_zh'] or e['name_en']}）所属赛季", str(e["season"]))
        add("本站是本赛季第几站", str(e["round"]))
        if total:
            add("本赛季总站数", str(total))
        add(f"本站赛制：{calendar.FORMAT_ZH.get(e['format'], e['format'] or '常规')}", None)
        for s in e["sessions"]:
            if not s["start_bj"]:
                continue
            dt = datetime.strptime(s["start_bj"], "%Y-%m-%d %H:%M")
            add(f"{calendar.SESSION_ZH.get(s['name'], s['name'])}开始时间（北京时间 {dt.month}月{dt.day}日 "
                f"周{WEEKDAY[dt.weekday()]}）", f"{dt:%H:%M}")
    return tags


def result_facts(conn, episode_id: int, season: int, rnd: int, label: str, rows: dict[str, list[dict]]) -> list[str]:
    src = f"FastF1 {season} 第 {rnd} 站成绩"
    tags = []
    with transaction(conn):
        for x in rows.get("Q", [])[:1]:
            tags.append(facts._insert(conn, episode_id, "F", f"{label}排位杆位：{x['name']}（{x['team']}）", source=src))
        race = rows.get("R", [])
        for x in race:
            tags.append(facts._insert(conn, episode_id, "F", f"{label}正赛第{x['pos']}名：{x['name']}（{x['team']}）",
                                      source=src))
        if race and race[0]["laps"]:
            tags.append(facts._insert(conn, episode_id, "F", f"{label}正赛冠军完成圈数", source=src,
                                      value=str(race[0]["laps"])))
    return tags


async def local_facts(conn, ep, *, results_fn=None, weather_fn=None, schedule_fn=None,
                      today: date | None = None) -> list[str]:
    """程序能拿到的数字先进事实表；任何一项失败只记 warning。"""
    warnings: list[str] = []
    e = find_event(conn, ep)
    if not e or ep["branch"] == "科普":
        return warnings
    results_fn = results_fn or _results
    try:
        schedule_facts(conn, ep["id"], e)
    except Exception as ex:  # noqa: BLE001
        warnings.append(f"赛程写入失败：{ex}")
    today = today or calendar._today()
    if ep["branch"] == "复盘" or date.fromisoformat(e["event_date"]) <= today:
        target, label = (e["season"], e["round"]), "本站"
    else:
        target = await past_round(conn, e["season"], e["location"], schedule_fn=schedule_fn)
        label = f"{target[0]}年本站" if target else ""
    if target:
        rows = {}
        for sess, top in (("Q", 1), ("R", 3)):
            try:
                rows[sess] = await asyncio.to_thread(results_fn, target[0], target[1], sess, top)
            except Exception as ex:  # noqa: BLE001
                warnings.append(f"FastF1 {target[0]} 第 {target[1]} 站 {sess} 成绩没取到：{ex}")
        result_facts(conn, ep["id"], target[0], target[1], label, rows)
    if ep["branch"] in ("前瞻", "复盘"):
        try:
            await (weather_fn or weather.add_weather_facts)(conn, ep["id"], e["season"], e["round"])
        except Exception as ex:  # noqa: BLE001
            warnings.append(f"天气没取到：{ex}")
    return warnings


def leads(conn) -> str:
    since = (datetime.now() - timedelta(days=3)).isoformat()
    news = conn.execute("SELECT title, source, url FROM news_items WHERE COALESCE(published_at, fetched_at) >= ? "
                        "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 20", (since,)).fetchall()
    docs = fia.recent(conn, 15)
    out = []
    if news:
        out.append("## 最近三天新闻线索（只是线索，要用请打开原文核实）\n" +
                   "\n".join(f"- {r['title']}（{r['source'] or ''}）{r['url']}" for r in news))
    if docs:
        out.append("## 最近的 FIA 文件\n" + "\n".join(f"- {d['title']}（{d['event'] or ''}）{d['url']}" for d in docs))
    return "\n\n".join(out)


def _official(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS)


def store_sources(conn, episode_id: int, items) -> dict:
    kept = {"F": 0, "S": 0, "dropped": []}
    with transaction(conn):
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            content, url, excerpt = _text(it.get("content")), _text(it.get("url")), _text(it.get("excerpt"))
            source, number = _text(it.get("source")) or (urlparse(url).hostname or ""), _text(it.get("number"))
            if not content or not valid_url(url) or not excerpt:
                kept["dropped"].append(f"缺链接或摘录：{content or url}")
                continue
            if number:
                if not _official(url):
                    kept["dropped"].append(f"数字不是来自 F1 官网/FIA：{content}")
                    continue
                facts._insert(conn, episode_id, "F", content, source=source, value=number, url=url, excerpt=excerpt)
                kept["F"] += 1
            else:
                facts._insert(conn, episode_id, "S", content, source=source, url=url, excerpt=excerpt)
                kept["S"] += 1
    return kept


async def collect(conn, ep, *, model: str, effort: str, task_id=None, feedback=None, runner=None,
                  results_fn=None, weather_fn=None, schedule_fn=None) -> dict:
    eid = ep["id"]
    d = Path(ep["folder"]) / "01_资料"
    d.mkdir(parents=True, exist_ok=True)
    # D1 重做时整表重建（此时还没有逐字稿引用这些标签）
    conn.execute("DELETE FROM facts WHERE episode_id=?", (eid,))
    warnings = await local_facts(conn, ep, results_fn=results_fn, weather_fn=weather_fn, schedule_fn=schedule_fn)
    ctx = "\n\n".join(x for x in (episode_context(ep), "## 事实表（程序已取）\n" + (facts.render(conn, eid) or "（空）"),
                                  leads(conn)) if x)
    prompt = with_feedback(prompts.assemble(ep["branch"], D1_INSTRUCTION, ctx,
                                            lesson_notes(conn, ep["branch"], "D1资料与事实")),
                           "G0", feedback)
    data = await ask(conn, ep, prompt, sub="01_资料", name="资料", schema=SOURCES_SCHEMA, model=model, effort=effort,
                     task_id=task_id, web_search=True, runner=runner)
    kept = store_sources(conn, eid, data.get("items"))
    summary = _text(data.get("summary"))
    (d / "资料.md").write_text(f"# 资料\n\n## 摘要\n\n{summary or '（GPT 没给摘要）'}\n\n## 事实表\n\n"
                              f"{facts.render(conn, eid) or '（空）'}\n", encoding="utf-8")
    total = {k: len(facts.items(conn, eid, k)) for k in ("F", "S")}
    return {"facts": total, "from_gpt": {"F": kept["F"], "S": kept["S"]}, "dropped": kept["dropped"],
            "warnings": warnings, "file": "01_资料/资料.md"}


# ---------- D5 梗 ----------

def clean_memes(items) -> list[dict]:
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not _text(it.get("text")):
            continue
        url, note = _text(it.get("url")), _text(it.get("note"))
        ok = it.get("verified") is True and valid_url(url)
        if not valid_url(url):
            note = (note + "；" if note else "") + "没有可用出处链接"
            url = ""
        out.append({"text": _text(it["text"]), "meaning": _text(it.get("meaning")), "url": url, "verified": ok,
                    "note": note if ok else ("未核实，不得使用" + ("；" + note if note else ""))})
    return out


async def memes(conn, ep, *, model: str, effort: str, task_id=None, feedback=None, runner=None) -> dict:
    d = Path(ep["folder"]) / "01_资料"
    d.mkdir(parents=True, exist_ok=True)
    prompt = with_feedback(prompts.assemble(ep["branch"], D5_INSTRUCTION, episode_context(ep),
                                            lesson_notes(conn, ep["branch"], "D5梗与出处")), "G0", feedback)
    data = await ask(conn, ep, prompt, sub="01_资料", name="梗", schema=MEMES_SCHEMA, model=model, effort=effort,
                     task_id=task_id, web_search=True, runner=runner)
    items = clean_memes(data.get("items"))
    (d / "梗.json").write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"items": len(items), "verified": sum(1 for i in items if i["verified"]), "file": "01_资料/梗.json"}


def load_memes(ep) -> list[dict]:
    p = Path(ep["folder"]) / "01_资料" / "梗.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError, AttributeError):
        return []


def memes_text(items: list[dict], *, verified_only: bool) -> str:
    rows = [m for m in items if m.get("verified") or not verified_only]
    return "\n".join(f"- {'' if m.get('verified') else '【未核实，不得使用】'}{m['text']}：{m.get('meaning', '')}"
                     f"（{m.get('note', '')}）{m.get('url', '')}" for m in rows)


# ---------- 总控汇总选题 ----------

def render_topic(data: dict, fact_rows: dict[str, dict]) -> str:
    seconds = data.get("seconds")
    lines = ["# 选题", "", "## 角度", "", _text(data.get("angle")), "", "## 标题方向", ""]
    lines += [f"{i}. {t}" for i, t in enumerate(data.get("titles") or [], 1)]
    lines += ["", f"## 结构大纲（预计 {seconds} 秒）" if seconds else "## 结构大纲", ""]
    used: list[str] = []
    for i, p in enumerate(data.get("outline") or [], 1):
        tags = [t for t in p.get("tags") or [] if t in fact_rows]
        used += [t for t in tags if t not in used]
        tag_s = "　用到：" + " ".join(f"[{t}]" for t in tags) if tags else ""
        lines.append(f"{i}. **{_text(p.get('part'))}**（约 {p.get('seconds')} 秒）：{_text(p.get('points'))}{tag_s}")
    lines += ["", "## 要用到的事实", ""]
    lines += [f"- [{t}] {fact_rows[t]['content']}" + (f"：{fact_rows[t]['value']}" if fact_rows[t]["value"] else "")
              for t in used] or ["（无）"]
    lines += ["", "## 准备用的梗", ""] + ([f"- {m}" for m in data.get("memes") or []] or ["（无）"])
    lines += ["", "## 需要注意", "", _text(data.get("risks")) or "无", ""]
    return "\n".join(lines)


async def topic(conn, ep, *, model: str, effort: str, task_id=None, feedback=None, runner=None) -> dict:
    eid = ep["id"]
    d = Path(ep["folder"]) / "01_资料"
    sources = (d / "资料.md").read_text(encoding="utf-8") if (d / "资料.md").exists() else "（没有资料.md）"
    meme_items = load_memes(ep)
    ctx = "\n\n".join([episode_context(ep), "## 资料.md\n" + sources,
                       "## 梗（只能用已核实的）\n" + (memes_text(meme_items, verified_only=False) or "（无）")])
    prompt = with_feedback(prompts.assemble(ep["branch"], TOPIC_INSTRUCTION, ctx,
                                            lesson_notes(conn, ep["branch"], "总控汇总选题")),
                           "G0", feedback)
    data = await ask(conn, ep, prompt, sub="01_资料", name="选题", schema=TOPIC_SCHEMA, model=model, effort=effort,
                     task_id=task_id, runner=runner)
    fact_rows = {f["tag"]: f for f in facts.items(conn, eid)}
    unknown = sorted({t for p in data.get("outline") or [] for t in p.get("tags") or [] if t not in fact_rows})
    ok_memes = {m["text"] for m in meme_items if m.get("verified")}
    bad_memes = [m for m in data.get("memes") or [] if m not in ok_memes]
    data["memes"] = [m for m in data.get("memes") or [] if m in ok_memes]
    md = render_topic(data, fact_rows)
    (d / "选题.md").write_text(md, encoding="utf-8")
    return {"file": "01_资料/选题.md", "markdown": md, "seconds": data.get("seconds"),
            "unknown_tags": unknown, "dropped_memes": bad_memes}
