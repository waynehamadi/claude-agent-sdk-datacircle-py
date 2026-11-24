# Datacircle tools for the Claude Agent SDK (Python)

Find the best GTM APIs. We benchmark all the GTM APIs and we put the results on our website.

Our website: https://datacircle.dev

This package gives the [Claude Agent SDK for Python](https://github.com/anthropics/claude-agent-sdk-python) four tools that call Datacircle's open API, https://api.datacircle.dev/v1. The SDK's `create_sdk_mcp_server()` puts the tools on an in-process MCP server. `query()` and `ClaudeSDKClient` give them to the model. You need no key and no account.

| Tool | What the tool gives | Call |
| --- | --- | --- |
| `list_apis` | Every GTM API on Datacircle, with the API's slug, number of plans, number of providers and best plan | `GET /apis` |
| `best_plans` | One API's plans, best score out of 100 first. Each plan comes with a rank and a price per 1,000 calls | `GET /apis/{slug}/best` |
| `search_plans` | Plans that match a provider or plan name, on all APIs or on one API. You can sort them by score, provider or price | `GET /plans` |
| `provider_ranks` | The APIs a provider sells, and the provider's rank on each API | `GET /providers/{slug}` |

## Install

```bash
pip install claude-agent-sdk-datacircle
```

The tools work with `claude-agent-sdk` 0.1.51 and later, on Python 3.10 and later.

## Use the tools

Give the server to `query()`:

```python
import anyio
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from claude_agent_sdk_datacircle import allowed_tools, datacircle_server

async def main():
    options = ClaudeAgentOptions(
        mcp_servers={"datacircle": datacircle_server()},
        allowed_tools=allowed_tools(),
    )
    async for message in query(prompt="Who are the 3 best providers for the email-finder API?", options=options):
        if isinstance(message, ResultMessage):
            print(message.result)

anyio.run(main)
```

The model sees each tool as `mcp__datacircle__<name>`, like `mcp__datacircle__best_plans`. The `datacircle` in that name is the server's key in `mcp_servers`. If you use another key, pass it to `allowed_tools()`, like `allowed_tools("dc")`.

`allowed_tools()` gives the four tool names, so the tools run with no permission prompt. `allowed_tools=["mcp__datacircle"]` allows them too. Leave out `allowed_tools` and `can_use_tool`, and the SDK refuses each call.

Recent versions of the SDK hide MCP tools from the model at first. The model finds the tools with the SDK's tool search.

`ClaudeSDKClient` takes the same options. A subagent can call the tools too.

You can put the tools beside your own tools on the same server:

```python
from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk_datacircle import datacircle_tools

server = create_sdk_mcp_server(name="datacircle", tools=[*datacircle_tools(), my_tool])
```

To give the model only some of the tools, filter them by name: `[t for t in datacircle_tools() if t.name != "provider_ranks"]`.

## Make each call yourself

You can make each call without a model. Each call returns the answer as a dict:

```python
from claude_agent_sdk_datacircle import best_plans

answer = await best_plans("email-finder", limit=3)
print(answer["plans"])
```

The other calls are `list_apis()`, `search_plans()` and `provider_ranks()`. If the API refuses a call, the dict is `{"error": ...}` with the API's message.

## What the model reads

Each tool gives the model a JSON object as text. Each plan comes with a rank, provider, plan, score, price per 1,000 (`perThousand`, in dollars) and a page on datacircle.dev. The page on datacircle.dev lists every field.

An unknown slug gets an error (`is_error`) with the API's message, and the tool to call instead. If an input does not fit the schema, the SDK returns an error that names the wrong argument. If the network fails, the model gets an error with the network error's message. After an error, the model can call the tool again and keep working.

`ClaudeSDKClient.interrupt()` stops the run while a tool waits for the API.

`datacircle_tools(timeout=10)` and `datacircle_server(timeout=10)` set how many seconds each call waits for an answer. The default is 30. The tools work in asyncio and in trio.

## Network

Each tool call makes one HTTPS GET request to https://api.datacircle.dev/v1 and nothing else. The request holds only the tool's own arguments: no key and nothing from your chats. The API takes up to 60 calls a minute from one IP address. After a 429 answer (too many calls), the tool waits up to a minute and tries once more. The package depends only on the SDK and anyio. The package stores nothing.

## Links

- Datacircle: https://datacircle.dev
- The API: https://datacircle.dev/api
- The API's spec: https://api.datacircle.dev/v1/openapi.json
- What each key means: https://datacircle.dev/fields
- The same tools for the TypeScript SDK: [@datacircle/claude-agent-sdk](https://www.npmjs.com/package/@datacircle/claude-agent-sdk)

MIT license.
