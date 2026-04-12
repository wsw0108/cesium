"""
CesiumJS PR Review MCP Server

Provides custom skills (tools) to the Copilot CLI review agent:
  - get_pr_diff: fetch open PR metadata + diff (requires an open PR)

Start with: python scripts/mcp_review_server/server.py
Configured via: .mcp.json in the repo root
"""

import asyncio
import json
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from tools.git_tools import get_pr_diff

app = Server("cesium-review")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_pr_diff",
            description=(
                "Fetches the open PR for the current branch and returns its metadata, "
                "description, file change classification (GLSL/JS/spec/etc.), and full diff. "
                "Raises an error if no open PR exists — the review agent requires an open PR."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "get_pr_diff":
        try:
            result = get_pr_diff()
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        except RuntimeError as e:
            return [types.TextContent(type="text", text=f"ERROR: {e}")]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
