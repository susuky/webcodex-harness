import base64
from io import BytesIO

from PIL import Image
from test_harness import call
from test_harness import harness as harness


async def test_mcp_image_overview_returns_one_sheet_and_selected_original(harness):
    Image.new("RGBA", (1920, 1080), (255, 0, 0, 220)).save(harness.repo / "frame.png")
    Image.new("RGB", (1000, 1500), "blue").save(harness.repo / "detail.jpg")
    (harness.repo / "broken.png").write_text("not an image")
    Image.new("RGB", (32, 32), "green").save(harness.repo.parent / "outside.png")

    async with harness.mcp() as client:
        task = await call(client, "task_create", {"project": "local/project", "title": "images"})

        async def image_call(name, arguments):
            reply = await client.call_tool(name, {"task_id": task["id"], **arguments})
            if reply.structuredContent["status"] != "succeeded":
                reply = await client.call_tool(
                    "job_get", {"job_id": reply.structuredContent["job_id"], "wait_seconds": 5}
                )
            assert not reply.isError
            assert reply.structuredContent["status"] == "succeeded"
            [image] = reply.content
            assert image.type == "image"
            return reply.structuredContent["result"], image

        overview, sheet = await image_call(
            "view_images",
            {"paths": ["frame.png", "./frame.png", "broken.png", "../outside.png", "detail.jpg"]},
        )
        assert overview["images"] == [
            {"index": 1, "path": "frame.png"},
            {"index": 2, "path": "detail.jpg"},
        ]
        assert [item["path"] for item in overview["skipped"]] == [
            "./frame.png",
            "broken.png",
            "../outside.png",
        ]
        assert "mcp_result" not in overview
        assert sheet.mimeType == "image/jpeg"
        with Image.open(BytesIO(base64.b64decode(sheet.data))) as image:
            assert image.size == (512, 192)
            red, green, blue = image.getpixel((128, 104))
            assert red > green + 150 and red > blue + 150
            red, green, blue = image.getpixel((384, 104))
            assert blue > red + 150 and blue > green + 150

        _, selected = await image_call("view_image", {"path": "detail.jpg"})
        assert base64.b64decode(selected.data) == (harness.repo / "detail.jpg").read_bytes()
