import asyncio
import base64
import hashlib
import json
import threading
import zlib
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from test_harness import call

pytest_plugins = ["test_harness"]

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDA"
    "AAANHQEDasKb6QAAAABJRU5ErkJggg=="
)
# Two frames, red then blue, with different durations and an infinite animation loop.
GIF = base64.b64decode(
    "R0lGODlhAgACAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQADAAAACwAAAAA"
    "AgACAAAIBgABCAQQEAAh+QQBGAABACwAAAAAAgACAIEAAP8AAAAAAAAAAAAIBgABCAQQEAA7"
)
IMAGE_LIMIT = 20 * 1024 * 1024
_PNG_TEXT = b"tEXtpadding\0" + b"x" * (3 * 1024 * 1024)
LARGE_PNG = (
    PNG[:-12]
    + (len(_PNG_TEXT) - 4).to_bytes(4, "big")
    + _PNG_TEXT
    + zlib.crc32(_PNG_TEXT).to_bytes(4, "big")
    + PNG[-12:]
)
SECRET = "private-signed-download-token"


@pytest.fixture
async def image_source():
    requests = Counter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            requests[path] += 1
            if path in {"/redirect", "/unsafe-redirect", "/loop"}:
                target = {
                    "/redirect": "/tiny.png",
                    "/unsafe-redirect": (
                        f"http://user:{SECRET}@127.0.0.1:{self.server.server_port}/tiny.png"
                    ),
                    "/loop": "/loop",
                }[path]
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/oversize-header":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(IMAGE_LIMIT + 1))
                self.end_headers()
                return
            if path == "/oversize-chunked":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                block = PNG + b"\0" * (64 * 1024 - len(PNG))
                remaining = IMAGE_LIMIT + 1
                try:
                    while remaining:
                        chunk = block[:remaining]
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        remaining -= len(chunk)
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The downloader closes the connection as soon as the limit is exceeded.
                return
            body = {
                "/tiny.png": PNG,
                "/animation.gif": GIF,
                "/large.png": LARGE_PNG,
                "/expired": SECRET.encode(),
                "/not-an-image": b"<html>this is not a PNG</html>",
                "/truncated": PNG,
            }[path]
            self.send_response(403 if path == "/expired" else 200)
            # A download host need not supply the image's MIME type.
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body) + (path == "/truncated")))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", requests=requests)
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)


async def test_mcp_image_import_preserves_animation_and_safe_overwrites(harness, image_source):
    async with harness.mcp() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        spec = tools["import_image"]
        assert spec.meta["openai/fileParams"] == ["file"]
        file_schema = spec.inputSchema["properties"]["file"]
        if "$ref" in file_schema:
            file_schema = spec.inputSchema["$defs"][file_schema["$ref"].rsplit("/", 1)[-1]]
        assert set(file_schema["properties"]) == {
            "download_url",
            "file_id",
            "mime_type",
            "file_name",
        }
        assert all(item["type"] == "string" for item in file_schema["properties"].values())
        assert set(file_schema["required"]) == {"download_url", "file_id"}
        assert spec.annotations.openWorldHint and not spec.annotations.readOnlyHint

        task = (
            await call(client, "task_create", {"project": "local/project", "title": "匯入圖片"})
        )["id"]
        args = {
            "task_id": task,
            "path": "assets/generated.png",
            "file": {
                "download_url": image_source.url + "/tiny.png?signature=" + SECRET,
                "file_id": "file-generated-png",
                "file_name": "../../untrusted.gif",
                "mime_type": "image/gif",
            },
            "request_id": "import-once",
        }
        first = await call(client, "import_image", args)
        assert first["result"] == {
            "path": args["path"],
            "bytes": len(PNG),
            "sha256": hashlib.sha256(PNG).hexdigest(),
            "mime_type": "image/png",
            "file_id": "file-generated-png",
        }
        assert (harness.repo / args["path"]).read_bytes() == PNG
        assert SECRET not in json.dumps(first)
        replay = await call(client, "import_image", args)
        assert replay["job_id"] == first["job_id"]
        assert image_source.requests["/tiny.png"] == 1

        gif_args = {
            "task_id": task,
            "path": "assets/animation.gif",
            "file": {
                "download_url": image_source.url + "/animation.gif",
                "file_id": "file-generated-gif",
            },
        }
        animation = (await call(client, "import_image", gif_args))["result"]
        assert animation["mime_type"] == "image/gif"
        assert animation["sha256"] == hashlib.sha256(GIF).hexdigest()
        assert animation["bytes"] == len(GIF)
        assert (harness.repo / gif_args["path"]).read_bytes() == GIF
        handoff = (await call(client, "task_get", {"task_id": task}))["handoff"]
        assert set(handoff["changed_files"]) == {args["path"], gif_args["path"]}

        for digest in (None, "0" * 64):
            await call(
                client,
                "import_image",
                {**gif_args, "path": args["path"], "expected_sha256": digest},
                error=True,
            )
            assert (harness.repo / args["path"]).read_bytes() == PNG

        large_args = {
            **args,
            "file": {
                "download_url": image_source.url + "/large.png",
                "file_id": "file-large-png",
            },
            "expected_sha256": first["result"]["sha256"],
            "request_id": "replace-large",
        }
        large = (await call(client, "import_image", large_args))["result"]
        assert (harness.repo / args["path"]).read_bytes() == LARGE_PNG
        assert large["sha256"] == hashlib.sha256(LARGE_PNG).hexdigest()
        assert large["bytes"] == len(LARGE_PNG)
        await call(
            client,
            "import_image",
            {**args, "expected_sha256": large["sha256"], "request_id": "replace-small"},
        )
        assert (harness.repo / args["path"]).read_bytes() == PNG
        assert {
            str(path.relative_to(harness.repo))
            for path in harness.repo.rglob("*")
            if path.is_file()
        } == {args["path"], gif_args["path"]}

        outcomes = await asyncio.gather(
            *(
                call(
                    client,
                    "import_image",
                    {
                        **replacement,
                        "path": args["path"],
                        "expected_sha256": first["result"]["sha256"],
                        "request_id": "concurrent-" + str(index),
                    },
                )
                for index, replacement in enumerate((gif_args, large_args))
            ),
            return_exceptions=True,
        )
        succeeded = [outcome for outcome in outcomes if isinstance(outcome, dict)]
        failed = [outcome for outcome in outcomes if isinstance(outcome, AssertionError)]
        assert len(succeeded) == len(failed) == 1, outcomes
        assert "expected_sha256" in str(failed[0])
        assert (
            hashlib.sha256((harness.repo / args["path"]).read_bytes()).hexdigest()
            == (succeeded[0]["result"]["sha256"])
        )


