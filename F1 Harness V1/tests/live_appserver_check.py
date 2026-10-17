"""实测 app-server：开对话、按消息指定模型和推理强度、逐字输出、token 更新。用法：.venv/bin/python tests/live_appserver_check.py"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes import config  # noqa: E402


async def main() -> None:
    p = await asyncio.create_subprocess_exec(config.CODEX_CLI, "app-server", stdin=asyncio.subprocess.PIPE,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    seq = 0

    async def call(method, params=None):
        nonlocal seq
        seq += 1
        p.stdin.write((json.dumps({"method": method, "id": seq, "params": params or {}}) + "\n").encode())
        await p.stdin.drain()
        return seq

    await call("initialize", {"clientInfo": {"name": "hermes", "title": "Hermes", "version": "0.1"}})
    p.stdin.write(b'{"method":"initialized"}\n')
    tid_req = await call("thread/start", {"cwd": str(config.CORE / "data"), "sandbox": "read-only",
                                          "approvalPolicy": "never", "ephemeral": True, "model": "gpt-6-astra"})
    thread_id, methods, text = None, {}, ""
    async with asyncio.timeout(240):
        while line := await p.stdout.readline():
            msg = json.loads(line)
            if msg.get("id") == tid_req:
                thread_id = msg["result"]["thread"]["id"]
                await call("turn/start", {"threadId": thread_id, "model": "gpt-6-sol", "effort": "low",
                                          "input": [{"type": "text", "text": "用一句话介绍 F1 雪邦赛道"}]})
            m = msg.get("method")
            if m:
                methods[m] = methods.get(m, 0) + 1
                if m.endswith("agentMessage/delta"):
                    text += msg["params"]["delta"]
                if m == "thread/tokenUsage/updated":
                    usage = msg["params"]["tokenUsage"]["total"]
                if m == "turn/completed":
                    break
    p.kill()
    print("收到的事件：", methods)
    print("逐字拼出的回复：", text)
    print("token：", usage)


asyncio.run(main())
