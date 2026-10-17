"""用户链接下载：yt-dlp 下到素材库 _incoming/<下载id>/，完成后交给素材入库。"""
import asyncio
import inspect
import json
import logging
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from . import bilibili, config, social_login
from .db import now, transaction

log = logging.getLogger("hermes.download")

FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"
FFMPEG_DIR = "/opt/homebrew/bin"
RETRY_DELAYS = (30, 120)
IDLE_SECONDS = 5
ERROR_TAIL = 2000
SKIP_SUFFIXES = {".json", ".part", ".ytdl", ".temp", ".vtt", ".srt", ".ass", ".jpg", ".jpeg", ".png", ".webp"}
PLATFORMS = [("bili", "B站"), ("youtube", "YouTube"), ("douyin", "抖音"), ("tiktok", "TikTok"),
             ("twitter", "X"), ("weibo", "微博"), ("ixigua", "西瓜视频"), ("kuaishou", "快手"),
             ("instagram", "Instagram"), ("vimeo", "Vimeo")]

Runner = Callable[[list[str], Callable[[str], None]], Awaitable[int]]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PERCENT = re.compile(r"^\[download\]\s+(\d+(?:\.\d+)?)%")
_FORMATS = re.compile(r"Downloading \d+ format\(s\): (\S+)")
_PART_START = re.compile(r"^\[download\] (Destination: |.* has already been downloaded)")

F1_ACCOUNT = "6057949432001"   # F1 官网在 Brightcove 上的账号
F1_UPLOADER = "FORMULA 1"
F1_BRIGHTCOVE_MAX = 13          # 更长的数字 id 是新版播放器，yt-dlp 不支持
_F1_NEW = re.compile(r"^/[a-z]{2}(?:-[a-z]{2})?/video/([^/.]+)\.(\d+)/?$", re.I)
_F1_OLD = re.compile(r"^/[a-z]{2}(?:-[a-z]{2})?/latest/video\.([^/.]+)\.(\d+)\.html$", re.I)


class DownloadError(RuntimeError):
    pass


def _f1_host(url: str | None) -> bool:
    try:
        return (urlsplit((url or "").strip()).hostname or "").lower() in ("formula1.com", "www.formula1.com")
    except ValueError:
        return False


def _f1_match(url: str | None):
    if not _f1_host(url):
        return None, None
    path = urlsplit(url.strip()).path
    if m := _F1_NEW.match(path):
        return "new", m
    if m := _F1_OLD.match(path):
        return "old", m
    return None, None


def f1_video_id(url: str | None) -> str | None:
    """F1 官网单个视频页（新旧两种格式）的数字视频号，其他链接返回 None。"""
    _, m = _f1_match(url)
    return m.group(2) if m else None


def ytdlp_url(url: str) -> str:
    """交给 yt-dlp 的链接：F1 官网新版视频页改写成它认的旧格式，其他原样返回。"""
    kind, m = _f1_match(url)
    if kind == "new" and len(m.group(2)) <= F1_BRIGHTCOVE_MAX:
        return f"https://www.formula1.com/en/latest/video.{m.group(1)}.{m.group(2)}.html"
    return url


def is_f1(info: dict, url: str | None = None) -> bool:
    return (any(_f1_host(u) for u in (url, info.get("original_url"), info.get("webpage_url")))
            or str(info.get("uploader_id") or "") == F1_ACCOUNT)


def uploader_name(info: dict, url: str | None = None) -> str | None:
    if is_f1(info, url):
        return F1_UPLOADER
    return info.get("uploader") or info.get("channel")


