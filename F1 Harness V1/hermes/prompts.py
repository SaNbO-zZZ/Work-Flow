"""按 B3 的五层组装提示词，并给出版本号（内容哈希），写进每个任务，供 benchmark 追溯。"""
import hashlib
from pathlib import Path

from . import config

BRANCH_FILES = {"前瞻": "preview.md", "资讯": "news.md", "复盘": "race.md", "科普": "science.md"}
COMMON_FILES = ["公共规范.md", "品牌与混音.md"]


def _layers(branch: str) -> list[Path]:
    common = [config.PROMPTS_DIR / "common" / f for f in COMMON_FILES]
    return common + [config.PROMPTS_DIR / "branches" / BRANCH_FILES[branch]]


def version(branch: str) -> str:
    h = hashlib.sha256()
    for p in _layers(branch):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def assemble(branch: str, step_instruction: str, episode_context: str = "", notes: str = "") -> str:
    parts = [p.read_text(encoding="utf-8") for p in _layers(branch)]
    parts.append(f"# 本步骤要做的事\n{step_instruction}")
    if episode_context:
        parts.append(f"# 本期上下文\n{episode_context}")
    if notes:
        parts.append(f"# 经验笔记（已经用户批准）\n{notes}")
    return "\n\n---\n\n".join(parts)
