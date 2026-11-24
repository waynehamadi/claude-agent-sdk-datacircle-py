"""The tools on a scripted API on 127.0.0.1, then on the live API: by each tool's own handler, through the SDK's MCP
server (create_sdk_mcp_server) as Claude Code reaches it, and by the calls without a model.

    pytest tests            # both
    pytest tests -m "not live"
"""

import json
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
from claude_agent_sdk import create_sdk_mcp_server

from claude_agent_sdk_datacircle import (TOOL_NAMES, allowed_tools, best_plans, datacircle_server, datacircle_tools,
                                         list_apis, provider_ranks, search_plans)

try:
    from mcp import Client as _McpClient  # mcp 2

    @asynccontextmanager
    async def mcp_session(server: Any):
        async with _McpClient(server) as client:
            yield client
except ImportError:  # mcp 1
    from mcp.shared.memory import create_connected_server_and_client_session as mcp_session

BACKENDS = ["asyncio"]
try:
    import trio  # noqa: F401
    BACKENDS.append("trio")
except ImportError:
    pass


@pytest.fixture(params=BACKENDS)
def anyio_backend(request: Any) -> str:
    return request.param


def text(result: Dict[str, Any]) -> str:
    return result["content"][0]["text"]


def answer(result: Dict[str, Any]) -> Any:
    assert not result.get("is_error"), text(result)
    return json.loads(text(result))


def is_error(result: Any) -> bool:
    return bool(getattr(result, "isError", None) or getattr(result, "is_error", None))


# A scripted API: each path's answers in turn, its last one again after them. Each call is kept.

class Scripted(BaseHTTPRequestHandler):
    script: Dict[str, List[Tuple[int, Dict[str, str], Any]]] = {}
    calls: List[Tuple[str, Dict[str, str], Dict[str, str]]] = []

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        Scripted.calls.append((url.path, query, dict(self.headers)))
        answers = Scripted.script.get(url.path) or [(404, {}, {"error": f"no path {url.path}"})]
        status, headers, body = answers.pop(0) if len(answers) > 1 else answers[0]
        if callable(body):
            body = body(query)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def scripted():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Scripted.script, Scripted.calls = {}, []
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


APIS = [{"api": "Employee email", "slug": "email-finder", "plans": 9, "providers": 3, "extra": 1},
        {"api": "Email validation", "slug": "email-verification", "plans": 4, "providers": 2},
        {"api": "Company profile", "slug": "linkedin-company-data", "plans": 2, "providers": 1}]
PLAN = {"api": "Employee email", "rank": 1, "provider": "A", "plan": "P", "score": 70, "perThousand": 1.5,
        "terms": "long", "aggregators": ["x"]}


def tools_on(base_url: str) -> Dict[str, Any]:
    return {tool.name: tool for tool in datacircle_tools(base_url=base_url, timeout=5)}


def test_names_and_schemas() -> None:
    tools = datacircle_tools()
    assert [tool.name for tool in tools] == TOOL_NAMES == ["list_apis", "best_plans", "search_plans", "provider_ranks"]
    for tool in tools:
        assert tool.input_schema["type"] == "object" and "datacircle.dev" in tool.description
    assert tools[1].input_schema["required"] == ["slug"] and tools[3].input_schema["required"] == ["slug"]
    assert allowed_tools() == [f"mcp__datacircle__{name}" for name in TOOL_NAMES]
    assert allowed_tools("dc")[0] == "mcp__dc__list_apis"


@pytest.mark.anyio
async def test_list_apis_words_and_limit(scripted: str) -> None:
    Scripted.script["/v1/apis"] = [(200, {}, APIS)]
    tools = tools_on(scripted)
    got = answer(await tools["list_apis"].handler({"query": "email verification"}))
    assert [api["slug"] for api in got["apis"]] == ["email-verification", "email-finder"] and got["total"] == 2
    assert "extra" not in json.dumps(got) and "note" not in got
    got = answer(await tools["list_apis"].handler({"limit": "1"}))
    assert got["total"] == 3 and len(got["apis"]) == 1
    # A provider's name: data matches one API's, and the note says where a provider's plans are.
    got = answer(await tools["list_apis"].handler({"query": "bright data", "limit": None}))
    assert got["total"] == 1 and got["apis"][0]["slug"] == "linkedin-company-data" and "search_plans" in got["note"]
    assert Scripted.calls[0][2]["User-Agent"].startswith("claude-agent-sdk-datacircle/")


