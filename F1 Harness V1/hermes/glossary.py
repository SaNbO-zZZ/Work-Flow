"""译名表：车手、车队、大奖赛、赛道的中文译名，用户审核后才算定稿。"""
import inspect
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable

from . import codex_runner, config, db

KINDS = ("车手", "车队", "大奖赛", "赛道", "其他")

# 中国大陆 F1 媒体/车迷常用写法；拿不准的不放，交给 GPT 建议、用户审
BUILTIN: dict[str, dict[str, str]] = {
    "车手": {
        "Max Verstappen": "维斯塔潘",
        "Lewis Hamilton": "汉密尔顿",
        "Charles Leclerc": "勒克莱尔",
        "Lando Norris": "诺里斯",
        "Oscar Piastri": "皮亚斯特里",
        "George Russell": "拉塞尔",
        "Kimi Antonelli": "安东内利",
        "Fernando Alonso": "阿隆索",
        "Lance Stroll": "斯托尔",
        "Carlos Sainz": "塞恩斯",
        "Alexander Albon": "阿尔本",
        "Pierre Gasly": "加斯利",
        "Franco Colapinto": "科拉平托",
        "Esteban Ocon": "奥孔",
        "Oliver Bearman": "贝尔曼",
        "Liam Lawson": "劳森",
        "Nico Hulkenberg": "霍肯伯格",
        "Valtteri Bottas": "博塔斯",
        "Sergio Perez": "佩雷兹",
    },
    "车队": {
        "Ferrari": "法拉利",
        "Red Bull Racing": "红牛",
        "McLaren": "迈凯伦",
        "Mercedes": "梅赛德斯",
        "Aston Martin": "阿斯顿·马丁",
        "Williams": "威廉姆斯",
        "Alpine": "Alpine",
        "Haas F1 Team": "哈斯",
        "Audi": "奥迪",
        "Cadillac": "凯迪拉克",
    },
    "大奖赛": {
        "Australian Grand Prix": "澳大利亚大奖赛",
        "Chinese Grand Prix": "中国大奖赛",
        "Japanese Grand Prix": "日本大奖赛",
        "Bahrain Grand Prix": "巴林大奖赛",
        "Saudi Arabian Grand Prix": "沙特阿拉伯大奖赛",
        "Miami Grand Prix": "迈阿密大奖赛",
        "Canadian Grand Prix": "加拿大大奖赛",
        "Monaco Grand Prix": "摩纳哥大奖赛",
        "Barcelona Grand Prix": "巴塞罗那大奖赛",
        "Spanish Grand Prix": "西班牙大奖赛",
        "Austrian Grand Prix": "奥地利大奖赛",
        "British Grand Prix": "英国大奖赛",
        "Belgian Grand Prix": "比利时大奖赛",
        "Hungarian Grand Prix": "匈牙利大奖赛",
        "Dutch Grand Prix": "荷兰大奖赛",
        "Italian Grand Prix": "意大利大奖赛",
        "Azerbaijan Grand Prix": "阿塞拜疆大奖赛",
        "Singapore Grand Prix": "新加坡大奖赛",
        "United States Grand Prix": "美国大奖赛",
        "Mexico City Grand Prix": "墨西哥城大奖赛",
        "São Paulo Grand Prix": "圣保罗大奖赛",
        "Las Vegas Grand Prix": "拉斯维加斯大奖赛",
        "Qatar Grand Prix": "卡塔尔大奖赛",
        "Abu Dhabi Grand Prix": "阿布扎比大奖赛",
    },
    "赛道": {
        "Melbourne": "墨尔本",
        "Shanghai": "上海",
        "Suzuka": "铃鹿",
        "Montréal": "蒙特利尔",
        "Monte Carlo": "蒙特卡洛",
        "Barcelona": "巴塞罗那",
        "Silverstone": "银石",
        "Spa-Francorchamps": "斯帕",
        "Budapest": "布达佩斯",
        "Zandvoort": "赞德沃特",
        "Monza": "蒙扎",
        "Madrid": "马德里",
        "Baku": "巴库",
        "Kuala Lumpur": "雪邦",
        "Marina Bay": "滨海湾",
        "Austin": "奥斯汀",
        "Mexico City": "墨西哥城",
        "São Paulo": "圣保罗",
        "Las Vegas": "拉斯维加斯",
        "Lusail": "卢赛尔",
        "Yas Marina": "亚斯码头",
        "Jeddah": "吉达",
    },
}

