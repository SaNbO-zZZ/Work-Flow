"""剪映专业版自动配音（M5-1）。

剪映草稿是加密的，程序写不了，只能操作界面：
新建草稿 → 字幕 → 新建字幕 → 手动写字幕（智能文案窗口）→ 粘贴逐字稿 → 配音选择 → 选音色 → 添加到轨道。
剪映把每句配音存到草稿的 textReading/ 下（扩展名 .wav，实际是 mp3），文件顺序是乱的，由 voice_post 用语音识别排序。

找控件优先按辅助功能里的控件名（剪映给控件起了 automation 名字），找不到再截图做文字识别。
"""
import json
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, macui
from .local_tags import ocr

log = logging.getLogger("hermes.jianying")

BUNDLE = "com.lemon.lvpro"
DRAFTS_ROOT = config.ROOT / "jianying" / "drafts" / "JianyingPro Drafts"
DEFAULT_VOICE = "热血解说员"

# 弹窗规则：窗口标题 → 处理办法（按 esc 关掉）。新见到的弹窗加在这里
POPUP_TITLES = {"版本更新", "提示", "新功能介绍", "温馨提示"}
KNOWN_TITLES = {"剪映专业版", "智能文案", ""}


class UIError(RuntimeError):
    """界面上没找到预期的东西；调用方据此降级到下一级办法。"""


@dataclass
class El:
    role: str
    name: str      # desc / title / value 里第一个非空的
    value: str
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2


# ---------- 基础操作 ----------

def _flatten(node: dict, out: list[El]) -> None:
    role = node.get("role", "")
    if role.startswith("AXMenu"):
        return
    f = node.get("frame")
    name = node.get("desc") or node.get("title") or ""
    value = str(node.get("value", ""))
    if f and (name or value):
        out.append(El(role, name or value, value, f["x"], f["y"], f["w"], f["h"]))
    for c in node.get("children", []):
        _flatten(c, out)


def elements() -> list[El]:
    _guard()  # 剪映不在前台时，辅助功能读不到它的控件
    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        macui.run("ax", BUNDLE, tmp.name, timeout=60)
        tree = json.loads(Path(tmp.name).read_text())
    out: list[El] = []
    _flatten(tree, out)
    return out


def find(name: str, els: list[El] | None = None, *, exact: bool = False) -> list[El]:
    els = elements() if els is None else els
    return [e for e in els if (e.name == name if exact else name in e.name) and e.w > 0 and e.h > 0]


def wait_for(name: str, timeout: float = 15, *, exact: bool = False) -> El:
    end = time.monotonic() + timeout
    while True:
        hits = find(name, exact=exact)
        if hits:
            return hits[0]
        if time.monotonic() > end:
            raise UIError(f"等了 {timeout:.0f} 秒没出现：{name}")
        time.sleep(0.8)


def _guard() -> None:
    """每次点击、按键前确认剪映在前台：用户中途切走时先切回来，切不回来就报错，绝不点到别的应用里。"""
    if macui.run("front")["bundle"] != BUNDLE:
        front()


def click(x: float, y: float, *, double: bool = False) -> None:
    _guard()
    macui.run("click", f"{x:.0f}", f"{y:.0f}", *(["--double"] if double else []))


def tap(name: str, *, exact: bool = False, timeout: float = 10) -> El:
    el = wait_for(name, timeout, exact=exact)
    click(*el.center)
    return el


def key(combo: str) -> None:
    _guard()
    macui.run("key", combo)


def windows() -> list[dict]:
    _guard()
    return macui.run("windows", BUNDLE, "--all")["windows"]


def window(title: str) -> dict | None:
    return next((w for w in windows() if w["title"].strip() == title), None)


def main_window() -> dict:
    ws = [w for w in windows() if w["layer"] == 0]
    if not ws:
        raise UIError("剪映没有可见窗口")
    return max(ws, key=lambda w: w["w"] * w["h"])


