"""Conversations on this machine, with you in them: what `rhine chat` and `rhine web` show.

A session is one conversation, sized by who is in it: with just you and the character she gets
every line; with anyone more, each character decides as in a group. Each character keeps its
own copy of the session: the first under the session id, an invited one under
`<session>@<first character>`, so it never mixes with her own chats. A message has the same
time, text and message id in every copy, so quotes and recalls find it in each.

Everything that happens while characters answer comes out as events (Judged, Speaking,
Streamed, Looking, Said, Failed) for the interface to show; nothing here prints.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace

from .engine import Engine, IncomingMessage, create_instance, instance_db, split_ref
from .engine.judge import Decision
from .engine.media import IncomingMedia, attachment_text
from .pack import PackError
from .providers import Done, StreamEvent
from .store import SYSTEM_SENDER, ChatKind, Quote, StoredMessage

SESSION_PREFIX = "cli:"
CHAR_PREFIX = "cli:c:"
USER_PREFIX = "cli:u:"
# How many replies characters' own messages may set off per message you send, so two
# characters naming each other don't talk forever.
CHAIN_LIMIT = 3


class ChatError(Exception):
    """Something you asked for that cannot be done, said so you can fix it."""


@dataclass(frozen=True)
class Judged:
    """A character's judge looked at what came in (groups only)."""

    engine: Engine
    decision: Decision


@dataclass(frozen=True)
class Speaking:
    engine: Engine


@dataclass(frozen=True)
class Streamed:
    """Part of a reply as it comes: TextDelta, ThinkingDelta, ToolUse or Done."""

    engine: Engine
    event: StreamEvent


@dataclass(frozen=True)
class Looking:
    """She is waiting for a lookup to come back, and answers once it is in."""

    engine: Engine


@dataclass(frozen=True)
class Said:
    """Her reply as stored, or None when she said nothing."""

    engine: Engine
    message: StoredMessage | None


@dataclass(frozen=True)
class Failed:
    engine: Engine | None
    what: str
    error: BaseException


Event = Judged | Speaking | Streamed | Looking | Said | Failed


@dataclass
class Usage:
    """Model calls made during this run, for /stats."""

    calls: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    # Models with no price; their calls are left out of `cost`.
    unpriced: set[str] = field(default_factory=set)
    last: Done | None = None


def name_of(engine: Engine) -> str:
    return engine.pack.character.name


def char_sender(engine: Engine) -> str:
    """How a character appears to the others in the conversation."""
    return f"{CHAR_PREFIX}{engine.ref}"


def new_message_id() -> str:
    return f"m:{secrets.token_hex(6)}"


