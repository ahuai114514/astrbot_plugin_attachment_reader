import asyncio
import importlib
import json
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import File, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

plugin = importlib.import_module("astrbot_plugin_attachment_reader.main")


class Event:
    def __init__(
        self, sender="alice", group="g1", components=None, message_id="m1", raw=None
    ):
        self.sender = sender
        self.unified_msg_origin = f"default:GroupMessage:{group}"
        self.message_obj = SimpleNamespace(
            message=components or [], message_id=message_id, raw_message=raw
        )

    def get_self_id(self):
        return "bot"

    def get_sender_id(self):
        return self.sender

    def get_messages(self):
        return self.message_obj.message


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = plugin.AttachmentReader(SimpleNamespace())
        temp_root = Path(get_astrbot_data_path()) / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.temp = Path(self.temp_dir.name)
        self.zip = self.temp / "example.zip"
        with zipfile.ZipFile(self.zip, "w") as archive:
            archive.writestr("资料/说明.txt", "普通成员可以阅读。")
        self.event = Event(components=[File(name="资料.zip", file=str(self.zip))])

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp_dir.cleanup()

    async def test_ordinary_member_zip_flow_uses_disk_workspace(self):
        await self.plugin.capture_attachment(self.event)
        query = Event(message_id="m2")
        req = ProviderRequest(prompt="看看附件", system_prompt="existing")
        await self.plugin.attachment_context(query, req)
        self.assertIn("attachment_read_text", req.system_prompt)
        attachments = json.loads(await self.plugin.list_attachments(query))[
            "attachments"
        ]
        attachment_id = attachments[0]["id"]
        listing = json.loads(await self.plugin.zip_list(query, attachment_id))
        self.assertEqual(listing["entries"][0]["name"], "资料/说明.txt")
        content = json.loads(
            await self.plugin.read_text(query, attachment_id, "资料/说明.txt")
        )
        self.assertEqual(content["content"], "普通成员可以阅读。")
        workspace = self.plugin.store.root / attachment_id
        self.assertTrue((workspace / "original.blob").is_file())
        self.assertTrue(any((workspace / "extracted").iterdir()))

    async def test_cross_sender_and_conversation_are_denied(self):
        await self.plugin.capture_attachment(self.event)
        item_id = json.loads(await self.plugin.list_attachments(self.event))[
            "attachments"
        ][0]["id"]
        for other in [Event(sender="bob"), Event(group="g2")]:
            result = json.loads(
                await self.plugin.read_text(other, item_id, "资料/说明.txt")
            )
            self.assertIn("error", result)

    async def test_own_reply_selects_attachment(self):
        reply = Reply(id="m1", chain=self.event.get_messages(), sender_id="alice")
        query = Event(components=[reply], message_id="m2")
        result = json.loads(await self.plugin.zip_list(query))
        self.assertEqual(result["total"], 1)

    async def test_upload_notice_lazily_resolves_file_url(self):
        raw = {
            "notice_type": "group_upload",
            "group_id": 123,
            "file": {"id": "file-id", "name": "upload.zip"},
        }
        notice = Event(raw=raw)
        notice.bot = SimpleNamespace(
            call_action=AsyncMock(return_value={"url": "https://example.com/file"})
        )
        await self.plugin.capture_attachment(notice)
        notice.bot.call_action.assert_not_awaited()

        async def download(source, path):
            path.write_bytes(self.zip.read_bytes())

        with patch.object(self.plugin, "_download", side_effect=download):
            response = json.loads(await self.plugin.zip_list(Event(message_id="query")))
        self.assertEqual(response["total"], 1)
        notice.bot.call_action.assert_awaited_once_with(
            "get_group_file_url", file_id="file-id", self_id="bot", group_id=123
        )

    async def test_private_download_targets_are_denied(self):
        for url in [
            "http://127.0.0.1/a",
            "http://[::1]/a",
            "http://169.254.169.254/a",
            "file:///etc/passwd",
            "https://u:p@example.com/a",
        ]:
            with self.assertRaises(plugin.AttachmentError):
                await self.plugin._download(url, self.temp / "download")

    async def test_dns_private_address_is_denied(self):
        resolver = plugin.PublicResolver()
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "getaddrinfo",
            AsyncMock(
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
                ]
            ),
        ):
            with self.assertRaises(plugin.AttachmentError):
                await resolver.resolve("example.com", 443)

    async def test_three_panel_settings_are_runtime_limits(self):
        await self.plugin.terminate()
        self.plugin = plugin.AttachmentReader(
            SimpleNamespace(),
            {"max_attachment_mb": 1, "workspace_max_mb": 2, "retention_hours": 2},
        )
        self.assertEqual(self.plugin.limits.max_attachment, 1024**2)
        self.assertEqual(self.plugin.limits.workspace_max, 2 * 1024**2)
        self.assertEqual(self.plugin.limits.ttl, 7200)
        await self.plugin.capture_attachment(self.event)
        self.assertEqual(json.loads(await self.plugin.zip_list(self.event))["total"], 1)

    async def test_registered_tools_are_member_tools(self):
        from astrbot.core.provider.register import llm_tools

        names = {
            tool.name
            for tool in llm_tools.func_list
            if tool.name.startswith("attachment_")
        }
        self.assertEqual(
            names, {"attachment_list", "attachment_zip_list", "attachment_read_text"}
        )


if __name__ == "__main__":
    unittest.main()
