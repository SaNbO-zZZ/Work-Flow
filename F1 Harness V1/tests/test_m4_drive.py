"""M4-5 Drive 归档：用 rclone alias 远端（指向本地临时目录）模拟 Google Drive，真实跑 copy/check/copyto。"""
import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hermes import config, drive, episodes
from hermes.db import now as db_now

RCLONE = Path("/Volumes/HermesCore/tools/bin/rclone")
pytestmark = pytest.mark.skipif(not RCLONE.exists(), reason="没有 rclone")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = tmp_path / "fakedrive"
    fake.mkdir()
    secrets = tmp_path / "secrets"
    monkeypatch.setattr(config, "RCLONE", RCLONE)
    monkeypatch.setattr(config, "SECRETS_DIR", secrets)
    monkeypatch.setattr(config, "RCLONE_CONF", secrets / "rclone.conf")
    monkeypatch.setattr(config, "PRODUCTIONS_DIR", tmp_path / "productions")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "素材库")
    secrets.mkdir()
    (secrets / "rclone.conf").write_text(f"[{config.DRIVE_REMOTE}]\ntype = alias\nremote = {fake}\n")
    return fake


def make_episode(conn, files: dict[str, bytes] | None = None) -> tuple[int, Path]:
    eid = episodes.new_session(conn)
    episodes.start(conn, eid, branch="资讯", gp_label="巴林大奖赛", title="测试",
                   when=datetime(2026, 9, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    folder = Path(episodes.get(conn, eid)["folder"])
    for rel, data in (files or {}).items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return eid, folder


FILES = {
    "02_文案/逐字稿.md": "头哥又赢了".encode(),
    "08_交付/成片.mp4": os.urandom(300_000),
    "06_合成/timeline.json": b"{}",
    "._成片.mp4": b"shadow",
    "08_交付/._成片.mp4": b"shadow",
    "08_交付/.DS_Store": b"ds",
}


def test_remote_dir_and_configured(env):
    assert drive.configured()
    ep = {"id": 1, "code": "20260929-资讯-巴林大奖赛-01", "branch": "资讯", "title": "资讯丨维斯塔潘 /  换队？"}
    assert drive.remote_dir_for_episode(ep) == "F1 Hermes/2026-09-29/资讯/资讯丨维斯塔潘 ／ 换队？"
    assert drive.remote_dir_for_episode(ep | {"title": ""}) == "F1 Hermes/2026-09-29/资讯/20260929-资讯-巴林大奖赛-01"
    config.RCLONE_CONF.write_text("[other]\ntype = local\n")
    assert not drive.configured()
    config.RCLONE_CONF.unlink()
    assert not drive.configured()


def test_upload_episode_verified(conn, env):
    eid, folder = make_episode(conn, FILES)
    res = run(drive.upload_episode(conn, eid))
    assert res["mismatch"] == []
    assert res["files"] == 3
    assert res["bytes"] == sum(len(v) for k, v in FILES.items() if "._" not in k and "DS_Store" not in k)
    remote = env / res["drive_path"]
    assert res["drive_path"] == "F1 Hermes/2026-09-29/资讯/测试"
    names = {p.relative_to(remote).as_posix() for p in remote.rglob("*") if p.is_file()}
    assert names == {"02_文案/逐字稿.md", "08_交付/成片.mp4", "06_合成/timeline.json"}
    assert not (remote / ".agents").exists()
    a = conn.execute("SELECT * FROM archives WHERE episode_id=?", (eid,)).fetchone()
    assert a["upload_state"] == "verified" and a["drive_path"] == res["drive_path"]
    left = datetime.fromisoformat(a["delete_after"]) - datetime.fromisoformat(a["verified_at"])
    assert left == timedelta(days=7)
    assert oct(config.SECRETS_DIR.stat().st_mode & 0o777) == "0o700"
    assert oct(config.RCLONE_CONF.stat().st_mode & 0o777) == "0o600"


def test_upload_detects_remote_corruption(conn, env):
    eid, folder = make_episode(conn, FILES)

    async def corrupting(args):
        code, out, err = await drive.default_runner(args)
        if "copy" in args:
            # 同样大小、不同内容：只比大小发现不了，必须靠哈希
            target = env / drive.remote_dir_for_episode(episodes.get(conn, eid)) / "08_交付/成片.mp4"
            target.write_bytes(b"\0" * target.stat().st_size)
        return code, out, err

    res = run(drive.upload_episode(conn, eid, runner=corrupting))
    assert any("08_交付/成片.mp4" in m for m in res["mismatch"])
    a = conn.execute("SELECT * FROM archives WHERE episode_id=?", (eid,)).fetchone()
    assert a["upload_state"] == "mismatch" and a["verified_at"] is None and a["delete_after"] is None


def test_upload_missing_folder(conn, env):
    eid, folder = make_episode(conn)
    import shutil
    shutil.rmtree(folder)
    with pytest.raises(drive.DriveError):
        run(drive.upload_episode(conn, eid))


def make_asset(conn) -> tuple[int, Path, bytes]:
    data = os.urandom(200_000)
    cur = conn.execute("INSERT INTO assets(source_type, sha256, created_at) VALUES ('脚本', ?, ?)",
                       (hashlib.sha256(data).hexdigest(), db_now()))
    aid = cur.lastrowid
    d = config.LIBRARY_DIR / str(aid)
    (d / "keyframes").mkdir(parents=True)
    (d / "原片.mp4").write_bytes(data)
    (d / "keyframes" / "0000.jpg").write_bytes(b"jpg0")
    (d / "keyframes" / "0001.jpg").write_bytes(b"jpg1")
    (d / "._原片.mp4").write_bytes(b"shadow")
    conn.execute("UPDATE assets SET local_path=? WHERE id=?", (str(d / "原片.mp4"), aid))
    return aid, d, data


def test_asset_roundtrip(conn, env, tmp_path):
    aid, d, data = make_asset(conn)
    path = run(drive.upload_asset(conn, aid))
    assert path == f"F1 Hermes/素材库/{aid}/原片.mp4"
    assert conn.execute("SELECT drive_path FROM assets WHERE id=?", (aid,)).fetchone()[0] == path
    remote = env / "F1 Hermes" / "素材库" / str(aid)
    assert (remote / "keyframes" / "0001.jpg").read_bytes() == b"jpg1"
    assert not (remote / "._原片.mp4").exists()

    (d / "原片.mp4").unlink()
    got = run(drive.fetch_asset(conn, aid))
    assert got == d / "原片.mp4" and got.read_bytes() == data
    other = run(drive.fetch_asset(conn, aid, tmp_path / "取回" / "x.mp4"))
    assert other.read_bytes() == data

    # 云端被改坏：下载后 sha 不符，报错且不留下文件
    (remote / "原片.mp4").write_bytes(os.urandom(len(data)))
    (d / "原片.mp4").unlink()
    with pytest.raises(drive.DriveError, match="sha256"):
        run(drive.fetch_asset(conn, aid))
    assert not (d / "原片.mp4").exists() and not (d / "原片.mp4.part").exists()


def test_asset_upload_detects_corruption(conn, env):
    aid, d, data = make_asset(conn)

    async def corrupting(args):
        code, out, err = await drive.default_runner(args)
        if "copy" in args:
            (env / "F1 Hermes" / "素材库" / str(aid) / "keyframes" / "0000.jpg").write_bytes(b"XXXX")
        return code, out, err

    with pytest.raises(drive.DriveError, match="核对不一致"):
        run(drive.upload_asset(conn, aid, runner=corrupting))
    assert conn.execute("SELECT drive_path FROM assets WHERE id=?", (aid,)).fetchone()[0] is None


def _archive(conn, eid, delete_after: datetime):
    conn.execute("""INSERT INTO archives(episode_id, drive_path, upload_state, verified_at, delete_after)
                    VALUES (?, 'F1 Hermes/x', 'verified', ?, ?)""",
                 (eid, db_now(), delete_after.isoformat(timespec="seconds")))


def test_reminders_and_extend(conn, env):
    # 删除日必须在未来，否则 extend 会从"现在"起算
    t0 = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    eid, _ = make_episode(conn)
    _archive(conn, eid, t0)
    assert drive.due_reminders(conn, t0 - timedelta(days=1, seconds=1)) == []
    due = drive.due_reminders(conn, t0 - timedelta(days=1))
    assert [r["episode_id"] for r in due] == [eid] and due[0]["code"].startswith("20260929-资讯")
    drive.mark_reminded(conn, eid)
    assert drive.due_reminders(conn, t0) == []

    new = drive.extend(conn, eid)
    a = conn.execute("SELECT * FROM archives WHERE episode_id=?", (eid,)).fetchone()
    assert datetime.fromisoformat(new) == t0 + timedelta(days=7)
    assert a["extensions"] == 1 and a["reminded_at"] is None and a["delete_after"] == new
    # 延期后又会在新删除日前一天提醒
    assert drive.due_reminders(conn, datetime.fromisoformat(new) - timedelta(days=1))[0]["episode_id"] == eid

    # 删除日已过才延期：从现在起算
    eid2, _ = make_episode(conn)
    _archive(conn, eid2, datetime(2020, 1, 1, tzinfo=timezone.utc))
    new2 = datetime.fromisoformat(drive.extend(conn, eid2, days=3))
    assert new2 > datetime.now(timezone.utc) + timedelta(days=2, hours=23)

    eid3, _ = make_episode(conn)
    with pytest.raises(drive.DriveError):
        drive.extend(conn, eid3)


def test_cleanup(conn, env):
    t_now = datetime.now(timezone.utc)
    eid, folder = make_episode(conn, FILES)
    run(drive.upload_episode(conn, eid))
    later, _ = make_episode(conn, FILES)
    run(drive.upload_episode(conn, later))

    conn.execute("UPDATE archives SET delete_after=? WHERE episode_id=?", ((t_now - timedelta(hours=1)).isoformat(), eid))
    dry = run(drive.cleanup(conn, t_now, dry_run=True))
    assert [r["episode_id"] for r in dry] == [eid] and folder.exists()

    res = run(drive.cleanup(conn, t_now))
    assert res[0]["deleted"] and res[0]["error"] is None
    assert not folder.exists() and config.PRODUCTIONS_DIR.exists()
    assert episodes.get(conn, eid)["state"] == "本地已清理"
    assert conn.execute("SELECT deleted_at FROM archives WHERE episode_id=?", (eid,)).fetchone()[0]
    assert Path(episodes.get(conn, later)["folder"]).exists()
    assert run(drive.cleanup(conn, t_now)) == []
    with pytest.raises(drive.DriveError):
        drive.extend(conn, eid)


def test_cleanup_refuses_outside_and_missing_remote(conn, env, tmp_path):
    t_now = datetime.now(timezone.utc)
    past = t_now - timedelta(days=1)
    outside = tmp_path / "别的目录" / "20260929-资讯-巴林大奖赛-01"
    outside.mkdir(parents=True)
    (outside / "重要.txt").write_text("别删")

    eid, _ = make_episode(conn)
    conn.execute("UPDATE episodes SET folder=? WHERE id=?", (str(outside), eid))
    _archive(conn, eid, past)
    # productions 本身也不能删
    eid2, _ = make_episode(conn)
    conn.execute("UPDATE episodes SET folder=? WHERE id=?", (str(config.PRODUCTIONS_DIR), eid2))
    _archive(conn, eid2, past)
    # 用 .. 绕出去
    eid3, _ = make_episode(conn)
    conn.execute("UPDATE episodes SET folder=? WHERE id=?",
                 (str(config.PRODUCTIONS_DIR / ".." / "别的目录" / "20260929-资讯-巴林大奖赛-01"), eid3))
    _archive(conn, eid3, past)
    # 云端已被删掉：本地不删
    eid4, folder4 = make_episode(conn, {"a.txt": b"1"})
    _archive(conn, eid4, past)

    res = {r["episode_id"]: r for r in run(drive.cleanup(conn, t_now))}
    for e in (eid, eid2, eid3):
        assert not res[e]["deleted"] and "拒绝删除" in res[e]["error"]
    assert not res[eid4]["deleted"] and "不一致" in res[eid4]["error"]
    assert (outside / "重要.txt").exists() and folder4.exists() and config.PRODUCTIONS_DIR.exists()
    assert conn.execute("SELECT COUNT(*) FROM archives WHERE deleted_at IS NOT NULL").fetchone()[0] == 0


def test_authorize(env, monkeypatch):
    old = config.RCLONE_CONF.read_text()
    seen = []

    async def ok_runner(args):
        seen.append(args)
        config.RCLONE_CONF.write_text(old + f"\n[{config.DRIVE_REMOTE}-x]\ntype = drive\n")
        return 0, "token = secret", ""

    assert run(drive.authorize(runner=ok_runner)) == {"ok": True, "error": None}
    a = seen[0]
    assert a[:3] == [str(RCLONE), "--config", str(config.RCLONE_CONF)]
    assert a[3:6] == ["config", "create", config.DRIVE_REMOTE] and "scope=drive.file" in a

    async def bad_runner(args):
        config.RCLONE_CONF.write_text("[hermes-drive]\ntype = drive\n# 半截\n")
        return 1, "", "Failed to configure token: access_denied"

    config.RCLONE_CONF.write_text(old)
    r = run(drive.authorize(runner=bad_runner))
    assert not r["ok"] and "access_denied" in r["error"] and config.RCLONE_CONF.read_text() == old

    async def slow_runner(args):
        config.RCLONE_CONF.write_text("[hermes-drive]\n")
        await asyncio.sleep(5)
        return 0, "", ""

    monkeypatch.setattr(drive, "AUTH_TIMEOUT", 0.2)
    config.RCLONE_CONF.unlink()
    r = run(drive.authorize(runner=slow_runner))
    assert not r["ok"] and "10 分钟" in r["error"] and not config.RCLONE_CONF.exists()


def test_timeout_kills_subprocess(env):
    import subprocess

    async def sleeper(args):
        return await drive.default_runner(["/bin/sleep", "31.7"])

    with pytest.raises(drive.DriveError, match="超时"):
        run(drive._rclone(["about"], runner=sleeper, timeout=0.3))
    assert subprocess.run(["pgrep", "-f", "sleep 31.7"], capture_output=True).returncode == 1


def test_usage(env):
    u = run(drive.usage())
    assert u["total"] > 0 and u["used"] >= 0
    config.RCLONE_CONF.unlink()
    assert run(drive.usage()) is None


def test_drive_folder_dedupe_and_rename(conn, env):
    """期标题做 Drive 文件夹名：别的期占了同名加 -02；标题改了再上传，旧文件夹改名过去，不留两份。"""
    eid, folder = make_episode(conn, FILES)
    conn.execute("INSERT INTO episodes(id, title, state, created_at, updated_at) VALUES (?, 'x', '闲聊', '', '')", (eid + 100,))
    conn.execute("INSERT INTO archives(episode_id, drive_path) VALUES (?, ?)", (eid + 100, "F1 Hermes/2026-09-29/资讯/测试"))
    assert drive.remote_dir_for_episode(episodes.get(conn, eid), conn) == "F1 Hermes/2026-09-29/资讯/测试-02"
    first = run(drive.upload_episode(conn, eid))
    assert first["drive_path"].endswith("测试-02") and (env / first["drive_path"]).is_dir()
    conn.execute("UPDATE episodes SET title='资讯丨新标题' WHERE id=?", (eid,))
    second = run(drive.upload_episode(conn, eid))
    assert second["mismatch"] == [] and second["drive_path"] == "F1 Hermes/2026-09-29/资讯/资讯丨新标题"
    assert not (env / first["drive_path"]).exists()
    assert (env / second["drive_path"] / "08_交付/成片.mp4").is_file()
