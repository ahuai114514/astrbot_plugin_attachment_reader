"""Disk-backed attachment storage with bounded streaming I/O."""

from __future__ import annotations

import codecs
import hashlib
import re
import shutil
import stat
import time
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

CHUNK_BYTES = 65536
PAGE_CHARS = 16000
DIRECTORY_PAGE_SIZE = 200


@dataclass(frozen=True)
class Limits:
    max_attachment: int = 20 * 1024**2
    workspace_max: int = 256 * 1024**2
    ttl: int = 24 * 3600

    @classmethod
    def from_config(cls, config: Mapping) -> Limits:
        """Validate panel values and convert MiB/hours into runtime units."""
        values = {}
        for key, target, default, multiplier in [
            ("max_attachment_mb", "max_attachment", 20, 1024**2),
            ("workspace_max_mb", "workspace_max", 256, 1024**2),
            ("retention_hours", "ttl", 24, 3600),
        ]:
            value = config.get(key, default)
            if type(value) is not int or value <= 0:
                raise ValueError(f"附件阅读助手配置 {key} 必须为正整数。")
            values[target] = value * multiplier
        # Older configurations may have a smaller workspace than one file.
        # Keep the plugin loadable and make the minimum capacity explicit.
        values["workspace_max"] = max(values["workspace_max"], values["max_attachment"])
        return cls(**values)


class AttachmentError(ValueError):
    """A user-facing error without private paths or download tokens."""


@dataclass
class Attachment:
    owner: tuple[str, str, str]
    message_id: str
    name: str
    source: Any = field(repr=False)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = field(default_factory=time.time)


