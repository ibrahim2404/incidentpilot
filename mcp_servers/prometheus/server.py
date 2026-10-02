from mcp.server import MCPServer

mcp = MCPServer("prometheus")


@mcp.tool()
def ping(name: str) -> str:
    """Health check. Returns a greeting to prove the server answers."""
    return f"pong, {name}"


if __name__ == "__main__":
    mcp.run()