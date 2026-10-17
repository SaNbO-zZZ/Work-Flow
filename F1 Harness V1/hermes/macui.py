"""调用本机界面操作小工具 tools/bin/hermes-ui（由服务进程拉起，系统权限算在 F1 Harness 名下）。"""
import json
import subprocess

from . import config

UI_BIN = config.CORE / "tools" / "bin" / "hermes-ui"
SETTINGS_URLS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
}


def run(*args: str, timeout: float = 30) -> dict:
    if not UI_BIN.exists():
        raise RuntimeError(f"缺少界面操作工具：{UI_BIN}（swiftc -O -o {UI_BIN} {config.CORE / 'tools' / 'macui.swift'}）")
    proc = subprocess.run([str(UI_BIN), *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"hermes-ui {' '.join(args)} 失败：{proc.stderr.strip()[-300:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def permissions(request: bool = False) -> dict:
    """{"accessibility": bool, "screen": bool}；request=True 时让系统把 F1 Harness 加进两个权限列表并弹提示。"""
    return run("perm", *(["--request"] if request else []))


def open_settings(kind: str) -> None:
    subprocess.run(["/usr/bin/open", SETTINGS_URLS[kind]], check=False)
