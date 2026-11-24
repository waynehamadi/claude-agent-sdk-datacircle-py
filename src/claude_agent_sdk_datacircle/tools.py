"""Claude Agent SDK tools on Datacircle's open API, https://api.datacircle.dev/v1: no key, no account.

Each tool is an SdkMcpTool, as the SDK's @tool makes it, for create_sdk_mcp_server(tools=[...]). It makes one HTTPS GET
and gives the model the API's answer as JSON text. A refusal (an unknown slug, the API's 4xx words, the API out of
reach) is an error result (is_error) in its words, so the model can call again and the run goes on.

The SDK checks the model's arguments against each tool's schema before the handler. The handler checks them again,
looser, for a direct call: None is a key left out, a number sent as
text is read as one, a fraction is cut to its whole part, a number below its least takes its default and one above its
most is cut to it.
"""

import json
import math
from typing import Any, Dict, List, Mapping, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool

from claude_agent_sdk_datacircle._api import (API_KEYS, VERSION, Client, DatacircleError, brief, objects, slug)

Answer = Dict[str, Any]

LIST_APIS_ABOUT = ("Lists the GTM APIs on Datacircle's sheet (https://datacircle.dev). GTM means go-to-market. GTM APIs "
                   "are the APIs sales and marketing teams use. Gives each API's slug, how many plans and providers it "
                   "has and its best plan. Call it first to find an API's slug.")
BEST_PLANS_ABOUT = ("Gives an API's plans best first by their score out of 100 on Datacircle's benchmark "
                    "(https://datacircle.dev), each with its rank, provider, plan, $ per 1,000 calls and its page. "
                    "perThousand is a plan's price of 1,000 items in dollars, pay the plan's whole price in its currency.")
SEARCH_PLANS_ABOUT = ("Searches every plan on Datacircle's sheet (https://datacircle.dev), or one API's, by provider or "
                      "plan name, sorted by score, provider or price. Gives how many were found and up to limit of them. "
                      "perThousand is a plan's price of 1,000 items in dollars, pay the plan's whole price in its currency.")
PROVIDER_ABOUT = ("Gives each API one provider sells and its rank on each on Datacircle's benchmark "
                  "(https://datacircle.dev), with its best plan there, its best rank first. total is how many APIs it "
                  "sells, ranked on how many of them it has a rank. For an API's best providers, call best_plans.")

# Tells a model that sent a provider's name to an API's tool where a provider's plans are.
FOR_PROVIDER = "A provider's plans: search_plans with its name as query, like bright data, or provider_ranks."

# Tells a model that sent an API's slug to the provider's tool where an API's best providers are.
FOR_API = "An API's best providers: best_plans with the API's slug, like email-finder."


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


LIST_APIS_SCHEMA = _schema({
    "query": {"type": "string", "description": "Words of the API's name or slug, like email or linkedin profile: the "
              "APIs that hold the most of them come first. Left out, the sheet's order"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 200,
              "description": "How many APIs to give, 1 to 200. 20 when left out: the sheet has about 170"},
})
BEST_PLANS_SCHEMA = _schema({
    "slug": {"type": "string", "description": "The API's slug, like email-finder or linkedin-profile, as list_apis gives it"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "How many plans to give, 1 to 50. 10 when left out"},
}, ["slug"])
SEARCH_PLANS_SCHEMA = _schema({
    "api": {"type": "string", "description": "Only this API's plans: its name or slug, like email-finder. A provider's "
            "name goes in query, never here. Left out, every API"},
    "query": {"type": "string", "description": "Words that must all appear in the provider's or plan's name, like bright "
              "data. Left out, every plan"},
    "sort": {"type": "string", "enum": ["score", "provider", "price"],
             "description": "score (best first, the default), provider (A to Z) or price ($ per 1,000, lowest first)"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "How many plans to give, 1 to 50. 10 when left out"},
    "offset": {"type": "integer", "minimum": 0, "description": "How many found plans to skip first. 0 when left out"},
})
PROVIDER_SCHEMA = _schema({
    "slug": {"type": "string", "description": "The provider's slug, like bright-data, or its name, like Bright Data: the "
             "name is made a slug"},
}, ["slug"])


