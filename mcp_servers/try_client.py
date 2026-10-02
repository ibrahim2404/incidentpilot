import asyncio
import json
import sys

from mcp import Client, StdioServerParameters

server = StdioServerParameters(command=sys.executable, args=["mcp_servers/prometheus/server.py"])


async def main() -> None:
    async with Client(server) as client:
        tools = await client.list_tools()
        for tool in tools.tools:
            print(tool.name, "-", tool.description)
            print("  input schema:", json.dumps(tool.input_schema))
        result = await client.call_tool("ping", {"name": "Ibrahim"})
        print("result:", result.content[0].text)


asyncio.run(main())