import json
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from reader import AttachmentError, AttachmentStore, Limits, inspect_attachment


class ConfigTests(unittest.TestCase):
    def test_schema_and_runtime_configuration(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text()
        )
        self.assertEqual(
            set(schema), {"max_attachment_mb", "workspace_max_mb", "retention_hours"}
        )
        defaults = {key: item["default"] for key, item in schema.items()}
        self.assertEqual(Limits.from_config(defaults), Limits())
        configured = Limits.from_config(
            {"max_attachment_mb": 31, "workspace_max_mb": 800, "retention_hours": 12}
        )
        self.assertEqual(configured.max_attachment, 31 * 1024**2)
        self.assertEqual(configured.workspace_max, 800 * 1024**2)
        self.assertEqual(configured.ttl, 12 * 3600)
        configured = Limits.from_config(
            {"max_attachment_mb": 1024, "workspace_max_mb": 256}
        )
        self.assertEqual(configured.max_attachment, 1024 * 1024**2)
        self.assertEqual(configured.workspace_max, 1024 * 1024**2)

    def test_invalid_configuration(self):
        for value in [0, -1, True, 1.5, "20", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Limits.from_config({"max_attachment_mb": value})


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.owner = ("bot", "group", "alice")
        self.store = AttachmentStore(self.root / "workspace")

    def tearDown(self):
        self.temp.cleanup()

    def make_attachment(self, name, data):
        item = self.store.register(self.owner, name, name, None)
        path = self.store.path(item)
        path.write_bytes(data)
        return item, path

    def make_zip(self, entries, name="archive.zip"):
        buffer = self.root / name
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename, contents in entries:
                archive.writestr(filename, contents)
        return self.make_attachment(name, buffer.read_bytes())

    def test_zip_list_and_disk_extraction(self):
        item, path = self.make_zip(
            [("资料/说明.md", "测试内容"), ("src/main.py", "print(1)")]
        )
        listing = inspect_attachment(path, item.name, "list", store=self.store)
        self.assertEqual(listing["total"], 2)
        result = inspect_attachment(
            path, item.name, "read", "资料/说明.md", store=self.store
        )
        self.assertEqual(result["content"], "测试内容")
        extracted = list((self.root / "workspace" / item.id / "extracted").iterdir())
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0].read_text(encoding="utf-8"), "测试内容")
        self.assertEqual(
            self.store.usage(), path.stat().st_size + extracted[0].stat().st_size
        )

    def test_large_text_is_read_in_pages(self):
        item, path = self.make_zip([("large.txt", "你好" * 500_000)])
        first = inspect_attachment(
            path, item.name, "read", member="large.txt", store=self.store
        )
        self.assertEqual(len(first["content"]), 16000)
        self.assertEqual(first["next_offset"], 16000)
        second = inspect_attachment(
            path,
            item.name,
            "read",
            member="large.txt",
            offset=16000,
            store=self.store,
        )
        self.assertEqual(len(second["content"]), 16000)
        self.assertEqual(second["next_offset"], 32000)
        self.assertEqual(second["total_characters"], 1_000_000)

    def test_supported_text_encodings(self):
        for encoding in ["utf-8-sig", "gb18030", "utf-16"]:
            with self.subTest(encoding=encoding):
                item, path = self.make_zip([("a.txt", "你好\n世界".encode(encoding))])
                self.assertEqual(
                    inspect_attachment(
                        path, item.name, "read", member="a.txt", store=self.store
                    )["content"],
                    "你好\n世界",
                )

    def test_non_zip_attachment_is_rejected(self):
        item, path = self.make_attachment("file.txt", b"x")
        small = AttachmentStore(
            self.root / "small", Limits.from_config({"max_attachment_mb": 1})
        )
        with self.assertRaisesRegex(AttachmentError, "仅支持读取 ZIP"):
            inspect_attachment(path, item.name, "read", store=small)

    def test_workspace_counts_original_and_extracted_data(self):
        item, path = self.make_zip([("doc.txt", "z" * 1024)])
        self.store.limits = Limits(
            max_attachment=1024**2, workspace_max=path.stat().st_size + 100
        )
        with self.assertRaises(AttachmentError):
            inspect_attachment(path, item.name, "read", "doc.txt", store=self.store)
        self.assertLessEqual(self.store.usage(), self.store.limits.workspace_max)
        self.assertEqual(list((self.root / "workspace" / item.id).rglob("*.part")), [])

    def test_no_compression_ratio_or_entry_count_cap(self):
        contents = "0" * 1_000_000
        item, path = self.make_zip([(f"file-{i}.txt", contents) for i in range(1001)])
        listing = inspect_attachment(
            path, item.name, "list", offset=999, store=self.store
        )
        self.assertEqual(listing["total"], 1001)
        self.assertEqual(listing["entries"][0]["name"], "file-999.txt")

    def test_unsafe_paths_and_symlinks_are_rejected(self):
        for name in ["../secret.txt", "/etc/passwd", "C:\\file.txt", "a\\..\\b.txt"]:
            with self.subTest(name=name):
                item, path = self.make_zip([(name, "secret")], "unsafe.zip")
                with self.assertRaises(AttachmentError):
                    inspect_attachment(path, item.name, "list", store=self.store)
                self.store.remove(item.id)
        item = self.store.register(self.owner, "link", "link.zip", None)
        path = self.store.path(item)
        info = zipfile.ZipInfo("link.txt")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(info, "target")
        with self.assertRaises(AttachmentError):
            inspect_attachment(path, item.name, "list", store=self.store)

    def test_owner_isolation(self):
        item = self.store.register(self.owner, "m1", "a.zip", object())
        with self.assertRaises(AttachmentError):
            self.store.select(("bot", "group", "bob"), item.id)

    def test_retention_removes_original_and_extracted(self):
        item, path = self.make_zip([("a.txt", "content")])
        inspect_attachment(path, item.name, "read", "a.txt", store=self.store)
        item.created = time.time() - 25 * 3600
        self.store.cleanup()
        self.assertFalse((self.root / "workspace" / item.id).exists())

    def test_reload_clears_only_managed_attachment_data(self):
        item, path = self.make_attachment("a.txt", b"data")
        unrelated = self.store.root / "keep.txt"
        unrelated.write_text("keep")
        AttachmentStore(self.store.root)
        self.assertFalse((self.root / "workspace" / item.id).exists())
        self.assertTrue(unrelated.exists())

    def test_non_text_and_corrupt_archives(self):
        item, path = self.make_attachment("file.pdf", b"pdf")
        with self.assertRaises(AttachmentError):
            inspect_attachment(path, item.name, "read", store=self.store)
        item, path = self.make_attachment("broken.zip", b"not a zip")
        with self.assertRaises(AttachmentError):
            inspect_attachment(path, item.name, "list", store=self.store)


if __name__ == "__main__":
    unittest.main()
