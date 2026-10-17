"""新闻来源与每日 08:00 前三推荐：用户脚本优先，不可用时由 GPT 联网抓取；总控模型挑前三并建议分支。"""
import inspect
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx

from . import codex_runner, config
from .codex_runner import CodexResult, QuotaExhausted
from .db import now, transaction

log = logging.getLogger("hermes.news")

BEIJING = ZoneInfo("Asia/Shanghai")
BRANCHES = ("资讯", "前瞻", "复盘", "科普")
WORKDIR = config.CORE / "data" / "news-work"
PICK_WINDOW_HOURS = 36
MAX_CANDIDATES = 80
GPT_TIMEOUT_SECONDS = 1200

SOURCES = [
    ("Motorsport.com", "motorsport.com"),
    ("Autosport", "autosport.com"),
    ("The Race", "the-race.com"),
    ("BBC Sport F1（Andrew Benson）", "bbc.com/sport/formula1"),
    ("Sky Sports F1", "skysports.com/f1"),
    ("Auto Motor und Sport", "auto-motor-und-sport.de"),
    ("Formu1a.uno", "formu1a.uno"),
    ("RaceFans", "racefans.net"),
    ("FIA", "fia.com"),
    ("Formula 1 官网", "formula1.com"),
]

TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src",
                   "cmpid", "icid", "ocid", "_ga", "spm"}

Runner = Callable[[str], "str | Awaitable[str]"]


@dataclass
class NewsItem:
    title: str
    url: str
    source: str | None = None
    published_at: str | None = None
    summary: str | None = None


class Source(Protocol):
    name: str

    async def fetch(self, since: datetime) -> list[NewsItem]: ...


def valid_url(u: str) -> bool:
    try:
        parts = urlsplit(u.strip())
    except (ValueError, AttributeError):
        return False
    host = parts.hostname or ""
    return parts.scheme in ("http", "https") and "." in host and not host.startswith(".") and not host.endswith(".")


def normalize_url(u: str) -> str:
    parts = urlsplit(u.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    netloc = parts.netloc.lower()
    if parts.port in (80, 443):
        netloc = netloc.rsplit(":", 1)[0]
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), urlencode(query), ""))


def store(conn: sqlite3.Connection, items: list[NewsItem], via: str) -> int:
    added = 0
    with transaction(conn):
        for it in items:
            title = (it.title or "").strip()
            if not title or not valid_url(it.url or ""):
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO news_items(url, title, source, published_at, summary, via, fetched_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (normalize_url(it.url), title, it.source, it.published_at, it.summary, via, now()))
            added += cur.rowcount
    return added


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _to_item(d: dict) -> NewsItem:
    def s(k):
        v = d.get(k)
        return str(v).strip() if v not in (None, "") else None
    return NewsItem(title=s("title") or "", url=s("url") or "", source=s("source"),
                    published_at=s("published_at"), summary=s("summary"))


def parse_json(text: str):
    """模型输出可能被 ``` 围栏包裹；解析失败抛 ValueError。"""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return json.loads(m.group(1) if m else text.strip())


async def _call(runner: Runner, prompt: str) -> str:
    out = runner(prompt)
    return await out if inspect.isawaitable(out) else out


class ScriptSource:
    """预留：用户脚本的 HTTP 接口。GET {base_url}/news?since=<ISO8601 UTC> → [{title,url,source,published_at,summary}]"""
    name = "script"

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None):
        self.base_url = (base_url or "").strip()
        self.client = client

    @classmethod
    def from_settings(cls, conn: sqlite3.Connection) -> "ScriptSource | None":
        row = conn.execute("SELECT value FROM settings WHERE key='news.script_url'").fetchone()
        return cls(row[0]) if row and row[0].strip() else None

    def available(self) -> bool:
        return bool(self.base_url)

    async def fetch(self, since: datetime) -> list[NewsItem]:
        url, params = self.base_url.rstrip("/") + "/news", {"since": _utc_iso(since)}
        if self.client:
            r = await self.client.get(url, params=params)
        else:
            # 脚本在本机，不能让系统代理截走回环地址
            async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
                r = await c.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("新闻脚本应返回 JSON 数组")
        return [_to_item(d) for d in data if isinstance(d, dict)]


FETCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "url", "source", "published_at", "summary"],
        "properties": {
            "title": {"type": "string"},
            "url": {"type": "string"},
            "source": {"type": "string"},
            "published_at": {"type": ["string", "null"]},
            "summary": {"type": "string"},
        },
    }}},
}

PICK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["picks"],
    "properties": {"picks": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["news_id", "branch", "headline", "reason"],
        "properties": {
            "news_id": {"type": "integer"},
            "branch": {"type": "string", "enum": list(BRANCHES)},
            "headline": {"type": "string"},
            "reason": {"type": "string"},
        },
    }}},
}