def front() -> None:
    """切到剪映。NSRunningApplication.activate 不会切换桌面空间，open -b 会。"""
    for _ in range(3):
        subprocess.run(["/usr/bin/open", "-b", BUNDLE], check=False)
        time.sleep(1.2)
        if macui.run("front")["bundle"] == BUNDLE:
            fit()
            return
    raise UIError("剪映切不到前台")


def fit() -> list[dict]:
    """窗口有一部分在屏幕外时，新建草稿后的确认框会落在看不见的地方；整个挪进主屏幕。"""
    moved = macui.run("fit", BUNDLE)["moved"]
    if moved:
        log.info("剪映窗口挪进主屏幕：%s", moved)
        time.sleep(0.5)
    return moved


def shot(win: dict, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", str(win["id"]), str(out)], check=True, timeout=20)
    return out


def ocr_window(win: dict, out: Path) -> list[dict]:
    """截窗口做文字识别，坐标换成屏幕坐标：[{t, x, y}]（x、y 是文字中心）。"""
    shot(win, out)
    lines = ocr([out]).get(str(out), [])
    return [{"t": ln["t"], "x": win["x"] + (ln["x"] + ln["w"] / 2) * win["w"],
             "y": win["y"] + (ln["y"] + ln["h"] / 2) * win["h"]} for ln in lines]


def click_text(win: dict, text: str, out: Path, *, prefer: str = "last") -> dict:
    hits = [ln for ln in ocr_window(win, out) if text in ln["t"].replace(" ", "")]
    if not hits:
        raise UIError(f"窗口「{win['title']}」里没认出文字：{text}")
    hit = hits[-1] if prefer == "last" else hits[0]
    click(hit["x"], hit["y"])
    return hit


def paste(text: str) -> None:
    """用剪贴板粘贴长文本，粘完恢复用户原来的剪贴板文字。"""
    old = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, text=True).stdout
    subprocess.run(["/usr/bin/pbcopy"], input=text, text=True, check=True)
    got = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, text=True).stdout
    if got.strip() != text.strip():
        raise UIError("剪贴板没写进去（可能被别的程序同时改了）")
    key("cmd+v")
    time.sleep(0.8)
    subprocess.run(["/usr/bin/pbcopy"], input=old, text=True, check=False)


def dismiss_popups() -> list[str]:
    closed = []
    for w in windows():
        t = w["title"].strip()
        if t in POPUP_TITLES:
            click(w["x"] + w["w"] / 2, w["y"] + 12)
            key("esc")
            closed.append(t)
    return closed


# ---------- 整套配音流程 ----------

MAX_CHARS = 5000          # 智能文案窗口的字数上限
SETTLE_SECONDS = 8        # textReading 文件数这么久不变、且没有进度弹窗，算生成完