class AttachmentStore:
    def __init__(self, root: Path, limits: Limits | None = None):
        self.limits = limits or Limits()
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, Attachment] = {}
        # Source references are process-local. Discard only managed attachment
        # directories when reloading, never other files in the plugin directory.
        for path in self.root.iterdir():
            if (
                re.fullmatch(r"[0-9a-f]{32}", path.name)
                and path.is_dir()
                and not path.is_symlink()
            ):
                shutil.rmtree(path)
            elif re.fullmatch(r"[0-9a-f]{32}\.blob(?:\.part)?", path.name):
                path.unlink(missing_ok=True)

    def cleanup(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for item in list(self.records.values()):
            if now - item.created >= self.limits.ttl:
                self.remove(item.id)

    def remove(self, attachment_id: str) -> None:
        if item := self.records.pop(attachment_id, None):
            directory = self.root / item.id
            if directory.is_dir():
                shutil.rmtree(directory)

    def register(
        self, owner: tuple[str, str, str], message_id: str, name: str, source: Any
    ) -> Attachment:
        self.cleanup()
        name = str(name).replace("\\", "/").split("/")[-1][:240] or "attachment"
        for item in self.records.values():
            if (item.owner, item.message_id, item.name) == (owner, message_id, name):
                return item
        item = Attachment(owner, message_id, name, source)
        self.records[item.id] = item
        return item

    def list(self, owner: tuple[str, str, str]) -> list[Attachment]:
        return sorted(
            (x for x in self.records.values() if x.owner == owner),
            key=lambda x: x.created,
            reverse=True,
        )

    def select(
        self, owner: tuple[str, str, str], attachment_id: str = "", reply_id: str = ""
    ) -> Attachment:
        self.cleanup()
        own = self.list(owner)
        if attachment_id:
            own = [x for x in own if x.id == attachment_id]
        elif reply_id:
            own = [x for x in own if x.message_id == reply_id]
        if not own:
            raise AttachmentError("找不到你在当前会话上传的附件，请重新发送文件。")
        if not attachment_id and reply_id and len(own) > 1:
            raise AttachmentError("引用消息包含多个附件，请指定附件 ID。")
        return own[0]

    def path(self, item: Attachment) -> Path:
        directory = self.root / item.id
        if directory.is_symlink():
            directory.unlink()
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "original.blob"

    def usage(self) -> int:
        return sum(
            p.stat().st_size
            for p in self.root.rglob("*")
            if p.is_file() and not p.is_symlink()
        )

    def check_capacity(self, additional: int = 0) -> None:
        if additional < 0 or self.usage() + additional > self.limits.workspace_max:
            raise AttachmentError("workspace 总容量不足，请等待清理或调大总容量。")

    def extract(
        self,
        archive: zipfile.ZipFile,
        item: zipfile.ZipInfo,
        original: Path,
        password: str,
    ) -> Path:
        """Extract one member to an opaque disk filename, charging actual bytes."""
        digest = hashlib.sha256(
            item.filename.encode("utf-8", errors="surrogatepass")
        ).hexdigest()
        target = original.parent / "extracted" / digest
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        self.check_capacity(item.file_size)
        remaining = self.limits.workspace_max - self.usage()
        try:
            with (
                archive.open(
                    item, pwd=password.encode("utf-8") if password else None
                ) as source,
                partial.open("xb") as output,
            ):
                while chunk := source.read(CHUNK_BYTES):
                    if len(chunk) > remaining:
                        raise AttachmentError("workspace 总容量不足，已停止解压。")
                    output.write(chunk)
                    remaining -= len(chunk)
            partial.replace(target)
            return target
        finally:
            partial.unlink(missing_ok=True)


def _members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    names: set[str] = set()
    for item in entries:
        normalized = item.filename.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        mode = item.external_attr >> 16
        if (
            not parts
            or normalized.startswith("/")
            or ".." in parts
            or ":" in normalized
            or "\x00" in item.orig_filename
            or stat.S_ISLNK(mode)
            or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
        ):
            raise AttachmentError("ZIP 含不安全路径或特殊文件，已拒绝处理。")
        if normalized in names:
            raise AttachmentError("ZIP 含重复文件名，无法准确选择文件。")
        names.add(normalized)
    return entries


def _read_text_page(path: Path, name: str, offset: int) -> dict:
    """Validate and count text in fixed chunks, retaining only the requested page."""
    basename = PurePosixPath(name.replace("\\", "/")).name
    if PurePosixPath(
        basename
    ).suffix.lower() not in TEXT_SUFFIXES and basename.lower() not in {
        "readme",
        "license",
        "dockerfile",
        "makefile",
        ".gitignore",
        ".env",
    }:
        raise AttachmentError("附件内容无法按文本读取。")
    with path.open("rb") as stream:
        prefix = stream.read(2)
    encodings = (
        ("utf-16",)
        if prefix in (b"\xff\xfe", b"\xfe\xff")
        else ("utf-8-sig", "gb18030")
    )
    for encoding in encodings:
        decoder = codecs.getincrementaldecoder(encoding)()
        position = 0
        page: list[str] = []
        try:
            with path.open("rb") as stream:
                while True:
                    raw = stream.read(CHUNK_BYTES)
                    text = decoder.decode(raw, final=not raw)
                    if any(ord(c) < 32 and c not in "\t\n\r" for c in text):
                        raise AttachmentError("文件包含二进制数据或非文本控制字符。")
                    start = max(offset - position, 0)
                    end = min(offset + PAGE_CHARS - position, len(text))
                    if end > start:
                        page.append(text[start:end])
                    position += len(text)
                    if not raw:
                        break
        except UnicodeError:
            continue
        content = "".join(page)
        return {
            "content": content,
            "offset": offset,
            "total_characters": position,
            "untrusted_attachment_content": True,
            "next_offset": offset + len(content)
            if offset + len(content) < position
            else None,
        }
    raise AttachmentError("无法识别文本编码，请转换为 UTF-8 后重试。")


def inspect_attachment(
    path: Path,
    name: str,
    action: str,
    member: str = "",
    offset: int = 0,
    password: str = "",
    store: AttachmentStore | None = None,
) -> dict:
    if isinstance(offset, float) and offset.is_integer():
        offset = int(offset)
    if type(offset) is not int or offset < 0:
        raise AttachmentError("offset 必须为非负整数。")
    if action not in {"list", "read"}:
        raise AttachmentError("不支持的操作。")
    if store is None:
        raise AttachmentError("附件 workspace 不可用。")
    if path.stat().st_size > store.limits.max_attachment:
        raise AttachmentError(
            f"附件超过 {store.limits.max_attachment / 1024**2:g} MiB，无法处理。"
        )
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                entries = _members(archive)
                if action == "list":
                    page = []
                    name_budget = 0
                    for entry in entries[offset : offset + DIRECTORY_PAGE_SIZE]:
                        name_budget += len(entry.filename)
                        if page and name_budget > 12000:
                            break
                        page.append(entry)
                    return {
                        "name": name,
                        "total": len(entries),
                        "entries": [
                            {
                                "name": x.filename,
                                "bytes": x.file_size,
                                "directory": x.is_dir(),
                                "encrypted": bool(x.flag_bits & 1),
                            }
                            for x in page
                        ],
                        "next_offset": offset + len(page)
                        if offset + len(page) < len(entries)
                        else None,
                    }
                if not member:
                    raise AttachmentError("请先查看 ZIP 目录，再指定 member 文件名。")
                matches = [
                    x for x in entries if x.filename == member and not x.is_dir()
                ]
                if not matches:
                    raise AttachmentError(
                        "ZIP 中找不到指定文件，请使用目录中的完整文件名。"
                    )
                text_path = store.extract(archive, matches[0], path, password)
                text_name = member
        else:
            raise AttachmentError("仅支持读取 ZIP 压缩包。")
        result = _read_text_page(text_path, text_name, offset)
        return {"name": name, "member": member, **result}
    except (zipfile.BadZipFile, EOFError) as exc:
        raise AttachmentError("ZIP 文件无效或已损坏。") from exc
    except (RuntimeError, NotImplementedError) as exc:
        raise AttachmentError(
            "ZIP 需要正确密码，或使用了暂不支持的加密/压缩方式。"
        ) from exc


TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".csv",
    ".tsv",
    ".log",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".sql",
    ".sh",
    ".bat",
    ".ps1",
    ".vue",
    ".svelte",
    ".tex",
    ".rst",
    ".properties",
}