def _schema_file(name: str, schema: dict) -> Path:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    path = WORKDIR / f"{name}.schema.json"
    path.write_text(json.dumps(schema, ensure_ascii=False))
    return path


async def run_with_search(prompt: str, model: str, effort: str, workdir: Path, *,
                          output_schema: Path | None = None, timeout: float = GPT_TIMEOUT_SECONDS) -> CodexResult:
    return await codex_runner.run(prompt, model, effort, workdir, sandbox="read-only", ephemeral=True,
                                  web_search=True, output_schema=output_schema, timeout=timeout)


def fetch_prompt(since: datetime) -> str:
    src = "\n".join(f"- {n}（{d}）" for n, d in SOURCES)
    return f"""你是 F1 中文自媒体的资料员。请用网页搜索，找出 {since.astimezone(BEIJING):%Y-%m-%d %H:%M}（北京时间，即 {_utc_iso(since)}）之后发布的 F1 新闻。

只用以下来源（零预算，不要付费墙内容）：
{src}
X/Twitter 上的记者只能用于二次核实，不能作为来源条目。

要求：
1. 每条必须是具体文章页的原文链接，而且必须是你在本次搜索结果里实际看到或打开过的 URL；禁止根据标题拼接或猜测链接，拿不准就不要列。
2. 不要首页、栏目页、标签页、直播页、视频列表页。
3. 同一事件多家报道时，保留信息最全的 1–2 家。
4. 最多 30 条，按重要性排序；过去 24 小时确实没有的就少列，不要凑数。
5. title 用原文标题；source 用上面列出的来源名（不含括号部分）；published_at 用 ISO8601 带时区，不确定填 null；summary 用中文一句话概括（不超过 60 字）。

只输出 JSON：{{"items": [{{"title": "...", "url": "https://...", "source": "...", "published_at": "...", "summary": "..."}}]}}"""


class GPTSource:
    """GPT 联网抓过去 24 小时 F1 新闻；runner(prompt)->str 供测试注入。"""
    name = "gpt"

    def __init__(self, conn: sqlite3.Connection, *, runner: Runner | None = None):
        self.conn = conn
        self.runner = runner or self._codex

    async def _codex(self, prompt: str) -> str:
        model, effort = config.ROLE_MODELS["新闻抓取"]
        result = await run_with_search(prompt, model, effort, WORKDIR,
                                       output_schema=_schema_file("news-fetch", FETCH_SCHEMA))
        codex_runner.record_usage(self.conn, result, model=model, effort=effort)
        return result.message

    async def fetch(self, since: datetime) -> list[NewsItem]:
        data = parse_json(await _call(self.runner, fetch_prompt(since)))
        rows = data.get("items", []) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("GPT 新闻输出不是数组")
        items = [_to_item(d) for d in rows if isinstance(d, dict)]
        return [it for it in items if it.title and valid_url(it.url)]


async def collect(conn: sqlite3.Connection, since: datetime, *, sources: list[Source] | None = None) -> dict:
    """按顺序尝试来源，第一个成功的入库即返回；额度用完直接上抛。"""
    if sources is None:
        script = ScriptSource.from_settings(conn)
        sources = [script, GPTSource(conn)] if script else [GPTSource(conn)]
    errors = []
    for src in sources:
        if not getattr(src, "available", lambda: True)():
            continue
        try:
            items = await src.fetch(since)
        except QuotaExhausted:
            raise
        except Exception as e:  # noqa: BLE001 — 任何来源故障都退回下一个来源
            log.warning("新闻来源 %s 失败：%s", src.name, e)
            errors.append(f"{src.name}: {e}")
            continue
        return {"via": src.name, "new": store(conn, items, src.name), "error": "; ".join(errors) or None}
    return {"via": None, "new": 0, "error": "; ".join(errors) or "没有可用的新闻来源"}


def pick_prompt(rows: list[sqlite3.Row], day: date) -> str:
    lines = "\n".join(
        f"[{r['id']}] {r['source'] or '未知来源'} | {r['published_at'] or '时间未知'} | {r['title']}"
        + (f" | {r['summary'][:200]}" if r["summary"] else "")
        for r in rows)
    return f"""你是 F1 中文自媒体（B 站/抖音）的总控。今天是北京时间 {day:%Y-%m-%d}，下面是最近 {PICK_WINDOW_HOURS} 小时入库的新闻（方括号内是 news_id）。

请挑出最值得今天做成视频的前三条（不足三条值得做就少选），并为每条建议视频分支：
- 资讯：单条（一个事件讲透）或合集（几条相关新闻打包），在理由里写明单条还是合集；
- 前瞻：适合结合即将到来的分站做赛前前瞻；
- 复盘：适合结合刚结束的比赛做赛后复盘；
- 科普：适合借新闻讲规则/技术/历史。
挑选标准：车迷关注度与讨论度（车手市场、车辆升级、围场冲突优先）、信息是否确凿、能否撑起一期视频、与近期赛程的关联。同一事件只选一条。

headline 写中文一句话标题（口语化，不超过 30 字，传闻不能写成确定句）；reason 用一两句中文说明为什么选、为什么做成这个分支。

只输出 JSON：{{"picks": [{{"news_id": 0, "branch": "资讯", "headline": "...", "reason": "..."}}]}}

新闻列表：
{lines}"""