@pytest.mark.anyio
async def test_best_plans_slug_limit_and_404(scripted: str) -> None:
    Scripted.script["/v1/apis/email-finder/best"] = [(200, {}, lambda q: {"slug": "email-finder", "total": 9, "plans": [PLAN] * 2, "q": q})]
    Scripted.script["/v1/apis/bright-data/best"] = [(404, {}, {"error": "no API has the slug bright-data", "slugs": ["email-finder"]})]
    tools = tools_on(scripted)
    got = answer(await tools["best_plans"].handler({"slug": "Email Finder", "limit": 99}))
    assert got["q"] == {"limit": "50"} and got["plans"][0] == {k: PLAN[k] for k in ("api", "rank", "provider", "plan", "score", "perThousand")}
    got = answer(await tools["best_plans"].handler({"slug": "email-finder", "limit": 0}))
    assert got["q"] == {}
    result = await tools["best_plans"].handler({"slug": "Bright Data"})
    assert result["is_error"] and text(result).startswith("no API has the slug bright-data. list_apis") and "search_plans" in text(result)
    result = await tools["best_plans"].handler({"slug": " "})
    assert result["is_error"] and "Give an API's slug" in text(result)
    result = await tools["best_plans"].handler({"slug": "email-finder", "limit": "many"})
    assert result["is_error"] and "limit must be a whole number" in text(result)


@pytest.mark.anyio
async def test_search_plans_fallbacks(scripted: str) -> None:
    def plans(q: Dict[str, str]) -> Any:
        found = q.get("query") in ("Bright Data", "bright data", "A-Leads")
        return {"total": 1 if found else 0, "offset": int(q.get("offset", 0)), "plans": [PLAN] if found else [], "q": q}

    tools = tools_on(scripted)
    # A provider's name sent as the API: the API answers 400 with every API's name.
    Scripted.script["/v1/plans"] = [(400, {}, {"error": "api must be one of: Employee email"}), (200, {}, plans)]
    got = answer(await tools["search_plans"].handler({"api": "Bright Data"}))
    assert got["q"] == {"query": "Bright Data"} and got["note"].startswith('No API is named "Bright Data"')
    Scripted.script["/v1/plans"] = [(200, {}, plans)]
    got = answer(await tools["search_plans"].handler({"query": "bright-data", "sort": "PRICE", "offset": 2, "limit": 5}))
    assert got["q"] == {"query": "bright data", "sort": "price", "limit": "5", "offset": "2"} and "bright data" in got["note"]
    got = answer(await tools["search_plans"].handler({"query": "A-Leads"}))
    assert got["total"] == 1 and "note" not in got
    result = await tools["search_plans"].handler({"sort": "cheapest"})
    assert result["is_error"] and "sort must be score, provider or price" in text(result)


@pytest.mark.anyio
async def test_provider_ranks_order_and_404(scripted: str) -> None:
    Scripted.script["/v1/providers/bright-data"] = [(200, {}, {"provider": "Bright Data", "slug": "bright-data", "apis": [
        {"api": "B", "slug": "b", "plans": [{"plan": "x", "score": 30}]},
        {"api": "A", "slug": "a", "rank": 4, "plans": [{"plan": "p1", "rank": 9, "score": 40}, {"plan": "p2", "rank": 4, "score": 50}]},
        {"api": "C", "slug": "c", "rank": 2, "plans": []}]})]
    Scripted.script["/v1/providers/email-finder"] = [(404, {}, {"error": "no provider has the slug email-finder"})]
    tools = tools_on(scripted)
    got = answer(await tools["provider_ranks"].handler({"slug": "Bright Data"}))
    assert [api["slug"] for api in got["apis"]] == ["c", "a", "b"] and got["total"] == 3 and got["ranked"] == 2
    assert got["apis"][1]["best"] == {"plan": "p2", "score": 50} and got["apis"][2]["best"] == {"plan": "x", "score": 30}
    assert "best" not in got["apis"][0] and got["apis"][1]["plans"] == 2
    result = await tools["provider_ranks"].handler({"slug": "email-finder"})
    assert result["is_error"] and "best_plans with the API's slug" in text(result)


@pytest.mark.anyio
async def test_429_waited_once_then_words(scripted: str) -> None:
    Scripted.script["/v1/apis"] = [(429, {"Retry-After": "1"}, {"error": "slow down"}), (200, {}, APIS)]
    tools = tools_on(scripted)
    assert answer(await tools["list_apis"].handler({}))["total"] == 3
    Scripted.script["/v1/apis"] = [(429, {"Retry-After": "1"}, {"error": "slow down"})]
    result = await tools["list_apis"].handler({})
    assert result["is_error"] and text(result) == "slow down" and len(Scripted.calls) == 4


