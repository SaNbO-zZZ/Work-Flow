"""版本保留和会话卡片（用户 2026-10-13 定，第 3 批）。

每次进关卡把要审的东西复制到 期文件夹/09_版本/<关卡>-v<序号>/（保持原来的相对路径），
找素材、图形、配音每轮也各存一份（找素材-r<轮次>/ 等）。09_版本 只留本地、不传 Drive，跟期文件夹 7 天后一起清理。
会话里的卡片消息（role='card'）只记类型和指向，网页取会话时由这里补上版本列表和实时状态。
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from . import asset_identity, db

log = logging.getLogger("hermes.versions")

DIR = "09_版本"
GATE_FILES = {
    "G0": ["01_资料/选题.md", "01_资料/资料.md"],
    "G1": ["02_文案/逐字稿.md", "02_文案/标题和简介.txt"],
    "G2": ["03_配音/试听.mp3", "06_合成/粗剪.mp4", "06_合成/时间线.json", "05_图形/叠层.json", "05_图形/*.png"],
    "G3": ["06_合成/成片.mp4", "06_合成/封面_横版.png", "06_合成/封面_竖版.png", "02_文案/标题和简介.txt",
           "07_QA/硬检查.json", "07_QA/AI评审.json", "07_QA/质检汇总.json"],
}
TEXT_MAX = 60_000   # 卡片里直接带的文字上限（逐字稿、选题都远小于这个）


def _files(folder: Path, patterns: list[str]) -> list[str]:
    out = []
    for pat in patterns:
        hits = sorted(folder.glob(pat)) if "*" in pat else [folder / pat]
        out += [str(p.relative_to(folder)) for p in hits if p.is_file() and not p.name.startswith("._")]
    return out


def copy(folder: Path, name: str, rels: list[str], meta: dict | None = None) -> Path:
    """把 rels 复制到 09_版本/<name>/（相对路径不变），写 版本.json；已有同名版本先清掉重存。"""
    dst = folder / DIR / name
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    for rel in rels:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(folder / rel, dst / rel)
    (dst / "版本.json").write_text(json.dumps({**(meta or {}), "files": rels, "at": db.now()},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
    return dst


def gate_number(conn, gate_id: int) -> tuple[str, int]:
    g = conn.execute("SELECT episode_id, gate FROM gates WHERE id=?", (gate_id,)).fetchone()
    n = conn.execute("SELECT COUNT(*) FROM gates WHERE episode_id=? AND gate=? AND id<=?",
                     (g["episode_id"], g["gate"], gate_id)).fetchone()[0]
    return g["gate"], n


def gate_job(conn, episode_id: int, gate_id: int) -> tuple | None:
    """进关卡时在事务里记下要存哪一版；真正复制（成片 100 多 MB）放到事务外、线程里做。"""
    ep = conn.execute("SELECT folder FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep or not ep["folder"]:
        return None
    gate, n = gate_number(conn, gate_id)
    return Path(ep["folder"]), gate, n, gate_id


def snapshot_gate(folder: Path, gate: str, n: int, gate_id: int) -> Path | None:
    """存这一版要审的东西。出错只记日志，不挡流程。"""
    try:
        return copy(folder, f"{gate}-v{n}", _files(folder, GATE_FILES[gate]), {"gate": gate, "gate_id": gate_id, "v": n})
    except OSError as e:
        log.warning("存版本失败（%s，%s-v%s）：%s", folder.name, gate, n, e)
        return None


def snapshot_run(ep, name: str, rels: list[str]) -> str | None:
    """找素材、图形、配音每轮存一份，返回版本目录的相对路径（给卡片用）。"""
    folder = Path(ep["folder"])
    try:
        copy(folder, name, [r for r in rels if (folder / r).is_file()])
        return f"{DIR}/{name}"
    except OSError as e:
        log.warning("存版本失败（期 %s，%s）：%s", ep["id"], name, e)
        return None


def add_card(conn, episode_id: int, card: dict, text: str = "") -> int:
    cur = conn.execute("INSERT INTO messages(episode_id, role, content, card, created_at) VALUES (?, 'card', ?, ?, ?)",
                       (episode_id, text, json.dumps(card, ensure_ascii=False), db.now()))
    return cur.lastrowid


def update_card(conn, message_id: int, card: dict, text: str | None = None) -> None:
    if text is None:
        conn.execute("UPDATE messages SET card=? WHERE id=?", (json.dumps(card, ensure_ascii=False), message_id))
    else:
        conn.execute("UPDATE messages SET card=?, content=? WHERE id=?",
                     (json.dumps(card, ensure_ascii=False), text, message_id))


def find_card(conn, episode_id: int, kind: str, run: int) -> tuple[int, dict] | None:
    for m in conn.execute("SELECT id, card FROM messages WHERE episode_id=? AND role='card' ORDER BY id DESC",
                          (episode_id,)):
        c = json.loads(m["card"] or "{}")
        if c.get("type") == kind and c.get("run") == run:
            return m["id"], c
    return None


# ---------- 给网页的数据 ----------

def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")[:TEXT_MAX]
    except OSError:
        return None


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _content(gate: str, base: Path) -> dict:
    """每版带上卡片要显示的文字和数字。"""
    if gate == "G0":
        return {"topic": _read(base / "01_资料" / "选题.md")}
    if gate == "G1":
        td = (_read(base / "02_文案" / "标题和简介.txt") or "").strip()
        title, _, desc = td.partition("\n")
        return {"script": _read(base / "02_文案" / "逐字稿.md"), "title": title.strip(), "desc": desc.strip()}
    if gate == "G2":
        tl = _json(base / "06_合成" / "时间线.json")
        ov = _json(base / "05_图形" / "叠层.json").get("overlays") or []
        return {"duration": tl.get("duration"), "shots": len(tl.get("video") or []),
                "overlays": [{"path": o["path"], "kind": o.get("kind"), "start": o["start"], "end": o["end"]}
                             for o in ov if o.get("path")]}
    if gate == "G3":
        hard, ai = _json(base / "07_QA" / "硬检查.json"), _json(base / "07_QA" / "AI评审.json")
        items = hard.get("items") or []
        return {"hard": {"passed": hard.get("passed"), "count": len(items),
                         "bad": [i["name"] for i in items if not i.get("ok")]},
                "ai": ai.get("total")}
    return {}


def gate_versions(conn, ep) -> dict:
    """各关卡每一版：关卡记录 + 存下的文件位置 + 卡片要的文字。期 14 等第 3 批之前的旧版没存文件，dir 为 None；
    每种关卡最新一版没存到时用当前文件（live）。"""
    folder = Path(ep["folder"]) if ep["folder"] else None
    out: dict[str, list] = {}
    rows = conn.execute("SELECT * FROM gates WHERE episode_id=? ORDER BY id", (ep["id"],)).fetchall()
    for i, g in enumerate(rows):
        lst = out.setdefault(g["gate"], [])
        v = {"v": len(lst) + 1, "gate_id": g["id"], "state": g["state"], "auto": g["auto"], "feedback": g["feedback"],
             "created_at": g["created_at"], "decided_at": g["decided_at"], "dir": None, "live": False, "files": []}
        name = f"{g['gate']}-v{v['v']}"
        if folder and (folder / DIR / name / "版本.json").is_file():
            v["dir"] = f"{DIR}/{name}"
            v["files"] = _json(folder / DIR / name / "版本.json").get("files", [])
        elif folder and folder.is_dir() and not any(r["gate"] == g["gate"] for r in rows[i + 1:]):
            v.update(dir="", live=True, files=_files(folder, GATE_FILES.get(g["gate"], [])))
        if v["dir"] is not None:
            v.update(_content(g["gate"], folder / v["dir"] if v["dir"] else folder))
        lst.append(v)
    return out


def enrich(conn, ep, card: dict) -> dict:
    """找素材卡片补上下载实时状态、素材标题和时长。"""
    if card.get("type") != "sourcing":
        return card
    folder = Path(ep["folder"]) if ep["folder"] else None
    data = {}
    if folder:
        data = _json(folder / card["file"]) if card.get("file") else {}
        if not data:
            live = _json(folder / "04_素材" / "素材清单.json")
            data = live if live.get("stage_run") == card.get("run") else {}
    found = []
    for f in data.get("found") or []:
        f = dict(f)
        if f.get("download_id"):
            row = conn.execute("SELECT state, error, asset_id FROM downloads WHERE id=?", (f["download_id"],)).fetchone()
            if row and f.get("state") == "下载中":
                if row["state"] == "failed":
                    f.update(state="下载失败", note=((row["error"] or "").strip().splitlines() or ["下载失败"])[0][:200])
                elif row["state"] == "done":
                    f.update(state="已下载", asset_id=row["asset_id"])
        if f.get("asset_id"):
            a = conn.execute("SELECT title, duration FROM assets WHERE id=?", (f["asset_id"],)).fetchone()
            if a:
                f.update(asset_title=a["title"], duration=a["duration"])
        found.append(f)
    ids = data.get("asset_ids") or []
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT * FROM assets WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    s = data.get("summary") or {}
    race = data.get("race")
    return {**card, "found": found, "segments": s.get("segments") or [], "gpt_called": s.get("gpt_called"),
            "forced_segments": s.get("forced_segments") or [], "lib_notes": s.get("library_notes") or [],
            "race": race, "other_library": data.get("other_library"),
            "assets": [{"id": i, "title": rows[i]["title"] if i in rows else "",
                        **({"label": asset_identity.label(rows[i]), "same_race": asset_identity.same_race(rows[i], race)}
                           if race and i in rows else {})}
                       for i in ids]}