def _valid(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = parts.hostname or ""
    return parts.scheme in ("http", "https") and "." in host.strip(".")


def enqueue(conn: sqlite3.Connection, url: str, episode_id: int | None = None) -> int:
    url = (url or "").strip()
    if not _valid(url):
        raise ValueError(f"不是有效的 http/https 链接：{url!r}")
    with transaction(conn):
        row = conn.execute("SELECT id FROM downloads WHERE url=? AND state IN ('queued','running') ORDER BY id LIMIT 1",
                           (url,)).fetchone()
        if row:
            return row["id"]
        return conn.execute("INSERT INTO downloads(url, episode_id, created_at) VALUES (?,?,?)",
                            (url, episode_id, now())).lastrowid


def items(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,))]


class Progress:
    """把 yt-dlp 输出折算成 0–1；视频+音频分开下载时按分段数平均。"""

    def __init__(self):
        self.parts, self.done, self.current, self.lines = 1, -1, 0.0, 0

    def feed(self, line: str) -> float | None:
        line = _ANSI.sub("", line).strip()
        if m := _FORMATS.search(line):
            self.parts = m.group(1).count("+") + 1
            return None
        if _PART_START.match(line):
            self.done, self.current = self.done + 1, 0.0
            if "has already been downloaded" in line:
                self.current = 1.0
        elif m := _PERCENT.match(line):
            self.done = max(self.done, 0)
            self.current = float(m.group(1)) / 100
        else:
            return None
        self.lines += 1
        return min(0.99, (max(self.done, 0) + self.current) / self.parts)


async def default_runner(args: list[str], on_line: Callable[[str], None]) -> int:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, limit=1 << 20)
    try:
        async for raw in proc.stdout:
            on_line(raw.decode("utf-8", "replace").rstrip())
        return await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def ytdlp_args(url: str, out_dir: Path) -> list[str]:
    args = [str(config.YTDLP), "--no-playlist", "--newline", "--color", "no_color", "-f", FORMAT,
            "--merge-output-format", "mp4", "--write-info-json", "-P", str(out_dir), "-o", "%(id)s.%(ext)s"]
    # 后台服务的 PATH 里通常没有 homebrew，显式指定 ffmpeg 才能合并音视频
    if Path(FFMPEG_DIR, "ffmpeg").exists():
        args += ["--ffmpeg-location", FFMPEG_DIR]
    # B 站、X、Instagram 用户自己登录过的，就带上 cookie
    if cookie := bilibili.cookie_file_for(url) or social_login.cookie_file_for(url):
        args += ["--cookies", str(cookie)]
    return args + ["--", ytdlp_url(url)]


def credit_text(info: dict, url: str | None = None) -> str:
    if is_f1(info, url):
        from .assets import site_name
        return f"来源：{site_name('https://www.formula1.com/')} @{F1_UPLOADER}"
    key = f"{info.get('extractor_key') or ''} {info.get('extractor') or ''}".lower()
    platform = next((name for k, name in PLATFORMS if k in key), None)
    if platform is None:
        name = (info.get("extractor_key") or info.get("extractor") or "").split(":")[0]
        host = urlsplit(info.get("webpage_url") or "").hostname
        # 通用提取器的名字对观众没意义，改用网站域名
        platform = (host if name.lower() == "generic" else name) or host or "网络"
    who = info.get("uploader") or info.get("channel") or info.get("uploader_id")
    return f"来源：{platform} @{who}" if who else f"来源：{platform}"


def _find_outputs(out_dir: Path) -> tuple[Path, dict]:
    # 移动硬盘（exFAT）上会有 ._ 开头的影子文件，内容不是 JSON
    infos = sorted(p for p in out_dir.glob("*.info.json") if not p.name.startswith("."))
    videos = [p for p in out_dir.iterdir() if p.is_file() and p.suffix.lower() not in SKIP_SUFFIXES
              and not p.name.startswith(".")]
    if not videos:
        raise DownloadError("yt-dlp 退出码为 0，但没有找到下载的视频文件")
    video = max(videos, key=lambda p: p.stat().st_size)
    info = json.loads(infos[0].read_text()) if infos else {}
    return video, info


