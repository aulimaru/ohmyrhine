"""`rhine web`: the local conversations of `rhine chat` (local.LocalChat) in a browser.

One process serves the page, its files and a WebSocket on the same port, on this machine only
by default. The page's address carries a token that every WebSocket and media request must
present; requests naming another host or coming from another site's page are refused.

Frames are JSON objects with a `type`. The page sends actions (say, recall, pat, ...); the
server answers with the state, the history, and while characters answer, their events.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import mimetypes
import secrets
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from ..catalog import MODELS
from ..config import Config, ConfigError, resolve
from ..engine import Engine, instance_db, split_ref
from ..engine.context import context_report
from ..engine.media import IncomingMedia
from ..engine.prompt import message_text
from ..local import (
    ChatError,
    Event,
    Failed,
    Judged,
    LocalChat,
    Looking,
    Said,
    Speaking,
    Streamed,
    name_of,
)
from ..pack import PackError
from ..providers import Done, TextDelta, ThinkingDelta, ToolUse, make_provider
from ..store import INSTRUCTION_SENDER, LOOKUP_SENDER, SYSTEM_SENDER, StoredMessage

log = logging.getLogger(__name__)

STATIC = ("index.html", "app.js", "tokens.css", "base.css", "app.css", "favicon.svg")
# Messages the page shows at once, newest last.
HISTORY_LIMIT = 300
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_TOOL_ACTIONS = {
    "read_message": lambda i: f"读了长消息的第 {i.get('part')} 段",
    "add_memory": lambda i: f"记下了：{i.get('text')}",
    "edit_memory": lambda i: f"改了笔记 {i.get('id')}：{i.get('text')}",
    "remove_memory": lambda i: f"删了笔记 {i.get('id')}：{i.get('reason')}",
    "agent_history": lambda i: f"去翻聊天记录了：{i.get('query') or '眼下在聊的'}",
    "agent_search": lambda i: f"去查了：{i.get('query')}",
}


def _static(name: str) -> bytes:
    return resources.files(__package__).joinpath("static", name).read_bytes()


def _response(status: HTTPStatus, body: bytes, content_type: str, cache: bool = False) -> Response:
    headers = Headers([
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "private, max-age=86400" if cache else "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
    ])
    return Response(status.value, status.phrase, headers, body)


def _text(status: HTTPStatus, text: str) -> Response:
    return _response(status, text.encode(), "text/plain; charset=utf-8")


@dataclass(frozen=True)
class _Pack:
    id: str
    name: str
    identity: str


def _packs(cfg: Config) -> list[_Pack]:
    """The character packs, read just far enough to list them."""
    out = []
    for path in sorted(cfg.characters_dir.glob("*/character.yaml")):
        name, identity = path.parent.name, ""
        with contextlib.suppress(yaml.YAMLError, OSError):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                name = str(raw.get("name") or name)
                identity = str(raw.get("identity") or "")
        out.append(_Pack(path.parent.name, name, " ".join(identity.split())[:80]))
    return out


class WebUI:
    def __init__(self, cfg: Config, ref: str | None, user: str, nickname: str | None, host: str, port: int):
        self.cfg, self.ref, self.user, self.nickname = cfg, ref, user, nickname
        self.host, self.port = host, port
        self.token = secrets.token_urlsafe(18)
        # The names a request's Host may give. Listening on every address, any (the token still guards it).
        local = {"localhost", "127.0.0.1"}
        self._names = None if host in ("0.0.0.0", "::", "") else local | {host} if host in local else {host}

    @property
    def hosts(self) -> set[str] | None:
        return None if self._names is None else {f"{h}:{self.port}" for h in self._names}

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self._names is None else self.host
        return f"http://{host}:{self.port}/?t={self.token}"

    async def run(self, open_browser: bool = True, ready=None) -> None:
        async with serve(self._ws, self.host, self.port, process_request=self._http,
                         max_size=MAX_UPLOAD_BYTES * 2, compression=None) as server:
            self.port = server.sockets[0].getsockname()[1]  # when it was 0: the one picked
            if ready is not None:
                ready(self)
            if open_browser:
                with contextlib.suppress(Exception):
                    webbrowser.open(self.url)
            await server.serve_forever()

    # --- HTTP -------------------------------------------------------------------------

    def _http(self, connection: ServerConnection, request: Request) -> Response | None:
        host = request.headers.get("Host", "")
        hosts = self.hosts
        if hosts is not None and host not in hosts:
            return _text(HTTPStatus.FORBIDDEN, "unknown host")  # DNS rebinding
        parts = urlsplit(request.path)
        token = parse_qs(parts.query).get("t", [""])[0]
        authorized = secrets.compare_digest(token, self.token)
        path = parts.path
        if path == "/ws":
            # Only this page may connect: other sites' pages in the same browser send their own origin.
            if not authorized or request.headers.get("Origin") != f"http://{host}":
                return _text(HTTPStatus.FORBIDDEN, "forbidden")
            return None  # on to the WebSocket handshake
        if path in ("/", "/index.html"):
            return _response(HTTPStatus.OK, _static("index.html"), "text/html; charset=utf-8")
        if path.startswith("/static/") and (name := path.removeprefix("/static/")) in STATIC:
            kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return _response(HTTPStatus.OK, _static(name), f"{kind}; charset=utf-8")
        if path.startswith("/media/"):
            if not authorized:
                return _text(HTTPStatus.FORBIDDEN, "forbidden")
            return self._media(path.removeprefix("/media/"))
        return _text(HTTPStatus.NOT_FOUND, "not found")

    def _media(self, sha: str) -> Response:
        """A stored image, by its hash (media files are kept under data_dir/media/)."""
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            return _text(HTTPStatus.NOT_FOUND, "not found")
        path = next((self.cfg.data_dir / "media" / sha[:2]).glob(f"{sha}.*"), None)
        if path is None or not path.is_file():
            return _text(HTTPStatus.NOT_FOUND, "not found")
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return _response(HTTPStatus.OK, path.read_bytes(), kind, cache=True)

    # --- WebSocket --------------------------------------------------------------------

    async def _ws(self, conn: ServerConnection) -> None:
        session = _Session(self, conn)
        try:
            await session.run()
        finally:
            session.close()


class _Session:
    """One open page: the conversation it shows and what is under way in it."""

    def __init__(self, ui: WebUI, conn: ServerConnection):
        self.ui, self.conn = ui, conn
        self.cfg = ui.cfg
        self.chat: LocalChat | None = None
        self.engines: dict[str, Engine] = {}
        self.task: asyncio.Task | None = None
        self.model_override: str | None = None
        self.user, self.nickname = ui.user, ui.nickname

    async def send(self, frame: dict[str, Any]) -> None:
        with contextlib.suppress(ConnectionClosed):
            await self.conn.send(json.dumps(frame, ensure_ascii=False))

    async def notice(self, text: str, level: str = "error") -> None:
        await self.send({"type": "notice", "level": level, "text": text})

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()

    async def run(self) -> None:
        async for raw in self.conn:
            try:
                frame = json.loads(raw) if isinstance(raw, str) else None
            except ValueError:
                frame = None
            if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                await self.notice("看不懂的请求")
                continue
            try:
                await self.dispatch(frame)
            except ChatError as e:
                await self.notice(str(e))
            except (KeyError, TypeError, ValueError) as e:
                await self.notice(f"请求有误：{e}")
            except Exception as e:  # one bad request must not close the page
                log.exception("处理 %s 失败", frame.get("type"))
                await self.notice(f"出错了：{type(e).__name__}: {e}")

    # --- actions ----------------------------------------------------------------------

    async def dispatch(self, f: dict[str, Any]) -> None:
        kind = f["type"]
        if kind == "open":
            await self.open(f.get("character"), f.get("session"), f.get("user"), f.get("nickname"))
            return
        chat = self.chat
        if chat is None:
            raise ChatError("先选一个角色")
        if kind == "stop":
            if self.task is not None:
                self.task.cancel()
            return
        if kind == "inspect":
            await self.send({"type": "inspect", "what": f["what"], "data": await self.inspect(f["what"])})
            return
        speaking = self.task is not None and not self.task.done()
        if speaking and kind != "judge":
            raise ChatError("角色还在说话：先等一等，或者点停止")
        match kind:
            case "say":
                quote = chat.message(int(f["quote"])) if f.get("quote") else None
                media = tuple(self._media(m) for m in f.get("media") or ())
                text = str(f.get("text") or "")
                if not text.strip() and not media:
                    return
                self.start(chat.say(text, media, quote))
            case "recall":
                target = chat.recallable(chat.message(int(f["id"])) if f.get("id") else None)
                self.start(chat.recall(target))
            case "pat":
                target = chat.character(f["ref"]) if f.get("ref") else chat.engine
                if target is None:
                    raise ChatError("这个角色不在对话里")
                self.start(chat.pat(target))
            case "system":
                if not str(f.get("text") or "").strip():
                    raise ChatError("系统指令不能是空的")
                self.start(chat.instruct(str(f["text"]).strip()))
            case "nudge":
                self.start(chat.nudge())
            case "retry":
                self.start(chat.retry())
            case "undo":
                chat.undo(max(1, int(f.get("n") or 1)))
                await self.refresh()
            case "rewind":
                target = chat.message(int(f["id"]))
                chat.rewind(target)
                await self.refresh()
                if target.sender_id == chat.sender_id:
                    await self.send({"type": "prefill", "text": target.content})
            case "new":
                left = chat.clear()
                if left:
                    await self.notice(f"{'、'.join(left)}留在原来的会话里", "info")
                await self.refresh()
            case "resume":
                for text in chat.resume(str(f["id"])):
                    await self.notice(text, "info")
                self.user, self.nickname = chat.user, chat.nickname
                await self.refresh()
            case "identity":
                user = str(f.get("user") or chat.user).strip() or chat.user
                nickname = str(f.get("nickname") or user).strip() or user
                chat.speak_as(user, nickname)
                self.user, self.nickname = user, nickname
                await self.refresh(history=False)
            case "invite":
                guest = chat.invite(str(f["ref"]))
                self.start(chat.announce(guest, joined=True))
            case "kick":
                guest = chat.kick(str(f["ref"]))
                self.start(chat.announce(guest, joined=False))
            case "instance":
                chat.new_instance(str(f["ref"]))
                await self.notice(f"已创建实例 {f['ref']}，可以把它拉进对话", "info")
            case "scene":
                chat.engine.store.add_scene(chat.chat_id, str(f["text"]).strip(), int(time.time() * 1000))
                await self.send({"type": "inspect", "what": "scenes", "data": await self.inspect("scenes")})
            case "unscene":
                if not chat.engine.store.delete_scene(chat.chat_id, int(f["id"])):
                    raise ChatError("没有这段情景")
                await self.send({"type": "inspect", "what": "scenes", "data": await self.inspect("scenes")})
            case "forget":
                engine = chat.character(str(f.get("ref") or chat.engine.ref))
                if engine is None:
                    raise ChatError("这个角色不在对话里")
                scope = engine.memory.scope(chat.chat_of(engine))
                if not engine.store.remove_memory(scope, int(f["id"]), int(time.time() * 1000), "owner"):
                    raise ChatError("没有这条笔记")
                await self.send({"type": "inspect", "what": "memory", "data": await self.inspect("memory")})
            case "judge":
                if not chat.engine.cfg.has_task("reply_judge"):
                    raise ChatError("config.yaml 里没有配置 reply_judge 任务")
                chat.engine.cfg.group.judge = bool(f.get("on"))
                await self.refresh(history=False)
            case "model":
                await self.model(str(f.get("name") or ""))
            case "reload":
                for engine in chat.cast:
                    try:
                        changed = engine.reload()
                    except PackError as e:
                        await self.notice(f"{name_of(engine)}的角色包有问题，继续使用旧版本：\n{e}")
                        continue
                    await self.notice(f"已重新加载{name_of(engine)}的角色包" if changed
                                      else f"{name_of(engine)}的角色包没有变化", "info")
                await self.refresh()
            case _:
                raise ChatError(f"不认识的操作 {kind}")

    def _media(self, m: dict[str, Any]) -> IncomingMedia:
        kind = m.get("kind")
        if kind not in ("image", "sticker"):
            raise ChatError("只能发图片或表情包")
        try:
            data = base64.b64decode(str(m.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            raise ChatError("图片没能读出来") from None
        if len(data) > MAX_UPLOAD_BYTES:
            raise ChatError(f"图片太大了，最多 {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
        hint = str(m.get("hint") or "").strip() or None
        return IncomingMedia(kind, data, hint=hint)

    async def open(self, ref: str | None, session: str | None, user: str | None, nickname: str | None) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        ref = ref or self.ui.ref or next((p.id for p in _packs(self.cfg)), None)
        if ref is None:
            await self.send({"type": "state", "state": self.state()})
            await self.notice(f"{self.cfg.characters_dir} 下还没有角色，先用 rhine new 创建一个")
            return
        if user:
            self.user = str(user)
            self.nickname = str(nickname or user)
        char_id, tag = split_ref(ref)
        if tag is not None and not instance_db(self.cfg, char_id, tag).is_file():
            raise ChatError(f"没有实例 {ref}，先用 rhine new {ref} 创建")
        if not (self.cfg.characters_dir / char_id).is_dir() and not (self.cfg.data_dir / f"{char_id}.db").is_file():
            raise ChatError(f"找不到角色 {char_id}")
        try:
            engine = self.engines.get(ref) or Engine(self.cfg, char_id, instance=tag)
        except PackError as e:
            raise ChatError(f"{ref} 的角色包有问题：\n{e}") from None
        self.engines[ref] = engine
        if self.model_override is not None:
            engine._provider = None  # back to the configured model
            self.model_override = None
        self.chat = chat = LocalChat(engine, self.user, self.nickname)
        notices = chat.problems
        if session and session != chat.chat_id and session in {c for c, _ in chat.sessions()}:
            notices = chat.resume(session)
            self.user, self.nickname = chat.user, chat.nickname
        for text in notices:
            await self.notice(text, "info")
        await self.refresh()

    async def model(self, name: str) -> None:
        chat = self._chat()
        cfg = chat.engine.cfg
        configured = cfg.tasks["roleplay"].model
        name = name or configured or ""
        task = cfg.tasks["roleplay"].model_copy(update={"model": name})
        try:
            settings = resolve(cfg, "roleplay", task)
        except (ConfigError, ValueError) as e:
            raise ChatError(str(e)) from None
        chat.engine._provider = make_provider(settings)
        self.model_override = None if name == configured else name
        await self.notice(f"这个页面的回复改用 {name or 'echo'}（{settings.provider}）", "info")
        await self.refresh(history=False)

    def _chat(self) -> LocalChat:
        if self.chat is None:
            raise ChatError("先选一个角色")
        return self.chat

    # --- characters answering ---------------------------------------------------------

    def start(self, events) -> None:
        self.task = asyncio.create_task(self._play(events))

    async def _play(self, events) -> None:
        chat = self._chat()
        await self.send({"type": "busy", "busy": True})
        first = True
        try:
            async for event in events:
                if first:  # what was just said is stored by now: show it before the replies
                    first = False
                    await self.refresh(state=False)
                await self.event(event)
        except asyncio.CancelledError:
            await self.notice("停下了，没说完的回复不会留下", "info")
        except ChatError as e:
            await self.notice(str(e))
        except Exception as e:
            log.exception("回复出错")
            await self.notice(f"出错了：{type(e).__name__}: {e}")
        finally:
            if self.task is asyncio.current_task():
                self.task = None
            if self.chat is chat:
                await self.send({"type": "busy", "busy": False})
                await self.refresh()

    async def event(self, event: Event) -> None:
        chat = self._chat()
        ref = getattr(event, "engine", None)
        who = {"ref": ref.ref, "name": name_of(ref)} if isinstance(ref, Engine) else {}
        if isinstance(event, Speaking):
            await self.send({"type": "speaking", **who})
        elif isinstance(event, Looking):
            await self.send({"type": "looking", **who})
        elif isinstance(event, Streamed):
            ev = event.event
            if isinstance(ev, TextDelta):
                await self.send({"type": "delta", **who, "text": ev.text})
            elif isinstance(ev, ThinkingDelta):
                await self.send({"type": "thinking", **who, "text": ev.text})
            elif isinstance(ev, ToolUse):
                action = _TOOL_ACTIONS.get(ev.name, lambda _: f"用了 {ev.name}")(ev.input)
                await self.send({"type": "tool", **who, "text": f"{who.get('name', '')}{action}"})
            elif isinstance(ev, Done):
                await self.send({"type": "done", **who, "stop": ev.stop_reason, "usage": ev.usage,
                                 "model": ev.model})
        elif isinstance(event, Said):
            message = None
            if event.message is not None:
                message = self.message(event.message, chat)
                if event.engine is not chat.engine:
                    # From the invited character's own copy, numbered its own way: shown until
                    # the history (the first character's copy) comes, and not acted on by number.
                    message["id"] = -message["id"]
            await self.send({"type": "said", **who, "message": message})
        elif isinstance(event, Judged):
            d = event.decision
            await self.send({"type": "judged", **who, "reply": d.reply, "reason": d.reason, "wait": d.wait})
        elif isinstance(event, Failed):
            what = {"reply": "生成", "judge": "判断", "summary": "更新对话摘要"}.get(event.what, event.what)
            await self.notice(f"{who.get('name', '')}{what}失败：{type(event.error).__name__}: {event.error}")

    # --- what the page shows ----------------------------------------------------------

    async def refresh(self, history: bool = True, state: bool = True) -> None:
        if state:
            await self.send({"type": "state", "state": self.state()})
        if history and self.chat is not None:
            messages = self.chat.engine.store.recent_messages(self.chat.chat_id, HISTORY_LIMIT)
            descriptions = self.chat.engine.descriptions(messages)
            await self.send({"type": "history", "chat": self.chat.chat_id,
                             "messages": [self.message(m, self.chat, descriptions) for m in messages]})

    def state(self) -> dict[str, Any]:
        packs = [{"id": p.id, "name": p.name, "identity": p.identity} for p in _packs(self.cfg)]
        chat = self.chat
        if chat is None:
            return {"characters": packs, "chat": None}
        engine = chat.engine
        store = engine.store
        sessions = []
        for chat_id, ts in chat.sessions()[:50]:
            last = store.recent_messages(chat_id, 1)
            preview = ""
            if last:
                m = last[0]
                who = chat.name if m.role == "assistant" else m.sender_name
                preview = f"{who}：{message_text(m)}"
            sessions.append({"id": chat_id, "label": chat.label(chat_id), "ts": ts,
                             "time": _when(ts), "preview": " ".join(preview.split())[:60]})
        try:
            s = engine.cfg.settings("roleplay")
            model = self.model_override or s.model or "echo"
            provider = s.provider
        except (KeyError, ConfigError):
            model, provider = "?", "?"
        c = engine.pack.character
        return {
            "characters": packs,
            "chat": {
                "id": chat.chat_id,
                "label": chat.label(),
                "kind": chat.kind,
                "character": {"ref": engine.ref, "name": c.name, "identity": c.identity,
                              "instance": engine.instance},
                "cast": [{"ref": e.ref, "name": name_of(e), "first": e is engine} for e in chat.cast],
                "me": {"user": chat.user, "nickname": chat.nickname, "id": chat.sender_id},
                "model": model,
                "provider": provider,
                "judge": {"available": engine.cfg.has_task("reply_judge"), "on": engine.judging},
                "usage": {"calls": chat.usage.calls, "cost": round(chat.usage.cost, 4)},
                "sessions": sessions,
                "busy": self.task is not None and not self.task.done(),
            },
        }

    def message(self, m: StoredMessage, chat: LocalChat, descriptions: dict[str, str | None] | None = None) -> dict:
        speaker = chat.speaker_of(m)
        if m.sender_id == SYSTEM_SENDER:
            kind = "system"
        elif m.sender_id == INSTRUCTION_SENDER:
            kind = "instruction"
        elif m.sender_id == LOOKUP_SENDER:
            kind = "lookup"
        elif speaker is not None:
            kind = "character"
        elif m.sender_id == chat.sender_id:
            kind = "me"
        else:
            kind = "person"
        descriptions = descriptions if descriptions is not None else chat.engine.descriptions([m])
        return {
            "id": m.id,
            # The same in every character's copy of the message.
            "pid": m.platform_id,
            "ts": m.ts,
            "time": _when(m.ts),
            "kind": kind,
            "who": name_of(speaker) if speaker is not None else (m.sender_name or ""),
            "ref": speaker.ref if speaker is not None else None,
            "sender": m.sender_id,
            "text": m.content,
            "quote": {"who": m.quote.sender_name, "text": m.quote.text} if m.quote else None,
            "attachments": [{"kind": a.kind, "url": f"/media/{a.sha256}" if a.sha256 else None, "hint": a.hint,
                             "description": descriptions.get(a.sha256 or "")} for a in m.attachments],
            "recalls": m.recalls,
            "bot": m.bot,
        }

    async def inspect(self, what: str) -> Any:
        chat = self._chat()
        engine = chat.engine
        if what == "prompt":
            return {"system": engine.system}
        if what == "memory":
            out = []
            for e in chat.cast:
                chat_id = chat.chat_of(e)
                summary, _ = e.store.summary(chat_id)
                keeper = e.memory
                notes = [{"id": m.id, "text": m.text,
                          "about": f"关于{keeper.label(m.subject_id)}" if m.subject_id
                          else "本会话" if m.chat_id == chat_id else "另一次聊天"}
                         for m in e.store.memories(keeper.scope(chat_id))]
                out.append({"ref": e.ref, "name": name_of(e), "instance": e.instance, "summary": summary,
                            "notes": notes})
            return out
        if what == "scenes":
            return [{"id": s.id, "text": s.text} for s in engine.store.scenes(chat.chat_id)]
        if what == "context":
            r = await context_report(engine, chat.chat_id)
            counted = r.total_tokens is not None

            def size(p) -> int:
                return p.tokens if counted and p.tokens is not None else p.chars

            return {"unit": "tokens" if counted else "字符", "total": r.total_tokens,
                    "sections": [{"label": p.label, "size": size(p)} for p in r.sections],
                    "history": {"size": size(r.history), "in_window": r.messages_in_window,
                                "total": r.messages_total}, "budget": r.history_tokens}
        if what == "stats":
            u = chat.usage
            return {"calls": u.calls, "tokens": u.tokens, "cost": round(u.cost, 4), "unpriced": sorted(u.unpriced)}
        if what == "models":
            cfg = engine.cfg
            tasks = []
            for name in cfg.tasks:
                try:
                    s = cfg.settings(name)
                    tasks.append({"task": name, "provider": s.provider, "model": s.model or "echo",
                                  "effort": s.effort, "thinking": s.thinking is not False,
                                  "max_tokens": s.max_tokens})
                except (KeyError, ConfigError) as e:
                    tasks.append({"task": name, "error": str(e)})
            return {"tasks": tasks, "override": self.model_override,
                    "catalog": [{"id": m, "vendor": spec.vendor} for m, spec in MODELS.items()]}
        raise ChatError(f"不能查看 {what}")


def _when(ts: int) -> str:
    dt = datetime.fromtimestamp(ts / 1000)
    return f"{dt:%H:%M}" if dt.date() == datetime.now().date() else f"{dt:%m-%d %H:%M}"


def run(cfg: Config, ref: str | None, user: str, nickname: str | None, host: str, port: int,
        open_browser: bool, ready=None) -> None:
    ui = WebUI(cfg, ref, user, nickname, host, port)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(ui.run(open_browser, ready))

