import asyncio
import importlib
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

plugin = importlib.import_module("astrbot_plugin_attachment_reader.main")


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reader = plugin.AttachmentReader(SimpleNamespace())
        self.started = asyncio.Event()

        async def slow(request):
            response = web.StreamResponse()
            await response.prepare(request)
            try:
                for _ in range(6):
                    await response.write(b"x" * 1024)
                    await asyncio.sleep(0.03)
                await response.write_eof()
            except ConnectionResetError:
                pass
            return response

        async def stalled(request):
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"x" * 1024)
            await self.started.wait()
            return response

        async def missing(request):
            raise web.HTTPNotFound()

        app = web.Application()
        app.router.add_get("/slow", slow)
        app.router.add_get("/stalled", stalled)
        app.router.add_get("/missing", missing)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        site = web.SockSite(self.runner, self.sock)
        await site.start()
        self.base = f"http://download.example.test:{self.sock.getsockname()[1]}"

        async def resolve(resolver, host, port=0, family=socket.AF_INET):
            return [
                {
                    "hostname": host,
                    "host": "127.0.0.1",
                    "port": port,
                    "family": socket.AF_INET,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        self.resolver = patch.object(plugin.PublicResolver, "resolve", resolve)
        self.resolver.start()

    async def asyncTearDown(self):
        self.started.set()
        self.resolver.stop()
        await self.runner.cleanup()
        await self.reader.terminate()
        self.temp.cleanup()

    async def test_slow_continuous_download_completes(self):
        with (
            patch.object(plugin, "DOWNLOAD_TOTAL_SECONDS", 2),
            patch.object(plugin, "DOWNLOAD_IDLE_SECONDS", 0.12),
        ):
            await self.reader._download(self.base + "/slow", self.root / "archive")
        self.assertEqual((self.root / "archive").stat().st_size, 6144)

    async def test_idle_timeout_reports_stage_and_progress(self):
        with patch.object(plugin, "DOWNLOAD_IDLE_SECONDS", 0.05):
            with self.assertRaisesRegex(plugin.AttachmentError, "接收压缩包数据"):
                await self.reader._download(
                    self.base + "/stalled", self.root / "archive"
                )
        self.assertEqual((self.root / "archive").stat().st_size, 1024)

    async def test_http_status_is_preserved(self):
        with self.assertRaisesRegex(plugin.AttachmentError, "HTTP 404"):
            await self.reader._download(self.base + "/missing", self.root / "archive")

    async def test_timeout_policy_supports_large_archives(self):
        self.assertEqual(plugin.DOWNLOAD_TOTAL_SECONDS, 1800)
        self.assertEqual(plugin.DOWNLOAD_CONNECT_SECONDS, 30)
        self.assertEqual(plugin.DOWNLOAD_IDLE_SECONDS, 120)


if __name__ == "__main__":
    unittest.main()
