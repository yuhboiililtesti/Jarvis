"""MCP server: other agents can call JARVIS tools, with the same policy gate.

No subprocess is needed: the JSON-RPC handler is exercised directly, and one
stdio round-trip is checked so the framing is real.
"""

from __future__ import annotations

import json

from jarvis.mcp import McpServer, PROTOCOL_VERSION


async def test_initialize_and_capabilities(kernel):
    server = McpServer(kernel)
    response = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    result = response["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "jarvis"
    assert "tools" in result["capabilities"]


async def test_tools_list_is_read_only_by_default(kernel):
    server = McpServer(kernel)
    response = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {t["name"] for t in response["result"]["tools"]}
    assert "time.now" in names
    assert "files.write" not in names          # mutating, not auto-allowed
    tool = next(t for t in response["result"]["tools"] if t["name"] == "time.now")
    assert tool["inputSchema"]["type"] == "object"
    assert tool["description"]


async def test_a_read_only_call_runs_through_the_controller(kernel):
    server = McpServer(kernel)
    response = await server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "time.now", "arguments": {}}})
    content = response["result"]
    assert content["isError"] is False
    payload = json.loads(content["content"][0]["text"])
    assert payload["ok"] is True and payload["certainty"] == "settled"
    # the call is in the normal journal
    assert kernel.db.query_one("SELECT COUNT(*) AS n FROM attempts")["n"] == 1


async def test_a_mutating_call_is_refused_without_an_approval(kernel):
    server = McpServer(kernel)
    response = await server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "files.write", "arguments": {"path": "x.txt", "content": "x"}}})
    content = response["result"]
    assert content["isError"] is True
    assert "approval" in content["content"][0]["text"]
    assert kernel.db.query_one("SELECT COUNT(*) AS n FROM attempts")["n"] == 0


async def test_unknown_tool_and_bad_arguments(kernel):
    server = McpServer(kernel)
    unknown = await server.handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "nope.nope", "arguments": {}}})
    assert unknown["result"]["isError"] is True
    bad = await server.handle({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "time.now", "arguments": "not-an-object"}})
    assert bad["result"]["isError"] is True


async def test_notifications_get_no_response(kernel):
    server = McpServer(kernel)
    assert await server.handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


async def test_allow_mutations_exposes_them_but_they_still_refuse(kernel):
    server = McpServer(kernel, allow_mutations=True)
    listed = await server.handle(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "files.write" in names
    called = await server.handle({
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "files.write", "arguments": {"path": "x.txt", "content": "x"}}})
    assert called["result"]["isError"] is True
    assert "approval" in called["result"]["content"][0]["text"]