def _count(name: str, value: Any, low: int, high: Optional[int]) -> Optional[int]:
    """A whole number from low to high, None for the API's default."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    number = value
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            pass
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
        raise DatacircleError(f"{name} must be a whole number, not {value!r}")
    whole = int(number)
    if whole < low:
        return None
    return high if high is not None and whole > high else whole


def _text(value: Any) -> str:
    return "" if value is None else str(value)


async def _list_apis(client: Client, args: Mapping[str, Any]) -> Answer:
    limit = _count("limit", args.get("limit"), 1, 200) or 20
    query = _text(args.get("query"))
    apis = objects(await client.get("/apis"))
    words = query.lower().replace("-", " ").split()
    found = []
    for order, api in enumerate(apis):
        name = f"{api.get('api') or ''} {str(api.get('slug') or '').replace('-', ' ')}".lower()
        matched = sum(word in name for word in words)
        if matched or not words:
            found.append((-matched, order, api))
    # The most words matched first, the sheet's order within each.
    found.sort(key=lambda held: held[:2])
    answer: Answer = {"total": len(found), "apis": [brief(api, API_KEYS) for _, _, api in found[:limit]]}
    if words and (not found or -found[0][0] < len(words)):
        # A provider's name sent as an API's, like Bright Data, whose data matches many APIs: where its plans are.
        answer["note"] = (f"No API's name holds every word of {json.dumps(query)}. A provider's plans: search_plans "
                          "with its name as query, or provider_ranks.")
    return answer


async def _best_plans(client: Client, args: Mapping[str, Any]) -> Answer:
    api = slug(args.get("slug"))
    if not api:
        return {"error": f"Give an API's slug, like email-finder: list_apis gives every slug. {FOR_PROVIDER}"}
    try:
        answer = await client.get(f"/apis/{api}/best", {"limit": _count("limit", args.get("limit"), 1, 50)})
    except DatacircleError as error:
        if error.status == 404:
            # A provider's name sent as the API's slug: the API's words, and where a provider's plans are.
            return {"error": f"{error}. list_apis gives every API's slug. {FOR_PROVIDER}"}
        raise
    return {**answer, "plans": [brief(plan) for plan in objects(answer.get("plans"))]}


async def _search_plans(client: Client, args: Mapping[str, Any]) -> Answer:
    sort = args.get("sort")
    sort = None if sort is None or sort == "" else str(sort).strip().lower()
    if sort is not None and sort not in ("score", "provider", "price"):
        return {"error": f"sort must be score, provider or price, not {args.get('sort')!r}"}
    limit = _count("limit", args.get("limit"), 1, 50)
    offset = _count("offset", args.get("offset"), 0, None) or None
    api, query = _text(args.get("api")).strip(), _text(args.get("query")).strip()

    async def search(api: str, query: str) -> Answer:
        return await client.get("/plans", {"api": api, "query": query, "sort": sort, "limit": limit, "offset": offset})

    notes = []
    try:
        answer = await search(api, query)
    except DatacircleError as error:
        if not (error.status == 400 and "api must be" in str(error) and not query):
            raise
        # A provider's name sent as the API: the plans whose provider or plan name holds it.
        api, query = "", api
        answer = await search(api, query)
        notes.append(f"No API is named {json.dumps(args.get('api'))}: these are the plans whose provider or plan name holds it.")
    if answer.get("total") == 0 and "-" in query:
        # A slug sent as a name, like bright-data, which no name holds: its words, like bright data. A name with a
        # hyphen, like A-Leads, is found as it is first.
        words = " ".join(query.replace("-", " ").split())
        try:
            answer = await search(api, words)
            notes.append(f"No provider's or plan's name holds {json.dumps(query)}: these are the plans whose name holds "
                         f"{json.dumps(words)}.")
        except DatacircleError:
            pass  # The first answer stands.
    answer = {**answer, "plans": [brief(plan) for plan in objects(answer.get("plans"))]}
    if notes:
        answer["note"] = " ".join(notes)
    return answer


def _best(plans: List[Answer]) -> Optional[Answer]:
    """A provider's best plan on an API: its lowest rank, the first when none has a rank."""
    ranked = [plan for plan in plans if _ranked(plan)]
    return min(ranked, key=lambda plan: plan["rank"]) if ranked else (plans[0] if plans else None)


def _ranked(item: Mapping[str, Any]) -> bool:
    return isinstance(item.get("rank"), (int, float)) and not isinstance(item.get("rank"), bool)


async def _provider(client: Client, args: Mapping[str, Any]) -> Answer:
    provider = slug(args.get("slug"))
    if not provider:
        return {"error": "Give a provider's slug or name, like bright-data or Bright Data"}
    try:
        answer = await client.get(f"/providers/{provider}")
    except DatacircleError as error:
        # An API's slug sent as a provider's, like email-finder: the API's words, and where an API's best providers are.
        if error.status == 404:
            return {"error": f"{error}. {FOR_API}"}
        raise
    apis = []
    for entry in objects(answer.get("apis")):
        plans = objects(entry.get("plans"))
        kept = {**brief(entry, ["api", "slug", "rank", "bestPage"]), "plans": len(plans)}
        best = _best(plans)
        if best is not None:
            kept["best"] = brief(best, ["plan", "score", "perThousand", "page"])
        apis.append(kept)
    # Its best rank first, the APIs where it has none last, in the API's order.
    apis.sort(key=lambda api: api["rank"] if _ranked(api) else math.inf)
    return {**answer, "apis": apis, "total": len(apis), "ranked": sum(_ranked(api) for api in apis)}


TOOL_NAMES = ["list_apis", "best_plans", "search_plans", "provider_ranks"]
"""The four tools' names, in their order. The model reads each as mcp__<the server's key>__<name>, like
mcp__datacircle__list_apis."""

