import hashlib
from pathlib import Path

import pytest
from mcp.shared.exceptions import McpError

from webcodex.config import Client, ServerConfig
from webcodex.continuation import APP_MIME, APP_URI
from webcodex.server import TOOL_ACTIVITY, Service

pytest_plugins = ["test_harness"]


def test_ui_uri_changes_with_html_and_keeps_each_service_snapshot(tmp_path, monkeypatch):
    original_read = Path.read_text
    html = "<main>first</main>"

    def read(path, *args, **kwargs):
        return html if path.name == "task_app.html" else original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    services = []
    try:
        for index, value in enumerate([html, html, html + "\n"]):
            html = value
            service = Service(
                ServerConfig(
                    state_dir=tmp_path / str(index),
                    admin_token="a" * 40,
                    clients={"owner": Client(token="c" * 40, projects=[])},
                    runners={},
                )
            )
            services.append(service)
            assert service.app_uri == (
                f"{APP_URI}/{hashlib.sha256(html.encode()).hexdigest()[:16]}.html"
            )
        assert services[0].app_uri == services[1].app_uri != services[2].app_uri
        assert services[0].app_html == "<main>first</main>"
    finally:
        for service in services:
            service.store.close()


async def test_mcp_ui_resource_metadata_aliases_and_access(harness):
    service = harness.app.state.service
    async with harness.mcp() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        metadata = tools["task_present"].meta
        assert metadata["ui"]["resourceUri"] == metadata["openai/outputTemplate"] == service.app_uri
        assert set(tools) - {"task_app"} <= TOOL_ACTIVITY.keys()
        for name, descriptor in tools.items():
            if name == "task_app":
                assert "openai/toolInvocation/invoking" not in descriptor.meta
                assert descriptor.meta["ui"]["visibility"] == ["app"]
                continue
            label = TOOL_ACTIVITY[name]
            assert descriptor.title == label
            assert descriptor.meta["openai/toolInvocation/invoking"] == label + "…"
            assert descriptor.meta["openai/toolInvocation/invoked"] == label + "：已收到回應"
            if name != "task_present":
                assert "resourceUri" not in descriptor.meta["ui"]
                assert "openai/outputTemplate" not in descriptor.meta
        resources = (await client.list_resources()).resources
        assert [str(resource.uri) for resource in resources] == [service.app_uri]
        assert resources[0].mimeType == APP_MIME
        for uri in [service.app_uri, APP_URI, APP_URI + "/0000000000000000.html"]:
            content = (await client.read_resource(uri)).contents[0]
            assert str(content.uri) == uri and content.mimeType == APP_MIME
            assert content.text == service.app_html
            assert content.meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
        for uri in [
            APP_URI + "/../task_app.html",
            APP_URI + "/ABCDEF1234567890.html",
            APP_URI + "evil",
        ]:
            with pytest.raises(McpError, match="Resource not found"):
                await client.read_resource(uri)
        harness.config.clients["owner"].permissions = {"write"}
        assert not (await client.list_resources()).resources
        with pytest.raises(McpError, match="Resource not found"):
            await client.read_resource(service.app_uri)
