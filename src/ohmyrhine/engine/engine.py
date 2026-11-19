"""The character runtime: store incoming messages, decide whether to reply, generate."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import Config
from ..pack import CompiledPack, PackError, load_pack, pack_hash
from ..providers import (
    ChatProvider,
    ChatRequest,
    Done,
    Image,
    ReplyWritten,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Tool,
    ToolError,
    ToolUse,
    make_chat_provider,
)
from ..store import (
    INSTRUCTION_SENDER,
    LOOKUP_SENDER,
    ChatKind,
    DiceKind,
    Quote,
    Store,
    StoredMessage,
)
from .judge import Decision, judge
from .lookup import LABELS, LOOKUP_TOOLS, Found, Kind, Lookups
from .media import IncomingMedia, MediaLibrary, attachment_text
from .memory import MEMORY_TOOLS, MemoryKeeper
from .prompt import (
    LONG_MESSAGE_CHARS,
    NO_REPLY,
    build_turns,
    escape_text,
    estimate_tokens,
    is_long,
    live_start,
    message_parts,
    render_instructions,
    render_message,
    render_scenes,
    render_system,
)

log = logging.getLogger(__name__)

_READ_TOOL = "read_message"
_READ_DESCRIPTION = (
    "读一条长消息的原文。聊天记录里很长的消息只显示了开头和系统写的摘要，带 full 和 parts 属性。"
    "需要知道原文的细节时，用 full 的值和段号读其中一段（每段约 800 字，段号从 1 开始）；摘要够用就不必读。"
    "读到的东西就当是你自己看到的，回复时照常说话，不要提起这个工具。"
)
_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "那条消息 full 属性的值"},
        "part": {"type": "integer", "description": "段号，从 1 开始"},
    },
    "required": ["message", "part"],
    "additionalProperties": False,
}

# The history window, in parts of the history budget: the gateway compacts it once it passes
# COMPACT_AT, keeping at least KEEP of the newest messages (Engine.compact); a request that finds
# it over the whole budget cuts it the same way (Engine._cut).
COMPACT_AT, KEEP = 5 / 6, 1 / 4

# One-time codes for system instructions (Engine.issue_code): letters and digits without the
# easily misread 0 O o 1 l I, from the system's secure random source; 56 ** 8 is about 10 ** 14.
CODE_LENGTH = 8
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
# After the code, exactly one of these takes back the chat's newest instruction, or all of them.
REVOKE, REVOKE_ALL = "撤销", "撤销全部"


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# A persistent instance of a character is named "<character>:<tag>": the same pack with a life
# of its own (its own database), whose long-term memory spans all of its sessions.
_TAG = re.compile(r"[\w-]+")
# A tool call written as text marks the end of the visible reply.
_TOOL_MARKUP = re.compile(r"<\s*/?\s*(?:antml:)?(?:function_calls|invoke|parameter)\b")


def _reply_text(raw: str) -> str:
    """What is kept and sent of what she wrote: without NO_REPLY or any tool markup."""
    if m := _TOOL_MARKUP.search(raw):
        log.warning("回复里有写成文字的工具调用，截掉了后面 %d 字", len(raw) - m.start())
        raw = raw[: m.start()]
    return raw.replace(NO_REPLY, "").strip()


def split_ref(ref: str) -> tuple[str, str | None]:
    """"sample:cafe" -> ("sample", "cafe"); a plain character id has no tag."""
    char_id, sep, tag = ref.partition(":")
    return char_id, (tag if sep else None)


def instance_db(cfg: Config, char_id: str, tag: str) -> Path:
    return cfg.data_dir / "instances" / char_id / f"{tag}.db"


def instances(cfg: Config) -> list[tuple[str, str]]:
    """Every persistent instance, as (character id, tag)."""
    return sorted((p.parent.name, p.stem) for p in (cfg.data_dir / "instances").glob("*/*.db"))


def create_instance(cfg: Config, char_id: str, tag: str) -> Engine:
    """Start a persistent instance. Raises ValueError for a bad tag, FileExistsError when it
    exists, PackError when the character's pack is missing or broken."""
    if not _TAG.fullmatch(tag):
        raise ValueError("标签只能用字母、数字、汉字、- 和 _")
    if not (cfg.characters_dir / char_id).is_dir():
        raise PackError(f"找不到角色包 {cfg.characters_dir / char_id}")
    db = instance_db(cfg, char_id, tag)
    if db.exists():
        raise FileExistsError(f"{char_id}:{tag} 已经存在")
    try:
        return Engine(cfg, char_id, instance=tag)
    except PackError:
        for leftover in db.parent.glob(f"{tag}.db*"):
            leftover.unlink()
        raise



