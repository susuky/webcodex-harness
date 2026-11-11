from __future__ import annotations

import asyncio

import httpx
import pytest
from test_management_sources import portal_config

from webcodex.config import ManagementSource
from webcodex.management import Management
from webcodex.management_sources import Sources, SourceUnavailable
from webcodex.server import create_app

pytest_plugins = ["test_task_liveness"]


async def test_source_connections_are_reused_per_grant_and_closed_at_shutdown(tmp_path):
    seen = []
    writers = []

    async def upstream(reader, writer):
        connection = len(writers)
        writers.append(writer)
        try:
            while True:
                try:
                    header = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError:
                    break
                authorization = next(
                    line.split(b": ", 1)[1].decode()
                    for line in header.split(b"\r\n")
                    if line.lower().startswith(b"authorization:")
                )
                seen.append((connection, authorization))
                body = b'{"tasks":[]}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    config = portal_config(
        tmp_path / "portal",
        {
            "windows": ManagementSource(url=url, token="windows" * 8),
            "wsl": ManagementSource(url=url, token="wsl" * 16),
        },
    )
    config.clients["other"].management_sources = {
        "windows": ManagementSource(url=url, token="other" * 10)
    }
    app = create_app(config)
    manager = next(
        route.endpoint.__self__
        for route in app.routes
        if getattr(route, "path", None) == "/api/manage/sources"
    )
    clients = []
    try:
        async with app.router.lifespan_context(app):
            for owner, source in (
                ("owner", "windows"),
                ("owner", "windows"),
                ("other", "windows"),
                ("other", "windows"),
                ("owner", "wsl"),
                ("owner", "wsl"),
            ):
                async with manager.remote_client(owner, source) as client:
                    clients.append(client)
                    assert await manager.remote_request(client, "GET", "state") == {"tasks": []}
            assert len(writers) == 3
            assert [connection for connection, _ in seen] == [0, 0, 1, 1, 2, 2]
            assert [authorization for _, authorization in seen] == [
                "Bearer " + token
                for token in (
                    "windows" * 8,
                    "windows" * 8,
                    "other" * 10,
                    "other" * 10,
                    "wsl" * 16,
                    "wsl" * 16,
                )
            ]
            assert all(not client.is_closed for client in clients)
        assert all(client.is_closed for client in clients)
        async with asyncio.timeout(2):
            while any(not writer.is_closing() for writer in writers):
                await asyncio.sleep(0.01)
    finally:
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in writers))


@pytest.mark.parametrize(
    "error,message",
    [
        (httpx.ConnectTimeout, "連線至來源逾時"),
        (httpx.ReadTimeout, "來源回應逾時"),
    ],
)
async def test_source_timeout_identifies_connect_or_response(activity_service, error, message):
    service, _ = activity_service
    manager = Sources(Management(service))

    async def upstream(request):
        raise error("private connection details", request=request)

    async with httpx.AsyncClient(
        base_url="https://source.test", transport=httpx.MockTransport(upstream)
    ) as client:
        with pytest.raises(SourceUnavailable, match=message):
            await manager.remote_request(client, "GET", "state")
