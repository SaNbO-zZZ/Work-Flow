"""M4-1 实机检查：B 站扫码接口 + yt-dlp 真实下载一个短视频（临时库、临时目录，结束后删除）。
用法：.venv/bin/python tests/live_download_check.py（不需要真的扫码）"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="hermes-dl-live-"))
os.environ["HERMES_CORE"] = str(_tmp / "core")
os.environ["HERMES_ROOT"] = str(_tmp / "root")
os.environ["HERMES_JOBS"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes import bilibili, config, db, download  # noqa: E402

URLS = ["https://www.youtube.com/watch?v=jNQXAC9IVRw"] + sys.argv[1:]


async def check_qr() -> None:
    res = await bilibili.qr_start()
    print(f"qr_start: url={res['url']}\n          key 长度={len(res['qrcode_key'])} svg 长度={len(res['svg'])}")
    print(f"qr_poll : {await bilibili.qr_poll(res['qrcode_key'])}")
    print(f"status  : {bilibili.status()}")


async def check_download(conn, url: str) -> bool:
    calls, lines, progress = [], [], []

    def ingest(conn, path, **kw):
        size = Path(path).stat().st_size
        calls.append((Path(path).name, size, kw))
        # 假入库：把原片挪进临时素材库，只登记参数
        dest = config.LIBRARY_DIR / "fake-asset" / Path(path).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(path, dest)
        return conn.execute("INSERT INTO assets(source_type, source_url, local_path, credit, created_at) "
                            "VALUES (?,?,?,?,?)", (kw["source_type"], kw["source_url"], str(dest), kw["credit"],
                                                   db.now())).lastrowid

    async def runner(args, on_line):
        parser = download.Progress()

        def spy(line):
            lines.append(line)
            if (p := parser.feed(line)) is not None:
                progress.append(f"{p:.3f}  {line}")
        print("  命令：", " ".join(args))
        return await download.default_runner(args, lambda line: (spy(line), on_line(line)))

    did = download.enqueue(conn, url)
    t0 = time.monotonic()
    try:
        asset_id = await download.run_one(conn, did, runner=runner, ingest=ingest)
    except download.DownloadError as e:
        row = conn.execute("SELECT error FROM downloads WHERE id=?", (did,)).fetchone()
        print(f"  失败（{time.monotonic() - t0:.1f}s）：{e}\n  错误尾部：\n{row['error'][-1500:]}")
        return False
    row = conn.execute("SELECT * FROM downloads WHERE id=?", (did,)).fetchone()
    name, size, kw = calls[0]
    print(f"  成功（{time.monotonic() - t0:.1f}s）asset_id={asset_id} 文件={name} 大小={size / 1024:.1f} KiB")
    print(f"  标题={kw['title']!r} uploader={kw['uploader']!r} credit={kw['credit']!r}")
    print(f"  downloads: state={row['state']} progress={row['progress']} finished_at={row['finished_at']}")
    print(f"  输出行数={len(lines)} 进度回调次数={len(progress)}")
    print("  进度行样例：", *progress[:3], *progress[-2:], sep="\n    ")
    print(f"  _incoming 已清理：{not (config.LIBRARY_DIR / '_incoming' / str(did)).exists()}")
    return True


async def main() -> None:
    print(f"临时目录 {_tmp}；yt-dlp {config.YTDLP}")
    try:
        await check_qr()
    except Exception as e:  # noqa: BLE001
        print(f"B 站扫码接口失败：{type(e).__name__}: {e}")
    conn = db.connect()
    try:
        for url in URLS:
            print(f"\n下载 {url}")
            if await check_download(conn, url):
                break
    finally:
        conn.close()
        shutil.rmtree(_tmp, ignore_errors=True)
        print(f"\n已删除临时目录：{not _tmp.exists()}")


if __name__ == "__main__":
    asyncio.run(main())