SUGGEST_PROMPT = """你是中文 F1 自媒体的资料员。请把下面的 F1 专有名词译成**中国大陆 F1 媒体和车迷最常用的写法**。
规则：
- 车手只写姓氏的通行译名（如 Max Verstappen→维斯塔潘，Lewis Hamilton→汉密尔顿，Kimi Antonelli→安东内利）。
- 车队用大陆常用简称（如 Red Bull Racing→红牛，Haas F1 Team→哈斯）；大陆习惯直接写英文的就保留英文（如 Alpine→Alpine）。
- 大奖赛写成“××大奖赛”（如 Mexico City Grand Prix→墨西哥城大奖赛）。
- 赛道条目是 FastF1 的 Location（地名），给出车迷称呼这条赛道时最常用的中文（如 Spa-Francorchamps→斯帕，Kuala Lumpur→雪邦，Yas Marina→亚斯码头）。
- 不要音译生僻字，不要加括注，每个原名都必须给出一个译名。
只输出一个 JSON 对象，不要任何解释，格式：{"车手": {"原名": "译名"}, "车队": {...}, "大奖赛": {...}, "赛道": {...}, "其他": {...}}
待翻译：
"""


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"未知类别：{kind}")


def zh(conn: sqlite3.Connection, kind: str, original: str) -> str | None:
    """已审译名优先，其次建议译名；都没有返回 None。"""
    row = conn.execute("SELECT approved, suggested FROM glossary WHERE kind=? AND original=?", (kind, original)).fetchone()
    if not row:
        return None
    return row["approved"] or row["suggested"]


def upsert_originals(conn: sqlite3.Connection, kind: str, originals: Iterable[str], *, source_hint: str | None = None) -> int:
    """新原名以 pending 插入，建议取内置词典；已存在的不动。source_hint 记进 note（原名从哪来）。"""
    _check_kind(kind)
    names = dict.fromkeys(n.strip() for n in originals if n and n.strip())
    added = 0
    with db.transaction(conn):
        for name in names:
            hint = BUILTIN.get(kind, {}).get(name)
            cur = conn.execute(
                "INSERT OR IGNORE INTO glossary(kind, original, suggested, source, note, updated_at) VALUES (?,?,?,?,?,?)",
                (kind, name, hint, "内置词典" if hint else None, source_hint, db.now()))
            added += cur.rowcount
    return added


def load_originals_fastf1(season: int = 2026) -> dict[str, list[str]]:
    import fastf1
    import pandas as pd

    config.FASTF1_CACHE.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(config.FASTF1_CACHE))
    logging.getLogger("fastf1").setLevel(logging.WARNING)
    sched = fastf1.get_event_schedule(season, include_testing=False)
    out = {"大奖赛": list(dict.fromkeys(sched["EventName"])), "赛道": list(dict.fromkeys(sched["Location"])),
           "车手": [], "车队": []}
    # 最新一站可能还没出成绩，往前找到第一场有结果的正赛
    done = sched[sched["EventDate"] < pd.Timestamp.now()].sort_values("RoundNumber", ascending=False)
    for rnd in done["RoundNumber"]:
        try:
            s = fastf1.get_session(season, int(rnd), "R")
            s.load(laps=False, telemetry=False, weather=False, messages=False)
            res = s.results
        except Exception as e:
            logging.getLogger("hermes.glossary").warning("第 %s 站正赛成绩读取失败：%s", rnd, e)
            continue
        if res is None or res.empty:
            continue
        out["车手"] = list(dict.fromkeys(n for n in res["FullName"] if isinstance(n, str) and n.strip()))
        out["车队"] = list(dict.fromkeys(n for n in res["TeamName"] if isinstance(n, str) and n.strip()))
        break
    return out


