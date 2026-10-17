"""常驻的 codex app-server 客户端（JSON-RPC over stdio）：开/续对话、按消息指定模型和推理强度、逐字输出、token 与额度推送。"""
import asyncio
import json
import logging
from collections import defaultdict
from typing import Any, Callable

from . import config

log = logging.getLogger("hermes.appserver")
Listener = Callable[[str, dict], None]


class AppServerError(RuntimeError):
    pass


class AppServer:
    def __init__(self, cli: str = config.CODEX_CLI, *, capabilities: dict | None = None):
        self.cli = cli
        self.capabilities = capabilities
        self.proc: asyncio.subprocess.Process | None = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._thread_listeners: dict[str, list[Listener]] = defaultdict(list)
        self._global_listeners: list[Listener] = []
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.rate_limits: dict | None = None

    async def ensure(self) -> None:
        async with self._lock:
            if self.proc and self.proc.returncode is None:
                return
            self.proc = await asyncio.create_subprocess_exec(
                self.cli, "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024)
            self._reader = asyncio.create_task(self._read_loop())
            params = {"clientInfo": {"name": "hermes", "title": "F1 Harness", "version": "1.0"}}
            if self.capabilities is not None:
                params["capabilities"] = self.capabilities
            await self._request_raw("initialize", params)
            await self._send({"method": "initialized"})

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()

    # ---------- 底层收发 ----------
    async def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def _request_raw(self, method: str, params: dict | None = None, timeout: float = 60) -> Any:
        self._seq += 1
        rid = self._seq
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"method": method, "id": rid, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def request(self, method: str, params: dict | None = None, timeout: float = 60) -> Any:
        await self.ensure()
        return await self._request_raw(method, params, timeout)

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while line := await self.proc.stdout.readline():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                fut = self._pending.get(msg["id"])
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(AppServerError(json.dumps(msg["error"], ensure_ascii=False)))
                    else:
                        fut.set_result(msg["result"])
                continue
            method, params = msg.get("method"), msg.get("params") or {}
            if not method:
                continue
            if "id" in msg:
                await self._handle_server_request(msg)
                continue
            if method == "account/rateLimits/updated":
                self.rate_limits = params.get("rateLimits", params)
            for fn in list(self._global_listeners):
                fn(method, params)
            tid = params.get("threadId") or (params.get("thread") or {}).get("id")
            for fn in list(self._thread_listeners.get(tid, [])):
                fn(method, params)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AppServerError("codex app-server 已退出"))

    async def _handle_server_request(self, msg: dict) -> None:
        """普通聊天不自动批准；专用客户端可处理明确授权的请求。"""
        await self._send({"id": msg["id"], "error": {"code": -32601,
                          "message": "This client does not handle this server request"}})

    # ---------- 业务接口 ----------
    def on_thread(self, thread_id: str, fn: Listener) -> Callable[[], None]:
        self._thread_listeners[thread_id].append(fn)
        return lambda: self._thread_listeners[thread_id].remove(fn)

    def on_any(self, fn: Listener) -> None:
        self._global_listeners.append(fn)

    async def start_thread(self, cwd: str, instructions: str, model: str, effort: str, sandbox: str = "workspace-write",
                           *, approval_policy: str = "never", ephemeral: bool = False) -> str:
        params = {
            "cwd": cwd, "model": model, "sandbox": sandbox, "approvalPolicy": approval_policy,
            "developerInstructions": instructions,
            "config": {"model_reasoning_effort": effort,
                       "skills": {"config": [{"path": str(p), "enabled": False} for p in config.HIDDEN_SKILLS]}},
        }
        if ephemeral:
            params["ephemeral"] = True
        res = await self.request("thread/start", params)
        return res["thread"]["id"]

    async def resume_thread(self, thread_id: str) -> None:
        await self.request("thread/resume", {"threadId": thread_id})

    async def run_turn(self, thread_id: str, text: str, model: str, effort: str,
                       on_delta: Callable[[str], None], timeout: float = 1800) -> dict:
        """发一条消息，逐字回调；返回最终文本和本轮 token。"""
        done = asyncio.get_running_loop().create_future()
        state = {"text": "", "usage": None, "error": None}

        def listen(method: str, params: dict) -> None:
            if method == "item/agentMessage/delta":
                state["text"] += params.get("delta", "")
                on_delta(params.get("delta", ""))
            elif method == "thread/tokenUsage/updated":
                state["usage"] = params["tokenUsage"].get("last")
            elif method == "error":
                state["error"] = json.dumps(params, ensure_ascii=False)
            elif method == "turn/completed" and not done.done():
                done.set_result(params.get("turn", {}))

        off = self.on_thread(thread_id, listen)
        try:
            await self.request("turn/start", {"threadId": thread_id, "model": model, "effort": effort,
                                              "input": [{"type": "text", "text": text}]})
            turn = await asyncio.wait_for(done, timeout)
        finally:
            off()
        status = turn.get("status")
        if status not in (None, "completed"):
            state["error"] = state["error"] or json.dumps(turn.get("error") or turn, ensure_ascii=False)[:2000]
        return {"text": state["text"], "usage": state["usage"] or {}, "error": state["error"], "status": status}

    async def read_rate_limits(self) -> dict | None:
        try:
            res = await self.request("account/rateLimits/read", timeout=20)
            self.rate_limits = res.get("rateLimits")
        except (AppServerError, TimeoutError) as e:
            log.warning("读取额度失败：%s", e)
        return self.rate_limits
