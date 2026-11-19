"""SQLite runtime store, one per character: the compiled pack, people, chats and their
messages, media, long-term memory and scenes."""

from __future__ import annotations

import contextlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from ..pack import CompiledPack

SCHEMA_VERSION = 9
# Sender id of the chat app's own lines (pats, joins, recalls).
SYSTEM_SENDER = "system"
# Sender id of system instructions: facts and rules the character takes as given, added by
# whoever runs her (Engine.instruct), never typed by people in the chat.
INSTRUCTION_SENDER = "instruction"
# Sender id of what the character's helper agent found when she looked something up
# (Engine.deliver_lookup): only she sees it, and she answers with it.
LOOKUP_SENDER = "lookup"
# Where the one-time code that turns a chat message into an instruction is kept (its hash).
_CODE_KEY = "instruction_code"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    nickname   TEXT NOT NULL,
    nicknames  TEXT NOT NULL,        -- JSON list of every nickname seen
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,       -- group | private
    created_at  INTEGER NOT NULL,
    judged_upto INTEGER,             -- newest message id already handled (answered, judged or skipped)
    summary     TEXT,                -- rolling summary of messages that left the history window
    summary_upto INTEGER,            -- newest message id the summary covers
    guests      TEXT,                -- CLI: JSON list of other characters invited into this chat
    window_from INTEGER,             -- first message id of the history window (cut back in chunks)
    hushed      INTEGER NOT NULL DEFAULT 0 -- gateway speech lock: someone asked the character not to talk
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT UNIQUE,
    chat_id     TEXT NOT NULL REFERENCES chats(id),
    role        TEXT NOT NULL,       -- user | assistant
    sender_id   TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    content     TEXT NOT NULL,
    ts          INTEGER NOT NULL,    -- unix ms
    platform_id TEXT,                -- the chat app's message id, for quotes
    quote       TEXT,                -- JSON {message_id, sender_name, text} of the quoted message
    bot         INTEGER NOT NULL DEFAULT 0, -- sent by a bot (e.g. dice): context only, never answered
    trigger     INTEGER NOT NULL DEFAULT 0, -- demands a reply (@-mention, quote of the character, private)
    backfill    INTEGER NOT NULL DEFAULT 0, -- pre-invite history, never pending
    dice        TEXT,                -- roll | result, with the dice component on (engine/dice.py)
    digest      TEXT,                -- for a very long message: a summary shown in its place ("" if refused)
    recalls     INTEGER,             -- a notice that the sender recalled (撤回) a message: its id, 0 if unknown
    action_id   TEXT,                -- gateway reply, until the connector reports its outcome
    send_due_ms INTEGER              -- NULL until dispatch starts, then the reserved send time
);
CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS messages_chat_sender ON messages(chat_id, sender_id, ts DESC, id DESC);
-- Preserve connector receipts when the event has no message, or its message is removed.
CREATE TABLE IF NOT EXISTS event_receipts (event_id TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS retain_event_receipt AFTER DELETE ON messages
WHEN OLD.event_id IS NOT NULL BEGIN
    INSERT OR IGNORE INTO event_receipts VALUES (OLD.event_id);
END;
CREATE TABLE IF NOT EXISTS attachments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    ord        INTEGER NOT NULL,
    kind       TEXT NOT NULL,        -- image | sticker
    sha256     TEXT,                 -- NULL when the platform couldn't provide the file
    hint       TEXT,                 -- the platform's own description (sticker names)
    thumbnail  INTEGER NOT NULL DEFAULT 0 -- only the chat app's small preview, not the original
);
CREATE INDEX IF NOT EXISTS attachments_message ON attachments(message_id);
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL,        -- the chat it was written in, and the only one that sees it
    subject_id TEXT,                 -- the person it is about, if one
    text       TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    removed_ts INTEGER               -- set when removed: kept, so it can be looked at and restored
);
CREATE INDEX IF NOT EXISTS memories_chat ON memories(chat_id, subject_id);
CREATE TABLE IF NOT EXISTS memory_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    action     TEXT NOT NULL,        -- add | edit | remove | restore
    actor      TEXT NOT NULL,        -- character | owner
    text       TEXT NOT NULL,        -- the memory's text after the change (at removal, for remove)
    before     TEXT,                 -- edit: the text it replaced
    reason     TEXT,                 -- remove: why
    ts         INTEGER NOT NULL,
    after_id   INTEGER NOT NULL      -- the newest message (of any chat) when it was made: shown after it
);
CREATE INDEX IF NOT EXISTS memory_changes_memory ON memory_changes(memory_id);
CREATE TABLE IF NOT EXISTS scenes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL,        -- author-set account of what happened earlier in this chat
    text       TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS scenes_chat ON scenes(chat_id);
