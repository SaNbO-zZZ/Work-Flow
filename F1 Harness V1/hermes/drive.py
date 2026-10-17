"""Google Drive 归档：rclone 上传、逐文件核对、本地 7 天后清理、素材反查下载。

授权 scope 用 drive.file：rclone 只能看到/改动它自己创建的文件，碰不到用户 Drive 里的其他资料；
代价是用户在网页里手动放进 "F1 Hermes" 的文件 rclone 看不见，且换 OAuth client_id 后旧文件也看不见。
"""
import asyncio
import configparser
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from . import config, episodes
from .db import now as db_now, transaction

Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

AUTH_TIMEOUT = 600
KEEP_DAYS = 7
# 期文件夹里不归档的东西：macOS 影子文件、Codex 技能链接
# 09_版本（各关卡旧版）、AI 封面的过程图和旧封面只留本地，Drive 只放最终版（用户 2026-10-13 定）
FILTERS = ["--exclude", "._*", "--exclude", ".DS_Store", "--exclude", ".agents/**",
           "--exclude", "09_版本/**", "--exclude", "06_合成/AI封面/**", "--exclude", "06_合成/封面版本/**"]
# 符号链接（如指向素材库的原片）不跟随，素材库单独归档
COMMON = ["--skip-links", "--retries", "3", "--low-level-retries", "10"]

_auth_lock = asyncio.Lock()
_proxy_env: dict[str, str] | None = None


class DriveError(RuntimeError):
    pass


def _system_proxy() -> dict[str, str]:
    """rclone（Go）不读 macOS 系统代理，只认环境变量；国内直连 Google 不通，所以把系统代理转成环境变量。"""
    global _proxy_env
    if _proxy_env is None:
        _proxy_env = {}
        try:
            out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        kv = dict(line.strip().split(" : ", 1) for line in out.splitlines() if " : " in line)
        for kind in ("HTTPS", "HTTP"):
            if kv.get(f"{kind}Enable") == "1" and kv.get(f"{kind}Proxy"):
                url = f"http://{kv[f'{kind}Proxy']}:{kv.get(f'{kind}Port', '80')}"
                _proxy_env = {"HTTPS_PROXY": url, "HTTP_PROXY": url, "NO_PROXY": "localhost,127.0.0.1"}
                break
    return _proxy_env


