import asyncio
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_qq_group_tools.store import FingerprintStore, sha256_file


class FingerprintStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = FingerprintStore(
            Path(self.directory.name) / "fingerprints.sqlite3"
        )
        await self.store.initialize()

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_exact_bytes_and_group_scope(self):
        first = await self.store.find_or_record(
            "bot", "group-a", "1", [("image", "hash-a")]
        )
        self.assertIsNone(first)
        duplicate = await self.store.find_or_record(
            "bot", "group-a", "2", [("image", "hash-a")]
        )
        self.assertEqual(duplicate, "1")
        self.assertIsNone(
            await self.store.find_or_record(
                "bot", "group-b", "3", [("image", "hash-a")]
            )
        )
        self.assertIsNone(
            await self.store.find_or_record(
                "other-bot", "group-a", "4", [("image", "hash-a")]
            )
        )
        self.assertIsNone(
            await self.store.find_or_record(
                "bot", "group-a", "1", [("image", "hash-a")]
            )
        )

        self.assertIsNone(
            await self.store.find_or_record(
                "bot", "group-a", "5", [("video", "hash-a")]
            )
        )

    async def test_duplicate_in_multi_image_message_does_not_record_new_image(self):
        await self.store.find_or_record("bot", "group", "1", [("image", "existing")])
        duplicate = await self.store.find_or_record(
            "bot", "group", "2", [("video", "new"), ("image", "existing")]
        )
        self.assertEqual(duplicate, "1")
        self.assertIsNone(
            await self.store.find_or_record("bot", "group", "3", [("video", "new")])
        )

    async def test_concurrent_first_sends_have_one_winner(self):
        results = await asyncio.gather(
            self.store.find_or_record("bot", "group", "1", [("video", "same")]),
            self.store.find_or_record("bot", "group", "2", [("video", "same")]),
        )
        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_hash_changes_with_bytes(self):
        first = Path(self.directory.name) / "first.png"
        second = Path(self.directory.name) / "second.png"
        first.write_bytes(b"image-a")
        second.write_bytes(b"image-b")
        self.assertNotEqual(sha256_file(str(first)), sha256_file(str(second)))
