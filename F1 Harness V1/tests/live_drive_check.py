"""M4-5 Drive 实测。

  python tests/live_drive_check.py local   用 rclone alias 远端（本地临时目录）模拟 Drive 跑完整流程
  python tests/live_drive_check.py real    用户授权后由总控跑：真实 hermes-drive 上一个自测目录，跑完删除

两种模式都用临时数据库和临时 productions/素材库，不碰正式数据。
"""
import os
import sys
import tempfile
from pathlib import Path

MODE = sys.argv[1] if len(sys.argv) > 1 else ""
if MODE not in ("local", "real"):
    sys.exit(__doc__)

REAL_CORE = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="hermes-drive-live-"))
os.environ["HERMES_CORE"] = str(TMP / "core")
os.environ["HERMES_ROOT"] = str(TMP / "root")
os.environ["HERMES_JOBS"] = "0"
sys.path.insert(0, str(REAL_CORE))

import asyncio  # noqa: E402
import hashlib  # noqa: E402
import shutil  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from hermes import config, db, drive, episodes  # noqa: E402

config.RCLONE = REAL_CORE / "tools" / "bin" / "rclone"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")

if MODE == "real":
    config.SECRETS_DIR = REAL_CORE / "data" / "secrets"
    config.RCLONE_CONF = config.SECRETS_DIR / "rclone.conf"
    # 自测全部放进一个独立目录，跑完整个删掉，不影响正式归档
    config.DRIVE_ROOT = f"F1 Hermes/_自测-{STAMP}"
else:
    fake = TMP / "fakedrive"
    fake.mkdir()
    config.SECRETS_DIR = TMP / "secrets"
    config.RCLONE_CONF = config.SECRETS_DIR / "rclone.conf"
    config.SECRETS_DIR.mkdir()
    config.RCLONE_CONF.write_text(f"[{config.DRIVE_REMOTE}]\ntype = alias\nremote = {fake}\n")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def step(msg: str) -> None:
    print(f"\n== {msg}")


async def main() -> int:
    if not drive.configured():
        print(f"未授权：{config.RCLONE_CONF} 里没有 [{config.DRIVE_REMOTE}]，先在网页里点「连接 Google Drive」")
        return 2
    conn = db.connect(TMP / "core" / "data" / "hermes.db")
    fails = 0

    step("Drive 容量")
    u = await drive.usage()
    print({k: (f"{v / 1e9:.1f} GB" if isinstance(v, int) else v) for k, v in u.items()})

    step("建测试期并放文件")
    eid = episodes.new_session(conn)
    code = episodes.start(conn, eid, branch="资讯", gp_label="自测", title="Drive 自测")
    folder = Path(episodes.get(conn, eid)["folder"])
    (folder / "02_文案" / "逐字稿.md").write_text("头哥又赢了，木瓜第二\n")
    (folder / "06_合成" / "timeline.json").write_text('{"clips": []}')
    (folder / "07_QA" / "report.txt").write_text("ok")
    big = folder / "08_交付" / "成片-5MB.bin"
    big.write_bytes(os.urandom(5 * 1024 * 1024))
    (folder / "08_交付" / "._成片-5MB.bin").write_bytes(b"shadow")
    (folder / ".DS_Store").write_bytes(b"ds")
    print(code, folder)

    step("上传并逐文件核对")
    t = time.monotonic()
    res = await drive.upload_episode(conn, eid)
    print(f"{res['files']} 个文件，{res['bytes']} 字节，mismatch={res['mismatch']}，用时 {time.monotonic() - t:.1f}s")
    print("远端：", res["drive_path"])
    a = dict(conn.execute("SELECT * FROM archives WHERE episode_id=?", (eid,)).fetchone())
    print({k: a[k] for k in ("upload_state", "verified_at", "delete_after")})
    fails += res["mismatch"] != [] or res["files"] != 4 or a["upload_state"] != "verified"

    step("按 sha256 把 5MB 文件取回")
    big_sha = sha(big)
    aid = conn.execute("INSERT INTO assets(source_type, local_path, drive_path, sha256, created_at) VALUES ('脚本',?,?,?,?)",
                       (str(TMP / "取回" / "成片-5MB.bin"), f"{res['drive_path']}/08_交付/成片-5MB.bin", big_sha, db.now())).lastrowid
    got = await drive.fetch_asset(conn, aid)
    ok = sha(got) == big_sha
    print(got, "sha256 一致" if ok else "sha256 不一致！")
    fails += not ok

    step("素材库上传 + 反查下载")
    clip = os.urandom(2 * 1024 * 1024)
    clip_sha = hashlib.sha256(clip).hexdigest()
    lib_id = conn.execute("INSERT INTO assets(source_type, sha256, created_at) VALUES ('脚本', ?, ?)",
                          (clip_sha, db.now())).lastrowid
    lib = config.LIBRARY_DIR / str(lib_id)
    (lib / "keyframes").mkdir(parents=True)
    (lib / "原片.mp4").write_bytes(clip)
    (lib / "keyframes" / "0000.jpg").write_bytes(b"jpg")
    conn.execute("UPDATE assets SET local_path=? WHERE id=?", (str(lib / "原片.mp4"), lib_id))
    print("drive_path =", await drive.upload_asset(conn, lib_id))
    (lib / "原片.mp4").unlink()
    got = await drive.fetch_asset(conn, lib_id)
    ok = got.exists() and sha(got) == clip_sha
    print("取回原片", "sha256 一致" if ok else "失败！")
    fails += not ok

    step("提醒 / 延期 / 清理")
    after = datetime.fromisoformat(a["delete_after"])
    print("删除日前 1 天 1 秒提醒：", len(drive.due_reminders(conn, after - timedelta(days=1, seconds=1))))
    print("删除日前 1 天提醒：", [r["code"] for r in drive.due_reminders(conn, after - timedelta(days=1))])
    drive.mark_reminded(conn, eid)
    new = drive.extend(conn, eid)
    print("再留 7 天 →", new)
    print("原删除日清理：", await drive.cleanup(conn, after + timedelta(seconds=1)))
    t_clean = datetime.fromisoformat(new) + timedelta(seconds=1)
    print("dry_run：", await drive.cleanup(conn, t_clean, dry_run=True))
    res_c = await drive.cleanup(conn, t_clean)
    print("清理：", res_c)
    ok = res_c and res_c[0]["deleted"] and not folder.exists() and episodes.get(conn, eid)["state"] == "本地已清理"
    print("本地文件夹已删、状态=本地已清理" if ok else "清理失败！")
    fails += not ok

    if MODE == "real":
        step("删除远端自测目录")
        code_, _o, err = await drive._rclone(["purge", drive._remote(config.DRIVE_ROOT)], runner=None, timeout=300)
        print("purge", "成功（进 Drive 回收站）" if code_ == 0 else f"失败：{err[-300:]}")
        fails += code_ != 0

    conn.close()
    print(f"\n结果：{'全部通过' if not fails else f'{fails} 项失败'}（临时目录 {TMP}）")
    return 1 if fails else 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(rc)
