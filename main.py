import sqlite3
from collections.abc import Mapping
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.message_components import File, Forward, Image, Video
from astrbot.api.star import Context, Star
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .fingerprint import (
    UnavailableFingerprint,
    forward_digest,
    is_regular_image_data,
    is_video_file,
    media_digest,
    video_file_digest,
)
from .store import FingerprintStore


class QQGroupToolsPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        config = config or {}
        self.enabled_groups = {str(group) for group in config.get("enabled_groups", [])}
        data_dir = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_qq_group_tools"
        )
        self.store = FingerprintStore(data_dir / "fingerprints.sqlite3")

    async def initialize(self):
        await self.store.initialize()

    @staticmethod
    def _regular_image_flags(event: AiocqhttpMessageEvent) -> list[bool]:
        raw = getattr(event.message_obj, "raw_message", None)
        raw_segments = raw.get("message") if isinstance(raw, Mapping) else None
        if not isinstance(raw_segments, list):
            return []
        flags = []
        for segment in raw_segments:
            if isinstance(segment, Mapping) and segment.get("type") == "image":
                data = segment.get("data")
                flags.append(isinstance(data, Mapping) and is_regular_image_data(data))
        return flags

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def on_group_message(self, event: AiocqhttpMessageEvent):
        """撤回本群中已出现过的相同图片、视频或转发记录。"""
        group_id = str(event.get_group_id())
        if self.enabled_groups and group_id not in self.enabled_groups:
            return
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return

        parts = [
            part
            for part in event.get_messages()
            if isinstance(part, Image | Video | Forward)
            or (isinstance(part, File) and is_video_file(part))
        ]
        if not parts:
            return

        try:
            message_id = int(event.message_obj.message_id)
        except (TypeError, ValueError):
            logger.warning("[qq_group_tools] Invalid message ID in group %s", group_id)
            return

        fingerprints = []
        image_flags = self._regular_image_flags(event)
        image_count = sum(isinstance(part, Image) for part in parts)
        if len(image_flags) != image_count:
            image_flags = [False] * image_count
        image_index = 0
        try:
            for part in parts:
                if isinstance(part, Image):
                    regular = image_flags[image_index]
                    image_index += 1
                    if regular:
                        fingerprints.append(("image", await media_digest(event, part)))
                elif isinstance(part, Video):
                    fingerprints.append(("video", await media_digest(event, part)))
                elif isinstance(part, File):
                    fingerprints.append(("video", await video_file_digest(event, part)))
                else:
                    fingerprints.append(
                        ("forward", await forward_digest(event, str(part.id)))
                    )
        except UnavailableFingerprint as exc:
            logger.warning(
                "[qq_group_tools] Could not fingerprint content in group %s: %s",
                group_id,
                exc,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[qq_group_tools] Could not read media in group %s: %s", group_id, exc
            )
            return

        if not fingerprints:
            return

        try:
            first_message_id = await self.store.find_or_record(
                str(event.get_self_id()),
                group_id,
                str(message_id),
                fingerprints,
            )
        except sqlite3.Error as exc:
            logger.error(
                "[qq_group_tools] Database error in group %s: %s", group_id, exc
            )
            return
        if first_message_id is None:
            return

        event.stop_event()
        try:
            await event.bot.delete_msg(
                message_id=message_id, self_id=str(event.get_self_id())
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[qq_group_tools] Could not recall message %s: %s", message_id, exc
            )
            await event.send(event.plain_result("检测到重复内容，但撤回失败。"))
            return

        await event.send(event.plain_result("重复内容已撤回。"))
