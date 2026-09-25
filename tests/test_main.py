import importlib
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_qq_group_tools.store import FingerprintStore


class FakeImage:
    def __init__(self, path=None, file=None, url=None):
        self.file = str(file or path or "")
        self.url = str(url or "")
        self.path = path or file or url

    async def convert_to_file_path(self):
        return str(self.path)


class FakeVideo:
    def __init__(self, path=None, file=None, url=None):
        self.file = str(file or path or "")
        self.url = str(url or "")
        self.path = path or file or url

    async def convert_to_file_path(self):
        return str(self.path)


class FakeForward:
    def __init__(self, forward_id):
        self.id = forward_id


class FakeFile:
    def __init__(self, name, file="", url=""):
        self.name = name
        self.file_ = file
        self.url = url

    async def get_file(self):
        self.file_ = self.file_ or self.url
        return self.file_


class FakeEvent:
    def __init__(self, message_id, sender_id, content, raw_message=None):
        self.sender_id = sender_id
        self.content = content if isinstance(content, list) else [content]
        if raw_message is None:
            raw_message = {
                "message": [
                    {"type": "image", "data": {"sub_type": 0}}
                    for part in self.content
                    if isinstance(part, FakeImage)
                ]
            }
        self.message_obj = types.SimpleNamespace(
            message_id=message_id, raw_message=raw_message
        )
        self.bot = types.SimpleNamespace(
            delete_msg=AsyncMock(),
            call_action=AsyncMock(),
        )
        self.sent = []
        self.stopped = False
        self.temporary_files = []

    def get_group_id(self):
        return "group"

    def get_sender_id(self):
        return self.sender_id

    def get_self_id(self):
        return "bot"

    def get_messages(self):
        return self.content

    def stop_event(self):
        self.stopped = True

    def plain_result(self, message):
        return message

    async def send(self, message):
        self.sent.append(message)

    def track_temporary_local_file(self, path):
        self.temporary_files.append(path)


def fake_astrbot_modules():
    names = [
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.platform",
        "astrbot.core.platform.sources",
        "astrbot.core.platform.sources.aiocqhttp",
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path",
    ]
    modules = {name: types.ModuleType(name) for name in names}
    for name, module in modules.items():
        if any(other.startswith(name + ".") for other in names):
            module.__path__ = []
    decorator = lambda *args, **kwargs: lambda function: function
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        platform_adapter_type=decorator,
        event_message_type=decorator,
        PlatformAdapterType=types.SimpleNamespace(AIOCQHTTP=1),
        EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1),
    )
    modules["astrbot.api"].logger = logging.getLogger("qq_group_tools_test")
    modules["astrbot.api.message_components"].Image = FakeImage
    modules["astrbot.api.message_components"].Video = FakeVideo
    modules["astrbot.api.message_components"].Forward = FakeForward
    modules["astrbot.api.message_components"].File = FakeFile
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = type(
        "Star", (), {"__init__": lambda self, context: None}
    )
    modules[
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    ].AiocqhttpMessageEvent = FakeEvent
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: "/tmp"
    return modules


class QQGroupToolsPluginTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules_patch = patch.dict(sys.modules, fake_astrbot_modules())
        cls.modules_patch.start()
        cls.plugin_module = importlib.import_module(
            "astrbot_plugin_qq_group_tools.main"
        )

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("astrbot_plugin_qq_group_tools.main", None)
        cls.modules_patch.stop()

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.image_path = Path(self.directory.name) / "image.png"
        self.image_path.write_bytes(b"image bytes")
        self.plugin = self.plugin_module.QQGroupToolsPlugin(object())
        self.plugin.store = FingerprintStore(
            Path(self.directory.name) / "fingerprints.sqlite3"
        )
        await self.plugin.initialize()

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_duplicate_is_recalled_and_reported(self):
        first = FakeEvent("1", "alice", FakeImage(self.image_path))
        await self.plugin.on_group_message(first)
        self.assertFalse(first.stopped)
        first.bot.delete_msg.assert_not_awaited()

        duplicate = FakeEvent("2", "bob", FakeImage(self.image_path))
        await self.plugin.on_group_message(duplicate)
        self.assertTrue(duplicate.stopped)
        duplicate.bot.delete_msg.assert_awaited_once_with(message_id=2, self_id="bot")
        self.assertEqual(duplicate.sent, ["重复内容已撤回。"])

    async def test_recall_failure_is_reported(self):
        first = FakeEvent("1", "alice", FakeImage(self.image_path))
        await self.plugin.on_group_message(first)

        duplicate = FakeEvent("2", "bob", FakeImage(self.image_path))
        duplicate.bot.delete_msg.side_effect = RuntimeError("no permission")
        await self.plugin.on_group_message(duplicate)
        self.assertTrue(duplicate.stopped)
        self.assertIn("撤回失败", duplicate.sent[0])

    async def test_invalid_message_id_is_not_indexed(self):
        invalid = FakeEvent("invalid", "alice", FakeImage(self.image_path))
        await self.plugin.on_group_message(invalid)
        self.assertFalse(invalid.stopped)

        first = FakeEvent("1", "bob", FakeImage(self.image_path))
        await self.plugin.on_group_message(first)
        self.assertFalse(first.stopped)

    async def test_video_bytes_are_deduplicated(self):
        first = FakeEvent("1", "alice", FakeVideo(self.image_path))
        await self.plugin.on_group_message(first)
        duplicate = FakeEvent("2", "bob", FakeVideo(self.image_path))
        await self.plugin.on_group_message(duplicate)
        duplicate.bot.delete_msg.assert_awaited_once_with(message_id=2, self_id="bot")

    async def test_video_file_matches_video_segment(self):
        first = FakeEvent("1", "alice", FakeVideo(self.image_path))
        await self.plugin.on_group_message(first)
        duplicate = FakeEvent(
            "2", "bob", FakeFile(name="clip.MP4", url=str(self.image_path))
        )
        await self.plugin.on_group_message(duplicate)
        duplicate.bot.delete_msg.assert_awaited_once_with(message_id=2, self_id="bot")
        self.assertEqual(duplicate.temporary_files, [])

    async def test_downloaded_video_file_is_tracked_for_cleanup(self):
        clip = FakeFile(name="clip.mp4", url="https://example.test/clip.mp4")
        clip.get_file = AsyncMock(return_value=str(self.image_path))
        event = FakeEvent("1", "alice", clip)
        await self.plugin.on_group_message(event)
        self.assertEqual(event.temporary_files, [str(self.image_path)])

    async def test_image_with_local_file_and_remote_url_tracks_download(self):
        image = FakeImage(file=self.image_path, url="https://example.test/img")
        downloaded = Path(self.directory.name) / "downloaded.png"
        downloaded.write_bytes(b"downloaded bytes")
        image.convert_to_file_path = AsyncMock(return_value=str(downloaded))
        event = FakeEvent("1", "alice", image)
        await self.plugin.on_group_message(event)
        self.assertEqual(event.temporary_files, [str(downloaded)])

    async def test_custom_sticker_is_not_indexed(self):
        sticker = {"message": [{"type": "image", "data": {"sub_type": 1}}]}
        first = FakeEvent("1", "alice", FakeImage(self.image_path), sticker)
        second = FakeEvent("2", "bob", FakeImage(self.image_path), sticker)
        await self.plugin.on_group_message(first)
        await self.plugin.on_group_message(second)
        second.bot.delete_msg.assert_not_awaited()
        self.assertFalse(second.stopped)

    async def test_market_sticker_is_not_indexed(self):
        sticker = {"message": [{"type": "image", "data": {"emoji_id": "x"}}]}
        first = FakeEvent("1", "alice", FakeImage(self.image_path), sticker)
        second = FakeEvent("2", "bob", FakeImage(self.image_path), sticker)
        await self.plugin.on_group_message(first)
        await self.plugin.on_group_message(second)
        second.bot.delete_msg.assert_not_awaited()

    async def test_unclassified_image_is_not_indexed(self):
        unknown = {"message": [{"type": "image", "data": {}}]}
        first = FakeEvent("1", "alice", FakeImage(self.image_path), unknown)
        second = FakeEvent("2", "bob", FakeImage(self.image_path), unknown)
        await self.plugin.on_group_message(first)
        await self.plugin.on_group_message(second)
        second.bot.delete_msg.assert_not_awaited()

    async def test_forward_compares_content_without_sender_or_time(self):
        first = FakeEvent("1", "alice", FakeForward("forward-1"))
        first.bot.call_action.return_value = {
            "messages": [
                {
                    "sender": {"user_id": 11},
                    "time": 100,
                    "message": [{"type": "text", "data": {"text": "同一段聊天"}}],
                }
            ]
        }
        await self.plugin.on_group_message(first)
        second = FakeEvent("2", "bob", FakeForward("forward-2"))
        second.bot.call_action.return_value = {
            "messages": [
                {
                    "sender": {"user_id": 22},
                    "time": 200,
                    "message": [{"type": "text", "data": {"text": "同一段聊天"}}],
                }
            ]
        }
        await self.plugin.on_group_message(second)
        first.bot.call_action.assert_awaited_once_with(
            action="get_forward_msg", message_id="forward-1", self_id="bot"
        )
        second.bot.delete_msg.assert_awaited_once_with(message_id=2, self_id="bot")
        self.assertEqual(second.sent, ["重复内容已撤回。"])

    async def test_forward_compares_embedded_image_and_video_bytes(self):
        messages = [
            {
                "type": "node",
                "data": {
                    "user_id": "123",
                    "time": 100,
                    "content": [
                        {"type": "text", "data": {"text": "片段"}},
                        {
                            "type": "image",
                            "data": {"file": str(self.image_path), "sub_type": 0},
                        },
                        {"type": "video", "data": {"file": str(self.image_path)}},
                    ],
                },
            }
        ]
        first = FakeEvent("1", "alice", FakeForward("forward-1"))
        first.bot.call_action.return_value = {"data": {"messages": messages}}
        second = FakeEvent("2", "bob", FakeForward("forward-2"))
        second.bot.call_action.return_value = {"messages": messages}
        await self.plugin.on_group_message(first)
        await self.plugin.on_group_message(second)
        second.bot.delete_msg.assert_awaited_once_with(message_id=2, self_id="bot")

    async def test_forward_containing_sticker_is_skipped(self):
        messages = [
            {
                "message": [
                    {"type": "text", "data": {"text": "片段"}},
                    {
                        "type": "image",
                        "data": {"file": str(self.image_path), "sub_type": 1},
                    },
                ]
            }
        ]
        first = FakeEvent("1", "alice", FakeForward("forward-1"))
        second = FakeEvent("2", "bob", FakeForward("forward-2"))
        first.bot.call_action.return_value = {"messages": messages}
        second.bot.call_action.return_value = {"messages": messages}
        await self.plugin.on_group_message(first)
        await self.plugin.on_group_message(second)
        second.bot.delete_msg.assert_not_awaited()

    async def test_forward_with_cq_string_is_skipped(self):
        messages = [{"message": "[CQ:image,file=sticker-id]"}]
        first = FakeEvent("1", "alice", FakeForward("forward-1"))
        second = FakeEvent("2", "bob", FakeForward("forward-2"))
        first.bot.call_action.return_value = {"messages": messages}
        second.bot.call_action.return_value = {"messages": messages}
        await self.plugin.on_group_message(first)
        await self.plugin.on_group_message(second)
        second.bot.delete_msg.assert_not_awaited()