@dataclass(frozen=True)
class IncomingMessage:
    chat_id: str
    chat_kind: ChatKind
    sender_id: str
    sender_name: str
    text: str
    ts: int = field(default_factory=lambda: int(time.time() * 1000))
    event_id: str | None = None
    # Set by the connector when the message @-mentions the character.
    mentions_self: bool = False
    # The chat app's id for this message, so later quotes of it can be resolved.
    message_id: str | None = None
    quote: Quote | None = None
    media: tuple[IncomingMedia, ...] = ()
    # From a bot (dice and the like): kept as context, never answered.
    bot: bool = False
    # Dice traffic as marked by the connector; see engine/dice.py.
    dice: DiceKind | None = None
    # A notice that the sender recalled (撤回) a message, the chat app's id of which is
    # `recalled_id` when the connector could tell.
    recall: bool = False
    recalled_id: str | None = None
    backfill: bool = False


class Engine:
    def __init__(
        self,
        cfg: Config,
        char_id: str,
        dice: bool | None = None,
        instance: str | None = None,
        recall_window_s: float = 0.0,
    ):
        # Its own config: the shared one with the character's section over it.
        self.cfg = cfg = cfg.for_character(char_id)
        self.char_id = char_id
        # The tag of a persistent instance (see create_instance); None for the character itself.
        self.instance = instance
        self.ref = char_id if instance is None else f"{char_id}:{instance}"
        # The dice component (engine/dice.py): on where the character's account runs the dice.
        self.dice = cfg.group.dice if dice is None else dice
        # How long people can still recall a message in this chat app. Memory waits that long
        # before reading a message, so what someone takes back is never remembered.
        self.recall_window_ms = int(recall_window_s * 1000)
        self.pack_dir = cfg.characters_dir / char_id
        self.store = Store(cfg.data_dir / f"{char_id}.db" if instance is None else instance_db(cfg, char_id, instance))
        self.pack = self._ensure_built()
        self.system = render_system(self.pack, dice=self.dice)
        self.media = MediaLibrary(self.store, cfg.data_dir, cfg.media.max_edge)
        self._provider: ChatProvider | None = None
        self._side: dict[str, ChatProvider] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._memory_locks: dict[str, asyncio.Lock] = {}
        # Called with the usage of every side call (judging, captions, memory upkeep), for
        # usage displays. The reply's own usage comes with the Done it yields.
        self.on_usage: Callable[[Done], None] | None = None
        # What she hands to the helper agent (engine/lookup.py). on_lookup is called with the
        # chat once what it found is in, which demands her reply.
        self.lookups = Lookups(cfg, self.store, self.pack.character.name, self.tz, self.deliver_lookup,
                               pronoun=self.pack.character.pronoun)
        self.on_lookup: Callable[[str], None] | None = None

    def _used(self, done: Done, task: str, chat_id: str | None, started: float | None = None) -> None:
        """A side call's usage: logged (with its latency when `started`, a time.monotonic(), is
        given), and passed on to on_usage for usage displays."""
        extra = {"ms": round((time.monotonic() - started) * 1000)} if started is not None else {}
        self._log_usage(done, task, chat_id, **extra)
        if self.on_usage is not None:
            self.on_usage(done)

    @property
    def usage_log(self) -> Path:
        return self.cfg.data_dir / "logs" / "usage.jsonl"

    def _log_usage(self, done: Done, task: str, chat_id: str | None, **extra) -> None:
        """One line per API call in data/logs/usage.jsonl, so costs can be looked into later by
        day, character, chat and task (the console only shows totals)."""
        price = self.cfg.price(done.model)
        record = {
            "ts": int(time.time() * 1000),
            "char": self.ref,
            "task": task,
            "chat_id": chat_id,
            "model": done.model,
            "stop": done.stop_reason,
            "usage": done.usage,
            "cost": round(price.cost(done.usage), 6) if price else None,
            **extra,
        }
        try:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            with self.usage_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("写用量日志失败", exc_info=True)

    @property
    def self_id(self) -> str:
        return f"self:{self.ref}"

    @property
    def provider(self) -> ChatProvider:
        # Created lazily so offline commands (prompt preview) need no credentials.
        if self._provider is None:
            self._provider = make_chat_provider(self.cfg, "roleplay")
        return self._provider

    def side_provider(self, task: str) -> ChatProvider | None:
        """Provider for a side task (reply_judge, vision, memory, summary), or None when it isn't configured."""
        if not self.cfg.has_task(task):
            return None
        if task not in self._side:
            self._side[task] = make_chat_provider(self.cfg, task)
        return self._side[task]

    @property
    def memory(self) -> MemoryKeeper:
        return MemoryKeeper(self.store, self.pack.character, spans_chats=self.instance is not None)

    @property
    def summarizing(self) -> bool:
        """Whether a rolling summary is kept (the `summary` task)."""
        return self.cfg.has_task("summary")

    @property
    def tz(self) -> ZoneInfo | None:
        """The character's time zone; None means the server's."""
        return ZoneInfo(tz) if (tz := self.pack.character.timezone) else None

    @property
    def judging(self) -> bool:
        return self.cfg.group.judge and self.cfg.has_task("reply_judge")

    def judges(self, chat_id: str) -> bool:
        """Whether new messages in this chat go to the reply judge: groups, when one is configured."""
        return self.judging and self.store.chat_kind(chat_id) == "group"

    # --- pack -------------------------------------------------------------

    def build(self) -> CompiledPack:
        pack = load_pack(self.pack_dir)
        self.store.save_pack(pack)
        self.pack = pack
        self.system = render_system(pack, dice=self.dice)
        return pack

    def reload(self) -> bool:
        """Pick up pack edits without restarting. Returns whether anything changed.

        On a broken pack, raises PackError and keeps running on the previous build.
        """
        before = self.pack.pack_hash
        self.pack = self._ensure_built()
        self.system = render_system(self.pack, dice=self.dice)
        return self.pack.pack_hash != before

    def _ensure_built(self) -> CompiledPack:
        """Rebuild when the pack on disk changed; fall back to the stored build if the pack is gone."""
        stored = self.store.load_pack()
        if not self.pack_dir.is_dir():
            if stored is None:
                raise PackError(f"找不到角色包 {self.pack_dir}，数据库里也没有已编译的版本")
            return stored
        if stored is not None and stored.pack_hash == pack_hash(self.pack_dir):
            return stored
        pack = load_pack(self.pack_dir)
        self.store.save_pack(pack)
        return pack

    # --- conversation -----------------------------------------------------

    def receive(self, msg: IncomingMessage) -> bool:
        """Record a message. Returns whether the character must reply now.

        In a group, False can still mean "maybe": with a reply judge configured, ask
        `decide()` once the burst of messages has settled.
        """
        with self.store.transaction():
            if self.store.has_event(msg.event_id):
                return False
            must = self._receive(msg)
            if msg.event_id and not self.store.has_event(msg.event_id):
                # A bare code, revocation or self-recall leaves no message to deduplicate.
                self.store.record_event(msg.event_id)
            return must

    def _receive(self, msg: IncomingMessage) -> bool:
        if msg.recall:
            self._recalled(msg)
            return False
        if not msg.backfill and (instruction := self._instruction(msg)) is not None:
            if instruction in (REVOKE, REVOKE_ALL):
                n = self.revoke(msg.chat_id, everything=instruction == REVOKE_ALL)
                log.info("%s：撤销了 %d 条系统指令", self.ref, n)
                return False
            if not instruction:  # the code alone: used up, nothing to pass on
                return False
            stored = self.instruct(msg.chat_id, instruction, msg.chat_kind, msg.ts, msg.event_id)
            if stored is not None:
                log.info("%s：收到系统指令，%d 字", self.ref, len(instruction))
            return stored is not None
        must = not msg.backfill and self.should_reply(msg)
        dice = msg.dice if self.dice else None
        stored = self.store.add_message(
            chat_id=msg.chat_id,
            chat_kind=msg.chat_kind,
            role="user",
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            content=msg.text,
            ts=msg.ts,
            event_id=msg.event_id,
            platform_id=msg.message_id,
            quote=msg.quote,
            # Without the dice component, dice traffic is context only, like a bot's.
            bot=msg.bot or (msg.dice is not None and dice is None),
            dice=dice,
            attachments=tuple(self.media.ingest(m) for m in msg.media),
            # Recorded so a reply owed survives a restart (see pending()).
            trigger=must,
            backfill=msg.backfill,
        )
        if stored is None:  # duplicate event
            return False
        return must

    # --- system instructions ------------------------------------------------

    def issue_code(self, minutes: float = 10) -> tuple[str, int]:
        """A new one-time code for system instructions, valid for `minutes`; any code issued before
        stops working. A chat message that starts with it becomes an instruction (see receive).
        Only its hash is kept. Returns the code and when it expires (unix ms)."""
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
        expires = int(time.time() * 1000 + minutes * 60_000)
        self.store.set_code(_code_hash(code), expires)
        return code, expires

    def _instruction(self, msg: IncomingMessage) -> str | None:
        """What follows the one-time code a message starts with; None when it doesn't start with
        the code issued. The code is used up by it."""
        if msg.bot or msg.dice:
            return None
        text = msg.text.lstrip()
        if len(text) < CODE_LENGTH or not self.store.use_code(_code_hash(text[:CODE_LENGTH]), int(time.time() * 1000)):
            return None
        return text[CODE_LENGTH:].strip()

    def instruct(
        self, chat_id: str, text: str, chat_kind: ChatKind = "private", ts: int | None = None,
        event_id: str | None = None,
    ) -> StoredMessage | None:
        """Add a system instruction to a chat: a fact she takes as given or a rule she keeps, and
        what it asks of her now she does in her next reply, which it demands. It is no one's
        message in the chat. Returns None for a duplicate event."""
        return self.store.add_message(
            chat_id=chat_id,
            chat_kind=chat_kind,
            role="user",
            sender_id=INSTRUCTION_SENDER,
            sender_name="系统指令",
            content=text,
            ts=int(time.time() * 1000) if ts is None else ts,
            event_id=event_id,
            trigger=True,
        )

    def revoke(self, chat_id: str, everything: bool = False) -> int:
        """Take back the chat's newest system instruction (all of them, with `everything`) as if it
        had never been given; what she said meanwhile stays. Returns how many were taken back."""
        given = self.store.instructions(chat_id)
        gone = given if everything else given[-1:]
        self.store.delete_messages([m.id for m in gone])
        return len(gone)

    def _recalled(self, msg: IncomingMessage) -> None:
        """Someone recalled a message. She may have read it already, and the cached history must
        not change, so it stays where it is; a notice quoting it follows as a message of its own,
        and the prompt asks her not to repeat it. Side tasks leave it out (MemoryKeeper.shown).
        What this account recalls (her words, the dice's) is taken back as if never sent."""
        gone = self.store.by_platform_id(msg.chat_id, msg.recalled_id) if msg.recalled_id else None
        if msg.bot or (gone is not None and gone.role == "assistant"):
            if gone is not None:
                self.store.delete_messages([gone.id])
            return
        quote = None
        if gone is not None:
            text = " ".join([gone.content, *(attachment_text(a, None) for a in gone.attachments)]).strip()
            quote = Quote(gone.platform_id, gone.sender_name, text[:200])
        self.store.add_message(
            chat_id=msg.chat_id,
            chat_kind=msg.chat_kind,
            role="user",
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            content="",
            ts=msg.ts,
            event_id=msg.event_id,
            quote=quote,
            recalls=gone.id if gone is not None else 0,
            backfill=msg.backfill,
        )

    def should_reply(self, msg: IncomingMessage) -> bool:
        """Replies that need no judgment: private chats, @-mentions, quotes of the character.
        Dice traffic never demands one; the judge may still hand a result on."""
        if msg.bot or msg.dice:
            return False
        if msg.chat_kind == "private" or msg.mentions_self or self._quotes_self(msg):
            return True
        # Without a judge, being named is the only other trigger.
        return not self.judging and self.pack.character.name in msg.text

    def _quotes_self(self, msg: IncomingMessage) -> bool:
        if msg.quote is None or msg.quote.message_id is None:
            return False
        quoted = self.store.by_platform_id(msg.chat_id, msg.quote.message_id)
        return quoted is not None and quoted.role == "assistant"

    async def describe_media(self, chat_id: str) -> int:
        """Caption this chat's images and stickers that have no description yet. Returns how many."""
        provider = self.side_provider("vision")
        if provider is None:
            return 0

        async def one(rec, att) -> bool:
            started = time.monotonic()
            try:
                text, done = await self.media.describe(provider, rec, att)
            except Exception:  # an API error leaves it undescribed; the next call retries
                log.warning("图片描述失败：%s", rec.sha256, exc_info=True)
                return False
            if done is not None:
                self._used(done, "vision", chat_id, started)
            return text is not None

        done = await asyncio.gather(*(one(r, a) for r, a in self.store.undescribed_media(chat_id)))
        return sum(done)

    async def digest_long(self, chat_id: str) -> int:
        """Write digests for this chat's very long messages that have none yet (the summary task),
        before they are first shown, so how they render never changes. Returns how many."""
        task = "summary"
        provider = self.side_provider(task)
        if provider is None:
            return 0
        done = 0
        for m in self.store.long_undigested(chat_id, 2 * LONG_MESSAGE_CHARS):
            started = time.monotonic()
            try:
                text, usage = await self.memory.digest(provider, m, LONG_MESSAGE_CHARS)
            except Exception:  # an API error leaves it undigested; the next call retries
                log.warning("长消息摘要失败", exc_info=True)
                continue
            self._used(usage, f"{task}:digest", chat_id, started)
            # "" for a refusal, so it isn't tried again; it then shows its start only.
            self.store.set_digest(m.id, text or "")
            done += 1
        return done

    def _while_kept(self, tool: Tool, chat_id: str, reply_id: int) -> Tool:
        """An after-reply tool (a note) that does nothing once the reply it follows is gone: a
        reply dropped unsent takes its notes with it."""

        async def run(args: dict) -> str:
            if self.store.message(chat_id, reply_id) is None:
                raise ToolError("这条回复没有发出去，不记了")
            return await tool.run(args)

        return replace(tool, run=run)

    def _reader(self, chat_id: str) -> Tool:
        """The tool for reading a long message of this chat in parts."""

        async def read(args: dict) -> str:
            ref, part = str(args.get("message", "")), args.get("part")
            m = self.store.message(chat_id, ref) if ref.isascii() and ref.isdecimal() else None
            if (m is None or m.role != "user" or m.sender_id == INSTRUCTION_SENDER or not is_long(m.content)
                    or m.id in self.store.recalled(chat_id, [m])):
                raise ToolError(f"没有 full 为 {ref} 的长消息")
            parts = message_parts(m.content)
            if not isinstance(part, int) or not 1 <= part <= parts:
                raise ToolError(f"这条消息分 {parts} 段，段号要在 1 到 {parts} 之间")
            text = m.content[(part - 1) * LONG_MESSAGE_CHARS : part * LONG_MESSAGE_CHARS]
            return f"{m.sender_name or '群友'} 的长消息，第 {part}/{parts} 段：\n{text}"

        return Tool(_READ_TOOL, _READ_DESCRIPTION, _READ_SCHEMA, read)

    def deliver_lookup(self, chat_id: str, kind: Kind, query: str, found: Found) -> None:
        """What the helper agent found: added to the chat as a message only she sees, which
        demands her reply."""
        self.store.add_message(
            chat_id=chat_id, chat_kind=self.store.chat_kind(chat_id) or "private", role="user",
            sender_id=LOOKUP_SENDER, sender_name=f"{LABELS[kind]}：{query}"[:200], content=found.text,
            ts=int(time.time() * 1000), trigger=True,
        )
        # On the subscription: what the API would have charged goes in api_cost, not cost.
        self._log_usage(
            Done("end" if found.ok else "error", found.usage, found.model),
            f"lookup:{kind.removeprefix('agent_')}", chat_id, cost=0.0, api_cost=found.api_cost, ms=found.ms,
            turns=found.turns,
        )
        if self.on_lookup is not None:
            self.on_lookup(chat_id)

    def mark_seen(self, chat_id: str) -> None:
        """Treat everything in the chat so far as already considered, e.g. history replayed
        by the connector when the character joins a group: context, but nothing to answer."""
        latest = self.store.latest_message_id(chat_id)
        if latest is not None:
            self.store.mark_handled(chat_id, latest)

    async def decide(self, chat_id: str, owed: bool = False, *, settle: bool = True) -> Decision:
        """Ask the reply judge whether a group's new messages should go to the character, and
        how long to wait for more. `owed`: only the wait and hush can affect the handoff.
        With settle=False the caller commits the verdict alongside its timer or speech lock."""
        provider = self.side_provider("reply_judge")
        if provider is None or not self.cfg.group.judge:
            return Decision(False, False, "没有配置 reply_judge")
        pending = self.store.pending(chat_id)
        if pending is None:
            return Decision(False, False, "没有新的消息")
        await self.describe_media(chat_id)
        await self.digest_long(chat_id)
        if self.store.pending(chat_id) != pending:
            return Decision(False, False, "有新的消息")
        # Instructions go to her whatever the judge says (they demand a reply), so it never sees them.
        history = self.store.judge_history(chat_id, pending.latest_id, self.cfg.group.judge_context)
        if not history:
            return Decision(False, False, "没有新的消息")
        started = time.monotonic()
        decision, done = await judge(
            provider, self.pack.character, history, self.descriptions(history), self.store.judged_upto(chat_id),
            dice=self.dice,
        )
        self._used(done, "reply_judge", chat_id, started)
        if owed and not decision.reply and not decision.hush:
            decision = replace(decision, reply=True, reason=f"{decision.reason}（必回）")
        # Not handing them on settles these messages now. Handed on, they are settled by the
        # reply (it lands after them), so a restart before it goes out retries them; "wait"
        # leaves them new, to be judged again with what follows.
        if settle and not decision.reply and not decision.wait and self.store.pending(chat_id) == pending:
            self.store.mark_handled(chat_id, pending.latest_id)
        return decision

    def descriptions(self, history: list[StoredMessage]) -> dict[str, str | None]:
        shas = [a.sha256 for m in history for a in m.attachments if a.sha256]
        return {sha: rec.description for sha, rec in self.store.media_map(shas).items()}

    def _live_images(self, history: list[StoredMessage]) -> dict[str, Image]:
        """Images in the latest messages of the turn being answered (see live_start), newest
        first, up to the configured count."""
        shas = [a.sha256 for m in history[live_start(history) :] for a in m.attachments if a.sha256]
        picked = list(dict.fromkeys(reversed(shas)))[: self.cfg.media.live_images]
        live = {}
        for sha, rec in self.store.media_map(picked).items():
            if (image := self.media.load(rec)) is not None:
                live[sha] = image
        return live

    def undo(self, chat_id: str, n: int = 1) -> list[StoredMessage]:
        """Delete the last n messages of a chat; returns them oldest first."""
        removed = self.store.recent_messages(chat_id, n)
        self.store.delete_messages([m.id for m in removed])
        return removed

    def rewind_points(self, chat_id: str, limit: int = 10) -> list[StoredMessage]:
        """Messages from people that the chat can be rewound to, newest first."""
        return self.store.recent_user_messages(chat_id, limit)

    def rewind(self, chat_id: str, message_id: int) -> int:
        """Rewind the chat to just before a message: it and everything after it are deleted."""
        return self.store.delete_from(chat_id, message_id)

    def _tokens(self, m: StoredMessage) -> int:
        return estimate_tokens(render_message(m, clock=True))

    def _newest_within(self, messages: list[StoredMessage], tokens: int) -> list[StoredMessage]:
        """The newest of these messages that fit in `tokens` (at least the newest one)."""
        total, start = 0, len(messages)
        while start > 0 and total + (size := self._tokens(messages[start - 1])) <= tokens:
            start -= 1
            total += size
        return messages[min(start, len(messages) - 1) :]

    def _newest_from(self, messages: list[StoredMessage], tokens: int) -> int:
        """Where to cut these messages to keep at least `tokens` of the newest ones (0 when they
        come to less): never on the character's own message, as a window never starts on one."""
        total, start = 0, len(messages)
        while start > 0 and total < tokens:
            start -= 1
            total += self._tokens(messages[start])
        while start > 0 and messages[start].role == "assistant":
            start -= 1
        return start

    def _cut(self, chat_id: str, over: int | None = None) -> list[StoredMessage]:
        """The messages from the window's cut on.

        Once they come to more than `over` estimated tokens (the history budget), the cut moves
        forward in one step, keeping at least KEEP of the budget's worth of the newest messages;
        what it passes goes to the rolling summary. In between it stays put, so the cached
        history prefix holds for many turns.
        """
        over = self.cfg.history_tokens if over is None else over
        messages = self.store.messages_between(chat_id, self.store.window_from(chat_id) - 1)
        if sum(self._tokens(m) for m in messages) <= over:
            return messages
        start = self._newest_from(messages, max(1, int(self.cfg.history_tokens * KEEP)))
        self.store.set_window_from(chat_id, messages[start].id)
        return messages[start:]

    def window(self, chat_id: str) -> list[StoredMessage]:
        """The messages currently sent to the model for this chat.

        It is cut back in chunks (see _cut), and what falls before the cut goes to the rolling
        summary. The summary is written after replies, never while one waits, so until it has
        caught up, the messages it doesn't cover yet stay in (one budget's worth at most).
        The window never starts on the character's own message: a conversation sent to the
        model opens with someone else's turn.
        """
        messages = self._cut(chat_id)
        if messages and self.summarizing:
            _, upto = self.store.summary(chat_id)
            lagging = self.store.messages_between(chat_id, upto, messages[0].id - 1)
            if lagging and sum(self._tokens(m) for m in lagging) <= self.cfg.history_tokens:
                messages = lagging + messages
        start = next((i for i, m in enumerate(messages) if m.role != "assistant"), len(messages))
        return messages[start:]

    async def compact(self, chat_id: str) -> bool:
        """Compact the window once it passes COMPACT_AT of the history budget, like an automatic
        /compact: cut it to the newest messages making up at least KEEP of the budget and fold the
        rest into the rolling summary. For the gateway, after each turn and in the background, so
        no reply waits for it. Returns whether anything was cut."""
        if not self.summarizing:
            return False
        before = self.store.window_from(chat_id)
        self._cut(chat_id, over=int(self.cfg.history_tokens * COMPACT_AT))
        if self.store.window_from(chat_id) == before:
            return False
        log.info("%s：历史超过 %d token，压缩到最近一段并写进摘要", self.ref, int(self.cfg.history_tokens * COMPACT_AT))
        await self.update_summary(chat_id)
        return True

    @property
    def thinking_log(self) -> Path:
        return self.cfg.data_dir / "logs" / f"{self.ref}-thinking.jsonl"

    def request(self, chat_id: str, show_thinking: bool = False) -> ChatRequest:
        history = self.window(chat_id)
        _, notes = self.memory.notes(chat_id, history[0].id if history else None, self.tz)
        turns = build_turns(
            history,
            clock=self.pack.character.perceives_clock,
            descriptions=self.descriptions(history),
            live=self._live_images(history),
            tz=self.tz,
            notes=notes,
        )
        context = "\n\n".join(text for _, text in self.chat_context(chat_id, history)) or None
        return ChatRequest(
            system=self.system, turns=turns, show_thinking=show_thinking,
            chat_context=context, recall=self.recall(chat_id),
            tools=(self._reader(chat_id), *self.memory.tools(chat_id, history), *self.lookups.tools(chat_id)),
            long_lived_system=True,
        )

    def chat_context(self, chat_id: str, history: list[StoredMessage] | None = None) -> list[tuple[str, str]]:
        """Per-chat blocks sent after the system prompt, as (label, text): scenes, the system
        instructions from before the history window (`history`, the window by default), the
        summary of earlier chat, then her notes as they stood when the window starts. They change
        rarely (all but the first only when the window is cut), so they go before the cached
        history."""
        history = self.window(chat_id) if history is None else history
        blocks = []
        if scenes := render_scenes(self.pack.character.name, self.pack.character.pronoun, self.store.scenes(chat_id)):
            blocks.append(("情景", scenes))
        earlier = self.store.instructions_before(chat_id, history[0].id) if history else []
        if instructions := render_instructions(earlier, self.pack.character.perceives_clock, self.tz):
            blocks.append(("更早的系统指令", instructions))
        if summary := self.memory.render_summary(chat_id):
            blocks.append(("更早的聊天", summary))
        notes, _ = self.memory.notes(chat_id, history[0].id if history else None, self.tz)
        if notes:
            blocks.append(("笔记", notes))
        return blocks

    def recall(self, chat_id: str) -> str | None:
        """What goes after the history for this turn: a reminder when her notes run long."""
        return self.memory.reminder(chat_id, self.cfg.memory.notes_chars)

    # --- long-term memory -------------------------------------------------

    def _settled(self, messages: list[StoredMessage]) -> list[StoredMessage]:
        if not self.recall_window_ms:
            return messages
        cutoff = time.time() * 1000 - self.recall_window_ms
        return messages[:next((i for i, m in enumerate(messages) if m.ts > cutoff), len(messages))]

    async def update_summary(self, chat_id: str) -> bool:
        """Fold the messages before the window's cut into the chat's rolling summary. Run it
        after replies: it can take a while, and the window waits for it (see window())."""
        task = "summary"
        provider = self.side_provider(task)
        if provider is None:
            return False
        async with self._memory_locks.setdefault(chat_id, asyncio.Lock()):
            kept = self._cut(chat_id)
            if not kept:
                return False
            previous, upto = self.store.summary(chat_id)
            dropped = self._settled(self.store.messages_between(chat_id, upto, kept[0].id - 1))
            if not dropped:
                return False
            # A long backlog (memory switched on for an old chat): only the latest part.
            dropped = self._newest_within(dropped, self.cfg.history_tokens)
            started = time.monotonic()
            text, done = await self.memory.summarize(
                provider, previous, dropped, self.descriptions(dropped), self.cfg.memory.summary_chars, self.tz
            )
            self._used(done, f"{task}:summary", chat_id, started)
            # A refused or empty summary keeps the old one, but these messages aren't tried again.
            self.store.set_summary(chat_id, text or previous, dropped[-1].id)
            return text is not None

    async def reply(
        self, chat_id: str, show_thinking: bool | None = None, *, action_id: str | None = None,
        unsent: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Generate a reply for the chat and store it.

        The reply takes its place in the chat when generation starts, so messages that arrive
        meanwhile come after it and are answered next. A refused or empty reply is not kept,
        so it never enters later context. The character may also choose not to answer (the
        model writes only NO_REPLY): then no text is streamed, nothing is kept, the messages
        she saw count as handled, and the final Done has stop_reason "silent".
        `unsent`: what she had yet to send of a reply in several messages when someone wrote
        in between; shown after the history so she can rewrite it for the new situation.
        With thinking on (config `log_thinking` unless overridden), the model's reasoning is
        streamed as ThinkingDelta events and appended to the thinking log; it never enters
        the stored history.
        When she takes notes after her reply, ReplyWritten comes as soon as the reply is
        stored: the caller can send it while the notes are written. Notes are only kept while
        the reply is: a reply the caller deletes unsent (rewritten, say) takes none.
        """
        if show_thinking is None:
            show_thinking = self.cfg.log_thinking
        await self.describe_media(chat_id)
        await self.digest_long(chat_id)
        async with self._locks.setdefault(chat_id, asyncio.Lock()):
            req = self.request(chat_id, show_thinking=show_thinking)
            if unsent:
                note = f"<unsent>{escape_text(unsent)}</unsent>"
                req = replace(req, recall=f"{req.recall}\n\n{note}" if req.recall else note)
            # Nothing new to respond to (APIs also reject a conversation ending on our turn).
            if not req.turns or req.turns[-1].role == "assistant":
                return
            seen = self.store.latest_message_id(chat_id)
            placeholder = self.store.add_message(
                chat_id=chat_id,
                role="assistant",
                sender_id=self.self_id,
                sender_name=self.pack.character.name,
                content="",
                ts=int(time.time() * 1000),
                action_id=action_id,
            )
            assert placeholder is not None  # no event_id, so never a duplicate
            req = replace(req, tools=tuple(self._while_kept(t, chat_id, placeholder.id) if t.after_reply else t for t in req.tools))
            kept = written = False
            try:
                parts: list[str] = []
                thoughts: list[str] = []
                # Text is held back while it could still turn out to be NO_REPLY.
                holding = True
                started, reads, notes, lookups = time.monotonic(), 0, 0, 0
                async with contextlib.aclosing(self.provider.stream(req)) as events:
                    async for event in events:
                        if isinstance(event, ReplyWritten):
                            text = _reply_text("".join(parts))
                            if not text:
                                continue  # nothing to send: a silence or nothing at all, settled at Done
                            if holding:
                                yield TextDelta(text)
                                holding = False
                            self.store.set_content(placeholder.id, text)
                            kept = written = True
                        elif written and isinstance(event, TextDelta):
                            continue  # the reply is out; nothing is added to it
                        elif isinstance(event, TextDelta):
                            parts.append(event.text)
                            if holding:
                                so_far = "".join(parts).lstrip()
                                if NO_REPLY.startswith(so_far):
                                    continue
                                holding = False
                                event = TextDelta(so_far.replace(NO_REPLY, "").lstrip())
                        elif isinstance(event, ThinkingDelta):
                            thoughts.append(event.text)
                        elif isinstance(event, ToolUse) and event.name in (*MEMORY_TOOLS, *LOOKUP_TOOLS):
                            # A note or a lookup comes after the reply ("let me look"), which stands.
                            if event.name in MEMORY_TOOLS:
                                notes += 1
                            else:
                                lookups += 1
                            log.info("%s：%s %s", self.ref, event.name, event.input)
                        elif isinstance(event, ToolUse):
                            # What she wrote before using a tool ("let me check") is not the reply.
                            parts.clear()
                            holding = True
                            reads += 1
                            log.info("%s：用了 %s %s", self.ref, event.name, event.input)
                        elif isinstance(event, Done):
                            raw = "".join(parts).strip()
                            text = _reply_text(raw)
                            if show_thinking:
                                try:
                                    self._log_thinking(chat_id, req, "".join(thoughts).strip(), raw, event)
                                except OSError:
                                    log.warning("写思考日志失败", exc_info=True)
                            if event.stop_reason == "refusal" or written:
                                pass
                            elif text:
                                if holding:  # short replies can end while still held back
                                    yield TextDelta(text)
                                self.store.set_content(placeholder.id, text)
                                kept = True
                            elif NO_REPLY in raw:
                                # She saw these messages and chose to let them pass.
                                if seen is not None:
                                    self.store.mark_handled(chat_id, seen)
                                event = Done("silent", event.usage, event.model)
                            self._log_usage(
                                event, "roleplay", chat_id, ms=round((time.monotonic() - started) * 1000), tool_calls=reads,
                                notes=notes, lookups=lookups,
                            )
                        yield event
            finally:
                if not kept:
                    self.store.delete_messages([placeholder.id])

    def _log_thinking(self, chat_id: str, req: ChatRequest, thinking: str, reply: str, done: Done) -> None:
        record = {
            "ts": int(time.time() * 1000),
            "chat_id": chat_id,
            "model": done.model,
            "stop_reason": done.stop_reason,
            # The turn being answered: the latest message(s) from people, as the model saw them.
            "prompt": req.turns[-1].text,
            "thinking": thinking,
            "reply": reply,
        }
        self.thinking_log.parent.mkdir(parents=True, exist_ok=True)
        with self.thinking_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
