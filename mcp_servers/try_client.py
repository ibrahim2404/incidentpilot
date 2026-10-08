"""Manual check of the Prometheus MCP server: list its tools, then call each one."""

import asyncio
import sys

from mcp import Client, StdioServerParameters

server = StdioServerParameters(command=sys.executable, args=["mcp_servers/prometheus/server.py"])

CALLS = [
    ("list_alerts", {}),
    ("service_overview", {"service": "orders"}),
    ("query_instant", {"promql": 'sum by (service) (rate(http_requests_total[1m]))'}),
    ("query_range", {"promql": 'container_memory_working_set_bytes{namespace="shop",container="orders"} / 1024 / 1024', "minutes": 15}),
    ("query_instant", {"promql": "sum(rate(http_requests_total[1m]"}),
    ("service_overview", {"service": 'orders"} or vector(1) #'}),
]


async def main() -> None:
    async with Client(server) as client:
        tools = await client.list_tools()
        print("tools:", [t.name for t in tools.tools])
        for name, args in CALLS:
            result = await client.call_tool(name, args)
            print(f"\n=== {name} {args}\n{result.content[0].text}")


asyncio.run(main())