def _drafts() -> set[str]:
    return {p.name for p in DRAFTS_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")}


def _small_dialogs() -> list[dict]:
    """剪映的确认框、进度框都是没标题的小窗口；切草稿时还会闪过全屏无标题的过渡窗，要排除。"""
    return [w for w in windows() if not w["title"].strip() and 100 < w["w"] < 900 and 100 < w["h"] < 700]


def _wait(pred, timeout: float, what: str, every: float = 0.8):
    end = time.monotonic() + timeout
    while True:
        got = pred()
        if got:
            return got
        if time.monotonic() > end:
            raise UIError(f"等了 {timeout:.0f} 秒：{what}")
        time.sleep(every)


def _new_draft(shots: "_Shots") -> Path:
    before = _drafts()
    if not macui.run("menu", BUNDLE, "文件", "新建草稿").get("ok"):
        raise UIError("菜单 文件→新建草稿 点不了")
    # 已经打开着草稿时会问"是否确认新建草稿"，点确认（当前草稿剪映会自动保存，不会被改）
    end = time.monotonic() + 6
    while time.monotonic() < end:
        if _drafts() - before:
            break
        dlg = _small_dialogs()
        if dlg:
            try:
                click_text(dlg[0], "确认", shots.next("确认新建"))
                break
            except UIError:
                pass
        time.sleep(0.5)
    name = _wait(lambda: sorted(_drafts() - before), 20, "新草稿目录没出现")[0]
    log.info("新建草稿：%s", name)
    return DRAFTS_ROOT / name


class _Shots:
    """每步截图留证：logs/剪映配音/01_xxx.png …"""

    def __init__(self, folder: Path):
        self.folder, self.n = folder, 0
        folder.mkdir(parents=True, exist_ok=True)

    def next(self, label: str) -> Path:
        self.n += 1
        return self.folder / f"{self.n:02d}_{label}.png"

    def take(self, label: str, title: str | None = None) -> None:
        try:
            shot(window(title) if title else main_window(), self.next(label))
        except Exception as e:  # 截图失败不影响流程
            log.warning("截图失败 %s：%s", label, e)

    def evidence(self, label: str) -> None:
        """失败时留整屏截图和界面树，事后能看出卡在哪。"""
        try:
            subprocess.run(["/usr/sbin/screencapture", "-x", str(self.next(f"{label}_整屏"))], timeout=20, check=False)
            macui.run("ax", BUNDLE, str(self.next(f"{label}_界面树").with_suffix(".json")), timeout=60)
        except Exception as e:  # noqa: BLE001
            log.warning("留失败现场失败：%s", e)


VOICE_TABS = ("超仿真", "收藏", "热门")   # 热血解说员实测在“超仿真”，不在默认打开的页


def _pick_voice(win: dict, voice: str, shots: "_Shots") -> None:
    """先在当前页等音色卡片，没有就依次切分类找；底栏出现选中的音色名才算选上。"""
    name = f"OnlineResourceInfoView:{voice}"
    card = None
    for tab in (None, *VOICE_TABS):
        if tab:
            try:
                click_text(win, tab, shots.next(f"分类_{tab}"), prefer="first")
            except UIError:
                continue
        try:
            card = wait_for(name, 20 if tab is None else 10, exact=True)
            break
        except UIError:
            log.info("音色 %s 不在%s", voice, f"“{tab}”页" if tab else "默认页")
    if card is None:
        raise UIError(f"各分类都没找到音色：{voice}")
    for _ in range(3):
        click(*card.center)
        time.sleep(1.5)
        bar = [ln for ln in ocr_window(win, shots.next("已选音色"))
               if voice in ln["t"].replace(" ", "") and ln["y"] > win["y"] + win["h"] * 0.85]
        if bar:
            return
    raise UIError(f"音色没选上：{voice}")


def _audio_files(draft: Path) -> list[Path]:
    d = draft / "textReading"
    return sorted(p for p in d.glob("*.wav") if not p.name.startswith("._")) if d.is_dir() else []


def generate(text: str, out_dir: Path, shots_dir: Path, *, voice: str = DEFAULT_VOICE,
             timeout: float = 900, keep_draft: bool = False) -> dict:
    """新建草稿 → 粘贴文本 → 选音色 → 添加到轨道 → 等配音生成完 → 复制到 out_dir。

    返回 {"draft": 草稿名, "files": [复制后的路径]}。界面不对时抛 UIError，调用方降级。
    文件顺序是乱的，由 voice_post 用语音识别排序。
    """
    text = text.strip()
    if not text:
        raise ValueError("配音文本是空的")
    if len(text) > MAX_CHARS:
        raise ValueError(f"配音文本 {len(text)} 字，超过剪映上限 {MAX_CHARS}")
    shots = _Shots(shots_dir)
    try:
        return _generate(text, out_dir, shots, voice=voice, timeout=timeout, keep_draft=keep_draft)
    except UIError:
        shots.evidence("失败")
        raise


def _generate(text: str, out_dir: Path, shots: "_Shots", *, voice: str, timeout: float, keep_draft: bool) -> dict:
    front()
    dismiss_popups()
    draft = _new_draft(shots)
    shots.take("新草稿")

    tap("root_字幕", exact=True)
    time.sleep(1.5)
    # 左侧树刚切过来时位置还会变，点"新建字幕"后等不到按钮就重点一次
    for attempt in range(3):
        tap("VETreeMainCellItem:新建字幕", exact=True)
        try:
            tap("automationaiwriteSubtitleButton", exact=True, timeout=5)
            break
        except UIError:
            if attempt == 2:
                shots.take("没找到手动写字幕")
                raise
    win = _wait(lambda: window("智能文案"), 15, "智能文案窗口没打开")
    time.sleep(1.5)

    for attempt in range(2):
        click_text(win, "输入文案后", shots.next("文案框"), prefer="first")
        time.sleep(0.5)
        paste(text)
        time.sleep(1)
        # 字数栏还是「0/5000」说明没粘进去
        if not any(ln["t"].replace(" ", "").startswith("0/5000")
                   for ln in ocr_window(win, shots.next("已粘贴"))):
            break
        if attempt == 1:
            raise UIError("文本没粘进智能文案框")

    click_text(win, "配音选择", shots.next("配音选择"))
    _pick_voice(win, voice, shots)

    click_text(win, "添加到轨道", shots.next("添加到轨道"))

    # 等：先出"自动拆句中"进度框，然后 textReading 下陆续出现文件；文件数稳定且进度框消失算完
    end = time.monotonic() + timeout
    last, stable_since = -1, time.monotonic()
    while True:
        n = len(_audio_files(draft))
        busy = bool(_small_dialogs())
        if n != last:
            last, stable_since = n, time.monotonic()
        if n > 0 and not busy and time.monotonic() - stable_since >= SETTLE_SECONDS:
            break
        if time.monotonic() > end:
            raise UIError(f"配音 {timeout:.0f} 秒没生成完（已有 {n} 个文件）")
        time.sleep(1)
    shots.take("生成完")

    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for src in _audio_files(draft):
        dst = out_dir / src.name
        dst.write_bytes(src.read_bytes())
        files.append(dst)
    log.info("剪映配音完成：草稿 %s，%d 个文件", draft.name, len(files))
    if not keep_draft:
        discard_draft(draft)
    return {"draft": draft.name, "files": files}


def discard_draft(draft: Path) -> None:
    """回首页关掉草稿，再用访达移到废纸篓（可恢复）。失败只记日志。"""
    try:
        macui.run("menu", BUNDLE, "剪映专业版", "返回首页")
        time.sleep(2)
        subprocess.run(["/usr/bin/osascript", "-e",
                        f'tell application "Finder" to delete (POSIX file "{draft}" as alias)'],
                       check=True, capture_output=True, timeout=30)
        (draft.parent / f"._{draft.name}").unlink(missing_ok=True)
    except Exception as e:
        log.warning("草稿 %s 没删掉：%s", draft.name, e)


# ---------- 调试入口 ----------

def _cli(argv: list[str]) -> None:
    cmd, *rest = argv
    if cmd == "ls":
        for e in elements():
            if not rest or rest[0] in e.name:
                print(f"{e.role} | {e.name[:60]} | {e.x:.0f} {e.y:.0f} {e.w:.0f} {e.h:.0f}")
    elif cmd == "win":
        for w in windows():
            print(w)
    elif cmd == "shot":
        title = rest[1] if len(rest) > 1 else None
        print(shot(window(title) if title else main_window(), Path(rest[0])))
    elif cmd == "ocr":
        w = window(rest[1]) if len(rest) > 1 else main_window()
        for ln in ocr_window(w, Path(rest[0])):
            print(f'{ln["x"]:.0f} {ln["y"]:.0f} {ln["t"]}')
    elif cmd == "tap":
        print(tap(rest[0]))
    elif cmd == "front":
        front()
        print(dismiss_popups())
    elif cmd == "gen":  # gen <文本文件> <输出目录>
        out = Path(rest[1])
        r = generate(Path(rest[0]).read_text(), out, out / "_shots")
        print(r["draft"], len(r["files"]))


if __name__ == "__main__":
    import sys
    _cli(sys.argv[1:])
