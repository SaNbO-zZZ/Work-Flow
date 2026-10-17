"""FIA 官方文件（Decision Documents）检查：抓 F1 赛季文件页，入库，找出新文件。"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from . import db

CHAMPIONSHIP_URL = "https://www.fia.com/documents/championships/fia-formula-one-world-championship-14"
BASE = "https://www.fia.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
# 页面标注 "CET"，实测是巴黎当地时间（夏令时为 UTC+2，与 PDF 内赛道当地时间对得上）
PAGE_TZ = ZoneInfo("Europe/Paris")


@dataclass(frozen=True)
class Doc:
    url: str
    title: str
    event: str | None
    published_at: str | None


def season_url(html: str, season: int) -> str | None:
    pat = re.compile(rf"fia-formula-one-world-championship-14/season/season-{season}-\d+$")
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select("option[value], a[href]"):
        v = el.get("value") or el.get("href")
        if pat.search(v):
            return urljoin(BASE, v)
    return None


def _published(text: str) -> str | None:
    try:
        local = datetime.strptime(text.strip(), "%d.%m.%y %H:%M").replace(tzinfo=PAGE_TZ)
    except ValueError:
        return None
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(html: str) -> list[Doc]:
    soup = BeautifulSoup(html, "html.parser")
    docs, seen = [], set()
    for wrapper in soup.select(".decision-document-list ul.event-wrapper"):
        head = wrapper.select_one(".event-title")
        event = " ".join(head.get_text().split()) if head else None
        for row in wrapper.select("li.document-row"):
            a, title = row.find("a", href=True), row.select_one(".title")
            if not a or not title:
                continue
            url = urljoin(BASE, a["href"])
            if url in seen:
                continue
            seen.add(url)
            date = row.select_one(".date-display-single")
            docs.append(Doc(url, " ".join(title.get_text().split()), event,
                            _published(date.get_text()) if date else None))
    return docs


def _previous_event_url(html: str) -> str | None:
    """赛季页只渲染最新一站，上一站的迟发文件（如赛后判罚）要单独按分站筛选页抓。"""
    soup = BeautifulSoup(html, "html.parser")
    events = [" ".join(t.get_text().split()) for t in soup.select(".decision-document-list .event-title")]
    if len(events) < 2:
        return None
    for opt in soup.select("option[value*='/event/']"):
        if " ".join(opt.get_text().split()) == events[1]:
            return urljoin(BASE, opt["value"])
    return None


async def fetch(season: int, *, client: httpx.AsyncClient | None = None) -> list[Doc]:
    own = client is None
    client = client or httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    try:
        async def get(url: str) -> str:
            r = await client.get(url)
            r.raise_for_status()
            return r.text

        url = season_url(await get(CHAMPIONSHIP_URL), season)
        if not url:
            raise ValueError(f"FIA 文件页找不到 {season} 赛季")
        html = await get(url)
        docs = parse(html)
        if prev := _previous_event_url(html):
            docs += parse(await get(prev))
        seen = set()
        return [d for d in docs if not (d.url in seen or seen.add(d.url))]
    finally:
        if own:
            await client.aclose()


def store(conn, docs: list[Doc]) -> list[dict]:
    new = []
    with db.transaction(conn):
        baseline = conn.execute("SELECT 1 FROM fia_docs LIMIT 1").fetchone() is None
        at = db.now()
        for d in docs:
            cur = conn.execute("INSERT OR IGNORE INTO fia_docs(url, title, event, published_at, first_seen_at, notified) "
                               "VALUES (?, ?, ?, ?, ?, ?)", (d.url, d.title, d.event, d.published_at, at, int(baseline)))
            if cur.rowcount and not baseline:
                new.append({"id": cur.lastrowid, "url": d.url, "title": d.title,
                            "event": d.event, "published_at": d.published_at})
    return new


async def poll(conn, season: int, *, fetch_fn=None) -> list[dict]:
    return store(conn, await (fetch_fn or fetch)(season))


def mark_notified(conn, ids: list[int]) -> None:
    if ids:
        conn.execute(f"UPDATE fia_docs SET notified=1 WHERE id IN ({','.join('?' * len(ids))})", ids)


def recent(conn, limit=50) -> list[dict]:
    rows = conn.execute("SELECT * FROM fia_docs ORDER BY COALESCE(published_at, first_seen_at) DESC, id DESC LIMIT ?",
                        (limit,))
    return [dict(r) for r in rows]


def interval_seconds(period_kind: str, race_day: bool) -> int:
    if period_kind == "比赛周":
        return 3600 if race_day else 7200
    if period_kind in ("发车区", "夏休", "冬歇"):
        return 86400
    raise ValueError(f"未知时期：{period_kind}")


def due(last_checked_iso: str | None, now: datetime, period_kind: str, race_day: bool) -> bool:
    if not last_checked_iso:
        return True
    last = datetime.fromisoformat(last_checked_iso)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(seconds=interval_seconds(period_kind, race_day))
