from __future__ import annotations

import base64

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
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

                @bridge.list_resources()
                async def list_resources():
                    return (await client.list_resources()).resources

                @bridge.read_resource()
                async def read_resource(uri):
                    result = await client.read_resource(uri)
                    return [
                        ReadResourceContents(
                            item.text if hasattr(item, "text") else base64.b64decode(item.blob),
                            item.mimeType,
                            item.meta,
                        )
                        for item in result.contents
                    ]

                @bridge.call_tool()
                async def call_tool(name, arguments):
                    context = bridge.request_context
                    token = context.meta.progressToken if context.meta else None
                    outer_meta = (
                        context.meta.model_dump(by_alias=True, exclude_none=True)
                        if context.meta
                        else {}
                    )
                    # Keep window identity request-scoped; never use a credential-wide default.
                    forwarded = {
                        key: outer_meta[key]
                        for key in (
                            "openai/session",
                            "io.modelcontextprotocol/clientCapabilities",
                            "io.modelcontextprotocol/protocolVersion",
                            "io.modelcontextprotocol/clientInfo",
                        )
                        if key in outer_meta
                    }

                    async def progress(value, total=None, message=None):
                        await context.session.send_progress_notification(
                            token, value, total, message, related_request_id=context.request_id
                        )

                    return await client.call_tool(
                        name,
                        arguments,
                        progress_callback=progress if token is not None else None,
                        meta=forwarded or None,
                    )

                async with stdio_server() as (reader, writer):
                    await bridge.run(reader, writer, bridge.create_initialization_options())
