from __future__ import annotations

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .config import server_url as validate_server_url


async def run_bridge(server_url: str, token: str):
    """Expose the authenticated HTTP service as stdio for MCP clients and private tunnels."""
    server_url = validate_server_url(server_url)
    bridge = Server("webcodex-bridge", version="0.1.0")
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + token},
        timeout=60,
        follow_redirects=True,
        trust_env=False,
    ) as http:
        async with streamable_http_client(
            server_url.rstrip("/") + "/mcp", http_client=http
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as client:
                initialized = await client.initialize()
                bridge.instructions = initialized.instructions

                @bridge.list_tools()
                async def list_tools():
                    return (await client.list_tools()).tools

                @bridge.call_tool()
                async def call_tool(name, arguments):
                    return await client.call_tool(name, arguments)

                async with stdio_server() as (reader, writer):
                    await bridge.run(reader, writer, bridge.create_initialization_options())