def seed(conn: sqlite3.Connection, *, loader: Callable[[int], dict[str, list[str]]] = load_originals_fastf1,
         season: int = 2026) -> int:
    return sum(upsert_originals(conn, kind, names, source_hint=f"FastF1 {season}")
               for kind, names in loader(season).items())


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1]) if start >= 0 else None
    except json.JSONDecodeError as e:
        raise ValueError(f"GPT 输出不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("GPT 输出不是 JSON 对象")
    return data


async def suggest_missing(conn: sqlite3.Connection, *, ask: Callable[[str], object] | None = None) -> int:
    """没有建议译名的 pending 词条，一次性批量让 GPT 给建议。"""
    rows = conn.execute("SELECT id, kind, original FROM glossary WHERE state='pending' AND COALESCE(suggested,'')=''"
                        " ORDER BY kind, id").fetchall()
    if not rows:
        return 0
    todo: dict[str, list[str]] = {}
    for r in rows:
        todo.setdefault(r["kind"], []).append(r["original"])
    prompt = SUGGEST_PROMPT + json.dumps(todo, ensure_ascii=False, indent=1)
    if ask is None:
        async def ask(p: str) -> str:
            model, effort = config.ROLE_MODELS["资料"]
            result = await codex_runner.run(p, model, effort, config.CORE / "data" / "glossary-work",
                                            ephemeral=True, sandbox="read-only")
            codex_runner.record_usage(conn, result, model=model, effort=effort)
            return result.message
    reply = ask(prompt)
    if inspect.isawaitable(reply):
        reply = await reply
    data = _parse_json(reply)
    filled = 0
    with db.transaction(conn):
        for r in rows:
            group = data.get(r["kind"])
            text = group.get(r["original"]) if isinstance(group, dict) else None
            if not isinstance(text, str) or not text.strip():
                continue
            # 等 GPT 期间用户可能已审过，只填仍然空着的
            filled += conn.execute(
                "UPDATE glossary SET suggested=?, source='GPT', updated_at=? "
                "WHERE id=? AND state='pending' AND COALESCE(suggested,'')=''",
                (text.strip(), db.now(), r["id"])).rowcount
    return filled


def items(conn: sqlite3.Connection, *, state: str | None = None, kind: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM glossary WHERE 1=1", []
    if state:
        sql += " AND state=?"
        args.append(state)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    rows = conn.execute(sql + " ORDER BY state DESC, kind, id", args).fetchall()
    return [dict(r) for r in rows]


def _get(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM glossary WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise ValueError(f"词条不存在：{item_id}")
    return row


def set_translation(conn: sqlite3.Connection, item_id: int, text: str | None = None) -> None:
    """用户审核：不给 text 即采纳建议，给了就以用户写法定稿。"""
    row = _get(conn, item_id)
    text = (text or "").strip()
    source = "用户" if text else row["source"]
    text = text or (row["suggested"] or "").strip()
    if not text:
        raise ValueError(f"「{row['original']}」没有建议译名，需要手动填写")
    conn.execute("UPDATE glossary SET approved=?, state='approved', source=?, updated_at=? WHERE id=?",
                 (text, source, db.now(), item_id))


def reopen(conn: sqlite3.Connection, item_id: int) -> None:
    """撤回为待审；原定稿写法留作建议，避免用户改过的字丢失。"""
    _get(conn, item_id)
    conn.execute("UPDATE glossary SET suggested=COALESCE(approved, suggested), approved=NULL, state='pending', "
                 "updated_at=? WHERE id=?", (db.now(), item_id))


def add_manual(conn: sqlite3.Connection, kind: str, original: str, text: str) -> int:
    _check_kind(kind)
    original, text = original.strip(), text.strip()
    if not original or not text:
        raise ValueError("原名和译名都不能为空")
    return conn.execute(
        """INSERT INTO glossary(kind, original, suggested, approved, state, source, updated_at)
           VALUES (?,?,?,?, 'approved', '用户', ?)
           ON CONFLICT(kind, original) DO UPDATE SET approved=excluded.approved, state='approved',
               source='用户', updated_at=excluded.updated_at
           RETURNING id""",
        (kind, original, text, text, db.now())).fetchone()[0]


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM glossary WHERE state='pending'").fetchone()[0]


def unapproved_in(conn: sqlite3.Connection, text: str) -> list[dict]:
    """文本中出现的未审词条（原名忽略大小写，或建议译名出现即算），供 G1 标黄。"""
    low = text.lower()
    return [dict(r) for r in conn.execute("SELECT * FROM glossary WHERE state='pending' ORDER BY kind, id")
            if r["original"].lower() in low or (r["suggested"] and r["suggested"] in text)]
