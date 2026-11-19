"""`rhine chat`: a conversation (local.LocalChat) in the terminal, with slash commands to steer
and look into it."""

from __future__ import annotations

import asyncio
import shlex
import signal
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    PathCompleter,
)
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

from .catalog import MODELS
from .colors import ACCENT, LINE, MUTED, SPEAKERS, THEME, WARN, person_color
from .config import ConfigError, resolve
from .engine import Engine, instance_db, instances
from .engine.context import context_report
from .engine.lookup import HISTORY_TOOL, SEARCH_TOOL
from .engine.media import IncomingMedia
from .engine.memory import ADD_TOOL, EDIT_TOOL, REMOVE_TOOL
from .engine.prompt import message_text
from .local import (
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
from .pack import PackError
from .providers import Done, TextDelta, ThinkingDelta, ToolUse, make_provider
from .store import StoredMessage, peek


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    desc: str
    group: str
    aliases: tuple[str, ...] = ()
    # What its first argument completes to (see ChatREPL.arguments), or "path" for a file.
    completes: str | None = None


COMMANDS: list[Command] = [
    Command("/quote", "<编号> <文字>", "引用一条消息再说话，编号见 /history", "对话", ("/引用",), "message"),
    Command("/recall", "[编号]", "撤回自己的一条消息，默认是最后一条", "对话", ("/撤回",), "own"),
    Command("/pat", "[角色]", "拍一拍角色", "对话", ("/拍一拍",), "cast"),
    Command("/img", "<路径> [文字]", "发一张图片，可以附一句话", "对话", ("/图片",), "path"),
    Command("/sticker", "<路径> [名字]", "发一个表情包，名字相当于聊天软件给它的描述", "对话", ("/表情",), "path"),
    Command("/system", "<文字>", "给角色一条系统指令：照着做，但不会提起", "对话"),
    Command("/retry", "", "让最后说话的角色重说一遍", "对话"),
    Command("/undo", "[n]", "删掉最后 n 条消息，默认 1 条", "对话"),
    Command("/rewind", "[n]", "回到前面某条消息之前，你的原话放回输入框", "对话"),
    Command("/name", "<昵称>", "改你的昵称，还是同一个人", "人和角色"),
    Command("/as", "<ID> [昵称]", "换成另一个人发言", "人和角色"),
    Command("/invite", "<角色>[:<标签>]", "把另一个角色拉进对话", "人和角色", (), "character"),
    Command("/kick", "<角色>", "把拉进来的角色移出对话", "人和角色", (), "guest"),
    Command("/instance", "[<角色>:<标签>]", "列出持久化实例；带名字时创建一个", "人和角色"),
    Command("/history", "[n]", "本会话的消息和编号", "会话"),
    Command("/resume", "[编号]", "回到以前的会话", "会话"),
    Command("/clear", "", "开一个新会话，旧的还在", "会话", ("/new",)),
    Command("/export", "[路径]", "把本会话导出成 Markdown", "会话", (), "path"),
    Command("/memory", "[角色]", "角色记的笔记和对话摘要", "笔记和情景", (), "cast"),
    Command("/forget", "<编号> [角色]", "删一条笔记（rhine memory restore 能恢复）", "笔记和情景"),
    Command("/scene", "[文字]", "加一段情景：这段聊天之前发生过的事；不带文字时列出", "笔记和情景"),
    Command("/unscene", "<编号>", "删一段情景", "笔记和情景"),
    Command("/status", "", "你、会话、角色和模型", "查看和设置", ("/who",)),
    Command("/model", "[模型]", "各任务的模型；带名字时本次运行换一个回复模型", "查看和设置", (), "model"),
    Command("/context", "", "提示词各部分和历史各占多少 token", "查看和设置"),
    Command("/stats", "", "本次运行的调用次数、token 和估算费用", "查看和设置", ("/cost",)),
    Command("/prompt", "", "完整的 system prompt", "查看和设置"),
    Command("/judge", "", "开关群聊筛选", "查看和设置"),
    Command("/think", "", "开关思考摘要的显示", "查看和设置"),
    Command("/usage", "", "开关每次回复后的用量显示", "查看和设置"),
    Command("/reload", "", "重新读取角色包", "查看和设置"),
    Command("/help", "", "这份帮助", "查看和设置", ("/?",)),
    Command("/quit", "", "退出", "查看和设置", ("/exit",)),
]
_BY_NAME = {n: c for c in COMMANDS for n in (c.name, *c.aliases)}

_TOOL_ACTIONS: dict[str, Callable[[dict], str]] = {
    "read_message": lambda i: f"读了长消息的第 {i.get('part')} 段",
    ADD_TOOL: lambda i: f"记下了：{i.get('text')}",
    EDIT_TOOL: lambda i: f"改了笔记 {i.get('id')}：{i.get('text')}",
    REMOVE_TOOL: lambda i: f"删了笔记 {i.get('id')}：{i.get('reason')}",
    HISTORY_TOOL: lambda i: f"去翻聊天记录了：{i.get('query') or '眼下在聊的'}",
    SEARCH_TOOL: lambda i: f"去查了：{i.get('query')}",
}
_STYLE = Style.from_dict({
    "prompt": f"{ACCENT} bold",
    "bottom-toolbar": f"noreverse {MUTED}",
    "bottom-toolbar.keys": f"noreverse {LINE}",
    "bottom-toolbar.hint": f"noreverse {WARN}",
    # The completion menu as the page's menus: a dark surface, the current entry in the accent.
    "completion-menu": "bg:#1d2026 #c4c8d0",
    "completion-menu.completion.current": "bg:#2b3250 #b1bdf3 bold",
    "completion-menu.meta.completion": "bg:#1d2026 #969ca8",
    "completion-menu.meta.completion.current": "bg:#2b3250 #b1bdf3",
    "scrollbar.background": "bg:#2d3139",
    "scrollbar.button": "bg:#48527f",
})
# Two Ctrl+C within this many seconds at an empty prompt quit.
_QUIT_WINDOW_S = 2.0


class _Usage(Exception):
    """A command written the wrong way: its usage line is shown."""


def _short(text: str, n: int = 30) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _number(arg: str) -> int | None:
    arg = arg.lstrip("#")
    return int(arg) if arg.isdigit() else None


class _Completer(Completer):
    """Slash commands with what they do, their arguments, and @names in messages."""

    def __init__(self, repl: ChatREPL):
        self.repl = repl
        self.paths = PathCompleter(expanduser=True)

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            word = document.get_word_before_cursor(WORD=True)
            if word.startswith("@"):
                for engine in self.repl.chat.cast:
                    name = f"@{name_of(engine)}"
                    if name.startswith(word):
                        yield Completion(name + " ", -len(word), display=name)
            return
        if " " not in text:
            for c in COMMANDS:
                # One line per command: its name, or the alias being typed.
                name = c.name if c.name.startswith(text) else next((a for a in c.aliases if a.startswith(text)), None)
                if name is not None:
                    others = " ".join(n for n in (c.name, *c.aliases) if n != name)
                    display = FormattedText([("", f"{name} {c.usage}".strip()), ("class:completion-menu.meta", f"  {others}")])
                    yield Completion(name, -len(text), display=display if others else name + (f" {c.usage}" if c.usage else ""),
                                     display_meta=c.desc)
            return
        head, rest = text.split(" ", 1)
        command = _BY_NAME.get(head)
        if command is None or command.completes is None or " " in rest:
            return
        if command.completes == "path":
            yield from self.paths.get_completions(Document(rest), complete_event)
            return
        for value, meta in self.repl.arguments(command.completes):
            if value.startswith(rest):
                yield Completion(value, -len(rest), display_meta=meta)


class ChatREPL:
    def __init__(self, engine: Engine, user: str, nickname: str, session: str | None, console: Console,
                 show_thinking: bool = True, history: History | None = None):
        self.chat = LocalChat(engine, user, nickname, session)
        self.console = console
        console.push_theme(THEME)
        self.show_usage = False
        self.show_thinking = show_thinking
        # Text placed in the next input line, e.g. a message brought back by /rewind.
        self.prefill = ""
        # The reply model chosen with /model for this run, if any.
        self.model_override: str | None = None
        self._ctrl_c = 0.0
        if history is None:
            try:
                engine.cfg.data_dir.mkdir(parents=True, exist_ok=True)
                history = FileHistory(str(engine.cfg.data_dir / "cli-history"))
            except OSError:
                history = InMemoryHistory()
        # prompt_toolkit handles wide (CJK) characters, input methods and history, which input() does not.
        self.session: PromptSession[str] = PromptSession(
            history=history,
            completer=_Completer(self),
            complete_while_typing=True,
            auto_suggest=AutoSuggestFromHistory(),
            key_bindings=self._keys(),
            bottom_toolbar=self._toolbar,
            style=_STYLE,
            reserve_space_for_menu=6,
        )
        for problem in self.chat.problems:
            self.error(problem)

    @property
    def engine(self) -> Engine:
        return self.chat.engine

    @property
    def chat_id(self) -> str:
        return self.chat.chat_id

    def color(self, engine: Engine) -> str:
        """The character you started with takes the first hue, invited ones the next ones."""
        if engine is self.chat.engine:
            return SPEAKERS[0]
        guests = list(self.chat.guests.values())
        return SPEAKERS[1 + guests.index(engine) % (len(SPEAKERS) - 1)] if engine in guests else MUTED

    def error(self, text: str) -> None:
        self.console.print(Text(text, style="err"))

    def note(self, text: str) -> None:
        self.console.print(Text(text, style="dim"))

    def model_name(self) -> str:
        if self.model_override:
            return self.model_override
        try:
            return self.engine.cfg.settings("roleplay").model or "echo"
        except (KeyError, ConfigError):
            return "?"

    # --- input ----------------------------------------------------------------------

    def _keys(self) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("enter")
        def _(event) -> None:
            buf = event.current_buffer
            state = buf.complete_state
            if state is not None and state.current_completion is not None:
                buf.apply_completion(state.current_completion)
            elif buf.document.text_before_cursor.endswith("\\"):
                buf.delete_before_cursor(1)
                buf.insert_text("\n")
            else:
                buf.validate_and_handle()

        @keys.add("escape", "enter")
        @keys.add("c-j")
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        @keys.add("c-c")
        def _(event) -> None:
            buf = event.current_buffer
            if buf.text:
                buf.reset()
                return
            now = time.monotonic()
            if now - self._ctrl_c < _QUIT_WINDOW_S:
                event.app.exit(exception=EOFError())
                return
            self._ctrl_c = now
            event.app.invalidate()

            async def clear_hint() -> None:
                await asyncio.sleep(_QUIT_WINDOW_S)
                event.app.invalidate()

            event.app.create_background_task(clear_hint())

        return keys

    def _toolbar(self) -> FormattedText:
        chat = self.chat
        kind = "群聊" if chat.kind == "group" else "私聊"
        cost = f" · ${chat.usage.cost:.4f}" if chat.usage.cost else ""
        parts = [
            ("class:bottom-toolbar", f" {chat.label()} · {kind} · {self.model_name()}{cost} · 以「{chat.nickname}」发言"),
            ("class:bottom-toolbar.keys", "   ⏎ 发送 · Alt+⏎ 换行 · / 命令"),
        ]
        if time.monotonic() - self._ctrl_c < _QUIT_WINDOW_S:
            parts.append(("class:bottom-toolbar.hint", "   再按一次 Ctrl+C 退出"))
        return FormattedText(parts)

    def arguments(self, kind: str) -> list[tuple[str, str]]:
        """What a command's argument can be, each with a word on it, for completion."""
        chat = self.chat
        cfg = chat.engine.cfg
        if kind == "character":
            ids = sorted(p.parent.name for p in cfg.characters_dir.glob("*/character.yaml"))
            return [(i, "") for i in ids] + [(f"{c}:{t}", "实例") for c, t in instances(cfg)]
        if kind in ("cast", "guest"):
            engines = chat.cast if kind == "cast" else list(chat.guests.values())
            return [(e.ref, name_of(e)) for e in engines]
        if kind == "model":
            return [(m, spec.vendor) for m, spec in MODELS.items()]
        if kind in ("message", "own"):
            out = []
            for m in reversed(chat.messages(30)):
                if kind == "own" and (m.sender_id != chat.sender_id or m.recalls is not None):
                    continue
                if m.recalls is not None:
                    continue
                out.append((str(m.id), _short(f"{self.who(m)}：{message_text(m)}", 40)))
            return out
        return []

    async def run(self) -> None:
        self.welcome()
        while True:
            try:
                default, self.prefill = self.prefill, ""
                line = await self.session.prompt_async(FormattedText([("class:prompt", "› ")]), default=default)
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                return
            line = line.strip()
            if line.startswith("/"):
                if await self.command(line) is False:
                    return
            elif line:
                await self.send(line)
            elif self.chat.kind == "group":
                await self.show(self.chat.nudge())

    def welcome(self) -> None:
        chat = self.chat
        c = chat.engine.pack.character
        lines = [Text.assemble(("✻ ", "accent"), (c.name, "title"), (f"  {chat.engine.ref}", "dim"))]
        if c.identity:
            lines.append(Text(_short(c.identity, 72), style="dim"))
        try:
            s = chat.engine.cfg.settings("roleplay")
            model = f"{s.model or 'echo'}（{s.provider}）"
        except (KeyError, ConfigError) as e:
            model = f"没法用：{e}"
        lines += [
            Text(""),
            Text.assemble(("会话  ", "dim"), f"{chat.chat_id} · {chat.label()}"),
            Text.assemble(("模型  ", "dim"), model),
            Text(""),
            Text("/ 看命令 · Tab 补全 · Alt+⏎ 换行 · 生成时 Ctrl+C 打断 · Ctrl+D 退出", style="dim"),
        ]
        self.console.print(Panel(Text("\n").join(lines), title=Text(" ohMyRhine ", style="muted"), title_align="left",
                                 border_style="line", expand=False, padding=(0, 2)))
        recent = chat.messages(6)
        if recent:
            total = chat.engine.store.count_messages(chat.chat_id)
            self.note(f"接着上次的会话（共 {total} 条消息），最近几条：")
            for m in recent:
                self.print_message(m)
            self.console.print()

    # --- the conversation -----------------------------------------------------------

    async def send(self, text: str, media: tuple[IncomingMedia, ...] = (), quote: StoredMessage | None = None) -> None:
        await self.show(self.chat.say(text, media, quote))

    async def show(self, events: AsyncIterator[Event]) -> None:
        """Show what happens while characters answer. Ctrl+C stops it; a reply cut short is not kept."""
        task = asyncio.ensure_future(self._render(events))
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, task.cancel)
            handled = True
        except (NotImplementedError, RuntimeError, ValueError):  # not on this platform or thread
            handled = False
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
            self.console.print()
            self.note("（打断了，没说完的回复不会留下）")
        except ChatError as e:
            self.error(str(e))
        finally:
            if handled:
                loop.remove_signal_handler(signal.SIGINT)

    async def _render(self, events: AsyncIterator[Event]) -> None:
        status: Status | None = None
        # What was printed last for the character speaking: None, "thinking", "text" or "tool".
        mode: str | None = None
        streamed = False

        def spin(text: str) -> None:
            nonlocal status
            stop()
            status = self.console.status(Text(text, style="dim"), spinner="dots", spinner_style="accent")
            status.start()

        def stop() -> None:
            nonlocal status
            if status is not None:
                status.stop()
                status = None

        try:
            async for event in events:
                if isinstance(event, Speaking):
                    mode, streamed = None, False
                    spin(f"{name_of(event.engine)}正在输入…")
                elif isinstance(event, Looking):
                    spin(f"{name_of(event.engine)}在查…")
                elif isinstance(event, Streamed):
                    stop()
                    streamed = True
                    mode = self._stream(event.engine, event.event, mode)
                elif isinstance(event, Said):
                    stop()
                    if mode in ("thinking", "text"):
                        self.console.print()
                    elif not streamed:
                        self.note(f"  （{name_of(event.engine)}没有新的消息可回）")
                    mode = None
                elif isinstance(event, Judged):
                    stop()
                    self.show_decision(event)
                elif isinstance(event, Failed):
                    stop()
                    if mode is not None:
                        self.console.print()
                        mode = None
                    who = name_of(event.engine) if event.engine is not None and self.chat.guests else ""
                    what = {"reply": "生成", "judge": "判断", "summary": "更新对话摘要"}.get(event.what, event.what)
                    self.error(f"{who}{what}失败：{type(event.error).__name__}: {event.error}")
        finally:
            stop()

    def _stream(self, engine: Engine, ev: object, mode: str | None) -> str | None:
        """Print one piece of a reply; returns what was printed last."""
        out = self.console.print
        name = name_of(engine)
        if isinstance(ev, ThinkingDelta):
            if not self.show_thinking:
                return mode
            if mode != "thinking":
                if mode is not None:
                    out()
                out(Text("✻ 思考", style="dim"))
                out("  ", end="")
            out(Text(ev.text.replace("\n", "\n  "), style="dim italic"), end="", soft_wrap=True)
            return "thinking"
        if isinstance(ev, ToolUse):
            if mode in ("thinking", "text"):
                out()
            action = _TOOL_ACTIONS.get(ev.name, lambda _: f"用了 {ev.name}")(ev.input)
            out(Text(f"  ⎿ {name}{action}", style="dim"))
            return "tool"
        if isinstance(ev, TextDelta):
            if mode != "text":
                if mode == "thinking":
                    out()
                color = self.color(engine)
                out(Text("● ", style=color) + Text(name, style=f"bold {color}"))
                out("  ", end="")
            out(Text(ev.text.replace("\n", "\n  ")), end="", soft_wrap=True)
            return "text"
        if isinstance(ev, Done):
            if mode in ("thinking", "text"):
                out()
            if ev.stop_reason == "silent":
                self.note(f"  （{name}没有回复）")
            elif ev.stop_reason == "refusal":
                out(Text("  （模型拒绝了这次回复，没有记进历史）", style="warn"))
            elif ev.stop_reason == "max_tokens":
                out(Text("  （回复被 max_tokens 截断了）", style="warn"))
            if self.show_usage and ev.usage:
                self.note(f"  ↳ {self.format_usage(ev)}")
            return None
        return mode

    def show_decision(self, event: Judged) -> None:
        d = event.decision
        verdict = ("接话" if d.reply else "不接话") + ("，话还没说完" if d.wait else "")
        addressed = "，在跟角色说话" if d.addressed else ""
        self.note(f"  ⋯ {name_of(event.engine)}的判断：{verdict}{addressed} · {d.reason}")

    def format_usage(self, done: Done) -> str:
        u = done.usage
        text = (f"{done.model} · 输入 {u.get('input', 0)} · 输出 {u.get('output', 0)}"
                f" · 缓存读 {u.get('cache_read', 0)} · 缓存写 {u.get('cache_write', 0)}")
        price = self.engine.cfg.price(done.model)
        return text + (f" · ${price.cost(u):.4f}" if price else "")

    def who(self, m: StoredMessage) -> str:
        speaker = self.chat.speaker_of(m)
        return name_of(speaker) if speaker is not None else (m.sender_name or "？")

    def print_message(self, m: StoredMessage, descriptions: dict[str, str | None] | None = None) -> None:
        """One stored message with its number, as /history and the welcome show it."""
        speaker = self.chat.speaker_of(m)
        if speaker is not None:
            color = f"bold {self.color(speaker)}"
        elif m.sender_id == self.chat.sender_id:
            color = "bold"
        else:
            color = f"bold {person_color(m.sender_id)}"
        when = datetime.fromtimestamp(m.ts / 1000).strftime("%m-%d %H:%M")
        text = message_text(m, descriptions if descriptions is not None else self.engine.descriptions([m]))
        line = Text.assemble((f"#{m.id:<5}", "accent"), (f"{when}  ", "dim"), (self.who(m), color), "  ")
        line.append(text.replace("\n", "\n" + " " * 20), style="dim" if m.recalls is not None else "")
        self.console.print(line)

    # --- commands -------------------------------------------------------------------

    async def command(self, line: str) -> bool | None:
        """Run a slash command. Returns False to quit."""
        try:
            head, *args = shlex.split(line)
        except ValueError:
            head, *args = line.split()
        command = _BY_NAME.get(head)
        if command is None:
            self.error(f"没有 {head} 这个命令，/help 查看所有命令")
            return None
        parts = line.split(None, 1)
        rest = parts[1].strip() if len(parts) > 1 else ""
        try:
            return await self._run(command.name, args, rest)
        except ChatError as e:
            self.error(str(e))
        except _Usage:
            self.error(f"用法：{command.name} {command.usage}")
        return None

    async def _run(self, cmd: str, args: list[str], rest: str) -> bool | None:
        chat = self.chat
        match cmd:
            case "/quit":
                return False
            case "/help":
                self.show_help()
            case "/status":
                self.show_status()
            case "/name":
                if not rest:
                    raise _Usage
                chat.nickname = rest
                self.note(f"昵称改为 {rest}，ID 仍是 {chat.sender_id}，会话不变")
            case "/as":
                if not args:
                    raise _Usage
                was = chat.kind
                chat.speak_as(args[0], args[1] if len(args) > 1 else None)
                self.note(f"现在以 {chat.nickname}（ID {chat.sender_id}）发言，会话不变")
                if was == "private" and chat.kind == "group":
                    self.note(f"对话里不止两个人了，{chat.name}会像在群里一样，自己判断要不要接话")
            case "/quote":
                n = _number(args[0]) if args else None
                text = rest.split(None, 1)[1].strip() if len(rest.split(None, 1)) > 1 else ""
                if n is None or not text:
                    raise _Usage
                target = chat.message(n)
                self.note(f"  ↪ 回复 {self.who(target)}：{_short(message_text(target), 40)}")
                await self.send(text, quote=target)
            case "/recall":
                n = _number(args[0]) if args else None
                if args and n is None:
                    raise _Usage
                target = chat.recallable(chat.message(n) if n is not None else None)
                self.note(f"（你撤回了一条消息：{_short(message_text(target), 30)}）")
                await self.show(chat.recall(target))
            case "/pat":
                target = chat.character(args[0]) if args else chat.engine
                if target is None:
                    raise ChatError(f"{args[0]} 不在对话里")
                self.note(f"（你拍了拍「{name_of(target)}」）")
                await self.show(chat.pat(target))
            case "/img" | "/sticker":
                if not args:
                    raise _Usage
                await self.send_media(cmd[1:], args)
            case "/system":
                if not rest:
                    raise _Usage
                await self.show(chat.instruct(rest))
            case "/retry":
                await self.show(chat.retry())
            case "/undo":
                if args and not args[0].isdigit():
                    raise _Usage
                for m in chat.undo(int(args[0]) if args else 1):
                    self.note(f"已删除 #{m.id} {self.who(m)}：{_short(message_text(m), 40)}")
            case "/rewind":
                await self.rewind(args[0] if args else None)
            case "/invite":
                if not args:
                    raise _Usage
                guest = chat.invite(args[0])
                remembers = "，带着以前的记忆" if guest.instance else ""
                self.note(f"{name_of(guest)}（{guest.ref}）加入了对话，只看得到从现在开始的消息{remembers}")
                await self.show(chat.announce(guest, joined=True))
            case "/kick":
                if not args:
                    raise _Usage
                guest = chat.kick(args[0])
                self.note(f"{name_of(guest)}（{guest.ref}）离开了对话")
                await self.show(chat.announce(guest, joined=False))
                if chat.kind == "private":
                    self.note(f"又只剩你和{chat.name}两个人了，每句话都会交给角色")
            case "/instance":
                if args:
                    engine = chat.new_instance(args[0])
                    self.note(f"已创建{name_of(engine)}的持久化实例 {args[0]}，长期记忆跨会话保留。"
                              f"/invite {args[0]} 拉进对话，或者 rhine chat {args[0]} 单独聊")
                else:
                    self.show_instances()
            case "/history":
                messages = chat.messages(int(args[0]) if args and args[0].isdigit() else None)
                if not messages:
                    self.note("本会话还没有消息")
                descriptions = self.engine.descriptions(messages)
                for m in messages:
                    self.print_message(m, descriptions)
            case "/resume":
                await self.resume(args[0] if args else None)
            case "/clear":
                left = chat.clear()
                if left:
                    self.note(f"{'、'.join(left)}留在原来的会话里")
                self.note(f"开了一个新会话 {chat.chat_id}。原来的会话还在，/resume 可以回去")
            case "/export":
                self.export(Path(rest).expanduser() if rest else None)
            case "/memory":
                if args and chat.character(args[0]) is None:
                    raise ChatError(f"{args[0]} 不在对话里")
                for engine in [chat.character(args[0])] if args else chat.cast:
                    if engine is not None:
                        self.show_memory(engine)
            case "/forget":
                if not args or not args[0].isdigit():
                    raise _Usage
                engine = chat.character(args[1]) if len(args) > 1 else chat.engine
                if engine is None:
                    raise ChatError(f"{args[1]} 不在对话里")
                scope = engine.memory.scope(chat.chat_of(engine))
                if not engine.store.remove_memory(scope, int(args[0]), int(time.time() * 1000), "owner"):
                    raise ChatError(f"{name_of(engine)}没有编号为 {args[0]} 的笔记，/memory 查看")
                self.note(f"已删除{name_of(engine)}的笔记 {args[0]}")
            case "/scene":
                if not rest:
                    self.show_scenes()
                else:
                    sid = self.engine.store.add_scene(chat.chat_id, rest, int(time.time() * 1000))
                    self.note(f"已添加情景 {sid}，{chat.name}会把它当作这段聊天之前发生过的事")
            case "/unscene":
                if not args or not args[0].isdigit():
                    raise _Usage
                if not self.engine.store.delete_scene(chat.chat_id, int(args[0])):
                    raise ChatError(f"本会话没有编号为 {args[0]} 的情景")
                self.note(f"已删除情景 {args[0]}")
            case "/model":
                self.model(args[0] if args else None)
            case "/context":
                await self.show_context()
            case "/stats":
                self.show_stats()
            case "/prompt":
                with self.console.pager():
                    self.console.print(self.engine.system, markup=False, highlight=False)
            case "/judge":
                cfg = self.engine.cfg
                if not cfg.has_task("reply_judge"):
                    self.note("config.yaml 里没有配置 reply_judge 任务，人多时只按 @ 和名字触发")
                    return None
                cfg.group.judge = not cfg.group.judge
                self.note("筛选：开。人多时没人 @ 的消息先由小模型筛一遍" if cfg.group.judge
                          else "筛选：关。人多时只有 @ 或叫到名字才回复")
            case "/think":
                self.show_thinking = not self.show_thinking
                state = "开" if self.show_thinking else "关"
                if self.engine.cfg.log_thinking:
                    self.note(f"思考显示：{state}。思考照常记录到 {self.engine.thinking_log}")
                else:
                    self.note(f"思考显示：{state}。不过 config.yaml 里 log_thinking 是关的，不会有思考内容")
            case "/usage":
                self.show_usage = not self.show_usage
                self.note(f"用量显示：{'开' if self.show_usage else '关'}")
            case "/reload":
                for engine in chat.cast:
                    who = f"{name_of(engine)}的" if chat.guests else ""
                    try:
                        changed = engine.reload()
                    except PackError as e:
                        self.error(f"{who}角色包有问题，继续使用旧版本：\n{e}")
                        continue
                    if changed:
                        self.console.print(Text(f"已重新加载{who}角色包", style="ok"))
                    else:
                        self.note(f"{who}角色包没有变化")
        return None

    async def send_media(self, kind: str, args: list[str]) -> None:
        path = Path(args[0]).expanduser()
        if not path.is_file():
            raise ChatError(f"找不到文件：{path}")
        rest = " ".join(args[1:])
        if kind == "sticker":
            await self.send("", (IncomingMedia("sticker", path.read_bytes(), hint=rest or None),))
        else:
            await self.send(rest, (IncomingMedia("image", path.read_bytes()),))

    async def rewind(self, choice: str | None) -> None:
        chat = self.chat
        limit = max(10, int(choice)) if choice and choice.isdigit() else 10
        points = chat.rewind_points(limit)
        if not points:
            self.note("本会话还没有可以回退的消息")
            return
        if choice is None:
            for i, m in enumerate(points, 1):
                self.console.print(Text.assemble((f"  {i:>2} ", "accent"), f"{m.sender_name}：{_short(m.content, 30)}"))
            choice = (await self.session.prompt_async("回退到哪一条之前？输入编号，直接回车取消：")).strip()
            if not choice:
                self.note("已取消")
                return
        if not choice.isdigit() or not 1 <= int(choice) <= len(points):
            raise ChatError(f"编号要在 1 到 {len(points)} 之间")
        target = points[int(choice) - 1]
        removed = chat.rewind(target)
        self.note(f"已回退到这条消息之前，删除了 {len(removed)} 条消息")
        if target.sender_id == chat.sender_id:
            self.prefill = target.content
            self.note("原话已放回输入框，可以改了再发")
        elif chat.speaker_of(target) is not None:
            self.note(f"那条消息是 {target.sender_name} 说的，直接回车让{chat.name}接话，或者自己说点什么")
        else:
            self.note(f"那条消息是 {target.sender_name} 发的；要重写的话，先用 /as 换成对方的身份")

    async def resume(self, choice: str | None) -> None:
        chat = self.chat
        store = self.engine.store
        sessions = [(c, ts) for c, ts in chat.sessions() if c != chat.chat_id][:20]
        if not sessions:
            self.note("没有别的会话可以接着聊")
            return
        if choice is None:
            table = Table(box=None, pad_edge=False, show_header=False)
            for i, (chat_id, ts) in enumerate(sessions, 1):
                last = store.recent_messages(chat_id, 1)
                preview = ""
                if last:
                    who = chat.name if last[0].role == "assistant" else last[0].sender_name
                    preview = _short(f"{who}：{last[0].content}", 30)
                table.add_row(Text(f"{i:>2}", style="accent"), chat.label(chat_id), f"{store.count_messages(chat_id)} 条",
                              Text(f"{datetime.fromtimestamp(ts / 1000):%m-%d %H:%M}", style="dim"),
                              Text(preview, style="dim"))
            self.console.print(table)
            choice = (await self.session.prompt_async("接着聊哪一个？输入编号，直接回车取消：")).strip()
            if not choice:
                self.note("已取消")
                return
        if not choice.isdigit() or not 1 <= int(choice) <= len(sessions):
            raise ChatError(f"编号要在 1 到 {len(sessions)} 之间")
        for notice in chat.resume(sessions[int(choice) - 1][0]):
            self.note(notice)
        self.note(f"回到了 {chat.chat_id}，共 {store.count_messages(chat.chat_id)} 条消息，/history 查看")
        if chat.guests:
            self.note(f"对话里还有：{'、'.join(name_of(e) for e in chat.guests.values())}")

    def model(self, name: str | None) -> None:
        cfg = self.engine.cfg
        if name is None:
            table = Table(box=None, pad_edge=False)
            for col in ("任务", "供应商", "模型", "effort", "思考", "输出上限"):
                table.add_column(col, style="bold" if col == "任务" else "")
            for task in cfg.tasks:
                try:
                    s = cfg.settings(task)
                except (KeyError, ConfigError) as e:
                    table.add_row(task, "", Text(str(e), style="err"), "", "", "")
                    continue
                model = Text(s.model or "echo")
                if task == "roleplay" and self.model_override:
                    model = Text.assemble(self.model_override, ("（本次运行换的）", "dim"))
                table.add_row(task, s.provider, model, s.effort or "", "关" if s.thinking is False else "开",
                              str(s.max_tokens))
            self.console.print(table)
            self.note("/model <模型> 在本次运行里换一个回复模型，config.yaml 不变")
            return
        task = cfg.tasks["roleplay"].model_copy(update={"model": name})
        try:
            settings = resolve(cfg, "roleplay", task)
        except (ConfigError, ValueError) as e:
            raise ChatError(str(e)) from None
        self.engine._provider = make_provider(settings)
        configured = cfg.tasks["roleplay"].model
        self.model_override = None if name == configured else name
        self.note(f"本次运行的回复改用 {name}（{settings.provider}）")

    def export(self, path: Path | None) -> None:
        chat = self.chat
        messages = self.engine.store.messages_between(chat.chat_id, 0)
        if not messages:
            raise ChatError("本会话还没有消息")
        path = path or self.engine.cfg.data_dir / "exports" / f"{chat.chat_id.replace(':', '-')}.md"
        descriptions = self.engine.descriptions(messages)
        lines = [f"# {chat.label()}", "", f"会话 {chat.chat_id}，共 {len(messages)} 条消息", ""]
        for m in messages:
            when = datetime.fromtimestamp(m.ts / 1000).strftime("%Y-%m-%d %H:%M")
            text = message_text(m, descriptions).replace("\n", "  \n")
            lines.append(f"**{self.who(m)}** · {when}  \n{text}\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        self.note(f"已导出到 {path}")

    # --- looking into it ------------------------------------------------------------

    def show_help(self) -> None:
        groups: dict[str, list[Command]] = {}
        for c in COMMANDS:
            groups.setdefault(c.group, []).append(c)
        for group, commands in groups.items():
            table = Table(box=None, pad_edge=False, show_header=False, title=group, title_justify="left",
                          title_style="bold")
            table.add_column(style="accent", no_wrap=True)
            table.add_column(style="dim", no_wrap=True)
            table.add_column()
            for c in commands:
                name = Text(c.name)
                if c.aliases:
                    name.append(" " + " ".join(c.aliases), style="dim")
                table.add_row(name, c.usage, c.desc)
            self.console.print(table)
        rule = "其余的先由小模型筛一遍（/judge 关掉）" if self.engine.judging else "其余的只在叫到名字时回复"
        self.note(f"只有你和角色两个人时，每句话都交给角色。人多时 @ 角色一定回复，{rule}，直接回车让角色说话。")
        self.note("⏎ 发送 · Alt+⏎、Ctrl+J 或行末的 \\ 换行 · Tab 补全 · ↑ 翻历史 · 生成时 Ctrl+C 打断 · Ctrl+D 退出")

    def show_status(self) -> None:
        chat = self.chat
        table = Table(box=None, pad_edge=False, show_header=False)
        table.add_column(style="dim")
        table.add_column()
        table.add_row("你", f"{chat.nickname}（ID {chat.sender_id}）")
        table.add_row("会话", f"{chat.chat_id} · {'群聊' if chat.kind == 'group' else '私聊'} · {chat.label()}")
        spans = " · 持久化实例，长期记忆跨会话" if self.engine.instance else ""
        table.add_row("角色", f"{chat.name}（{self.engine.ref}）{spans}")
        if chat.guests:
            table.add_row("拉进来的", "、".join(f"{name_of(e)}（{ref}）" for ref, e in chat.guests.items()))
        table.add_row("群聊筛选", "开" if self.engine.judging else "关")
        table.add_row("思考显示", "开" if self.show_thinking else "关")
        table.add_row("数据目录", str(self.engine.cfg.data_dir))
        self.console.print(table)
        self.console.print()
        self.model(None)

    def show_instances(self) -> None:
        cfg = self.engine.cfg
        found = instances(cfg)
        if not found:
            self.note("还没有持久化实例，用 /instance <角色>:<标签> 创建")
            return
        for char_id, tag in found:
            summary = peek(instance_db(cfg, char_id, tag))
            name = (summary.name if summary else None) or "?"
            count = f"{summary.messages} 条消息" if summary else ""
            self.console.print(Text.assemble((f"  {char_id}:{tag} ", "accent"), name, (f"  {count}", "dim")))

    async def show_context(self) -> None:
        try:
            r = await context_report(self.engine, self.chat_id)
        except Exception as e:
            raise ChatError(f"统计失败：{type(e).__name__}: {e}") from None
        counted = r.total_tokens is not None

        def size(p) -> int:
            return p.tokens if counted and p.tokens is not None else p.chars

        system_size = sum(size(p) for p in r.sections)
        grand = system_size + size(r.history) or 1

        def bar(n: int, width: int = 24) -> Text:
            filled = round(n / grand * width)
            return Text.assemble(("█" * filled, "accent"), ("░" * (width - filled), "dim"))

        head = Text.assemble(("上下文", "bold"), f" · {self.model_name()}")
        head.append(f" · 共 {r.total_tokens:,} tokens" if counted else " · 这个接口不能计数，下面是字符数")
        self.console.print(head)
        table = Table(box=None, pad_edge=False, show_header=False)
        for justify in ("left", "right", "left", "right"):
            table.add_column(justify=justify)
        table.add_row("system prompt", f"{system_size:,}", bar(system_size), f"{system_size / grand:.0%}")
        for p in r.sections:
            table.add_row(Text(f"  {p.label}", style="dim"), Text(f"{size(p):,}", style="dim"), "", "")
        h = size(r.history)
        table.add_row(f"历史（{r.messages_in_window}/{r.messages_total} 条）", f"{h:,}", bar(h), f"{h / grand:.0%}")
        self.console.print(table)
        if r.messages_total > r.messages_in_window:
            self.console.print(Text(f"本会话共 {r.messages_total} 条消息，现在送给模型 {r.messages_in_window} 条；"
                                    "更早的由对话摘要接手。", style="warn"))
        unit = "tokens" if counted else "字符"
        self.note(f"历史预算约 {r.history_tokens:,} token（估算，config.yaml 的 history_tokens）· 单位：{unit}")
        if self.chat.usage.last and self.chat.usage.last.usage:
            self.note(f"上一次调用：{self.format_usage(self.chat.usage.last)}")

    def show_memory(self, engine: Engine) -> None:
        chat = self.chat
        color = self.color(engine)
        spans = "，长期记忆跨会话" if engine.instance else ""
        self.console.print(Text.assemble((name_of(engine), f"bold {color}"), (f"（{engine.ref}{spans}）", "dim")))
        chat_id = chat.chat_of(engine)
        summary, _ = engine.store.summary(chat_id)
        memories = engine.store.memories(engine.memory.scope(chat_id))
        if not summary and not memories:
            self.note(f"  {'还' if engine.instance else '本会话还'}没有笔记和摘要")
            return
        if summary:
            self.console.print(Text("  对话摘要", style="bold"))
            self.console.print(Text("  " + summary.replace("\n", "\n  "), style="dim"))
        if memories:
            self.console.print(Text("  笔记", style="bold"))
        keeper = engine.memory
        for m in memories:
            about = f"关于{keeper.label(m.subject_id)}" if m.subject_id else "本会话" if m.chat_id == chat_id else "另一次聊天"
            self.console.print(Text.assemble((f"  {m.id:>3} ", "accent"), (f"{about}  ", "dim"), m.text))

    def show_scenes(self) -> None:
        scenes = self.engine.store.scenes(self.chat_id)
        if not scenes:
            self.note("本会话还没有情景，用 /scene <文字> 添加")
            return
        for sc in scenes:
            self.console.print(Text.assemble((f"  {sc.id:>3} ", "accent"), sc.text))

    def show_stats(self) -> None:
        u = self.chat.usage
        if not u.calls:
            self.note("本次运行还没有调用过模型")
            return
        t = u.tokens
        prompt_total = t.get("input", 0) + t.get("cache_read", 0) + t.get("cache_write", 0)
        hit = t.get("cache_read", 0) / prompt_total if prompt_total else 0
        self.console.print(Text.assemble(("本次运行", "bold"), f" · {u.calls} 次调用"))
        self.console.print(f"  输入 {t.get('input', 0):,} · 缓存读 {t.get('cache_read', 0):,}"
                           f" · 缓存写 {t.get('cache_write', 0):,} · 输出 {t.get('output', 0):,}", highlight=False)
        self.console.print(f"  缓存命中率 {hit:.0%}", highlight=False)
        cost = f"  估算费用 ${u.cost:.4f}"
        if u.unpriced:
            cost += f"（不含没有价格的模型：{', '.join(sorted(u.unpriced))}）"
        self.console.print(cost, highlight=False)
        self.note("按内置价格表估算，可在 config.yaml 的 models.<模型>.price 里改；以各家控制台的账单为准")
