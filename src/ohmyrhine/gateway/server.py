"""WebSocket server for chat-app connectors (docs/protocol.md).

One connector connects; its events are routed to the Engine of the character each chat is
bound to. Per chat, events are processed in order by a worker. Group messages go to the reply
judge at once, which says whether they go to the character (a mention or a quote of her always
does) and how long to wait for the rest; in private chats a reply waits until the chat has been
quiet for a moment, so a burst gets one answer. A message that starts with the character's
one-time code is a system instruction (Engine.instruct) and goes to her without the judge. A
reply is written again when the people it answers write more, or an instruction comes, before it
goes out. Outgoing messages respect the account's rate limits.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import re
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import Any, Literal

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.protocol import State

from ..config import Config
from ..engine import Engine
from ..engine.media import IncomingMedia
from ..pack import PackError
from ..providers import Done, ReplyWritten, StreamEvent
from ..store import SYSTEM_SENDER, Pending
from .protocol import (
    PROTOCOL_VERSION,
    MediaRef,
    SelfInfo,
    decode_b64,
    parse_message,
    parse_system,
)

log = logging.getLogger(__name__)

# The connector refuses longer messages as bad_text (docs/protocol.md).
# What a connector takes unless its hello says otherwise (hello.limits).
MAX_REPLY_CHARS = 2000
RECALL_WINDOW_S = 120.0

type QueuedEvent = tuple[ServerConnection | None, dict[str, Any]]

# Only loopback and Tailscale addresses may connect, on top of the token.
_ALLOWED_NETS = [
    ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48")
]


# Typing speed for the gap between two of her messages: 100 words a minute, about 500
# keystrokes; a Chinese character takes about 3 (pinyin), anything else one. Thinking time is
# not counted, and no gap is longer than MAX_GAP_S.
_KEYS_PER_S = 500 / 60
MAX_GAP_S = 5.0


def typing_s(text: str) -> float:
    keys = sum(3 if "\u4e00" <= ch <= "\u9fff" else 1 for ch in text)
    return min(MAX_GAP_S, keys / _KEYS_PER_S)


class RateLimiter:
    """Earliest send times per chat: one of her messages after another no sooner than it takes
    to type the later one (typing_s)."""

    def __init__(self):
        self.last_in_chat: dict[str, float] = {}

    def reserve(self, chat_id: str, text: str, now: float | None = None) -> float:
        """Book a slot for `text`; returns how many seconds from now to send."""
        now = time.monotonic() if now is None else now
        at = max(now, self.last_in_chat.get(chat_id, -1e9) + typing_s(text))
        self.last_in_chat[chat_id] = at
        return at - now


class Gateway:
    def __init__(
        self,
        cfg: Config,
        token: str,
        debounce_s: float = 2.5,
        reply_ttl_s: float = 60.0,
        stale_s: float = 120.0,
        wait_short_s: float | None = None,
        wait_long_s: float | None = None,
        dice: bool | None = None,
        recall_window_s: float | None = None,
        max_rewrites: int = 2,
        hush_s: float = 600.0,
    ):
        if not token:
            raise ValueError("token 不能为空")
        self.cfg = cfg
        self.token = token
        self.debounce_s = debounce_s
        self.reply_ttl_s = reply_ttl_s
        # Messages older than this when their turn comes (left over from a restart or an
        # outage the connector replays) are let go instead of answered late.
        self.stale_s = stale_s
        # How long to wait for the rest when the judge finds the speaker not finished: sending
        # it in pieces ("short"), or broken off mid-sentence ("long").
        # Each character's own unless given here.
        self.wait_s = {k: v for k, v in (("short", wait_short_s), ("long", wait_long_s)) if v is not None}
        self.dice = dice
        # How long people can still recall a message there: memory waits that long before reading
        # one. The connector's hello may say (limits.recall_window_s); given here, this wins.
        self._recall_window_s = recall_window_s
        self.recall_window_s = RECALL_WINDOW_S if recall_window_s is None else recall_window_s
        # The longest message the connector sends (limits.max_text_chars); longer replies are cut.
        self.max_text_chars = MAX_REPLY_CHARS
        # A reply not sent yet is dropped and written again with what came in when anyone writes
        # meanwhile, at most this many times in a row; then it goes out.
        self.max_rewrites = max_rewrites
        # Speech lock: once the judge finds someone asking her not to talk, she stays quiet in
        # that group until someone @-mentions her or nothing has happened there for this long.
        self.hush_s = hush_s
        self.engines: dict[str, Engine] = {}
        self.conn: ServerConnection | None = None
        self._connect_lock = asyncio.Lock()
        self.me = SelfInfo("", "")
        # What the connector says it supports (hello.features), e.g. "action_dedupe".
        self.features: set[str] = set()
        self.limiter = RateLimiter()
        self._queues: dict[str, asyncio.Queue[QueuedEvent]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._turns: dict[str, set[asyncio.Task[None]]] = {}
        self._bound: dict[str, Engine] = {}
        # One respond() at a time per chat, so a reply owed is never answered twice.
        self._responding: dict[str, asyncio.Lock] = {}
        self._media: dict[str, asyncio.Future[bytes | None]] = {}
        # Chats waiting for the rest of a message the judge found unfinished: the newest
        # message id at the time, and whether the judge would hand it to the character as it is,
        # which is what happens if nothing follows.
        self._waiting: dict[str, tuple[int, bool]] = {}
        # Number of consecutive rewrites for the current batch.
        self._rewrites: dict[str, int] = {}
        # Per chat, the newest message that @-mentioned her (see the speech lock in respond()).
        self._mentioned: dict[str, int] = {}
        # A reply in several messages waits for each to go out before typing the next.
        self._receipts: dict[str, asyncio.Future[bool]] = {}
        # Per chat, the rest of a reply in several messages that someone wrote into the middle of.
        self._unsent: dict[str, str] = {}
        # Notes she is still writing about replies already on their way.
        self._noting: set[asyncio.Task] = set()

    # --- connection -------------------------------------------------------

    def authorize(self, conn: ServerConnection, request: Request) -> Response | None:
        host = conn.remote_address[0] if conn.remote_address else ""
        try:
            allowed = any(ipaddress.ip_address(host.split("%")[0]) in net for net in _ALLOWED_NETS)
        except ValueError:
            allowed = False
        if not allowed:
            return conn.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        if request.path != "/v1/ws":
            return conn.respond(HTTPStatus.NOT_FOUND, "not found\n")
        given = request.headers.get_all("Authorization")
        if len(given) != 1 or not hmac.compare_digest(given[0].encode(), f"Bearer {self.token}".encode()):
            return conn.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    async def serve(self, host: str, port: int) -> None:
        async with serve(
            self.handle, host, port, process_request=self.authorize, max_size=16 * 2**20
        ) as server:
            log.info("连接器服务端已启动：ws://%s:%d/v1/ws", host, port)
            await server.serve_forever()

    async def handle(self, conn: ServerConnection) -> None:
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=10)
            hello = json.loads(raw) if isinstance(raw, str) else None
        except (TimeoutError, ValueError, ConnectionClosed):
            await conn.close(4002, "expected hello")
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            await conn.close(4002, "expected hello")
            return
        version = hello.get("protocol_version")
        if type(version) is not int or version != PROTOCOL_VERSION:
            await conn.close(4001, f"protocol_version must be {PROTOCOL_VERSION}")
            return
        me = hello.get("self")
        if (not isinstance(me, dict) or not isinstance(me.get("id"), str) or not me["id"]
                or not isinstance(me.get("nickname", ""), str)):
            await conn.close(4002, "expected hello.self.id")
            return
        try:
            async with self._connect_lock:
                if self.conn is not None:
                    log.info("新的连接器连上了，断开旧连接")
                    old, self.conn = self.conn, None
                    self._release_media()
                    await old.close(4003, "replaced by a new connection")
                await conn.send(json.dumps({"type": "welcome", "protocol_version": PROTOCOL_VERSION}))
                self.me = SelfInfo(me["id"], me.get("nickname") or "")
                features = hello.get("features")
                self.features = {f for f in features if isinstance(f, str)} if isinstance(features, list) else set()
                self._limits(hello.get("limits"))
                self.conn = conn
                log.info("连接器已连接：%s", hello.get("connector"))
                self.resume()
            async for raw in conn:
                if self.conn is not conn:
                    break
                try:
                    ev = json.loads(raw) if isinstance(raw, str) else None
                except ValueError:
                    ev = None
                if not isinstance(ev, dict):
                    log.warning("收到无法解析的帧，已忽略")
                    continue
                try:
                    self.dispatch(ev)
                    if ev.get("type") in ("message_sent", "send_failed") and ev.get("action_id"):
                        # Lets the connector stop replaying this outcome.
                        await self.send({"type": "ack", "action_id": ev["action_id"]})
                except Exception:  # one bad frame must not drop the connection
                    log.exception("处理 %s 帧失败", ev.get("type"))
                    if ev.get("type") in ("message", "system", "chat_update") and ev.get("event_id"):
                        await self.send({"type": "ack", "event_id": ev["event_id"]})
        except ConnectionClosed:
            pass
        finally:
            if self.conn is conn:
                self.conn = None
                self._release_media()
                log.info("连接器已断开")

    async def send(self, frame: dict[str, Any]) -> bool:
        if self.conn is None:
            return False
        try:
            await self.conn.send(json.dumps(frame, ensure_ascii=False))
            return True
        except ConnectionClosed:
            return False

    # --- upstream ---------------------------------------------------------

    def dispatch(self, ev: dict[str, Any]) -> None:
        match ev.get("type"):
            case "message" | "system" | "chat_update" | "media_update":
                chat_id = (ev.get("chat") or {}).get("id")
                if not isinstance(chat_id, str) or not chat_id:
                    raise ValueError("事件缺少 chat.id")
                self._queue(chat_id).put_nowait((self.conn, ev))
            case "media":
                fut = self._media.get(ev.get("media_id") or "")
                if fut is not None and not fut.done():
                    fut.set_result(None if ev.get("error") else decode_b64(ev.get("data_b64")))
            case "message_sent" | "send_failed":
                failed = ev["type"] == "send_failed"
                if (fut := self._receipts.pop(ev.get("action_id") or "", None)) and not fut.done():
                    fut.set_result(not failed or ev.get("outcome") == "unknown")
                for engine in self.engines.values():
                    due = engine.store.finish_delivery(ev.get("action_id") or "", ev.get("message_id"),
                                                       failed=failed and ev.get("outcome") != "unknown")
                    if due is not None:
                        if failed:
                            log.warning("消息没有发出去：%s", ev.get("reason"))
                        else:
                            log.info("回复已发出：连接器用了 %.1f 秒", time.time() - due / 1000)
                        break
            case other:
                log.debug("忽略未知事件 %s", other)

    def _queue(self, chat_id: str) -> asyncio.Queue[QueuedEvent]:
        if chat_id not in self._queues:
            self._queues[chat_id] = asyncio.Queue()
            self._workers[chat_id] = asyncio.create_task(self._work(chat_id))
        return self._queues[chat_id]

    async def _work(self, chat_id: str) -> None:
        queue = self._queues[chat_id]
        try:
            while not queue.empty():
                source, ev = queue.get_nowait()
                if source is not self.conn:
                    continue  # unacknowledged events will be replayed by the connector
                try:
                    await self.process(chat_id, ev, source=source)
                except (sqlite3.OperationalError, OSError):
                    # Not stored: left unacknowledged, the connector sends it again.
                    log.exception("存储 %s 事件失败，不回 ack，等连接器重发", ev.get("type"))
                    continue
                except Exception:
                    log.exception("处理 %s 事件失败", ev.get("type"))
                if source is self.conn and ev.get("event_id"):
                    await self.send({"type": "ack", "event_id": ev["event_id"]})
        finally:
            del self._queues[chat_id], self._workers[chat_id]

    def _limits(self, limits: object) -> None:
        """What the connector's platform allows, from its hello; defaults for what it leaves out."""
        limits = limits if isinstance(limits, dict) else {}
        chars = limits.get("max_text_chars")
        self.max_text_chars = chars if type(chars) is int and chars > 0 else MAX_REPLY_CHARS
        if self._recall_window_s is None:
            window = limits.get("recall_window_s")
            ok = isinstance(window, int | float) and not isinstance(window, bool) and window >= 0
            self.recall_window_s = float(window) if ok else RECALL_WINDOW_S
            for engine in self.engines.values():
                engine.recall_window_ms = int(self.recall_window_s * 1000)

    def engine_for(self, bind: str | None) -> Engine | None:
        if not bind:
            return None
        if bind not in self.engines:
            pack = self.cfg.characters_dir / bind / "character.yaml"
            db = self.cfg.data_dir / f"{bind}.db"
            if Path(bind).name != bind or bind == ".." or not (pack.is_file() or db.is_file()):
                log.error("未知角色 %s", bind)
                return None
            try:
                engine = Engine(self.cfg, bind, dice=self.dice, recall_window_s=self.recall_window_s)
                # Only on first opening this database. A later hello can arrive during generation.
                if engine.store.discard_gateway_drafts():
                    log.warning("删掉了上次中断时还没交给连接器的回复")
                engine.on_lookup = lambda chat_id, engine=engine: self._looked_up(engine, chat_id)
            except (PackError, OSError, sqlite3.DatabaseError) as e:
                log.error("无法加载角色 %s：%s", bind, e)
                return None
            self.engines[bind] = engine
        return self.engines[bind]

    def resume(self) -> None:
        """Pick up chats with messages not handled yet, e.g. acked just before a restart or
        while the connector was away. Too old ones are let go in respond()."""
        for db in sorted(self.cfg.data_dir.glob("*.db")):
            engine = self.engine_for(db.stem)
            if engine is None:
                continue
            if "action_dedupe" in self.features and (flying := engine.store.in_flight()):
                asyncio.create_task(self._ask_outcomes(flying))
            for chat_id in engine.store.chat_ids():
                if chat_id.startswith("cli:"):  # local `rhine chat` sessions, not connector chats
                    continue
                if engine.store.pending(chat_id) is None or chat_id in self._timers:
                    continue
                log.info("接着处理 %s 里没处理完的消息", _short(chat_id))
                self._bound[chat_id] = engine
                if held := self._waiting.get(chat_id):
                    # The wait expired offline. Act on its verdict if the batch still matches.
                    self._arm(chat_id, 0, waited=held[0])
                else:
                    self._arm(chat_id)

    async def process(self, chat_id: str, ev: dict[str, Any], *, source: ServerConnection | None = None) -> None:
        """Store one event. Ack is sent by the caller once this returns."""
        bind = ev["chat"].get("bind")
        engine = self.engine_for(bind)
        if bind is not None and engine is None:
            return
        eid = ev.get("event_id")
        if engine is not None and engine.store.has_event(eid):
            return
        if engine is None and ev["type"] == "chat_update" and any(e.store.has_event(eid) for e in self.engines.values()):
            return
        if ev["type"] == "media_update":
            if engine is not None:
                await self._update_media(engine, chat_id, ev)
            return
        old = self._bound.get(chat_id)
        if old is not None and old is not engine:
            # Unbound or rebound: what the old character had not answered stays unanswered.
            for turn in self._turns.get(chat_id, ()):
                turn.cancel()
            self._timers.pop(chat_id, None)
            self._waiting.pop(chat_id, None)
            self._mentioned.pop(chat_id, None)
            self._rewrites.pop(chat_id, None)
            self._unsent.pop(chat_id, None)
            with old.store.transaction():
                old.mark_seen(chat_id)
                if engine is None and eid:
                    old.store.record_event(eid)
        if engine is None:
            self._bound.pop(chat_id, None)
            return
        self._bound[chat_id] = engine
        if ev["type"] == "chat_update":
            if eid:
                engine.store.record_event(eid)
            return
        name = engine.pack.character.name
        if ev["type"] == "system":
            msg = parse_system(ev, self.me, name)
            wake = msg.chat_kind != "private"
        else:
            msg, refs = parse_message(ev, self.me, name)
            if refs:
                msg = replace(msg, media=tuple(await asyncio.gather(*(self._fetch(r) for r in refs))))
                if source is not None and (source is not self.conn or source.state != State.OPEN):
                    return  # replay can still fetch the available file on its own connection
            wake = engine.dice if msg.dice else not msg.bot
        with engine.store.transaction():
            if not msg.backfill:
                self._lift_hush(engine, chat_id, msg.ts, mentioned=msg.mentions_self)
            engine.receive(msg)
        if msg.backfill:
            return
        if msg.mentions_self:
            self._mentioned[chat_id] = engine.store.latest_message_id(chat_id) or 0
        if wake:
            self._arm(chat_id)

    def _lift_hush(self, engine: Engine, chat_id: str, ts: int, mentioned: bool) -> None:
        """Lift the chat's speech lock when a message @-mentions her, or comes after hush_s of quiet."""
        if not engine.store.hushed(chat_id):
            return
        last = engine.store.recent_messages(chat_id, 1)
        quiet = not last or ts - last[0].ts >= self.hush_s * 1000
        if mentioned or quiet:
            engine.store.set_hushed(chat_id, False)
            why = "有人 @ 角色" if mentioned else f"安静了 {self.hush_s / 60:g} 分钟"
            log.info("%s：发言锁解除（%s）", _short(chat_id), why)

    async def _fetch(self, ref: MediaRef) -> IncomingMedia:
        if ref.data is not None or not ref.available or ref.media_id is None:
            return IncomingMedia(ref.kind, ref.data, ref.hint, ref.thumbnail)
        fut = self._media.get(ref.media_id)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._media[ref.media_id] = fut
            try:
                if await self.send({"type": "get_media", "media_id": ref.media_id}):
                    await asyncio.wait_for(asyncio.shield(fut), timeout=30)
            except TimeoutError:
                pass
            finally:
                if not fut.done():
                    fut.set_result(None)
                if self._media.get(ref.media_id) is fut:
                    del self._media[ref.media_id]
        data = await asyncio.shield(fut)
        return IncomingMedia(ref.kind, data, ref.hint, ref.thumbnail)

    async def _update_media(self, engine: Engine, chat_id: str, ev: dict[str, Any]) -> None:
        """The original of an image that came in as a thumbnail (docs/protocol.md, media_update)."""
        msg = engine.store.by_platform_id(chat_id, ev.get("message_id") or "")
        index = ev.get("index", 0)
        if msg is not None and isinstance(index, int) and 0 <= index < len(msg.attachments):
            old = msg.attachments[index]
            data = decode_b64(ev.get("data_b64"))
            media = await self._fetch(MediaRef(old.kind, ev.get("media_id"), True, data))
            att = engine.media.ingest(media)
            if att.sha256 and engine.store.replace_thumbnail(msg.id, index, att.sha256):
                log.info("%s：缩略图换成了原图", _short(chat_id))
        if ev.get("event_id"):
            engine.store.record_event(ev["event_id"])

    async def _ask_outcomes(self, flying: list[tuple[str, str, str, int]]) -> None:
        """After a restart, ask again about replies sent without an outcome. The connector runs
        each action_id once, so this only brings back the outcome (or sends one never started)."""
        for chat_id, action_id, text, due_ms in flying:
            await self.send({"type": "send_message", "action_id": action_id, "chat_id": chat_id, "text": text,
                             "delay_ms": 0, "expires_at": due_ms + int(self.reply_ttl_s * 1000)})
        log.info("向连接器补问 %d 条回复的结果", len(flying))

    def _release_media(self) -> None:
        for fut in self._media.values():
            if not fut.done():
                fut.set_result(None)
        self._media.clear()

    # --- replying ---------------------------------------------------------

    def _looked_up(self, engine: Engine, chat_id: str) -> None:
        """What she looked up is in: her answer is owed, if the chat is still hers."""
        if self._bound.get(chat_id) is engine:
            self._arm(chat_id)

    def _arm(self, chat_id: str, delay: float | None = None, waited: int | None = None) -> None:
        """(Re)start the chat's timer; whatever it was waiting for is superseded.

        By default the delay fits what is new: in a judged group it goes to the judge at once,
        which says how long to wait for more; elsewhere a reply waits for the quiet period, so
        a burst gets one answer. `waited` marks the timer of such a wait (the newest message id
        when it started).
        """
        timer = self._timers.get(chat_id)
        if timer is not None and not timer.done():
            if delay is None and (held := self._waiting.get(chat_id)):
                pending = self._bound[chat_id].store.pending(chat_id)
                if pending is not None and pending.latest_id == held[0]:
                    return  # a context-only event cannot replace the timer for this batch
            timer.cancel()
        if delay is None:
            engine = self._bound.get(chat_id)
            delay = self.debounce_s
            if engine and (
                engine.judges(chat_id)
                or ((pending := engine.store.pending(chat_id)) is not None and (pending.instructed or pending.looked_up))
            ):
                delay = 0.0
        task = self._timers[chat_id] = asyncio.create_task(self._fire(chat_id, delay, waited))
        turns = self._turns.setdefault(chat_id, set())
        turns.add(task)
        task.add_done_callback(turns.discard)

    async def _fire(self, chat_id: str, delay: float, waited: int | None = None) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        try:
            await self.respond(chat_id, waited)
        except Exception:
            log.exception("回复失败")
        engine = self._bound.get(chat_id)
        if engine is not None:
            # The summary comes after replying, so no reply ever waits for it.
            try:
                if not await engine.compact(chat_id):
                    await engine.update_summary(chat_id)
            except Exception:
                log.warning("更新对话摘要失败", exc_info=True)

    async def respond(self, chat_id: str, waited: int | None = None) -> None:
        """Deal with what is new in the chat: judge it, wait for more, or reply. `waited` is
        set when a wait for the rest of a message (up to that message id) has run out."""
        async with self._responding.setdefault(chat_id, asyncio.Lock()):
            # Until we own the lock a newer timer can still replace us. From here only a
            # binding change cancels this turn.
            if self._timers.get(chat_id) is asyncio.current_task():
                del self._timers[chat_id]
            engine = self._bound.get(chat_id)
            if engine is None or self.conn is None:
                return
            pending = engine.store.pending(chat_id)
            if pending is None:
                return
            if time.time() * 1000 - pending.newest_ts > self.stale_s * 1000:
                engine.store.mark_handled(chat_id, pending.latest_id)
                self._rewrites.pop(chat_id, None)
                self._unsent.pop(chat_id, None)
                self._waiting.pop(chat_id, None)
                log.warning("放弃 %s 里没处理完的消息：已经超过 %d 秒", _short(chat_id), self.stale_s)
                return
            held = self._waiting.get(chat_id)
            owed = pending.trigger_id is not None
            # A system instruction is complete as it is and always goes to her: nothing to judge or wait for.
            instructed = pending.instructed
            if not instructed and engine.store.hushed(chat_id):
                # Asked not to talk: what comes in is context only, not even judged.
                engine.store.mark_handled(chat_id, pending.latest_id)
                self._waiting.pop(chat_id, None)
                log.info("%s：发言锁中，不接话", _short(chat_id))
                return
            # Nor is what she looked up: she said she would come back with it.
            if not engine.judges(chat_id) or instructed or pending.looked_up:
                if not owed:  # a group without a judge: only what demands a reply gets one
                    engine.store.mark_handled(chat_id, pending.latest_id)
                    return
                self._waiting.pop(chat_id, None)
            elif held is not None and held[0] == pending.latest_id:
                if waited != pending.latest_id:
                    return  # judged already; the wait for the rest is still running
                del self._waiting[chat_id]
                if not held[1]:
                    engine.store.mark_handled(chat_id, pending.latest_id)
                    log.info("%s：没有下文，不接话", _short(chat_id))
                    return
                log.info("%s：没有下文，交给角色", _short(chat_id))
            else:
                self._waiting.pop(chat_id, None)
                decision = await engine.decide(chat_id, owed=owed, settle=False)
                verdict = ("接话" if decision.reply else "不接话") + {"": "", "short": "，等一下", "long": "，等下文"}[decision.wait]
                if decision.hush:
                    verdict = "有人不想让角色说话，上发言锁"
                log.info("判断 %s：%s · %s", _short(chat_id), verdict, decision.reason)
                if decision.hush:
                    # Set even if more came in meanwhile, so their turn finds the lock; but not
                    # when one of them @-mentions her: that lifts it anyway.
                    with engine.store.transaction():
                        engine.store.mark_handled(chat_id, pending.latest_id)
                        if self._mentioned.get(chat_id, 0) <= pending.latest_id:
                            engine.store.set_hushed(chat_id, True)
                    self._rewrites.pop(chat_id, None)
                    self._unsent.pop(chat_id, None)
                    return
                latest = engine.store.pending(chat_id)
                if latest is None or latest.latest_id != pending.latest_id:
                    # More arrived while judging: this verdict is out of date, and the newer
                    # message's own turn (queued behind this one) judges them together.
                    return
                if decision.wait:
                    self._waiting[chat_id] = (pending.latest_id, decision.reply)
                    group = engine.cfg.group
                    wait = self.wait_s.get(decision.wait, group.wait_short_s if decision.wait == "short" else group.wait_long_s)
                    self._arm(chat_id, wait, waited=pending.latest_id)
                    return
                if not decision.reply:
                    engine.store.mark_handled(chat_id, pending.latest_id)
                    return
            if not owed:
                # Once handed on, this batch is still owed after a rewrite, outage or restart.
                engine.store.require_reply(pending.latest_id)
            if self.conn is None:
                return
            outcome = await self._reply(engine, chat_id, pending)
            if outcome == "rewrite":
                # Left new: the follow-up's own turn (queued behind this one) answers them together.
                return
            self._rewrites.pop(chat_id, None)
            self._unsent.pop(chat_id, None)
            if outcome == "handled":
                # Only once handled: a restart or an outage before this picks the chat up again.
                engine.store.mark_handled(chat_id, pending.latest_id)

    async def _reply(self, engine: Engine, chat_id: str, pending: Pending) -> Literal["handled", "undelivered", "rewrite"]:
        """Generate and send a reply to what is pending. "undelivered": the connector is away;
        "rewrite": dropped unsent, because someone wrote meanwhile."""
        action_id = f"act_{uuid.uuid4().hex[:16]}"
        instructions = engine.store.instruction_ids(chat_id)
        unsent = self._unsent.get(chat_id)
        stream = engine.reply(chat_id, action_id=action_id, unsent=unsent)
        noting = None  # the rest of the stream, when she goes on to take notes after the reply
        async for ev in stream:
            if isinstance(ev, ReplyWritten):
                noting = stream
                break
            if isinstance(ev, Done):
                if ev.stop_reason == "refusal":
                    log.warning("角色回复被拒绝，未发送")
                if ev.stop_reason == "silent":
                    log.info("%s：角色选择不回复", _short(chat_id))
        # Send exactly what was stored (the engine strips markers the model may add).
        reply = engine.store.outgoing_reply(action_id)
        if reply is None:
            await _drain(noting)
            return "handled"
        if instructions and instructions - engine.store.instruction_ids(chat_id):
            # A revoked instruction must not speak through a reply that is still unsent.
            engine.store.delete_messages([reply.id])
            await _drain(noting)  # with the reply gone, so are its notes
            if engine.store.pending(chat_id):
                self._arm(chat_id)
            return "rewrite"
        later = [m for m in engine.store.messages_between(chat_id, reply.id)
                 if m.role == "user" and not m.bot and not m.backfill and m.sender_id != SYSTEM_SENDER]
        rewrites = self._rewrites.get(chat_id, 0)
        # Like a person reading what came in before pressing send: whoever wrote it (or a system
        # instruction), she answers it in this one message rather than in a second right after.
        if rewrites < self.max_rewrites and later:
            engine.store.delete_messages([reply.id])
            await _drain(noting)  # with the reply gone, so are its notes
            self._rewrites[chat_id] = rewrites + 1
            log.info("%s：回复还没发出，又来了新消息，重写（第 %d 次）", _short(chat_id), rewrites + 1)
            return "rewrite"
        if noting is not None:
            # The reply goes out now; her notes about it are written meanwhile. Her next reply
            # in this chat waits for them (the engine's lock), so it sees them.
            task = asyncio.create_task(_drain(noting))
            self._noting.add(task)
            task.add_done_callback(self._noting.discard)
        newest_from_people = engine.store.recent_user_messages(chat_id, 1)
        quote = None
        trigger = pending.trigger_platform_id
        # Quote the message being answered when the chat has moved on past it.
        if trigger and newest_from_people and newest_from_people[0].platform_id != trigger:
            quote = trigger
        self._unsent.pop(chat_id, None)
        first, *rest = [chunk for part in _split(reply.content) for chunk in _chunks(part, self.max_text_chars)]
        if first != reply.content:
            # Each message is stored as sent, so she remembers what the chat saw.
            engine.store.set_content(reply.id, first)
            if len(reply.content) > self.max_text_chars:
                log.warning("%s：回复有 %d 字，超过单条上限，分成 %d 条发", _short(chat_id), len(reply.content), 1 + len(rest))
        if not await self._send_reply(engine, chat_id, action_id, reply.id, first, quote, track=bool(rest)):
            return "undelivered"
        return await self._send_rest(engine, chat_id, action_id, reply.id, rest)

    async def _send_rest(self, engine: Engine, chat_id: str, action_id: str, stored_id: int, rest: list[str]) -> Literal["handled", "rewrite"]:
        """The rest of a reply in several messages, one at a time like someone typing: each after
        the one before has gone out and a pause for its length. Whoever writes in between gets
        answered with what was left (the rewrite rule, see _reply)."""
        for k, part in enumerate(rest):
            try:
                sent = await asyncio.wait_for(asyncio.shield(self._receipts[action_id]), timeout=30)
            except TimeoutError:
                sent = False
            finally:
                self._receipts.pop(action_id, None)
            if not sent:
                log.warning("%s：上一条没确认发出，剩下 %d 条不发了", _short(chat_id), len(rest) - k)
                return "handled"
            await asyncio.sleep(typing_s(part))
            later = [m for m in engine.store.messages_between(chat_id, stored_id)
                     if m.role == "user" and not m.bot and not m.backfill and m.sender_id != SYSTEM_SENDER]
            rewrites = self._rewrites.get(chat_id, 0)
            if later and rewrites < self.max_rewrites:
                self._rewrites[chat_id] = rewrites + 1
                self._unsent[chat_id] = "\n\n".join(rest[k:])
                # She was mid-reply: what came in is answered, together with what she had left.
                engine.store.require_reply(later[-1].id)
                log.info("%s：分条发到一半有人插话，剩下 %d 条重写（第 %d 次）", _short(chat_id), len(rest) - k, rewrites + 1)
                return "rewrite"
            action_id = f"act_{uuid.uuid4().hex[:16]}"
            stored = engine.store.add_message(
                chat_id=chat_id, role="assistant", sender_id=engine.self_id, sender_name=engine.pack.character.name,
                content=part, ts=int(time.time() * 1000), action_id=action_id,
            )
            assert stored is not None  # no event_id, so never a duplicate
            stored_id = stored.id
            if not await self._send_reply(engine, chat_id, action_id, stored_id, part, None,
                                          track=k + 1 < len(rest), follow_up=True):
                return "handled"
        return "handled"

    async def _send_reply(self, engine: Engine, chat_id: str, action_id: str, stored_id: int, text: str,
                          quote: str | None, *, track: bool = False, follow_up: bool = False) -> bool:
        """`track`: a later message of the same reply waits for this one's receipt. A `follow_up`
        has already waited for its typing (see _send_rest)."""
        if self.conn is None:
            engine.store.delete_messages([stored_id])
            return False
        # A follow-up has already waited for its typing; it only marks when it went out.
        delay = self.limiter.reserve(chat_id, "" if follow_up else text)
        if track:
            self._receipts[action_id] = asyncio.get_running_loop().create_future()
        now_ms = int(time.time() * 1000)
        frame: dict[str, Any] = {
            "type": "send_message",
            "action_id": action_id,
            "chat_id": chat_id,
            "text": text,
            "delay_ms": int(delay * 1000),
            "expires_at": now_ms + int((delay + self.reply_ttl_s) * 1000),
        }
        if quote:
            frame["quote"] = quote
        conn = self.conn
        engine.store.start_delivery(stored_id, now_ms + frame["delay_ms"])
        if not await self.send(frame):
            # Not sent, so not said: drop it, and let the chat be answered when the connector is back.
            engine.store.delete_messages([stored_id])
            if self.conn is not None and self.conn is not conn:
                self._arm(chat_id)
            log.warning("连接器不在线，回复没有发出")
            return False
        newest = engine.store.recent_user_messages(chat_id, 1)
        since = f"，距最新消息 {now_ms / 1000 - newest[0].ts / 1000:.1f} 秒" if newest else ""
        log.info("回复 %s：%d 字%s，%.1f 秒后发送", _short(chat_id), len(text), since, delay)
        return True