async def default_runner(args: list[str]) -> tuple[int, str, str]:
    env = dict(os.environ)
    if not any(k in env for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")):
        env.update(_system_proxy())
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await proc.communicate()
    except BaseException:
        # 超时被取消时连带结束 rclone，免得它继续占着 53682 端口或继续上传
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _secure_secrets() -> None:
    config.SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(config.SECRETS_DIR, 0o700)
    if config.RCLONE_CONF.exists():
        os.chmod(config.RCLONE_CONF, 0o600)


async def _rclone(args: list[str], *, runner: Runner | None, timeout: float) -> tuple[int, str, str]:
    _secure_secrets()
    full = [str(config.RCLONE), "--config", str(config.RCLONE_CONF), *args]
    try:
        return await asyncio.wait_for((runner or default_runner)(full), timeout)
    except TimeoutError:
        raise DriveError(f"rclone {args[0]} 超时（{int(timeout)} 秒）") from None


def _tail(text: str, n: int = 5) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def _remote(path: str) -> str:
    return f"{config.DRIVE_REMOTE}:{path}"


def _budget(nbytes: int) -> float:
    # 按最慢 200KB/s 估算上传时长，另留半小时余量
    return 1800 + nbytes / 200_000


def configured() -> bool:
    if not config.RCLONE_CONF.exists():
        return False
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(config.RCLONE_CONF, encoding="utf-8")
    except configparser.Error:
        return False
    return cp.has_section(config.DRIVE_REMOTE)


async def authorize(*, runner: Runner | None = None) -> dict:
    """rclone 打开浏览器让用户点"允许"，在 127.0.0.1:53682 收回调；失败时把配置文件恢复原样。"""
    if _auth_lock.locked():
        return {"ok": False, "error": "授权正在进行中，请在已打开的浏览器页面里完成"}
    async with _auth_lock:
        _secure_secrets()
        before = config.RCLONE_CONF.read_bytes() if config.RCLONE_CONF.exists() else None
        args = ["config", "create", config.DRIVE_REMOTE, "drive", "scope=drive.file", "config_is_local=true"]
        try:
            code, _out, err = await _rclone(args, runner=runner, timeout=AUTH_TIMEOUT)
            # 标准输出里有 token，不能外传
            error = None if code == 0 and configured() else (_tail(err) or f"rclone 退出码 {code}")
        except DriveError:
            error = "10 分钟内没有完成浏览器授权"
        if error:
            if before is None:
                config.RCLONE_CONF.unlink(missing_ok=True)
            else:
                config.RCLONE_CONF.write_bytes(before)
        _secure_secrets()
        return {"ok": error is None, "error": error}


def remote_dir_for_episode(ep, conn: sqlite3.Connection | None = None) -> str:
    """F1 Hermes/日期/分支/期标题（前缀丨发布标题）；同名被别的期占了就加 -02、-03。"""
    code = ep["code"]
    day = f"{code[:4]}-{code[4:6]}-{code[6:8]}"
    name = " ".join((ep["title"] or code).replace("/", "／").split()) or code
    base = f"{config.DRIVE_ROOT}/{day}/{ep['branch']}/{name}"
    if conn is None:
        return base
    taken = {r[0] for r in conn.execute("SELECT drive_path FROM archives WHERE episode_id != ? AND drive_path IS NOT NULL",
                                        (ep["id"],))}
    path, n = base, 1
    while path in taken:
        n += 1
        path = f"{base}-{n:02d}"
    return path


async def _check(src: str, dst_path: str, *, runner: Runner | None, timeout: float,
                 extra: list[str] | None = None) -> tuple[list[str], list[str]]:
    """逐文件核对（云端 MD5 对本地 MD5）；返回（一致的文件, 有问题的行）。"""
    base = ["check", src, _remote(dst_path), "--one-way", "--combined", "-", *FILTERS, *COMMON, *(extra or [])]
    code, out, err = await _rclone(base, runner=runner, timeout=timeout)
    if "no common hash" in err.lower() or "not using a hash" in err.lower():
        # 两边没有共同的哈希算法时只比大小不够，改为下载回来逐字节比
        code, out, err = await _rclone([*base, "--download"], runner=runner, timeout=timeout * 2)
    ok, bad = [], []
    for line in out.splitlines():
        if len(line) < 3 or line[1] != " ":
            continue
        if line[0] == "=":
            ok.append(line[2:])
        else:
            bad.append(line)
    if code != 0 and not bad:
        bad.append(f"! rclone check 失败：{_tail(err, 3)}")
    return ok, bad


def _dir_size(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            p = Path(root, name)
            if not p.is_symlink():
                total += p.stat().st_size
    return total


def _set_archive(conn: sqlite3.Connection, episode_id: int, **cols) -> None:
    conn.execute("INSERT OR IGNORE INTO archives(episode_id) VALUES (?)", (episode_id,))
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE archives SET {sets} WHERE episode_id=?", (*cols.values(), episode_id))


async def upload_episode(conn: sqlite3.Connection, episode_id: int, *, runner: Runner | None = None) -> dict:
    ep = episodes.get(conn, episode_id)
    if not ep["code"] or not ep["folder"]:
        raise DriveError("这一期还没有期号或文件夹")
    folder = Path(ep["folder"])
    if not folder.is_dir():
        raise DriveError(f"本地文件夹不存在：{folder}")
    drive_path = remote_dir_for_episode(ep, conn)
    timeout = _budget(await asyncio.to_thread(_dir_size, folder))
    old = conn.execute("SELECT drive_path, verified_at FROM archives WHERE episode_id=?", (episode_id,)).fetchone()
    if old and old["verified_at"] and old["drive_path"] and old["drive_path"] != drive_path:
        # 期标题改过：Drive 上已传过的文件夹改名过去，再补传有变化的文件
        code, _out, err = await _rclone(["moveto", _remote(old["drive_path"]), _remote(drive_path), *COMMON],
                                        runner=runner, timeout=timeout)
        if code != 0:
            raise DriveError(f"Drive 文件夹改名失败：{_tail(err)}")
    _set_archive(conn, episode_id, drive_path=drive_path, upload_state="uploading")

    code, _out, err = await _rclone(["copy", str(folder), _remote(drive_path), *FILTERS, *COMMON],
                                    runner=runner, timeout=timeout)
    if code != 0:
        _set_archive(conn, episode_id, upload_state="failed")
        raise DriveError(f"上传失败：{_tail(err)}")

    ok, bad = await _check(str(folder), drive_path, runner=runner, timeout=timeout)
    nbytes = sum((folder / p).stat().st_size for p in ok)
    if bad or not ok:
        _set_archive(conn, episode_id, upload_state="mismatch")
        return {"files": len(ok), "bytes": nbytes, "mismatch": bad or ["! 没有任何文件被核对"], "drive_path": drive_path}
    ts = datetime.now(timezone.utc)
    _set_archive(conn, episode_id, upload_state="verified", verified_at=ts.isoformat(timespec="seconds"),
                 delete_after=(ts + timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"),
                 extensions=0, reminded_at=None, deleted_at=None)
    return {"files": len(ok), "bytes": nbytes, "mismatch": [], "drive_path": drive_path}


def _asset(conn: sqlite3.Connection, asset_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if row is None:
        raise DriveError(f"没有 id={asset_id} 的素材")
    return row


def _asset_file(row: sqlite3.Row) -> Path:
    if not row["local_path"]:
        raise DriveError(f"素材 {row['id']} 没有本地路径")
    p = Path(row["local_path"])
    return p if p.is_absolute() else config.LIBRARY_DIR / str(row["id"]) / p


async def upload_asset(conn: sqlite3.Connection, asset_id: int, *, runner: Runner | None = None) -> str:
    """素材文件夹（原片、关键帧、拼图、缩略图）整体传到 F1 Hermes/素材库/<id>/，核对后写 drive_path（指向原片）。"""
    row = _asset(conn, asset_id)
    src = _asset_file(row)
    if not src.is_file():
        raise DriveError(f"素材原片不在本地：{src}")
    asset_dir = config.LIBRARY_DIR / str(asset_id)
    remote_dir = f"{config.DRIVE_ROOT}/素材库/{asset_id}"
    inside = asset_dir in src.parents
    rel = src.relative_to(asset_dir).as_posix() if inside else src.name
    timeout = _budget(src.stat().st_size + (await asyncio.to_thread(_dir_size, asset_dir) if asset_dir.is_dir() else 0))

    # 原片不在素材文件夹里时单独传一份到同一远端目录
    sources = ([str(asset_dir)] if asset_dir.is_dir() else []) + ([] if inside else [str(src)])
    for s in sources:
        cmd = ["copy", s, _remote(remote_dir), *FILTERS, *COMMON]
        code, _out, err = await _rclone(cmd, runner=runner, timeout=timeout)
        if code != 0:
            raise DriveError(f"素材 {asset_id} 上传失败：{_tail(err)}")
    for s in sources:
        ok, bad = await _check(s, remote_dir, runner=runner, timeout=timeout)
        if bad or not ok:
            raise DriveError(f"素材 {asset_id} 核对不一致：{bad[:5] or '没有文件被核对'}")
    drive_path = f"{remote_dir}/{rel}"
    conn.execute("UPDATE assets SET drive_path=? WHERE id=?", (drive_path, asset_id))
    return drive_path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 << 20):
            h.update(chunk)
    return h.hexdigest()


async def fetch_asset(conn: sqlite3.Connection, asset_id: int, dest: Path | None = None, *,
                      runner: Runner | None = None) -> Path:
    """从 Drive 取回素材原片；先下到 .part 临时文件，sha256 对上才改名，不会用坏数据覆盖本地文件。"""
    row = _asset(conn, asset_id)
    if not row["drive_path"]:
        raise DriveError(f"素材 {asset_id} 没有上传过 Drive")
    if not row["sha256"]:
        raise DriveError(f"素材 {asset_id} 没有 sha256，无法校验")
    dest = Path(dest) if dest else _asset_file(row)
    if dest.is_file() and await asyncio.to_thread(_sha256, dest) == row["sha256"]:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    code, _out, err = await _rclone(["copyto", _remote(row["drive_path"]), str(part), *COMMON],
                                    runner=runner, timeout=_budget(0) * 4)
    if code != 0 or not part.is_file():
        part.unlink(missing_ok=True)
        raise DriveError(f"素材 {asset_id} 下载失败：{_tail(err)}")
    got = await asyncio.to_thread(_sha256, part)
    if got != row["sha256"]:
        part.unlink(missing_ok=True)
        raise DriveError(f"素材 {asset_id} 下载后 sha256 不符：{got[:12]}… ≠ {row['sha256'][:12]}…")
    part.replace(dest)
    return dest


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _archive_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT a.*, e.code, e.title, e.folder, e.state FROM archives a JOIN episodes e ON e.id=a.episode_id
           WHERE a.upload_state='verified' AND a.deleted_at IS NULL AND a.delete_after IS NOT NULL
           ORDER BY a.delete_after""").fetchall()


def due_reminders(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    return [dict(r) for r in _archive_rows(conn)
            if r["reminded_at"] is None and _ts(r["delete_after"]) - timedelta(days=1) <= now]


def mark_reminded(conn: sqlite3.Connection, episode_id: int) -> None:
    conn.execute("UPDATE archives SET reminded_at=? WHERE episode_id=?", (db_now(), episode_id))


def extend(conn: sqlite3.Connection, episode_id: int, days: int = KEEP_DAYS) -> str:
    """再留 N 天：从原删除日顺延；原删除日已过（清理还没跑到）则从现在起算。"""
    with transaction(conn):
        row = conn.execute("SELECT * FROM archives WHERE episode_id=?", (episode_id,)).fetchone()
        if row is None or row["upload_state"] != "verified" or not row["delete_after"]:
            raise DriveError("这一期还没有核对通过的 Drive 归档")
        if row["deleted_at"]:
            raise DriveError("本地文件已经清理，不能再延期")
        base = max(_ts(row["delete_after"]), datetime.now(timezone.utc))
        new = (base + timedelta(days=days)).isoformat(timespec="seconds")
        conn.execute("UPDATE archives SET delete_after=?, extensions=extensions+1, reminded_at=NULL WHERE episode_id=?",
                     (new, episode_id))
    return new


def _safe_folder(row) -> Path:
    """只允许删 PRODUCTIONS_DIR 下、名字就是期号的那一层文件夹。"""
    raw = Path(row["folder"] or "")
    if not row["folder"] or raw.is_symlink():
        raise DriveError(f"拒绝删除：{raw}")
    folder = raw.resolve()
    prod = config.PRODUCTIONS_DIR.resolve()
    if folder.parent != prod or folder.name != row["code"]:
        raise DriveError(f"拒绝删除 productions 之外或与期号不符的路径：{folder}")
    return folder


async def cleanup(conn: sqlite3.Connection, now: datetime, *, dry_run: bool = False,
                  runner: Runner | None = None) -> list[dict]:
    done = []
    for r in _archive_rows(conn):
        if _ts(r["delete_after"]) > now:
            continue
        item = {"episode_id": r["episode_id"], "code": r["code"], "folder": r["folder"], "deleted": False, "error": None}
        done.append(item)
        try:
            folder = _safe_folder(r)
            if dry_run:
                continue
            if folder.exists():
                # 删前再确认一次云端文件还在（只比大小，快）；用户可能在 Drive 里误删过
                _ok, bad = await _check(str(folder), r["drive_path"], runner=runner, timeout=1800, extra=["--size-only"])
                if bad:
                    raise DriveError(f"云端与本地不一致，暂不删除：{bad[:3]}")
                await asyncio.to_thread(shutil.rmtree, folder)
            with transaction(conn):
                conn.execute("UPDATE archives SET deleted_at=? WHERE episode_id=?", (db_now(), r["episode_id"]))
                episodes.set_state(conn, r["episode_id"], "本地已清理")
            item["deleted"] = True
        except (DriveError, OSError) as e:
            item["error"] = str(e)
    return done


async def usage(*, runner: Runner | None = None) -> dict | None:
    if not configured():
        return None
    code, out, err = await _rclone(["about", _remote(""), "--json"], runner=runner, timeout=120)
    if code != 0:
        raise DriveError(f"查询 Drive 容量失败：{_tail(err)}")
    data = json.loads(out)
    return {k: data.get(k) for k in ("total", "used", "free", "trashed", "other")}
