import asyncio
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as media:
        for chunk in iter(lambda: media.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FingerprintStore:
    def __init__(self, path: Path):
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                    CREATE TABLE IF NOT EXISTS fingerprints (
                        bot_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        digest TEXT NOT NULL,
                        first_message_id TEXT NOT NULL,
                        PRIMARY KEY (bot_id, group_id, kind, digest)
                    )
                    """
            )

    async def find_or_record(
        self,
        bot_id: str,
        group_id: str,
        message_id: str,
        fingerprints: list[tuple[str, str]],
    ) -> str | None:
        return await asyncio.to_thread(
            self._find_or_record,
            bot_id,
            group_id,
            message_id,
            fingerprints,
        )

    def _find_or_record(
        self,
        bot_id: str,
        group_id: str,
        message_id: str,
        fingerprints: list[tuple[str, str]],
    ) -> str | None:
        unique_fingerprints = list(dict.fromkeys(fingerprints))
        if not unique_fingerprints:
            return None

        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for kind, digest in unique_fingerprints:
                row = connection.execute(
                    """
                    SELECT first_message_id
                    FROM fingerprints
                    WHERE bot_id = ? AND group_id = ? AND kind = ? AND digest = ?
                    """,
                    (bot_id, group_id, kind, digest),
                ).fetchone()
                if row is not None and row["first_message_id"] != message_id:
                    return row["first_message_id"]

            connection.executemany(
                """
                    INSERT OR IGNORE INTO fingerprints
                        (bot_id, group_id, kind, digest, first_message_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                (
                    (bot_id, group_id, kind, digest, message_id)
                    for kind, digest in unique_fingerprints
                ),
            )
            return None