async def test_mcp_image_import_download_failures_leave_no_file_or_signed_url(
    harness, image_source
):
    async with harness.mcp() as client:
        task = (
            await call(client, "task_create", {"project": "local/project", "title": "驗證下載"})
        )["id"]
        base = {"task_id": task, "path": "download.png"}
        redirected = await call(
            client,
            "import_image",
            {
                **base,
                "file": {"download_url": image_source.url + "/redirect", "file_id": "redirect"},
            },
        )
        assert (harness.repo / base["path"]).read_bytes() == PNG
        assert image_source.requests["/redirect"] == image_source.requests["/tiny.png"] == 1

        for route in (
            "/expired",
            "/oversize-header",
            "/oversize-chunked",
            "/not-an-image",
            "/truncated",
            "/unsafe-redirect",
            "/loop",
        ):
            destination = "rejected" + route + ".png"
            rejected = await call(
                client,
                "import_image",
                {
                    **base,
                    "path": destination,
                    "file": {
                        "download_url": image_source.url + route + "?signature=" + SECRET,
                        "file_id": "rejected",
                    },
                },
                error=True,
            )
            assert SECRET not in json.dumps(rejected)
            assert not (harness.repo / destination).exists()
        assert image_source.requests["/loop"] <= 6
        assert image_source.requests["/tiny.png"] == 1

        for url in (
            "file:///etc/passwd",
            f"http://user:{SECRET}@127.0.0.1:{urlsplit(image_source.url).port}/tiny.png",
        ):
            rejected = await call(
                client,
                "import_image",
                {**base, "file": {"download_url": url, "file_id": "unsafe"}},
                error=True,
            )
            assert SECRET not in json.dumps(rejected)
        await call(
            client,
            "import_image",
            {
                **base,
                "path": "../escaped.png",
                "file": {"download_url": image_source.url + "/tiny.png", "file_id": "escape"},
            },
            error=True,
        )
        assert not (harness.repo.parent / "escaped.png").exists()

        await call(
            client,
            "import_image",
            {
                **base,
                "expected_sha256": redirected["result"]["sha256"],
                "file": {"download_url": image_source.url + "/not-an-image", "file_id": "bad"},
            },
            error=True,
        )
        assert (harness.repo / base["path"]).read_bytes() == PNG
        assert list(harness.repo.rglob(".webcodex-write-*")) == []


async def test_mcp_image_import_requires_execute_on_client_and_runner(harness, image_source):
    async with harness.mcp() as client:
        task = (
            await call(client, "task_create", {"project": "local/project", "title": "檢查傳檔權限"})
        )["id"]
        args = {
            "task_id": task,
            "path": "denied.png",
            "file": {"download_url": image_source.url + "/tiny.png", "file_id": "denied"},
        }
        harness.runner.config.permissions.remove("execute")
        denied = await call(client, "import_image", args, error=True)
        assert "not permitted" in denied["result"]["error"]
        harness.runner.config.permissions.add("execute")
        harness.config.clients["owner"].permissions = {"write"}
        assert "import_image" not in {tool.name for tool in (await client.list_tools()).tools}
        jobs = harness.app.state.service.store.one("SELECT count(*) AS n FROM jobs")["n"]
        denied = await call(client, "import_image", args, error=True)
        assert "not permitted" in denied["error"]
        assert harness.app.state.service.store.one("SELECT count(*) AS n FROM jobs")["n"] == jobs
        assert not image_source.requests
        assert not (harness.repo / args["path"]).exists()
