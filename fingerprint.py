import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote, urlsplit

from astrbot.api.message_components import File, Image, Video

from .store import sha256_file


class UnavailableFingerprint(ValueError):
    pass


VIDEO_EXTENSIONS = {".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}


def is_video_file(file: File) -> bool:
    name = getattr(file, "name", "") or ""
    return Path(str(name)).suffix.lower() in VIDEO_EXTENSIONS


def is_regular_image_data(data: Mapping) -> bool:
    if any(data.get(key) for key in ("emoji_id", "emoji_package_id", "key")):
        return False
    subtype = data.get("sub_type", data.get("subType"))
    return subtype == 0 or subtype == "0"


async def media_digest(event, media: Image | Video) -> str:
    references = [getattr(media, key, "") or "" for key in ("file", "url")]
    local_sources = {_local_path(reference) for reference in references}
    path = await media.convert_to_file_path()
    if Path(path).resolve() not in local_sources:
        event.track_temporary_local_file(path)
    return await asyncio.to_thread(sha256_file, path)


def _local_path(reference: str) -> Path | None:
    if not reference or reference.startswith(
        ("http://", "https://", "base64://", "data:")
    ):
        return None
    if reference.startswith("file://"):
        parsed = urlsplit(reference)
        if parsed.netloc not in ("", "localhost"):
            return None
        reference = unquote(parsed.path)
    path = Path(reference)
    return path.resolve() if path.is_file() else None


async def video_file_digest(event, file: File) -> str:
    local_sources = {
        _local_path(getattr(file, key, "") or "") for key in ("file_", "url")
    }
    path = await file.get_file()
    if not path:
        raise UnavailableFingerprint("video file is unavailable")
    if Path(path).resolve() not in local_sources:
        event.track_temporary_local_file(path)
    return await asyncio.to_thread(sha256_file, path)


async def forward_digest(event, forward_id: str) -> str:
    nodes = await _fetch_forward(event, forward_id)
    content = await _normalize_nodes(event, nodes, depth=0, visited={forward_id})
    if not content or not any(content):
        raise UnavailableFingerprint("forward record has no supported content")
    canonical = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"forward:v1:{canonical}".encode()).hexdigest()


async def _fetch_forward(event, forward_id: str) -> list:
    last_error = None
    for params in ({"message_id": forward_id}, {"id": forward_id}):
        try:
            response = await event.bot.call_action(
                action="get_forward_msg", self_id=str(event.get_self_id()), **params
            )
            if isinstance(response, Mapping):
                payload = response.get("data", response)
                if isinstance(payload, Mapping) and isinstance(
                    payload.get("messages"), list
                ):
                    return payload["messages"]
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise UnavailableFingerprint(f"cannot fetch forward record: {last_error}")


async def _normalize_nodes(event, nodes: list, depth: int, visited: set[str]) -> list:
    if depth > 4 or len(nodes) > 200:
        raise UnavailableFingerprint("forward record is too deep or too long")
    result = []
    for node in nodes:
        if not isinstance(node, Mapping):
            raise UnavailableFingerprint("forward node is not structured")
        data = node.get("data") if node.get("type") == "node" else node
        if not isinstance(data, Mapping):
            raise UnavailableFingerprint("forward node has no data")
        segments = data.get("message") or data.get("content")
        if segments is None:
            raise UnavailableFingerprint("forward node has no message")
        result.append(await _normalize_segments(event, segments, depth + 1, visited))
    return result


async def _normalize_segments(event, segments, depth: int, visited: set[str]) -> list:
    if depth > 4:
        raise UnavailableFingerprint("forward record is too deep")
    if isinstance(segments, str):
        if "[CQ:" in segments:
            raise UnavailableFingerprint("forward CQ string cannot be classified")
        segments = [{"type": "text", "data": {"text": segments}}]
    elif isinstance(segments, Mapping):
        segments = [segments]
    if not isinstance(segments, list) or len(segments) > 200:
        raise UnavailableFingerprint("forward message is not structured")

    result = []
    for segment in segments:
        if isinstance(segment, str):
            if "[CQ:" in segment:
                raise UnavailableFingerprint("forward CQ string cannot be classified")
            segment = {"type": "text", "data": {"text": segment}}
        if not isinstance(segment, Mapping) or not isinstance(
            segment.get("data"), Mapping
        ):
            raise UnavailableFingerprint("forward segment is not structured")
        kind = segment.get("type")
        data = segment["data"]
        if kind == "text":
            text = data.get("text")
            if not isinstance(text, str):
                raise UnavailableFingerprint("forward text is unavailable")
            if result and result[-1][0] == "text":
                result[-1][1] += text
            elif text:
                result.append(["text", text])
        elif kind in {"image", "video"}:
            if kind == "image" and not is_regular_image_data(data):
                raise UnavailableFingerprint(
                    "forward contains a sticker or unclassified image"
                )
            file = data.get("file") or data.get("url")
            if not isinstance(file, str) or not file:
                raise UnavailableFingerprint(f"forward {kind} is unavailable")
            media = (
                Image(file=file, url=data.get("url"))
                if kind == "image"
                else Video(file=file, url=data.get("url"))
            )
            result.append([kind, await media_digest(event, media)])
        elif kind == "file":
            name = data.get("name") or data.get("file_name") or ""
            file = File(
                name=str(name), file=data.get("file") or "", url=data.get("url") or ""
            )
            if not is_video_file(file):
                raise UnavailableFingerprint("forward contains a non-video file")
            result.append(["video", await video_file_digest(event, file)])
        elif kind == "forward":
            nested = data.get("content")
            nested_id = data.get("id")
            if not isinstance(nested, list):
                if not isinstance(nested_id, str) or nested_id in visited:
                    raise UnavailableFingerprint("nested forward is unavailable")
                nested = await _fetch_forward(event, nested_id)
            child_visited = visited | (
                {nested_id} if isinstance(nested_id, str) else set()
            )
            result.append(
                [
                    "forward",
                    await _normalize_nodes(event, nested, depth + 1, child_visited),
                ]
            )
        elif kind == "node":
            result.append(
                ["node", await _normalize_nodes(event, [segment], depth + 1, visited)]
            )
        elif kind == "at":
            result.append(["at", str(data.get("qq", ""))])
        else:
            raise UnavailableFingerprint(f"unsupported forward segment: {kind}")
    return result