async def run_one(conn: sqlite3.Connection, download_id: int, *, runner: Runner | None = None,
                  ingest=None) -> int:
    """下载一条并入库，返回 asset_id；失败时标 failed 并抛 DownloadError。"""
    row = conn.execute("SELECT * FROM downloads WHERE id=?", (download_id,)).fetchone()
    if row is None:
        raise ValueError(f"没有下载记录 {download_id}")
    url, runner = row["url"], runner or default_runner
    if ingest is None:
        from .assets import ingest
    conn.execute("UPDATE downloads SET state='running', progress=0, error=NULL, finished_at=NULL WHERE id=?",
                 (download_id,))
    out_dir = config.LIBRARY_DIR / "_incoming" / str(download_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    progress, tail, last = Progress(), [], [0.0]

    def on_line(line: str) -> None:
        tail.append(line)
        del tail[:-200]
        p = progress.feed(line)
        if p is not None and p - last[0] >= 0.01:
            last[0] = p
            conn.execute("UPDATE downloads SET progress=? WHERE id=?", (round(p, 3), download_id))

    try:
        code = await runner(ytdlp_args(url, out_dir), on_line)
        if code != 0:
            raise DownloadError(f"yt-dlp 退出码 {code}")
        video, info = _find_outputs(out_dir)
        result = ingest(conn, video, source_type="用户链接", source_url=url, title=info.get("title"),
                        uploader=uploader_name(info, url), credit=credit_text(info, url))
        asset_id = await result if inspect.isawaitable(result) else result
    except Exception as e:
        err = f"{e}\n" + "\n".join(tail)
        conn.execute("UPDATE downloads SET state='failed', error=?, finished_at=? WHERE id=?",
                     (err[-ERROR_TAIL:], now(), download_id))
        if isinstance(e, DownloadError):
            raise
        raise DownloadError(str(e)) from e
    conn.execute("UPDATE downloads SET state='done', progress=1, asset_id=?, finished_at=? WHERE id=?",
                 (asset_id, now(), download_id))
    _cleanup(conn, out_dir, asset_id)
    return asset_id


def _cleanup(conn: sqlite3.Connection, out_dir: Path, asset_id: int) -> None:
    # 入库若只登记了路径而没搬走原片，就保留 _incoming，免得删掉素材本体
    row = conn.execute("SELECT local_path FROM assets WHERE id=?", (asset_id,)).fetchone()
    local = Path(row["local_path"]).resolve() if row and row["local_path"] else None
    if local is None or not local.is_relative_to(out_dir.resolve()):
        shutil.rmtree(out_dir, ignore_errors=True)


async def _sleep(stop: asyncio.Event | None, seconds: float) -> bool:
    """睡 seconds 秒；期间收到 stop 返回 True。"""
    if stop is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(stop.wait(), seconds)
        return True
    except TimeoutError:
        return False


async def worker(conn: sqlite3.Connection, *, stop: asyncio.Event | None = None, runner: Runner | None = None,
                 ingest=None, retry_delays: tuple[float, ...] = RETRY_DELAYS,
                 idle_seconds: float = IDLE_SECONDS) -> None:
    """按 id 顺序逐条下载；每条共尝试 len(retry_delays)+1 次。"""
    # 上次进程中途退出留下的 running 重新排队
    conn.execute("UPDATE downloads SET state='queued' WHERE state='running'")
    while not (stop and stop.is_set()):
        row = conn.execute("SELECT id FROM downloads WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            if await _sleep(stop, idle_seconds):
                return
            continue
        for attempt, delay in enumerate((*retry_delays, None), 1):
            try:
                await run_one(conn, row["id"], runner=runner, ingest=ingest)
                break
            except DownloadError as e:
                log.warning("下载 %s 第 %d 次失败：%s", row["id"], attempt, str(e).splitlines()[0])
                if delay is None:
                    break
                conn.execute("UPDATE downloads SET state='queued' WHERE id=?", (row["id"],))
                if await _sleep(stop, delay):
                    return