_TOOLS = {
    "list_apis": ("List the APIs", LIST_APIS_ABOUT, LIST_APIS_SCHEMA, _list_apis),
    "best_plans": ("Who is best for an API", BEST_PLANS_ABOUT, BEST_PLANS_SCHEMA, _best_plans),
    "search_plans": ("Search the plans", SEARCH_PLANS_ABOUT, SEARCH_PLANS_SCHEMA, _search_plans),
    "provider_ranks": ("One provider on every API", PROVIDER_ABOUT, PROVIDER_SCHEMA, _provider),
}

# The largest answer (list_apis with limit 200) is about 55,000 characters: Claude Code keeps it in the model's
# context, where a smaller limit of its own would save it to a file and show the model a preview.
MAX_RESULT_SIZE_CHARS = 100_000


def _annotations(title: str) -> Any:
    """The hints the model reads: each tool only reads, from the open web. The size Claude Code keeps inline too, where
    the SDK's ToolAnnotations takes one (0.2.x)."""
    import claude_agent_sdk
    from mcp.types import ToolAnnotations

    hints = {"title": title, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
    annotations = getattr(claude_agent_sdk, "ToolAnnotations", ToolAnnotations)
    if "maxResultSizeChars" in getattr(annotations, "model_fields", {}):
        return annotations(**hints, maxResultSizeChars=MAX_RESULT_SIZE_CHARS)
    return ToolAnnotations(**hints)


def _make(client: Client, name: str) -> Any:
    title, description, schema, call = _TOOLS[name]

    async def handler(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            answer = await call(client, args if isinstance(args, Mapping) else {})
        except DatacircleError as error:
            answer = {"error": str(error)}
        if isinstance(answer.get("error"), str):
            return {"content": [{"type": "text", "text": answer["error"]}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(answer, ensure_ascii=False, separators=(",", ":"))}]}

    handler.__name__ = name
    return tool(name, description, schema, annotations=_annotations(title))(handler)


def datacircle_tools(base_url: Optional[str] = None, timeout: Optional[float] = None) -> List[Any]:
    """The four tools, SdkMcpTool each, for create_sdk_mcp_server(name="datacircle", tools=[...]) beside an app's own:
    list_apis (GET /apis), best_plans (GET /apis/{slug}/best), search_plans (GET /plans) and provider_ranks
    (GET /providers/{slug}). base_url sets where they call, https://api.datacircle.dev/v1 when left out, and timeout
    how many seconds each waits for an answer, 30 when left out."""
    client = Client(base_url, timeout)
    return [_make(client, name) for name in TOOL_NAMES]


def datacircle_server(name: str = "datacircle", base_url: Optional[str] = None,
                      timeout: Optional[float] = None) -> Any:
    """An in-process MCP server with the four tools, for ClaudeAgentOptions(mcp_servers={"datacircle": ...})."""
    return create_sdk_mcp_server(name=name, version=VERSION, tools=datacircle_tools(base_url, timeout))


def allowed_tools(server: str = "datacircle") -> List[str]:
    """The four tools' names as the model reads them, for ClaudeAgentOptions(allowed_tools=...): server is the
    server's key in mcp_servers."""
    return [f"mcp__{server}__{name}" for name in TOOL_NAMES]


# The same calls without a model: each gives the answer the model would read, as a dict, a refusal as {"error": ...}.

async def _plain(name: str, args: Dict[str, Any], base_url: Optional[str], timeout: Optional[float]) -> Answer:
    try:
        return await _TOOLS[name][3](Client(base_url, timeout), args)
    except DatacircleError as error:
        return {"error": str(error)}


async def list_apis(query: Optional[str] = None, limit: Optional[int] = None, *, base_url: Optional[str] = None,
                    timeout: Optional[float] = None) -> Answer:
    """GET /apis: the APIs, as list_apis gives them."""
    return await _plain("list_apis", {"query": query, "limit": limit}, base_url, timeout)


async def best_plans(slug: str, limit: Optional[int] = None, *, base_url: Optional[str] = None,
                     timeout: Optional[float] = None) -> Answer:
    """GET /apis/{slug}/best: an API's plans best first, as best_plans gives them."""
    return await _plain("best_plans", {"slug": slug, "limit": limit}, base_url, timeout)


async def search_plans(api: Optional[str] = None, query: Optional[str] = None, sort: Optional[str] = None,
                       limit: Optional[int] = None, offset: Optional[int] = None, *, base_url: Optional[str] = None,
                       timeout: Optional[float] = None) -> Answer:
    """GET /plans: the plans found, as search_plans gives them."""
    return await _plain("search_plans", {"api": api, "query": query, "sort": sort, "limit": limit, "offset": offset},
                        base_url, timeout)


async def provider_ranks(slug: str, *, base_url: Optional[str] = None,
                         timeout: Optional[float] = None) -> Answer:
    """GET /providers/{slug}: where a provider ranks, as provider_ranks gives it."""
    return await _plain("provider_ranks", {"slug": slug}, base_url, timeout)
