import asyncio

import pytest

from hermes.computer_use_runner import JianyingAppServer, approval_response


def request():
    return {"serverName": "cua_repl", "mode": "form", "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {"codex_approval_kind": "mcp_tool_call", "connector_id": "computer-use",
                      "tool_name": "get_app", "tool_params": {"app": "com.lemon.lvpro"}, "riskLevel": "low"}}


def test_authorized_jianying_is_session_scoped():
    assert approval_response(request()) == {"action": "accept", "content": {}, "_meta": {"persist": "session"}}


@pytest.mark.parametrize("change", [
    {"tool_params": {"app": "com.apple.Terminal"}}, {"tool_name": "start_audio_recording"},
    {"riskLevel": "high"}, {"connector_id": "other"}, {"codex_sensitive_action": True},
    {"codex_requires_user_input": True},
])
def test_other_apps_and_sensitive_requests_are_declined(change):
    r = request()
    r["_meta"].update(change)
    assert approval_response(r)["action"] == "decline"


def test_url_and_forms_requiring_input_are_declined():
    r = request()
    r["mode"] = "url"
    assert approval_response(r)["action"] == "decline"
    r = request()
    r["requestedSchema"]["properties"] = {"password": {"type": "string"}}
    assert approval_response(r)["action"] == "decline"


def test_rpc_response_uses_server_id_and_records_decision(tmp_path):
    async def exercise():
        server = JianyingAppServer(tmp_path / "events.jsonl")
        sent = []

        async def send(obj):
            sent.append(obj)

        server._send = send
        await server._handle_server_request({"id": "server-request-7", "method": "mcpServer/elicitation/request", "params": request()})
        await server._handle_server_request({"id": "server-request-8", "method": "item/commandExecution/requestApproval", "params": {"command": "anything"}})
        assert sent[0]["id"] == "server-request-7"
        assert sent[0]["result"]["action"] == "accept"
        assert sent[1]["result"]["decision"] == "decline"
        assert len(server.events) == 2

    asyncio.run(exercise())