def _clean(v) -> str:
    return " ".join(str(v or "").split())


def validate_picks(data, valid_ids: set[int]) -> list[dict]:
    """规则：news_id 不在候选里、分支不在四分支、重复 news_id 的条目直接过滤；多于 3 条截断。"""
    rows = data.get("picks", []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("前三推荐输出不是数组")
    picks, seen = [], set()
    for p in rows:
        if not isinstance(p, dict):
            continue
        try:
            nid = int(p.get("news_id"))
        except (TypeError, ValueError):
            continue
        if nid not in valid_ids or nid in seen or p.get("branch") not in BRANCHES:
            continue
        seen.add(nid)
        picks.append({"news_id": nid, "branch": p["branch"], "headline": _clean(p.get("headline")),
                      "reason": _clean(p.get("reason"))})
    return picks[:3]


async def _pick_codex(conn: sqlite3.Connection, prompt: str) -> str:
    model, effort = config.ROLE_MODELS["总控"]
    result = await codex_runner.run(prompt, model, effort, WORKDIR, sandbox="read-only", ephemeral=True,
                                    output_schema=_schema_file("news-pick", PICK_SCHEMA))
    codex_runner.record_usage(conn, result, model=model, effort=effort)
    return result.message


async def pick_top3(conn: sqlite3.Connection, day: date, *, runner: Runner | None = None) -> list[dict]:
    """候选为最近 36 小时入库（按 fetched_at）的新闻；没有候选不调 GPT、不改当天记录。
    非法条目过滤；有候选却一条合法都没有、或输出不是 JSON 时抛 ValueError（交给调度器重试）。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=PICK_WINDOW_HOURS)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM news_items WHERE fetched_at >= ? ORDER BY fetched_at DESC, id DESC LIMIT ?",
        (cutoff, MAX_CANDIDATES)).fetchall()
    if not rows:
        return []
    prompt = pick_prompt(rows, day)
    text = await (_call(runner, prompt) if runner else _pick_codex(conn, prompt))
    picks = validate_picks(parse_json(text), {r["id"] for r in rows})
    if not picks:
        raise ValueError(f"前三推荐没有合法条目：{text[:500]}")
    with transaction(conn):
        conn.execute("DELETE FROM news_picks WHERE day=?", (day.isoformat(),))
        for rank, p in enumerate(picks, 1):
            conn.execute("INSERT INTO news_picks(day, rank, news_id, branch, headline, reason) VALUES (?,?,?,?,?,?)",
                         (day.isoformat(), rank, p["news_id"], p["branch"], p["headline"], p["reason"]))
    return picks_for(conn, day)


def picks_for(conn: sqlite3.Connection, day: date) -> list[dict]:
    rows = conn.execute(
        """SELECT p.rank, p.news_id, p.branch, p.headline, p.reason, n.title, n.url, n.source, n.published_at, n.summary
           FROM news_picks p JOIN news_items n ON n.id = p.news_id WHERE p.day=? ORDER BY p.rank""",
        (day.isoformat(),)).fetchall()
    return [{**dict(r), "headline": r["headline"] or r["title"]} for r in rows]


def daily_since(day: date) -> datetime:
    return datetime.combine(day - timedelta(days=1), time(8), BEIJING)


async def daily(conn: sqlite3.Connection, day: date, *, sources: list[Source] | None = None,
                runner: Runner | None = None) -> list[dict]:
    res = await collect(conn, daily_since(day), sources=sources)
    if res["error"]:
        log.warning("每日新闻抓取：via=%s 新增=%s 错误=%s", res["via"], res["new"], res["error"])
    return await pick_top3(conn, day, runner=runner)


def format_picks(picks: list[dict], day: date) -> str:
    head = f"{day.month}月{day.day}日 前三推荐"
    if not picks:
        return head + "\n今天没有值得推荐的新闻。"
    lines = [head]
    for i, p in enumerate(picks, 1):
        lines += [f"{p.get('rank', i)}. {p.get('headline') or p['title']}",
                  f"   建议分支：{p['branch']}",
                  f"   理由：{p.get('reason') or '—'}",
                  f"   来源：{p.get('source') or '未知来源'} {p['url']}"]
    return "\n".join(lines)