async def _drain(stream: AsyncIterator[StreamEvent] | None) -> None:
    """Let the rest of a reply's stream run: her notes, and the call's usage at its end."""
    if stream is None:
        return
    try:
        async for _ in stream:
            pass
    except Exception:
        log.warning("回复之后记笔记失败", exc_info=True)


# A reply is sent as at most this many messages; blank lines in it separate them. A long reply
# goes out whole (its parts on separate lines): several long messages in a row read as a wall.
MAX_PARTS = 3
SPLIT_MAX_CHARS = 90


def _split(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()] or [text]
    if len(parts) > 1 and sum(map(len, parts)) > SPLIT_MAX_CHARS:
        return ["\n".join(parts)]
    return parts[: MAX_PARTS - 1] + ["\n".join(parts[MAX_PARTS - 1:])] if len(parts) > MAX_PARTS else parts


def _chunks(text: str, limit: int = MAX_REPLY_CHARS) -> list[str]:
    """A message the connector would refuse, cut into ones it takes, at sentence ends where possible."""
    out = []
    while len(text) > limit:
        head = text[:limit]
        end = max(head.rfind(c) for c in "。！？!?\n")
        cut = end + 1 if end >= limit // 2 else limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [*out, text] if text else out


def _short(chat_id: str) -> str:
    return chat_id if len(chat_id) <= 16 else chat_id[:5] + chat_id[-6:]
