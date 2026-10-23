from __future__ import annotations

import asyncio
import base64
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from .config import server_url as validate_server_url
from .diagnostics import Diagnostics, hash_id

RESPONSE_TIMEOUT = timedelta(seconds=60)


async def _legacy_requests(reader, writer, forwarded):
    async with forwarded:
        async for message in reader:
            request = message.message.root if isinstance(message, SessionMessage) else None
            if isinstance(request, types.JSONRPCRequest) and request.method == "server/discover":
                # Legacy discovery probes need method-not-found so clients can initialize instead.
                await writer.send(
                    SessionMessage(
                        types.JSONRPCMessage(
                            types.JSONRPCError(
                                jsonrpc="2.0",
                                id=request.id,
                                error=types.ErrorData(
                                    code=types.METHOD_NOT_FOUND, message="Method not found"
                                ),
                            )
                        )
                    )
                )
            else:
                await forwarded.send(message)


@contextmanager
def _trace_request(diagnostics, kind, request_id, **fields):
    started = time.monotonic()
    fields["trace_id"] = uuid.uuid4().hex
    fields["transport"] = "stdio"
    fields["rpc_id_hash"] = hash_id(request_id)
    diagnostics.record(kind + ".started", **fields)
    try:
        yield fields
    except asyncio.CancelledError:
        diagnostics.record(
            kind + ".cancelled", **fields, duration_ms=(time.monotonic() - started) * 1000
        )
        raise
    except Exception as error:
        diagnostics.record(
            kind + ".error",
            **fields,
            duration_ms=(time.monotonic() - started) * 1000,
            exception_type=type(error).__name__,
        )
        raise
    else:
        diagnostics.record(
            kind + ".completed", **fields, duration_ms=(time.monotonic() - started) * 1000
        )


async def run_bridge(server_url: str, token: str):
    """Expose the authenticated HTTP service as stdio for MCP clients and private tunnels."""
    diagnostics = Diagnostics("bridge")
    try:
        with _trace_request(diagnostics, "bridge", None):
            await _run_bridge(server_url, token, diagnostics)
    finally:
        diagnostics.close()


async def _run_bridge(server_url: str, token: str, diagnostics: Diagnostics):
    server_url = validate_server_url(server_url)
    bridge = Server("webcodex-bridge", version="0.1.0")
    known_tools = set()

    def request(kind, **fields):
        return _trace_request(diagnostics, kind, bridge.request_context.request_id, **fields)

    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + token},
        timeout=60,
        follow_redirects=True,
        trust_env=False,
    ) as http:
        async with streamable_http_client(
            server_url.rstrip("/") + "/mcp", http_client=http
        ) as streams:
            async with ClientSession(
                streams[0], streams[1], read_timeout_seconds=RESPONSE_TIMEOUT
            ) as client:
                initialized = await client.initialize()
                bridge.instructions = initialized.instructions

                @bridge.list_tools()
                async def list_tools():
                    with request("discovery", rpc_method="tools/list"):
                        tools = (await client.list_tools()).tools
                        known_tools.clear()
                        known_tools.update(tool.name for tool in tools)
                        return tools

                @bridge.list_resources()
                async def list_resources():
                    with request("discovery", rpc_method="resources/list"):
                        return (await client.list_resources()).resources

                @bridge.read_resource()
                async def read_resource(uri):
                    with request(
                        "resource", rpc_method="resources/read", resource_hash=hash_id(str(uri))
                    ):
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
                        try:
                            await context.session.send_progress_notification(
                                token, value, total, message, related_request_id=context.request_id
                            )
                        except Exception as error:
                            diagnostics.record(
                                "progress.error",
                                trace_id=fields["trace_id"],
                                tool=name if name in known_tools else "unknown",
                                exception_type=type(error).__name__,
                            )

                    with request(
                        "tool",
                        tool=name if name in known_tools else "unknown",
                        rpc_method="tools/call",
                        task_id=arguments.get("task_id"),
                        job_id=arguments.get("job_id"),
                    ) as fields:
                        forwarded["webcodex/trace_id"] = fields["trace_id"]
                        result = await client.call_tool(
                            name,
                            arguments,
                            progress_callback=progress if token is not None else None,
                            meta=forwarded,
                        )
                        fields["is_error"] = result.isError
                        fields["outcome"] = "failed" if result.isError else "returned"
                        if isinstance(result.structuredContent, dict):
                            for key in ("task_id", "job_id"):
                                if key in result.structuredContent:
                                    fields[key] = result.structuredContent[key]
                        return result

                async with stdio_server() as (reader, writer):
                    forwarded, incoming = anyio.create_memory_object_stream(0)
                    async with anyio.create_task_group() as group:
                        group.start_soon(_legacy_requests, reader, writer, forwarded)
                        await bridge.run(incoming, writer, bridge.create_initialization_options())
                        group.cancel_scope.cancel()