class LocalChat:
    def __init__(self, engine: Engine, user: str = "me", nickname: str | None = None, session: str | None = None,
                 on_usage: Callable[[Done], None] | None = None):
        self.engine = engine
        # Characters invited into the session, by ref ("<character>" or "<character>:<tag>").
        self.guests: dict[str, Engine] = {}
        self.user = user
        self.nickname = nickname or user
        self.usage = Usage()
        self._on_usage = on_usage
        engine.on_usage = self._used
        if session:
            self.chat_id = f"{SESSION_PREFIX}{session}"
        else:
            sessions = self.sessions()
            self.chat_id = sessions[0][0] if sessions else self.new_session_id()
        # What kept invited characters from coming back, for the interface to say.
        self.problems = self.load_cast()

    # --- who is here --------------------------------------------------------------------

    @property
    def name(self) -> str:
        return name_of(self.engine)

    @property
    def sender_id(self) -> str:
        return f"{USER_PREFIX}{self.user}"

    @property
    def cast(self) -> list[Engine]:
        """Every character in the conversation, the one you started with first."""
        return [self.engine, *self.guests.values()]

    @property
    def kind(self) -> ChatKind:
        return "group" if self.guests or len(self.people() | {self.sender_id}) > 1 else "private"

    def people(self) -> set[str]:
        """The people (not characters) who have spoken in this session."""
        return {s for s in self.engine.store.senders(self.chat_id) if not s.startswith(CHAR_PREFIX)}

    def chat_of(self, engine: Engine) -> str:
        """The id a character keeps this session under."""
        return self.chat_id if engine is self.engine else f"{self.chat_id}@{self.engine.ref}"

    def speak_as(self, user: str, nickname: str | None = None) -> None:
        """Speak as someone else from now on (a new person makes the session a group)."""
        self.user, self.nickname = user, nickname or user

    def speaker_of(self, m: StoredMessage) -> Engine | None:
        """The character in the conversation who sent a message (from the first character's copy)."""
        if m.role == "assistant":
            return self.engine
        return next((e for e in self.guests.values() if m.sender_id == char_sender(e)), None)

    def character(self, ref: str) -> Engine | None:
        """A character in the conversation by its ref or its character's id."""
        return next((e for e in self.cast if ref in (e.ref, e.char_id)), None)

    def label(self, chat_id: str | None = None) -> str:
        """Who is in a session: the first character, then everyone else who has spoken."""
        chat_id = chat_id or self.chat_id
        names = [self.name, *self.engine.store.senders(chat_id).values()]
        if chat_id == self.chat_id:
            names += [name_of(e) for e in self.guests.values()] + [self.nickname]
        return "、".join(dict.fromkeys(names))

    # --- sessions -----------------------------------------------------------------------

    def sessions(self) -> list[tuple[str, int]]:
        """This character's sessions (not its copies of other characters' ones), newest first."""
        return [(c, ts) for c, ts in self.engine.store.chats_by_activity(SESSION_PREFIX) if "@" not in c]

    def new_session_id(self) -> str:
        base = f"{SESSION_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}"
        taken = {c for c, _ in self.sessions()}
        chat_id, n = base, 1
        while chat_id in taken:
            n += 1
            chat_id = f"{base}-{n}"
        return chat_id

    def clear(self) -> list[str]:
        """Start a new session; returns who stays behind in the old one."""
        left = [name_of(e) for e in self.guests.values()]
        self.guests.clear()
        self.chat_id = self.new_session_id()
        return left

    def resume(self, chat_id: str) -> list[str]:
        """Go back to an earlier session; returns notices for the interface."""
        if chat_id not in {c for c, _ in self.sessions()}:
            raise ChatError(f"没有会话 {chat_id}")
        notices = []
        if self.guests:
            notices.append(f"{'、'.join(name_of(e) for e in self.guests.values())}留在原来的对话里，回到那里时会一起回来")
            self.guests.clear()
        self.chat_id = chat_id
        people = self.people()
        if self.sender_id not in people and len(people) == 1:
            # Someone else's two-person conversation: carry it on as them.
            [sender] = people
            self.user = sender.removeprefix(USER_PREFIX)
            self.nickname = self.engine.store.nickname(sender) or self.user
            notices.append(f"这是 {self.nickname} 和{self.name}的对话，现在以 {self.nickname}（ID {self.sender_id}）发言")
        notices += self.load_cast()
        return notices

    # --- characters joining and leaving -------------------------------------------------

    def missing(self, ref: str) -> str | None:
        """Why a character or instance can't be brought in, or None when it can."""
        cfg = self.engine.cfg
        char_id, tag = split_ref(ref)
        if tag is not None:
            if not instance_db(cfg, char_id, tag).is_file():
                return f"没有实例 {ref}，用 /instance {ref} 创建"
        elif not (cfg.characters_dir / char_id).is_dir() and not (cfg.data_dir / f"{char_id}.db").is_file():
            return f"找不到角色 {char_id}，rhine list 查看所有角色"
        return None

    def _join(self, ref: str) -> Engine:
        char_id, tag = split_ref(ref)
        guest = Engine(self.engine.cfg, char_id, instance=tag)
        guest.on_usage = self._used
        self.guests[ref] = guest
        return guest

    def load_cast(self) -> list[str]:
        """Bring back the characters invited into this session earlier; returns what kept any away."""
        problems = []
        for ref in self.engine.store.guests(self.chat_id):
            if (problem := self.missing(ref)) is not None:
                problems.append(f"{ref} 没能回到对话里：{problem}")
                continue
            try:
                self._join(ref)
            except PackError as e:
                problems.append(f"{ref} 的角色包有问题，没能回到对话里：\n{e}")
        return problems

    def _save_cast(self) -> None:
        self.engine.store.set_guests(self.chat_id, list(self.guests))

    def invite(self, ref: str) -> Engine:
        """Bring a character into the conversation; `announce` then tells everyone."""
        char_id, _ = split_ref(ref)
        if (there := next((e for e in self.cast if e.char_id == char_id), None)) is not None:
            # Two of the same character would share a name and talk past each other.
            raise ChatError(f"{name_of(there)}（{there.ref}）已经在对话里了，同一个角色只能有一个")
        if (problem := self.missing(ref)) is not None:
            raise ChatError(problem)
        try:
            guest = self._join(ref)
        except PackError as e:
            raise ChatError(f"{ref} 的角色包有问题：\n{e}") from None
        self._save_cast()
        return guest

    def kick(self, ref: str) -> Engine:
        """Take an invited character out of the conversation; `announce` then tells everyone."""
        guest = next((e for e in self.guests.values() if ref in (e.ref, e.char_id)), None)
        if guest is None:
            raise ChatError(f"{ref} 不在拉进来的角色里")
        del self.guests[guest.ref]
        self._save_cast()
        return guest

    async def announce(self, guest: Engine, joined: bool) -> AsyncIterator[Event]:
        """The chat app's notice that a character joined or left, which the others may answer."""
        text = (f"「{self.nickname}」邀请「{name_of(guest)}」加入了群聊" if joined
                else f"「{self.nickname}」将「{name_of(guest)}」移出了群聊")
        async for event in self._post(self._system(text)):
            yield event

    def new_instance(self, ref: str) -> Engine:
        char_id, tag = split_ref(ref)
        if not tag:
            raise ChatError("实例要写成 <角色>:<标签>，比如 sample:cafe")
        try:
            return create_instance(self.engine.cfg, char_id, tag)
        except (ValueError, FileExistsError, PackError) as e:
            raise ChatError(str(e)) from None

    # --- saying things ------------------------------------------------------------------

    def messages(self, limit: int | None = None) -> list[StoredMessage]:
        """The session from the first character's copy: its last `limit` messages, or what she
        is shown now."""
        store = self.engine.store
        return store.recent_messages(self.chat_id, limit) if limit else self.engine.window(self.chat_id)

    def message(self, message_id: int) -> StoredMessage:
        m = self.engine.store.message(self.chat_id, message_id)
        if m is None:
            raise ChatError(f"本会话没有编号为 {message_id} 的消息")
        return m

    def text_of(self, m: StoredMessage) -> str:
        """A message as its quote shows it."""
        return " ".join([m.content, *(attachment_text(a, None) for a in m.attachments)]).strip()

    def _platform_id(self, m: StoredMessage) -> str:
        """The message id every copy knows a message by (given to older messages when needed)."""
        if m.platform_id:
            return m.platform_id
        pid = new_message_id()
        self.engine.store.set_platform_id(m.id, pid)
        return pid

    async def say(self, text: str, media: tuple[IncomingMedia, ...] = (),
                  quote: StoredMessage | None = None) -> AsyncIterator[Event]:
        """Post a message as you, let the characters react, then let the summaries catch up."""
        q = None
        if quote is not None:
            speaker = self.speaker_of(quote)
            who = name_of(speaker) if speaker is not None else quote.sender_name
            q = Quote(self._platform_id(quote), who, self.text_of(quote)[:200])
        msg = IncomingMessage(self.chat_id, self.kind, self.sender_id, self.nickname, text, media=media,
                              message_id=new_message_id(), quote=q)
        async for event in self._post(msg):
            yield event

    def recallable(self, m: StoredMessage | None = None) -> StoredMessage:
        """The message `recall` would take back: this one, or your latest."""
        store = self.engine.store
        if m is None:
            recent = store.recent_user_messages(self.chat_id, 50)
            gone = store.recalled(self.chat_id, recent)
            mine = [x for x in recent if x.sender_id == self.sender_id and x.recalls is None and x.id not in gone]
            if not mine:
                raise ChatError("你在本会话里还没有可以撤回的消息")
            m = mine[0]  # newest first
        if m.sender_id != self.sender_id:
            raise ChatError("只能撤回自己发的消息")
        if m.recalls is not None or m.id in store.recalled(self.chat_id, [m]):
            raise ChatError("这条消息已经撤回了")
        return m

    async def recall(self, m: StoredMessage | None = None) -> AsyncIterator[Event]:
        """Take back one of your messages (your latest by default). Characters may already have
        read it: like in a chat app, they see that it was recalled."""
        m = self.recallable(m)
        msg = IncomingMessage(self.chat_id, self.kind, self.sender_id, self.nickname, "",
                              recall=True, recalled_id=self._platform_id(m))
        async for event in self._post(msg):
            yield event

    async def pat(self, target: Engine | None = None) -> AsyncIterator[Event]:
        """拍一拍 a character (the one you started with by default)."""
        target = target or self.engine
        async for event in self._post(self._system(f"「{self.nickname}」拍了拍「{name_of(target)}」")):
            yield event

    async def instruct(self, text: str) -> AsyncIterator[Event]:
        """A system instruction to the character you started with: she acts on it, never mentions it."""
        self.engine.instruct(self.chat_id, text, self.kind)
        async for event in self._speak([self.engine]):
            yield event
        async for event in self._summarize():
            yield event

    async def nudge(self) -> AsyncIterator[Event]:
        """Let the character you started with speak, with nothing new said."""
        async for event in self._speak([self.engine]):
            yield event

    async def retry(self) -> AsyncIterator[Event]:
        """Say the last character's message again; after someone else's message, the character
        you started with answers."""
        last = self.engine.store.recent_messages(self.chat_id, 1)
        if not last:
            raise ChatError("本会话还没有消息")
        speaker = self.speaker_of(last[0])
        if speaker is not None:
            self.undo(1)
        engine = speaker or self.engine
        said: StoredMessage | None = None
        async for event in self._reply(engine):
            if isinstance(event, Said):
                said = event.message
            yield event
        if said is not None and self.guests:
            await self._hear(self._as_message(engine, said), speaker=engine, react=False)

    def undo(self, n: int = 1) -> list[StoredMessage]:
        """Delete the last n messages; returns them from the first character's copy."""
        removed = self.engine.undo(self.chat_id, n)
        self._drop_copies(removed)
        return removed

    def rewind_points(self, limit: int = 10) -> list[StoredMessage]:
        return self.engine.rewind_points(self.chat_id, limit)

    def rewind(self, target: StoredMessage) -> list[StoredMessage]:
        """Go back to just before a message; returns what was deleted."""
        removed = self.engine.store.messages_between(self.chat_id, target.id - 1)
        self.engine.rewind(self.chat_id, target.id)
        self._drop_copies(removed)
        return removed

    def _drop_copies(self, removed: list[StoredMessage]) -> None:
        """Delete these messages (from the first character's copy) from the other copies, found
        by time and text: a character who joined later or left meanwhile just has fewer."""
        if not removed:
            return
        keys = {(m.ts, m.content) for m in removed}
        since = min(m.ts for m in removed)
        for guest in self.guests.values():
            copies = guest.store.messages_since(self.chat_of(guest), since)
            guest.store.delete_messages([m.id for m in copies if (m.ts, m.content) in keys])

    # --- the conversation ---------------------------------------------------------------

    def _system(self, text: str) -> IncomingMessage:
        return IncomingMessage(self.chat_id, self.kind, SYSTEM_SENDER, "系统", text)

    def _as_message(self, engine: Engine, said: StoredMessage) -> IncomingMessage:
        """A character's reply as the others receive it: same time and id, so all copies line up."""
        return IncomingMessage(self.chat_id, "group", char_sender(engine), name_of(engine), said.content, ts=said.ts,
                               message_id=said.platform_id)

    async def _post(self, msg: IncomingMessage) -> AsyncIterator[Event]:
        due, events = await self._hear(msg)
        self._save_cast()  # the session's record exists once something is said in it
        for event in events:
            yield event
        async for event in self._speak(due):
            yield event
        async for event in self._summarize():
            yield event

    async def _hear(self, msg: IncomingMessage, speaker: Engine | None = None,
                    react: bool = True) -> tuple[list[Engine], list[Event]]:
        """Record a message with every character but its speaker. Returns who should answer it,
        and what their judges said. With react False it is only recorded, as already considered."""
        due: list[Engine] = []
        maybe: list[Engine] = []
        for engine in self.cast:
            if engine is speaker:
                continue
            chat_id = self.chat_of(engine)
            must = engine.receive(replace(msg, chat_id=chat_id, mentions_self=f"@{name_of(engine)}" in msg.text))
            if not react:
                engine.mark_seen(chat_id)
            elif must:
                due.append(engine)
            elif engine.judging:
                maybe.append(engine)
        events: list[Event] = []
        decisions = await asyncio.gather(*(e.decide(self.chat_of(e)) for e in maybe), return_exceptions=True)
        for engine, decision in zip(maybe, decisions, strict=True):
            if isinstance(decision, BaseException):
                events.append(Failed(engine, "judge", decision))
                continue
            events.append(Judged(engine, decision))
            if decision.reply:
                due.append(engine)
        return due, events

    async def _speak(self, due: list[Engine]) -> AsyncIterator[Event]:
        """Let these characters answer in turn. What each says goes on to the others, who may
        answer it too, up to CHAIN_LIMIT such replies."""
        chained = 0
        due = list(due)
        while due:
            engine = due.pop(0)
            said: StoredMessage | None = None
            async for event in self._reply(engine):
                if isinstance(event, Said):
                    said = event.message
                yield event
            if said is None or not self.guests:
                continue
            follow, events = await self._hear(self._as_message(engine, said), speaker=engine,
                                              react=chained < CHAIN_LIMIT)
            for event in events:
                yield event
            follow = [e for e in follow if e not in due]
            for dropped in follow[CHAIN_LIMIT - chained:]:
                dropped.mark_seen(self.chat_of(dropped))
            follow = follow[: CHAIN_LIMIT - chained]
            chained += len(follow)
            due += follow

    async def _reply(self, engine: Engine) -> AsyncIterator[Event]:
        """One character's reply, as events; Said comes last (also when she said nothing)."""
        chat_id = self.chat_of(engine)
        before = engine.store.latest_message_id(chat_id) or 0
        yield Speaking(engine)
        try:
            async for event in engine.reply(chat_id):
                if isinstance(event, Done):
                    self._used(event)
                yield Streamed(engine, event)
        except Exception as e:  # a provider or network error ends this reply, not the session
            yield Failed(engine, "reply", e)
            yield Said(engine, None)
            return
        if engine.lookups.running(chat_id):
            # Like the gateway: she answers once what she looked up is in.
            yield Looking(engine)
            await engine.lookups.wait(chat_id)
            async for event in self._reply(engine):
                yield event
            return
        last = engine.store.recent_messages(chat_id, 1)
        said = last[0] if last and last[0].role == "assistant" and last[0].id > before else None
        if said is not None and said.platform_id is None:
            pid = new_message_id()
            engine.store.set_platform_id(said.id, pid)
            said = replace(said, platform_id=pid)
        yield Said(engine, said)

    async def _summarize(self) -> AsyncIterator[Event]:
        """After replies: fold what left the history window into each character's summary."""
        for engine in self.cast:
            try:
                await engine.update_summary(self.chat_of(engine))
            except Exception as e:
                yield Failed(engine, "summary", e)

    # --- usage --------------------------------------------------------------------------

    def _used(self, done: Done) -> None:
        u = self.usage
        u.calls += 1
        u.last = done
        for k, v in done.usage.items():
            u.tokens[k] = u.tokens.get(k, 0) + v
        price = self.engine.cfg.price(done.model)
        if price:
            u.cost += price.cost(done.usage)
        elif done.model:
            u.unpriced.add(done.model)
        if self._on_usage is not None:
            self._on_usage(done)