CREATE TABLE IF NOT EXISTS media (
    sha256       TEXT PRIMARY KEY,   -- of the normalized file
    mime         TEXT NOT NULL,
    path         TEXT NOT NULL,      -- relative to the data dir
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    description  TEXT,               -- written by the vision task; NULL until then
    described_by TEXT
);
"""
# Columns added after schema v1, for databases created before them.
_ADDED_COLUMNS = {
    "chats": [
        ("judged_upto", "INTEGER"),
        ("summary", "TEXT"),
        ("summary_upto", "INTEGER"),
        ("guests", "TEXT"),
        ("window_from", "INTEGER"),
        ("hushed", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "messages": [
        ("platform_id", "TEXT"),
        ("quote", "TEXT"),
        ("bot", "INTEGER NOT NULL DEFAULT 0"),
        ("trigger", "INTEGER NOT NULL DEFAULT 0"),
        ("backfill", "INTEGER NOT NULL DEFAULT 0"),
        ("dice", "TEXT"),
        ("digest", "TEXT"),
        ("recalls", "INTEGER"),
        ("action_id", "TEXT"),
        ("send_due_ms", "INTEGER"),
    ],
    "attachments": [
        ("thumbnail", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "memories": [
        ("removed_ts", "INTEGER"),
    ],
    # Changes logged before v9 are placed by their time (Store._migrate).
    "memory_changes": [
        ("after_id", "INTEGER NOT NULL DEFAULT -1"),
    ],
}
# Columns of earlier schemas that nothing reads any more: memories were sorted into kinds, and
# read from the chat by a memory task up to a cursor, until the character kept them herself (v8).
_DROPPED_COLUMNS = {"memories": ["kind"], "chats": ["memory_upto"]}
_MSG_COLUMNS = "id, chat_id, role, sender_id, sender_name, content, ts, platform_id, quote, bot, dice, digest, recalls, backfill"

ChatKind = Literal["group", "private"]
Role = Literal["user", "assistant"]
# A person asking the dice for a roll, or the dice answering.
DiceKind = Literal["roll", "result"]


MediaKind = Literal["image", "sticker"]


@dataclass(frozen=True)
class Quote:
    """The message a message replies to, as the chat app showed it."""

    message_id: str | None
    sender_name: str
    text: str


@dataclass(frozen=True)
class Attachment:
    kind: MediaKind
    # None when the file wasn't available (e.g. an image the client never downloaded).
    sha256: str | None = None
    hint: str | None = None
    # Only the chat app's small preview of an image: the original wasn't available.
    thumbnail: bool = False


@dataclass(frozen=True)
class MediaRecord:
    sha256: str
    mime: str
    path: str
    width: int
    height: int
    description: str | None


@dataclass(frozen=True)
class StoredMessage:
    id: int
    chat_id: str
    role: Role
    sender_id: str
    sender_name: str
    content: str
    ts: int
    platform_id: str | None = None
    quote: Quote | None = None
    bot: bool = False
    attachments: tuple[Attachment, ...] = ()
    dice: DiceKind | None = None
    digest: str | None = None
    # A recall notice (see Engine.receive): the id of the message recalled, 0 when unknown.
    recalls: int | None = None
    backfill: bool = False


@dataclass(frozen=True)
class Pending:
    """Messages from people that arrived after the chat was last handled."""

    latest_id: int
    newest_ts: int
    # The newest message among them that demands a reply, if any.
    trigger_id: int | None
    trigger_platform_id: str | None
    # A system instruction is among them.
    instructed: bool = False
    # What she looked up has come back (it demands a reply, like an instruction, but a speech
    # lock still holds).
    looked_up: bool = False


# Who changed a memory: the character herself (her memory tools), or whoever runs her (the CLI).
MemoryActor = Literal["character", "owner"]


@dataclass(frozen=True)
class Memory:
    """A note the character keeps about a chat, optionally about one person in it."""

    id: int
    chat_id: str
    subject_id: str | None
    text: str
    updated_ts: int
    removed_ts: int | None = None
    created_ts: int = 0


@dataclass(frozen=True)
class MemoryChange:
    id: int
    memory_id: int
    chat_id: str
    action: Literal["add", "edit", "remove", "restore"]
    actor: MemoryActor
    text: str
    before: str | None
    reason: str | None
    ts: int
    # The newest message of the store when the change was made: in a chat's history the change
    # comes after it, so a change never lands before anything already shown.
    after_id: int


@dataclass(frozen=True)
class Scene:
    id: int
    chat_id: str
    text: str
    created_ts: int


@dataclass(frozen=True)
class StoreSummary:
    pack_hash: str | None
    name: str | None
    chats: int
    messages: int


def peek(path: Path) -> StoreSummary | None:
    """Read-only summary of a character database, without creating or migrating it."""
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        name = None
        if "pack" in meta:
            with contextlib.suppress(ValidationError):  # compiled by an older version
                name = CompiledPack.model_validate_json(meta["pack"]).character.name
        return StoreSummary(
            pack_hash=meta.get("pack_hash"),
            name=name,
            chats=conn.execute("SELECT count(*) FROM chats").fetchone()[0],
            messages=conn.execute("SELECT count(*) FROM messages").fetchone()[0],
        )
    finally:
        conn.close()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self._set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    def _migrate(self) -> None:
        for table, columns in _ADDED_COLUMNS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.execute(
            "UPDATE memory_changes SET after_id = (SELECT coalesce(max(id), 0) FROM messages WHERE ts <= memory_changes.ts)"
            " WHERE after_id = -1"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS messages_platform ON messages(chat_id, platform_id)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS messages_recalls ON messages(chat_id, recalls) WHERE recalls IS NOT NULL")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_action ON messages(action_id) WHERE action_id IS NOT NULL")
        self.conn.execute("DROP TABLE IF EXISTS canon_index")  # written by early versions, never read
        for table, columns in _DROPPED_COLUMNS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name in columns:
                if name in have:
                    self.conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")

    def close(self) -> None:
        self.conn.close()

    @contextlib.contextmanager
    def transaction(self):
        """Let the writes for one incoming event commit or roll back together."""
        if self.conn.in_transaction:
            yield
        else:
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                yield

    def has_event(self, event_id: str | None) -> bool:
        return event_id is not None and self.conn.execute(
            "SELECT 1 FROM messages WHERE event_id = ? UNION ALL "
            "SELECT 1 FROM event_receipts WHERE event_id = ? LIMIT 1", (event_id, event_id)
        ).fetchone() is not None

    def record_event(self, event_id: str) -> None:
        with self.transaction():
            self.conn.execute("INSERT OR IGNORE INTO event_receipts VALUES (?)", (event_id,))

    # --- meta -------------------------------------------------------------

    def _get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- compiled pack ----------------------------------------------------

    def save_pack(self, pack: CompiledPack) -> None:
        """Keep a freshly compiled pack. Learned data is untouched."""
        with self.conn:
            self._set_meta("pack", pack.model_dump_json())
            self._set_meta("pack_hash", pack.pack_hash)

    def load_pack(self) -> CompiledPack | None:
        """The last compiled pack; None if there is none, or it no longer fits the current schema."""
        raw = self._get_meta("pack")
        if not raw:
            return None
        try:
            return CompiledPack.model_validate_json(raw)
        except ValidationError:
            return None

    def pack_hash(self) -> str | None:
        return self._get_meta("pack_hash")

    # --- messages ---------------------------------------------------------

    def add_message(
        self,
        *,
        chat_id: str,
        role: Role,
        sender_id: str,
        sender_name: str,
        content: str,
        ts: int,
        event_id: str | None = None,
        platform_id: str | None = None,
        quote: Quote | None = None,
        bot: bool = False,
        attachments: tuple[Attachment, ...] = (),
        trigger: bool = False,
        dice: DiceKind | None = None,
        recalls: int | None = None,
        chat_kind: ChatKind = "private",
        action_id: str | None = None,
        backfill: bool = False,
    ) -> StoredMessage | None:
        """Store a message. Returns None if event_id was already stored (idempotent).

        `chat_kind` only matters for the first message of a chat, which creates it.
        """
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO chats(id, kind, created_at) VALUES(?, ?, ?)",
                (chat_id, chat_kind, ts),
            )
            quote_json = None
            if quote is not None:
                quote_json = json.dumps(
                    {"message_id": quote.message_id, "sender_name": quote.sender_name, "text": quote.text},
                    ensure_ascii=False,
                )
            cur = self.conn.execute(
                "INSERT INTO messages(event_id, chat_id, role, sender_id, sender_name, content, ts, "
                "platform_id, quote, bot, trigger, dice, recalls, action_id, backfill) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO NOTHING",
                (event_id, chat_id, role, sender_id, sender_name, content, ts, platform_id, quote_json,
                 int(bot), int(trigger), dice, recalls, action_id, int(backfill)),
            )
            if cur.rowcount == 0 or cur.lastrowid is None:
                return None
            msg_id = cur.lastrowid
            if role == "user" and sender_id not in (INSTRUCTION_SENDER, LOOKUP_SENDER):
                self._touch_user(sender_id, sender_name, ts)
            self.conn.executemany(
                "INSERT INTO attachments(message_id, ord, kind, sha256, hint, thumbnail) VALUES(?, ?, ?, ?, ?, ?)",
                [(msg_id, i, a.kind, a.sha256, a.hint, int(a.thumbnail)) for i, a in enumerate(attachments)],
            )
        return StoredMessage(
            msg_id, chat_id, role, sender_id, sender_name, content, ts,
            platform_id, quote, bot, tuple(attachments), dice, recalls=recalls, backfill=backfill,
        )

    def _messages(self, where: str, params: tuple, order: str = "id", limit: str = "") -> list[StoredMessage]:
        rows = self.conn.execute(
            f"SELECT {_MSG_COLUMNS} FROM messages WHERE {where} ORDER BY {order} {limit}", params
        ).fetchall()
        ids = [r["id"] for r in rows]
        attached: dict[int, list[Attachment]] = {}
        limit_vars = self.conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        for start in range(0, len(ids), limit_vars):
            batch = ids[start:start + limit_vars]
            marks = ",".join("?" * len(batch))
            for a in self.conn.execute(
                f"SELECT message_id, kind, sha256, hint, thumbnail FROM attachments WHERE message_id IN ({marks}) "
                "ORDER BY message_id, ord",
                batch,
            ):
                attached.setdefault(a["message_id"], []).append(Attachment(a["kind"], a["sha256"], a["hint"], bool(a["thumbnail"])))
        out = []
        for r in rows:
            q = json.loads(r["quote"]) if r["quote"] else None
            out.append(
                StoredMessage(
                    r["id"], r["chat_id"], r["role"], r["sender_id"], r["sender_name"], r["content"], r["ts"],
                    r["platform_id"], Quote(**q) if q else None, bool(r["bot"]),
                    tuple(attached.get(r["id"], ())), r["dice"], r["digest"], r["recalls"], bool(r["backfill"]),
                )
            )
        return out

    def recent_messages(self, chat_id: str, limit: int) -> list[StoredMessage]:
        rows = self._messages("chat_id = ?", (chat_id,), "id DESC", f"LIMIT {int(limit)}")
        return list(reversed(rows))

    def judge_history(self, chat_id: str, newest_id: int, limit: int) -> list[StoredMessage]:
        """End at the newest pending message, so subsequent backfill cannot crowd it out."""
        rows = self._messages("chat_id = ? AND sender_id NOT IN (?, ?) AND id <= ?",
                              (chat_id, INSTRUCTION_SENDER, LOOKUP_SENDER, newest_id), "id DESC", f"LIMIT {int(limit)}")
        return list(reversed(rows))

    def messages_from(self, chat_id: str, offset: int) -> list[StoredMessage]:
        """All messages of a chat after skipping the first `offset`, oldest first."""
        return self._messages("chat_id = ?", (chat_id,), "id", f"LIMIT -1 OFFSET {int(offset)}")

    def by_platform_id(self, chat_id: str, platform_id: str) -> StoredMessage | None:
        found = self._messages("chat_id = ? AND platform_id = ?", (chat_id, platform_id), "id DESC", "LIMIT 1")
        return found[0] if found else None

    def set_content(self, message_id: int, content: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE messages SET content = ? WHERE id = ?", (content, message_id))

    def message(self, chat_id: str, message_id: int | str) -> StoredMessage | None:
        found = self._messages("chat_id = ? AND id = ?", (chat_id, message_id))
        return found[0] if found else None

    def recalled(self, chat_id: str, messages: list[StoredMessage]) -> set[int]:
        """Which of these messages people have since recalled."""
        if not messages:
            return set()
        rows = self.conn.execute(
            "SELECT recalls FROM messages WHERE chat_id = ? AND recalls BETWEEN ? AND ?",
            (chat_id, min(m.id for m in messages), max(m.id for m in messages)),
        ).fetchall()
        return {r["recalls"] for r in rows}

    def recalled_platform_ids(self, chat_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT m.platform_id FROM messages n JOIN messages m ON n.recalls = m.id "
            "WHERE n.chat_id = ? AND n.recalls IS NOT NULL AND m.platform_id IS NOT NULL", (chat_id,)
        )
        return {r[0] for r in rows}

    def long_undigested(self, chat_id: str, chars: int) -> list[StoredMessage]:
        """People's messages longer than `chars` that have no digest yet."""
        return self._messages("chat_id = ? AND role = 'user' AND sender_id NOT IN (?, ?) AND length(content) > ? "
                              "AND digest IS NULL", (chat_id, INSTRUCTION_SENDER, LOOKUP_SENDER, chars))

    def set_digest(self, message_id: int, digest: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE messages SET digest = ? WHERE id = ?", (digest, message_id))

    def window_from(self, chat_id: str) -> int:
        row = self.conn.execute("SELECT window_from FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return (row["window_from"] or 0) if row else 0

    def set_window_from(self, chat_id: str, message_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE chats SET window_from = ? WHERE id = ?", (message_id, chat_id))

    def set_platform_id(self, message_id: int, platform_id: str) -> None:
        """Record the chat app's id for a message we sent, once the connector reports it."""
        with self.transaction():
            self.conn.execute("UPDATE messages SET platform_id = ? WHERE id = ?", (platform_id, message_id))

    def in_flight(self) -> list[tuple[str, str, str, int]]:
        """Replies handed to the connector with no outcome yet: (chat id, action id, text, due ms)."""
        return [
            (r["chat_id"], r["action_id"], r["content"], r["send_due_ms"])
            for r in self.conn.execute(
                "SELECT chat_id, action_id, content, send_due_ms FROM messages "
                "WHERE action_id IS NOT NULL AND send_due_ms IS NOT NULL ORDER BY id"
            )
        ]

    def replace_thumbnail(self, message_id: int, ord_: int, sha256: str) -> bool:
        """Swap a thumbnail attachment for its original. False if there is no such thumbnail."""
        with self.transaction():
            return self.conn.execute(
                "UPDATE attachments SET sha256 = ?, thumbnail = 0 WHERE message_id = ? AND ord = ? AND thumbnail = 1",
                (sha256, message_id, ord_),
            ).rowcount > 0

    def outgoing_reply(self, action_id: str) -> StoredMessage | None:
        replies = self._messages("action_id = ?", (action_id,))
        return replies[0] if replies else None

    def start_delivery(self, message_id: int, due_ms: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE messages SET send_due_ms = ? WHERE id = ?", (due_ms, message_id))

    def finish_delivery(self, action_id: str, platform_id: str | None, failed: bool) -> int | None:
        row = self.conn.execute(
            "SELECT id, chat_id, send_due_ms FROM messages WHERE action_id = ? AND send_due_ms IS NOT NULL", (action_id,)
        ).fetchone()
        if row is None:
            return None
        with self.transaction():
            self.mark_handled(row["chat_id"], row["id"] - 1)
            if failed:
                self.delete_messages([row["id"]])
            else:
                if platform_id:
                    self.set_platform_id(row["id"], platform_id)
                self.conn.execute("UPDATE messages SET action_id = NULL, send_due_ms = NULL WHERE id = ?", (row["id"],))
        return row["send_due_ms"]

    def count_messages(self, chat_id: str, upto_id: int | None = None) -> int:
        """How many messages a chat has (up to and including upto_id, when given)."""
        return self.conn.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ? AND (? IS NULL OR id <= ?)", (chat_id, upto_id, upto_id)
        ).fetchone()[0]

    def senders(self, chat_id: str) -> dict[str, str]:
        """Everyone (but the system) who has posted in a chat, by ID, with the name they used last."""
        rows = self.conn.execute(
            "SELECT sender_id, sender_name FROM messages WHERE chat_id = ? AND role = 'user' "
            "AND sender_id NOT IN (?, ?, ?) ORDER BY id",
            (chat_id, SYSTEM_SENDER, INSTRUCTION_SENDER, LOOKUP_SENDER),
        ).fetchall()
        return {r["sender_id"]: r["sender_name"] for r in rows}

    def recent_user_messages(self, chat_id: str, limit: int) -> list[StoredMessage]:
        """Latest messages from people (not the character), newest first."""
        return self._messages(
            "chat_id = ? AND role = 'user' AND bot = 0", (chat_id,), "id DESC", f"LIMIT {int(limit)}"
        )

    def delete_from(self, chat_id: str, message_id: int) -> int:
        """Delete a message and everything after it in the chat. Returns how many were removed."""
        with self.conn:
            return self.conn.execute(
                "DELETE FROM messages WHERE chat_id = ? AND id >= ?", (chat_id, message_id)
            ).rowcount

    def delete_messages(self, ids: list[int]) -> None:
        with self.transaction():
            self.conn.executemany("DELETE FROM messages WHERE id = ?", [(i,) for i in ids])

    def messages_between(self, chat_id: str, after_id: int, upto_id: int | None = None) -> list[StoredMessage]:
        """Messages with after_id < id <= upto_id (no upper bound when upto_id is None), oldest first."""
        if upto_id is None:
            return self._messages("chat_id = ? AND id > ?", (chat_id, after_id))
        return self._messages("chat_id = ? AND id > ? AND id <= ?", (chat_id, after_id, upto_id))

    def chat_ids(self) -> list[str]:
        return [r["id"] for r in self.conn.execute("SELECT id FROM chats")]

    def chats_by_activity(self, prefix: str) -> list[tuple[str, int]]:
        """Chats whose id starts with `prefix`, most recently active first, with that time
        (the last message's, or when the chat was created)."""
        rows = self.conn.execute(
            "SELECT c.id, coalesce(max(m.ts), c.created_at) AS active FROM chats c "
            "LEFT JOIN messages m ON m.chat_id = c.id WHERE substr(c.id, 1, ?) = ? "
            "GROUP BY c.id ORDER BY active DESC",
            (len(prefix), prefix),
        ).fetchall()
        return [(r["id"], r["active"]) for r in rows]

    def latest_message_id(self, chat_id: str) -> int | None:
        return self.conn.execute("SELECT max(id) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]

    def messages_since(self, chat_id: str, ts: int) -> list[StoredMessage]:
        """A chat's messages sent at or after `ts`, oldest first."""
        return self._messages("chat_id = ? AND ts >= ?", (chat_id, ts))

    # --- media ------------------------------------------------------------

    def save_media(self, rec: MediaRecord) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO media(sha256, mime, path, width, height, description) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (rec.sha256, rec.mime, rec.path, rec.width, rec.height, rec.description),
            )

    def media_map(self, shas: list[str]) -> dict[str, MediaRecord]:
        shas = list(dict.fromkeys(shas))
        media = {}
        limit_vars = self.conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        for start in range(0, len(shas), limit_vars):
            batch = shas[start:start + limit_vars]
            marks = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT sha256, mime, path, width, height, description FROM media WHERE sha256 IN ({marks})", batch
            )
            media.update((r["sha256"], MediaRecord(**dict(r))) for r in rows)
        return media

    def undescribed_media(self, chat_id: str) -> list[tuple[MediaRecord, Attachment]]:
        """Media attached in this chat that has no description yet (one row per file)."""
        rows = self.conn.execute(
            "SELECT DISTINCT m.sha256, m.mime, m.path, m.width, m.height, m.description, a.kind, a.hint, a.thumbnail "
            "FROM attachments a JOIN messages msg ON msg.id = a.message_id JOIN media m ON m.sha256 = a.sha256 "
            "WHERE msg.chat_id = ? AND m.description IS NULL GROUP BY m.sha256",
            (chat_id,),
        ).fetchall()
        return [
            (MediaRecord(r["sha256"], r["mime"], r["path"], r["width"], r["height"], None),
             Attachment(r["kind"], r["sha256"], r["hint"], bool(r["thumbnail"])))
            for r in rows
        ]

    def describe_media(self, sha256: str, description: str, model: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE media SET description = ?, described_by = ? WHERE sha256 = ?",
                (description, model, sha256),
            )

    # --- what has been handled ------------------------------------------

    def judged_upto(self, chat_id: str) -> int | None:
        row = self.conn.execute("SELECT judged_upto FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return row["judged_upto"] if row else None

    def pending(self, chat_id: str) -> Pending | None:
        """What still needs handling: people's messages (and dice results, which the judge may
        hand on) after the last reply and the handled mark."""
        floor = self.conn.execute(
            "SELECT max(coalesce((SELECT judged_upto FROM chats WHERE id = :c), 0),"
            " coalesce((SELECT max(id) FROM messages WHERE chat_id = :c AND role = 'assistant'), 0))",
            {"c": chat_id},
        ).fetchone()[0]
        rows = self.conn.execute(
            "SELECT id, ts, trigger, platform_id, sender_id FROM messages "
            "WHERE chat_id = ? AND role = 'user' AND backfill = 0 AND (bot = 0 OR dice = 'result') AND id > ? ORDER BY id",
            (chat_id, floor),
        ).fetchall()
        if not rows:
            return None
        triggers = [r for r in rows if r["trigger"]]
        last = triggers[-1] if triggers else None
        return Pending(
            rows[-1]["id"], rows[-1]["ts"],
            last["id"] if last else None, last["platform_id"] if last else None,
            any(r["sender_id"] == INSTRUCTION_SENDER for r in rows),
            any(r["sender_id"] == LOOKUP_SENDER for r in rows),
        )

    def chat_kind(self, chat_id: str) -> ChatKind | None:
        row = self.conn.execute("SELECT kind FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return row["kind"] if row else None

    def discard_gateway_drafts(self) -> int:
        with self.conn:
            return self.conn.execute(
                "DELETE FROM messages WHERE chat_id NOT GLOB 'cli:*' AND role = 'assistant' "
                "AND (content = '' OR (action_id IS NOT NULL AND send_due_ms IS NULL))"
            ).rowcount

    def hushed(self, chat_id: str) -> bool:
        row = self.conn.execute("SELECT hushed FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return bool(row and row["hushed"])

    def set_hushed(self, chat_id: str, on: bool) -> None:
        with self.transaction():
            self.conn.execute("UPDATE chats SET hushed = ? WHERE id = ?", (int(on), chat_id))

    def mark_handled(self, chat_id: str, upto_id: int) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE chats SET judged_upto = max(coalesce(judged_upto, 0), ?) WHERE id = ?",
                (upto_id, chat_id),
            )

    def require_reply(self, message_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE messages SET trigger = 1 WHERE id = ?", (message_id,))

    # --- long-term memory -------------------------------------------------

    def memories(self, chat_id: str | None, *, removed: bool = False) -> list[Memory]:
        """A chat's memories (every chat's, with None), oldest first; with `removed`, the removed
        ones instead."""
        rows = self.conn.execute(
            "SELECT id, chat_id, subject_id, text, updated_ts, removed_ts, created_ts FROM memories"
            f" WHERE (? IS NULL OR chat_id = ?) AND removed_ts IS {'NOT ' if removed else ''}NULL ORDER BY id",
            (chat_id, chat_id),
        ).fetchall()
        return [Memory(**dict(r)) for r in rows]

    def _memory(self, chat_id: str | None, memory_id: int, removed: bool = False) -> Memory | None:
        return next((m for m in self.memories(chat_id, removed=removed) if m.id == memory_id), None)

    def _log_change(self, memory_id: int, action: str, actor: MemoryActor, text: str, ts: int,
                    before: str | None = None, reason: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO memory_changes(memory_id, action, actor, text, before, reason, ts, after_id)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, (SELECT coalesce(max(id), 0) FROM messages))",
            (memory_id, action, actor, text, before, reason, ts),
        )

    def add_memory(self, chat_id: str, subject_id: str | None, text: str, ts: int, actor: MemoryActor = "character") -> int:
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO memories(chat_id, subject_id, text, created_ts, updated_ts) VALUES(?, ?, ?, ?, ?)",
                (chat_id, subject_id, text, ts, ts),
            )
            memory_id = cur.lastrowid or 0
            self._log_change(memory_id, "add", actor, text, ts)
        return memory_id

    def update_memory(self, chat_id: str | None, memory_id: int, text: str, ts: int,
                      actor: MemoryActor = "character", *, dated: int | None = None) -> bool:
        """Rewrite a memory of this chat (of any chat, with None). It is dated `ts` unless
        `dated` says otherwise (a correction keeps the date of what it corrects)."""
        with self.transaction():
            old = self._memory(chat_id, memory_id)
            if old is None:
                return False
            self.conn.execute("UPDATE memories SET text = ?, updated_ts = ? WHERE id = ?",
                              (text, ts if dated is None else dated, memory_id))
            self._log_change(memory_id, "edit", actor, text, ts, before=old.text)
        return True

    def remove_memory(self, chat_id: str | None, memory_id: int, ts: int, actor: MemoryActor = "character",
                      reason: str | None = None) -> bool:
        """Take a memory of this chat (of any chat, with None) out of use. It stays in the
        database with its history, so it can be restored (restore_memory)."""
        with self.transaction():
            old = self._memory(chat_id, memory_id)
            if old is None:
                return False
            self.conn.execute("UPDATE memories SET removed_ts = ? WHERE id = ?", (ts, memory_id))
            self._log_change(memory_id, "remove", actor, old.text, ts, reason=reason)
        return True

    def restore_memory(self, memory_id: int, ts: int, actor: MemoryActor = "owner") -> bool:
        with self.transaction():
            old = self._memory(None, memory_id, removed=True)
            if old is None:
                return False
            self.conn.execute("UPDATE memories SET removed_ts = NULL WHERE id = ?", (memory_id,))
            self._log_change(memory_id, "restore", actor, old.text, ts)
        return True

    def purge_memory(self, memory_id: int) -> bool:
        """Delete a memory and its history for good (things that must not be kept at all)."""
        with self.transaction():
            cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cur.rowcount > 0

    def memory_changes(self, chat_id: str | None) -> list[MemoryChange]:
        """Every change to the memories of a chat (of every chat, with None), oldest first."""
        rows = self.conn.execute(
            "SELECT c.id, c.memory_id, m.chat_id, c.action, c.actor, c.text, c.before, c.reason, c.ts, c.after_id"
            " FROM memory_changes c JOIN memories m ON m.id = c.memory_id"
            " WHERE (? IS NULL OR m.chat_id = ?) ORDER BY c.id",
            (chat_id, chat_id),
        ).fetchall()
        return [MemoryChange(**dict(r)) for r in rows]

    def summary(self, chat_id: str) -> tuple[str | None, int]:
        row = self.conn.execute("SELECT summary, summary_upto FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return (row["summary"], row["summary_upto"] or 0) if row else (None, 0)

    def set_summary(self, chat_id: str, summary: str | None, upto: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE chats SET summary = ?, summary_upto = ? WHERE id = ?", (summary, upto, chat_id)
            )

    # --- other characters in the chat (CLI) --------------------------------

    def guests(self, chat_id: str) -> list[str]:
        row = self.conn.execute("SELECT guests FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return json.loads(row["guests"]) if row and row["guests"] else []

    def set_guests(self, chat_id: str, char_ids: list[str]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE chats SET guests = ? WHERE id = ?", (json.dumps(char_ids) if char_ids else None, chat_id)
            )

    # --- scenes -----------------------------------------------------------

    def scenes(self, chat_id: str) -> list[Scene]:
        rows = self.conn.execute(
            "SELECT id, chat_id, text, created_ts FROM scenes WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
        return [Scene(**dict(r)) for r in rows]

    def add_scene(self, chat_id: str, text: str, ts: int) -> int:
        with self.conn:
            # A scene set before anyone has spoken still makes the chat exist.
            self.conn.execute(
                "INSERT OR IGNORE INTO chats(id, kind, created_at) VALUES(?, 'private', ?)", (chat_id, ts)
            )
            cur = self.conn.execute(
                "INSERT INTO scenes(chat_id, text, created_ts) VALUES(?, ?, ?)", (chat_id, text, ts)
            )
        return cur.lastrowid or 0

    def delete_scene(self, chat_id: str, scene_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM scenes WHERE id = ? AND chat_id = ?", (scene_id, chat_id))
        return cur.rowcount > 0

    # --- system instructions ----------------------------------------------

    def instructions(self, chat_id: str | None) -> list[StoredMessage]:
        """A chat's system instructions (every chat's, with None), oldest first."""
        if chat_id is None:
            return self._messages("sender_id = ?", (INSTRUCTION_SENDER,))
        return self._messages("chat_id = ? AND sender_id = ?", (chat_id, INSTRUCTION_SENDER))

    def instruction_ids(self, chat_id: str) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT id FROM messages WHERE chat_id = ? AND sender_id = ?",
                                                (chat_id, INSTRUCTION_SENDER))}

    def instructions_before(self, chat_id: str, message_id: int) -> list[StoredMessage]:
        """A chat's system instructions that come before this message, oldest first."""
        return self._messages("chat_id = ? AND sender_id = ? AND id < ?", (chat_id, INSTRUCTION_SENDER, message_id))

    def set_code(self, digest: str, expires_ms: int) -> None:
        """Keep the hash of the one-time code, replacing any code issued before."""
        with self.conn:
            self._set_meta(_CODE_KEY, json.dumps({"sha256": digest, "expires": expires_ms}))

    def use_code(self, digest: str, now_ms: int) -> bool:
        """Whether this is the hash of the code issued and still valid; a match uses it up, so it
        works once, even with several processes on the database. An expired code is dropped."""
        raw = self._get_meta(_CODE_KEY)
        if raw is None:
            return False
        try:
            code = json.loads(raw)
            stored, expires = str(code["sha256"]), int(code["expires"])
        except (ValueError, KeyError, TypeError):
            stored, expires = "", 0
        if now_ms >= expires:
            with self.transaction():
                self.conn.execute("DELETE FROM meta WHERE key = ? AND value = ?", (_CODE_KEY, raw))
            return False
        if not hmac.compare_digest(stored.encode(), digest.encode()):
            return False
        with self.transaction():
            cur = self.conn.execute("DELETE FROM meta WHERE key = ? AND value = ?", (_CODE_KEY, raw))
        return cur.rowcount > 0

    # --- users ------------------------------------------------------------

    def nickname(self, user_id: str, chat_id: str | None = None) -> str | None:
        """The name a person went by most recently."""
        if chat_id is not None:
            row = self.conn.execute(
                "SELECT sender_name FROM messages WHERE chat_id = ? AND sender_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
                (chat_id, user_id),
            ).fetchone()
            return row[0] if row else None
        row = self.conn.execute("SELECT nickname FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["nickname"] if row else None

    def _touch_user(self, user_id: str, nickname: str, ts: int) -> None:
        row = self.conn.execute("SELECT nicknames, last_seen FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO users(id, nickname, nicknames, first_seen, last_seen) VALUES(?, ?, ?, ?, ?)",
                (user_id, nickname, json.dumps([nickname], ensure_ascii=False), ts, ts),
            )
            return
        names = json.loads(row["nicknames"])
        if nickname not in names:
            names.append(nickname)
        # An older message arriving late (a replay) doesn't bring back an old nickname.
        if ts >= row["last_seen"]:
            self.conn.execute(
                "UPDATE users SET nickname = ?, nicknames = ?, last_seen = ? WHERE id = ?",
                (nickname, json.dumps(names, ensure_ascii=False), ts, user_id),
            )
        else:
            self.conn.execute(
                "UPDATE users SET nicknames = ? WHERE id = ?", (json.dumps(names, ensure_ascii=False), user_id)
            )
