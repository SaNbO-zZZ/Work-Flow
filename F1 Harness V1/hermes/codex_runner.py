"""调用 Codex CLI：指定模型/推理强度/工作目录，解析 JSONL 事件，记录 token，读取订阅额度。"""
import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import config

QUOTA_MARKERS = ("usage limit", "rate limit", "rate_limit", "quota", "too many requests")


class CodexError(RuntimeError):
    pass


class QuotaExhausted(CodexError):
    """额度用完：调度器只等待恢复，不计入重试次数。"""


@dataclass
class CodexResult:
    thread_id: str | None
    message: str
    usage: dict = field(default_factory=dict)
    events: list = field(default_factory=list)


def _skills_override() -> str:
    items = ",".join(f'{{path="{p}",enabled=false}}' for p in config.HIDDEN_SKILLS)
    return f"skills.config=[{items}]"


def build_args(model: str, effort: str, workdir: Path, *, sandbox: str = "workspace-write",
               network: bool = False, resume_thread: str | None = None,
               output_schema: Path | None = None, ephemeral: bool = False,
               web_search: bool = False, images: list[Path] | None = None) -> list[str]:
    head = [config.CODEX_CLI, "exec"]
    if resume_thread:
        head += ["resume", resume_thread]
    args = head + [
        "--json", "--skip-git-repo-check",
        "-m", model,
        "-c", f'model_reasoning_effort="{effort}"',
        "-c", _skills_override(),
    ]
    if not resume_thread:
        args += ["-C", str(workdir), "-s", sandbox]
    if network:
        args += ["-c", "sandbox_workspace_write.network_access=true"]
    if output_schema:
        args += ["--output-schema", str(output_schema)]
    if ephemeral:
        args.append("--ephemeral")
    if web_search:
        # exec 没有 --search，只能用配置项开启原生搜索工具
        args += ["-c", 'web_search="live"']
    for img in images or []:
        args += ["-i", str(img)]
    return args + ["-"]


def parse_events(lines: list[str]) -> CodexResult:
    thread_id, message, usage, events, errors = None, "", {}, [], []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(ev)
        kind = ev.get("type")
        if kind == "thread.started":
            thread_id = ev.get("thread_id")
        elif kind == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
            message = ev["item"].get("text", "")
        elif kind == "turn.completed":
            for k, v in (ev.get("usage") or {}).items():
                usage[k] = usage.get(k, 0) + int(v or 0)
        elif kind in ("error", "turn.failed"):
            # 断网重连是过程提示；真连不上会再报 turn.failed
            if kind == "error" and str(ev.get("message", "")).startswith("Reconnecting"):
                continue
            errors.append(json.dumps(ev, ensure_ascii=False))
    if errors:
        text = " | ".join(errors)
        raise (QuotaExhausted if any(m in text.lower() for m in QUOTA_MARKERS) else CodexError)(text)
    return CodexResult(thread_id, message, usage, events)


async def run(prompt: str, model: str, effort: str, workdir: Path, *, timeout: float | None = None, **kw) -> CodexResult:
    workdir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        # Hermes Agent 技能脚本默认写 ~/.hermes，改到映像，避免碰用户自己的 Hermes Agent 数据
        "HERMES_HOME": str(config.CORE / "data" / "skill-home"),
        "HERMES_CITATION_LEDGER": str(workdir / "citations.json"),
    }
    proc = await asyncio.create_subprocess_exec(
        *build_args(model, effort, workdir, **kw),
        cwd=str(workdir),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            out, err = await proc.communicate(prompt.encode())
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise CodexError(f"codex 超时（{timeout:.0f} 秒）")
    stderr = err.decode(errors="replace")
    result = parse_events(out.decode(errors="replace").splitlines())
    if proc.returncode != 0:
        low = stderr.lower()
        raise (QuotaExhausted if any(m in low for m in QUOTA_MARKERS) else CodexError)(
            f"codex 退出码 {proc.returncode}: {stderr[-2000:]}")
    return result


def record_usage(conn, result: CodexResult, *, model: str, effort: str, episode_id: int | None = None,
                 task_id: int | None = None, quota_used_percent: float | None = None) -> None:
    from .db import now
    u = result.usage
    conn.execute(
        """INSERT INTO usage(ts, episode_id, task_id, model, effort, input_tokens, cached_tokens, cache_write_tokens,
                             output_tokens, reasoning_tokens, quota_used_percent) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (now(), episode_id, task_id, model, effort, u.get("input_tokens", 0), u.get("cached_input_tokens", 0),
         u.get("cache_write_input_tokens", 0), u.get("output_tokens", 0), u.get("reasoning_output_tokens", 0),
         quota_used_percent))


async def read_rate_limits() -> dict | None:
    """通过 app-server 读取订阅额度；只读，不消耗额度。"""
    requests = [
        {"method": "initialize", "id": 0, "params": {"clientInfo": {"name": "hermes", "title": "Hermes", "version": "0.1"}}},
        {"method": "initialized"},
        {"method": "account/rateLimits/read", "id": 1},
    ]
    proc = await asyncio.create_subprocess_exec(
        config.CODEX_CLI, "app-server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdin and proc.stdout
    proc.stdin.write(("\n".join(json.dumps(r) for r in requests) + "\n").encode())
    await proc.stdin.drain()
    try:
        async with asyncio.timeout(20):
            while line := await proc.stdout.readline():
                msg = json.loads(line)
                if msg.get("id") == 1:
                    return msg.get("result", {}).get("rateLimits")
    except (TimeoutError, json.JSONDecodeError):
        return None
    finally:
        proc.kill()
        await proc.wait()
    return None


def available_models() -> list[dict]:
    """聊天框下拉用：从 Codex 的模型缓存读取可见模型及其推理档位。"""
    try:
        data = json.loads(config.CODEX_MODELS_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [
        {
            "slug": m["slug"],
            "name": m.get("display_name", m["slug"]),
            "efforts": [e["effort"] if isinstance(e, dict) else e for e in m.get("supported_reasoning_levels", [])],
        }
        for m in data.get("models", [])
        if m.get("visibility") == "list"
    ]