@pytest.mark.anyio
async def test_5xx_no_json_and_no_network(scripted: str) -> None:
    Scripted.script["/v1/apis"] = [(502, {}, b"bad gateway"), (200, {}, b"<html>")]
    tools = tools_on(scripted)
    result = await tools["list_apis"].handler({})
    assert result["is_error"] and text(result) == "Datacircle's API answered 502 Bad Gateway"
    result = await tools["list_apis"].handler({})
    assert result["is_error"] and text(result) == "Datacircle's API gave no JSON"
    closed = tools_on("http://127.0.0.1:9/v1")
    result = await closed["list_apis"].handler({})
    assert result["is_error"] and text(result).startswith("Datacircle's API could not be reached")
    assert await list_apis(base_url="http://127.0.0.1:9/v1") == {"error": text(result)}


@pytest.mark.anyio
async def test_a_stopped_call_stops_at_once(scripted: str) -> None:
    Scripted.script["/v1/apis"] = [(429, {"Retry-After": "60"}, {"error": "slow down"})]
    tools = tools_on(scripted)
    with anyio.move_on_after(1) as scope:
        await tools["list_apis"].handler({})
    assert scope.cancelled_caught


# Live: https://api.datacircle.dev/v1.

@pytest.mark.live
@pytest.mark.anyio
async def test_live_each_tool_handler() -> None:
    tools = {tool.name: tool for tool in datacircle_tools()}
    got = answer(await tools["list_apis"].handler({"query": "email finder", "limit": 3}))
    assert got["apis"][0]["slug"] == "email-finder" and got["total"] >= 1
    got = answer(await tools["best_plans"].handler({"slug": "email-finder", "limit": 3}))
    assert len(got["plans"]) == 3 and got["plans"][0]["rank"] == 1 and got["plans"][0]["page"].startswith("https://datacircle.dev/")
    got = answer(await tools["search_plans"].handler({"api": "email-finder", "sort": "price", "limit": 2}))
    assert len(got["plans"]) == 2 and got["total"] > 2
    got = answer(await tools["provider_ranks"].handler({"slug": "Bright Data"}))
    assert got["slug"] == "bright-data" and got["total"] >= got["ranked"] > 0


@pytest.mark.live
@pytest.mark.anyio
async def test_live_through_the_sdk_mcp_server() -> None:
    config = datacircle_server()
    assert config["type"] == "sdk" and config["name"] == "datacircle"
    async with mcp_session(config["instance"]) as session:
        listed = (await session.list_tools()).tools
        assert [tool.name for tool in listed] == TOOL_NAMES
        schema = listed[2].inputSchema if hasattr(listed[2], "inputSchema") else listed[2].input_schema
        assert schema["properties"]["sort"]["enum"] == ["score", "provider", "price"]
        calls = {"list_apis": {"query": "linkedin profile"}, "best_plans": {"slug": "linkedin-profile", "limit": 2},
                 "search_plans": {"query": "bright data", "limit": 2}, "provider_ranks": {"slug": "bright-data"}}
        for name, arguments in calls.items():
            result = await session.call_tool(name, arguments)
            assert not is_error(result), result
            assert json.loads(result.content[0].text)
        result = await session.call_tool("best_plans", {"slug": "no-such-api"})
        assert is_error(result) and "no API has the slug no-such-api" in result.content[0].text
        result = await session.call_tool("best_plans", {"limit": 2})
        assert is_error(result) and "slug" in result.content[0].text


@pytest.mark.live
@pytest.mark.anyio
async def test_live_beside_an_app_tool_on_one_server() -> None:
    from claude_agent_sdk import tool

    @tool("echo", "Echoes its text", {"text": str})
    async def echo(args: Dict[str, Any]) -> Dict[str, Any]:
        return {"content": [{"type": "text", "text": args["text"]}]}

    config = create_sdk_mcp_server(name="mine", tools=[*datacircle_tools(), echo])
    async with mcp_session(config["instance"]) as session:
        assert [tool.name for tool in (await session.list_tools()).tools] == [*TOOL_NAMES, "echo"]
        assert (await session.call_tool("echo", {"text": "hi"})).content[0].text == "hi"
        assert json.loads((await session.call_tool("best_plans", {"slug": "serp", "limit": 1})).content[0].text)["plans"]


@pytest.mark.live
@pytest.mark.anyio
async def test_live_calls_without_a_model() -> None:
    assert (await list_apis("email"))["apis"]
    assert (await best_plans("email-finder", 1))["plans"][0]["rank"] == 1
    assert (await search_plans(api="email-finder", limit=1))["plans"]
    assert (await provider_ranks("bright-data"))["provider"] == "Bright Data"
    assert "no provider has the slug" in (await provider_ranks("no-such-provider-x"))["error"]
