"""定时任务：赛历每天刷新（变动通知、补译名）、FIA 文件按频率表检查、每天 08:00 新闻前三推荐、素材补处理、Drive 归档提醒与清理。"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import calendar, db, drive, episodes, fia, glossary, library, news
from .codex_runner import QuotaExhausted
from .events import broker, notify

log = logging.getLogger("hermes.jobs")
BEIJING = calendar.BEIJING
CALENDAR_HOUR, NEWS_HOUR, DRIVE_HOUR = 6, 8, 9
NEWS_MAX_TRIES, NEWS_RETRY_MINUTES = 3, 30
FIA_RETRY_MINUTES = 10
LIBRARY_MINUTES = 10
JOB_TIMEOUT = {"calendar": 600, "fia": 120, "news": 1800, "library": 3600, "drive": 7200}


def _get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))


def _bj(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso).astimezone(BEIJING) if iso else None


def calendar_due(conn, now: datetime) -> bool:
    last = _bj(_get(conn, "job.calendar.last"))
    bj = now.astimezone(BEIJING)
    return last is None or now - last > timedelta(hours=26) or (last.date() < bj.date() and bj.hour >= CALENDAR_HOUR)


def fia_due(conn, now: datetime) -> bool:
    retry = _get(conn, "job.fia.retry_after")
    if retry and now < datetime.fromisoformat(retry):
        return False
    today = now.astimezone(BEIJING).date()
    return fia.due(_get(conn, "job.fia.last"), now, calendar.period(conn, today).kind, calendar.is_race_day(conn, today))


def news_due(conn, now: datetime) -> bool:
    bj = now.astimezone(BEIJING)
    if bj.hour < NEWS_HOUR or _get(conn, "job.news.done") == bj.date().isoformat():
        return False
    tries = _get(conn, "job.news.tries") or ""
    day, _, n = tries.partition(":")
    if day == bj.date().isoformat():
        last = _bj(_get(conn, "job.news.last_try"))
        return int(n) < NEWS_MAX_TRIES and (last is None or now - last >= timedelta(minutes=NEWS_RETRY_MINUTES))
    return True


def run_calendar(conn_factory) -> list[str]:
    """在线程里跑（FastF1 是同步库），所以自己开连接。"""
    conn = conn_factory()
    try:
        changes = calendar.refresh(conn)
        added = glossary.seed(conn, season=datetime.now(BEIJING).year)
        _set(conn, "job.calendar.last", db.now())
        return changes + ([f"译名表新增 {added} 条待审"] if added else [])
    finally:
        conn.close()


async def job_calendar(conn, conn_factory) -> None:
    changes = await asyncio.to_thread(run_calendar, conn_factory)
    if conn.execute("SELECT 1 FROM glossary WHERE state='pending' AND suggested IS NULL LIMIT 1").fetchone():
        await glossary.suggest_missing(conn)
    if changes:
        notify("F1 Harness · 赛历有变动", "；".join(changes[:3]) + (f" 等 {len(changes)} 条" if len(changes) > 3 else ""))
    broker.publish("*", "data", {"kind": "calendar"})


async def job_fia(conn) -> list[dict]:
    new = await fia.poll(conn, datetime.now(BEIJING).year)
    _set(conn, "job.fia.last", db.now())
    if new:
        titles = [d["title"] for d in new]
        notify(f"F1 Harness · FIA 新文件 {len(new)} 份", "；".join(titles[:3]))
        fia.mark_notified(conn, [d["id"] for d in new])
        broker.publish("*", "data", {"kind": "fia"})
    return new


async def job_news(conn, now: datetime | None = None) -> int:
    """生成当天前三推荐，发到一个新的闲聊会话里；返回会话 id。"""
    day = (now or datetime.now(timezone.utc)).astimezone(BEIJING).date()
    picks = await news.daily(conn, day)
    eid = episodes.new_session(conn, f"{day.month}月{day.day}日 前三推荐")
    from .chat import add_message  # 避免循环导入
    add_message(conn, eid, "assistant", news.format_picks(picks, day))
    _set(conn, "job.news.done", day.isoformat())
    _set(conn, "job.news.session", str(eid))
    first = picks[0]["headline"] if picks else "今天没有值得推荐的新闻"
    notify("F1 Harness · 今日前三推荐", first, eid)
    broker.publish("*", "refresh", {"episode_id": eid})
    broker.publish("*", "data", {"kind": "news"})
    return eid


def library_due(conn, now: datetime) -> bool:
    last = _get(conn, "job.library.last")
    return last is None or now - datetime.fromisoformat(last) >= timedelta(minutes=LIBRARY_MINUTES)


def drive_due(conn, now: datetime) -> bool:
    last = _bj(_get(conn, "job.drive.last"))
    bj = now.astimezone(BEIJING)
    return bj.hour >= DRIVE_HOUR and (last is None or last.date() < bj.date())


async def job_library(conn) -> int:
    """补做重启、额度用完时没做完的素材指纹、向量、打标。"""
    _set(conn, "job.library.last", db.now())
    ids = library.pending(conn)
    for asset_id in ids:
        await library.process(conn, asset_id)
    return len(ids)


async def job_drive(conn) -> dict:
    now = datetime.now(timezone.utc)
    from .chat import add_message  # 避免循环导入
    reminded = drive.due_reminders(conn, now)
    for r in reminded:
        day = datetime.fromisoformat(r["delete_after"]).astimezone(BEIJING)
        text = f"本期已在 Drive 核对完毕，本地文件将在 {day.month}月{day.day}日 {day:%H:%M} 删除；要保留就点\"再留 7 天\"。"
        add_message(conn, r["episode_id"], "system", text)
        notify("F1 Harness · 本地文件明天清理", f"{r['code']}：{text}", r["episode_id"])
        drive.mark_reminded(conn, r["episode_id"])
    cleaned = await drive.cleanup(conn, now)
    for c in cleaned:
        if c["error"]:
            log.warning("清理 %s 失败：%s", c["code"], c["error"])
    _set(conn, "job.drive.last", db.now())
    if reminded or cleaned:
        broker.publish("*", "refresh", {})
    return {"reminded": len(reminded), "cleaned": sum(c["deleted"] for c in cleaned)}


class Jobs:
    def __init__(self, conn, conn_factory):
        self.conn, self.conn_factory = conn, conn_factory
        self.running: dict[str, asyncio.Task] = {}

    def trigger(self, name: str) -> bool:
        """网页"立即执行"；同一任务正在跑就不重复开。"""
        if name in self.running and not self.running[name].done():
            return False
        self.running[name] = asyncio.create_task(self._guarded(name))
        return True

    async def _guarded(self, name: str) -> None:
        try:
            async with asyncio.timeout(JOB_TIMEOUT[name]):
                if name == "calendar":
                    await job_calendar(self.conn, self.conn_factory)
                elif name == "fia":
                    await job_fia(self.conn)
                elif name == "news":
                    await self._news()
                elif name == "library":
                    await job_library(self.conn)
                elif name == "drive":
                    await job_drive(self.conn)
        except QuotaExhausted:
            log.warning("%s：额度用完，稍后再试", name)
        except Exception:
            log.exception("定时任务 %s 失败", name)
            if name == "fia":
                # 失败后 10 分钟再试，避免网络故障时每分钟狂刷 FIA
                _set(self.conn, "job.fia.retry_after",
                     (datetime.now(timezone.utc) + timedelta(minutes=FIA_RETRY_MINUTES)).isoformat(timespec="seconds"))

    async def _news(self) -> None:
        bj = datetime.now(BEIJING)
        day, _, n = (_get(self.conn, "job.news.tries") or "").partition(":")
        tries = int(n) + 1 if day == bj.date().isoformat() else 1
        _set(self.conn, "job.news.tries", f"{bj.date().isoformat()}:{tries}")
        _set(self.conn, "job.news.last_try", db.now())
        await job_news(self.conn)

    async def run_forever(self) -> None:
        checks = {"calendar": calendar_due, "fia": fia_due, "news": news_due, "library": library_due, "drive": drive_due}
        while True:
            now = datetime.now(timezone.utc)
            for name, due in checks.items():
                try:
                    if due(self.conn, now):
                        self.trigger(name)
                except Exception:
                    log.exception("检查任务 %s 是否到期失败", name)
            await asyncio.sleep(60)
