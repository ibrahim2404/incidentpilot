"""Call one MCP tool from the command line, the way the agent will.

  python mcp_servers/call.py kubernetes                      # list the tools of a server
  python mcp_servers/call.py kubernetes list_pods
  python mcp_servers/call.py logs get_logs '{"workload": "orders", "contains": "error"}'
  python mcp_servers/call.py prometheus service_overview '{"service": "orders"}'
"""

import asyncio
import json
import os
import sys

from mcp import Client, StdioServerParameters

HERE = os.path.dirname(os.path.abspath(__file__))
SERVERS = {
    "prometheus": os.path.join(HERE, "prometheus", "server.py"),
    "kubernetes": os.path.join(HERE, "kube", "server.py"),
    "logs": os.path.join(HERE, "kube", "logs_server.py"),
}


async def main(server: str, tool: str | None, args: dict) -> None:
    params = StdioServerParameters(command=sys.executable, args=[SERVERS[server]], env=dict(os.environ))
    async with Client(params) as client:
        if tool is None:
            for t in (await client.list_tools()).tools:
                print(f"{t.name}: {t.description.splitlines()[0]}")
            return
        result = await client.call_tool(tool, args)
        print(result.content[0].text)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in SERVERS:
        sys.exit(f"usage: call.py {{{'|'.join(SERVERS)}}} [tool] ['json args']")
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None,
                     json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}))
