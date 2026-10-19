"""Start the three MCP servers over stdio and expose their tools in OpenAI function format."""

from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack

from mcp import Client, StdioServerParameters

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVERS = {
    "prometheus": os.path.join(ROOT, "mcp_servers", "prometheus", "server.py"),
    "kubernetes": os.path.join(ROOT, "mcp_servers", "kube", "server.py"),
    "logs": os.path.join(ROOT, "mcp_servers", "kube", "logs_server.py"),
}


class MCPBridge:
    """async with MCPBridge() as bridge: bridge.tools (OpenAI format), await bridge.call(name, args)"""

    def __init__(self):
        self._stack = AsyncExitStack()
        self._owner: dict[str, Client] = {}
        self.tools: list[dict] = []

    async def __aenter__(self) -> "MCPBridge":
        env = dict(os.environ)
        for path in SERVERS.values():
            client = await self._stack.enter_async_context(
                Client(StdioServerParameters(command=sys.executable, args=[path], env=env)))
            for t in (await client.list_tools()).tools:
                self._owner[t.name] = client
                self.tools.append({"type": "function", "function": {
                    "name": t.name, "description": t.description, "parameters": t.input_schema}})
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    async def call(self, name: str, args: dict) -> str:
        client = self._owner.get(name)
        if client is None:
            return f"ERROR: unknown tool {name}. Available: {', '.join(sorted(self._owner))}"
        try:
            result = await client.call_tool(name, args)
        except Exception as e:  # a tool crash must not end the investigation
            return f"ERROR: tool {name} failed: {e}"
        return "\n".join(getattr(c, "text", "") for c in result.content) or "(empty result)"
