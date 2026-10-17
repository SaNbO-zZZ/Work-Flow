"""配音专用 Codex 客户端：保留只读沙箱，只响应用户授权的剪映访问请求。

2026-10-14 用户明确授权后台操作剪映生成配音/字幕。该授权仅用于
这个入口，不写入全局 Always allow，也不批准其他应用或命令升级。
"""
import asyncio
import json
from pathlib import Path

from .appserver import AppServer
from .codex_runner import CodexError, CodexResult, QuotaExhausted, QUOTA_MARKERS

APP_ID = "com.lemon.lvpro"
APP_PATH = "/Applications/VideoFusion-macOS.app"
ACCESS_TOOLS = {"get_app", "get_app_state", "get_app_state_and_screenshot",
                "get_app_screenshot", "launch_app"}


def approval_response(request: dict) -> dict:
    """精确匹配应用访问；输入/录音/其他应用/URL/未知请求均拒绝。"""
    declined = {"action": "decline", "content": None, "_meta": {}}
    if not isinstance(request, dict):
        return declined
    meta = request.get("_meta") or {}
    if not isinstance(meta, dict):
        return declined
    params = meta.get("tool_params") or {}
    schema = request.get("requestedSchema") or {}
    if not isinstance(params, dict) or not isinstance(schema, dict):
        return declined
    allowed = (
        request.get("serverName") in {"cua_repl", "node_repl"}
        and request.get("mode") in {"form", "openai/form"}
        and meta.get("codex_approval_kind") == "mcp_tool_call"
        and meta.get("connector_id") == "computer-use"
        and params.get("app") == APP_ID
        and meta.get("tool_name") in ACCESS_TOOLS
        and meta.get("riskLevel") == "low"
        and not meta.get("codex_sensitive_action")
        and not meta.get("codex_requires_user_input")
        and schema.get("type") == "object"
        and not schema.get("properties")
        and not schema.get("required")
    )
    return {"action": "accept" if allowed else "decline",
            "content": {} if allowed else None,
            "_meta": {"persist": "session"} if allowed else {}}


class JianyingAppServer(AppServer):
    def __init__(self, audit_path: Path):
        super().__init__(capabilities={"experimentalApi": True,
                                      "mcpServerOpenaiFormElicitation": True})
        self.audit_path = audit_path
        self.events = []
        self.on_any(self._audit_event)

    def _audit_event(self, method: str, params: dict) -> None:
        if method in {"item/agentMessage/delta", "item/reasoning/textDelta",
                      "item/reasoning/summaryTextDelta"}:
            return
        event = {"method": method, "params": params}
        self.events.append(event)
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    async def _handle_server_request(self, msg: dict) -> None:
        method, params = msg["method"], msg.get("params") or {}
        if method == "mcpServer/elicitation/request":
            response = approval_response(params)
        elif method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            response = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            response = {"permissions": {}, "scope": "turn"}
        else:
            self._audit_event("approval/unhandled", {"method": method})
            await super()._handle_server_request(msg)
            return
        self._audit_event("approval/decision", {"request": params, "response": response})
        await self._send({"id": msg["id"], "result": response})


async def run(prompt: str, model: str, effort: str, workdir: Path, *, timeout: float = 1200) -> CodexResult:
    workdir.mkdir(parents=True, exist_ok=True)
    audit = workdir / "computer-use-events.jsonl"
    server = JianyingAppServer(audit)
    instructions = ("仅使用 Computer Use/cua_repl 操作剪映专业版，主应用路径 " + APP_PATH +
                    "。用户已授权此入口访问剪映用于配音/字幕。"
                    "禁止用 shell、AppleScript、System Events 或其他技术操作界面；"
                    "不得操作其他应用、安装软件、修改权限、读取密钥或删除现有草稿。")
    try:
        async with asyncio.timeout(timeout):
            tid = await server.start_thread(str(workdir), instructions, model, effort,
                                           sandbox="read-only", approval_policy="on-request", ephemeral=True)
            result = await server.run_turn(tid, prompt, model, effort, lambda _: None, timeout=timeout)
            if result["error"]:
                error = result["error"]
                raise (QuotaExhausted if any(m in error.lower() for m in QUOTA_MARKERS) else CodexError)(error)
            usage = result["usage"]
            return CodexResult(tid, result["text"], {
                "input_tokens": usage.get("inputTokens", 0),
                "cached_input_tokens": usage.get("cachedInputTokens", 0),
                "output_tokens": usage.get("outputTokens", 0),
                "reasoning_output_tokens": usage.get("reasoningOutputTokens", 0),
            }, server.events)
    except TimeoutError as e:
        raise CodexError(f"剪映 Codex 操作超时（{timeout:.0f} 秒）；审核日志：{audit}") from e
    finally:
        await server.close()
