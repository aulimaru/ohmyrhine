"""`rhine` command line."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import typer
import typer.rich_utils
import yaml
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .catalog import HARNESSES, MODELS, ROLES
from .chat_repl import ChatREPL
from .colors import ACCENT, ERR, MUTED, THEME
from .config import Config, ConfigError, load_config, resolve
from .engine import Engine, create_instance, instance_db, instances, split_ref
from .engine.prompt import display_id, format_ts
from .pack import PackError, create_pack, load_pack, pack_hash
from .store import Memory, peek

app = typer.Typer(
    no_args_is_help=True, add_completion=False, rich_markup_mode="rich",
    help="ohMyRhine：给聊天角色一个会记事、会看场合说话的大脑。\n\n"
         "先 [bold]rhine new[/bold] 写一个角色，再 [bold]rhine chat[/bold] 或 [bold]rhine web[/bold] 和角色聊。",
)
console = Console(theme=THEME)
CHAT, PACKS, SETUP, LIVE = "聊天", "角色包", "配置", "运行中的角色"

# The help screens in the same colours as everything else (colors.py).
_ru = typer.rich_utils
_ru.STYLE_OPTION = _ru.STYLE_SWITCH = _ru.STYLE_NEGATIVE_OPTION = _ru.STYLE_NEGATIVE_SWITCH = f"bold {ACCENT}"
_ru.STYLE_COMMANDS_TABLE_FIRST_COLUMN = f"bold {ACCENT}"
_ru.STYLE_TYPES = _ru.STYLE_USAGE = MUTED
_ru.STYLE_OPTION_ENVVAR = f"dim {MUTED}"
_ru.STYLE_DEPRECATED = _ru.STYLE_REQUIRED_SHORT = _ru.STYLE_ERRORS_PANEL_BORDER = _ru.STYLE_ABORTED = ERR
_ru.STYLE_REQUIRED_LONG = f"dim {ERR}"


def _version(value: bool) -> None:
    if value:
        try:
            console.print(f"ohMyRhine {version('ohmyrhine')}")
        except PackageNotFoundError:
            console.print("ohMyRhine（未安装的源码）")
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[bool, typer.Option("--version", "-V", callback=_version, is_eager=True,
                                               help="版本号")] = False,
):
    pass

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="config.yaml 路径")]


def _config(config: Path | None) -> Config:
    try:
        return load_config(config)
    except (FileNotFoundError, ConfigError) as e:
        console.print(f"[err]{e}[/err]")
        raise typer.Exit(1) from None


def _engine(ref: str, config: Path | None, dice: bool | None = None) -> Engine:
    """A character, or one of its persistent instances ("<character>:<tag>")."""
    cfg = _config(config)
    char_id, tag = split_ref(ref)
    if tag is not None and not instance_db(cfg, char_id, tag).is_file():
        console.print(f"[err]没有实例 {ref}，先用 rhine new {ref} 创建[/err]", highlight=False)
        raise typer.Exit(1)
    try:
        return Engine(cfg, char_id, dice=dice, instance=tag)
    except PackError as e:
        console.print(f"[err]角色包有问题：[/err]\n{e}")
        raise typer.Exit(1) from None


def _packs(cfg: Config) -> list[tuple[str, str, str]]:
    """(id, name, identity) of each character pack, read just far enough to list them."""
    out = []
    for path in sorted(cfg.characters_dir.glob("*/character.yaml")):
        name, identity = path.parent.name, ""
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            raw = None
        if isinstance(raw, dict):
            name, identity = str(raw.get("name") or name), " ".join(str(raw.get("identity") or "").split())
        out.append((path.parent.name, name, identity))
    return out


def _choose_character(cfg: Config) -> str:
    """The character to talk to when none was named: the only one, or one chosen from a list."""
    packs = _packs(cfg)
    if not packs:
        console.print(f"[err]{cfg.characters_dir} 下还没有角色[/err]，先用 rhine new <ID> --name <名字> 写一个")
        raise typer.Exit(1)
    if len(packs) == 1:
        return packs[0][0]
    if not sys.stdin.isatty():
        console.print("[err]有好几个角色，说一下和谁聊：rhine chat <角色>[/err]")
        raise typer.Exit(1)
    table = Table(box=None, pad_edge=False, show_header=False)
    for i, (char_id, name, identity) in enumerate(packs, 1):
        table.add_row(Text(f"{i:>2}", style="accent"), Text(name, style="bold"), Text(char_id, style="dim"),
                      Text(identity[:48] + ("…" if len(identity) > 48 else ""), style="dim"))
    console.print(table)
    while True:
        choice = typer.prompt("和谁聊？输入编号或 ID").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(packs):
            return packs[int(choice) - 1][0]
        if any(choice == p[0] for p in packs) or ":" in choice:
            return choice
        console.print(f"[err]编号要在 1 到 {len(packs)} 之间，或者写角色 ID[/err]")


@app.command(rich_help_panel=CHAT)
def chat(
    char_id: Annotated[str | None, typer.Argument(help="角色 ID；<角色>:<标签> 是角色的持久化实例。不写时从列表里选")] = None,
    user: Annotated[str, typer.Option("--as", help="你的用户 ID")] = "me",
    nickname: Annotated[str | None, typer.Option("--name", help="你的昵称（默认同 ID）")] = None,
    session: Annotated[
        str | None, typer.Option("--session", "-s", help="进入（或新建）这个名字的会话；不写时回到最近的会话")
    ] = None,
    hide_thinking: Annotated[
        bool, typer.Option("--hide-thinking", help="不显示思考摘要（仍按 config 记录到日志）")
    ] = False,
    config: ConfigOpt = None,
):
    """在终端里和角色聊天：/ 看命令，Tab 补全，Ctrl+C 打断回复。"""
    engine = _engine(char_id or _choose_character(_config(config)), config)
    repl = ChatREPL(engine, user, nickname or user, session, console, show_thinking=not hide_thinking)
    asyncio.run(repl.run())


@app.command(rich_help_panel=CHAT)
def web(
    char_id: Annotated[str | None, typer.Argument(help="先打开哪个角色；不写时是上次在网页里打开的")] = None,
    host: Annotated[str, typer.Option(help="监听的地址。默认只有这台电脑能打开")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="端口")] = 8790,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="启动后打开浏览器")] = True,
    user: Annotated[str, typer.Option("--as", help="你的用户 ID")] = "me",
    nickname: Annotated[str | None, typer.Option("--name", help="你的昵称（默认同 ID）")] = None,
    config: ConfigOpt = None,
):
    """在浏览器里和角色聊天，和 rhine chat 共用会话。

    引用、撤回、拍一拍、发图片和表情包都有；笔记、情景、上下文和提示词在右边的面板里看。
    """
    cfg = _config(config)
    from .web import run

    if host not in ("127.0.0.1", "localhost"):
        console.print("[warn]别的机器也能连上这个地址，只靠链接里的 token 挡住外人；只在信得过的网络里这样用[/warn]")

    def ready(ui) -> None:
        console.print(f"[title]✻ ohMyRhine[/title] 网页版开好了：[link={ui.url}]{ui.url}[/link]")
        console.print("[dim]链接里带着访问用的 token，别发给别人。Ctrl+C 关掉[/dim]")

    try:
        run(cfg, char_id, user, nickname or user, host, port, open_browser, ready)
    except OSError as e:
        console.print(f"[err]没能在 {host}:{port} 上开网页：{e.strerror or e}[/err]，换个端口试试：--port 8792")
        raise typer.Exit(1) from None


@app.command("list", rich_help_panel=PACKS)
def list_characters(config: ConfigOpt = None):
    """列出所有角色和编译状态。"""
    cfg = _config(config)
    packs = {p.parent.name: p.parent for p in cfg.characters_dir.glob("*/character.yaml")}
    dbs = {p.stem: p for p in cfg.data_dir.glob("*.db")}
    dbs |= {f"{c}:{t}": instance_db(cfg, c, t) for c, t in instances(cfg)}
    ids = sorted(packs.keys() | dbs.keys())
    if not ids:
        console.print(f"[dim]{cfg.characters_dir} 下还没有角色，用 rhine new 创建一个[/dim]")
        return

    table = Table(box=None, pad_edge=False)
    for col in ("ID", "名字", "状态", "会话", "消息"):
        table.add_column(col)
    for ref in ids:
        char_id, _ = split_ref(ref)
        summary = peek(dbs[ref]) if ref in dbs else None
        name = summary.name if summary else None
        if char_id not in packs:
            status = "[warn]只有数据库，角色包已删除[/warn]"
        else:
            try:
                raw = yaml.safe_load((packs[char_id] / "character.yaml").read_text(encoding="utf-8"))
                name = raw.get("name", name) if isinstance(raw, dict) else name
            except yaml.YAMLError:
                pass
            if summary is None or summary.pack_hash is None:
                status = "[dim]未编译[/dim]"
            elif summary.pack_hash == pack_hash(packs[char_id]):
                status = "[ok]已编译[/ok]"
            else:
                status = "[warn]有改动，下次运行时自动重编译[/warn]"
        table.add_row(
            ref,
            name or "?",
            status,
            str(summary.chats) if summary else "0",
            str(summary.messages) if summary else "0",
        )
    console.print(table)


@app.command(rich_help_panel=PACKS)
def new(
    char_id: Annotated[str, typer.Argument(help="角色 ID（目录名，建议用英文）；写成 <角色>:<标签> 时创建持久化实例")],
    name: Annotated[str | None, typer.Option("--name", "-n", help="角色显示名（创建角色包时必填）")] = None,
    config: ConfigOpt = None,
):
    """照推荐的写法新建一个角色包，或者已有角色的持久化实例。

    角色包是一个带注释的小例子，照着改就行；持久化实例（<角色>:<标签>）的长期记忆跨会话保留。
    """
    cfg = _config(config)
    base, tag = split_ref(char_id)
    if tag is not None:
        try:
            engine = create_instance(cfg, base, tag)
        except (ValueError, FileExistsError, PackError) as e:
            console.print(f"[err]{e}[/err]", highlight=False)
            raise typer.Exit(1) from None
        console.print(f"已创建{engine.pack.character.name}的持久化实例 {char_id}：rhine chat {char_id}，"
                      f"或在聊天里 /invite {char_id}", highlight=False)
        return
    if not name:
        console.print("[err]创建角色包要用 --name 给出显示名[/err]")
        raise typer.Exit(1)
    pack_dir = cfg.characters_dir / char_id
    try:
        files = create_pack(pack_dir, char_id, name)
    except FileExistsError as e:
        console.print(f"[err]{e}[/err]")
        raise typer.Exit(1) from None
    for f in files:
        console.print(f"  [ok]+[/ok] {f.relative_to(pack_dir)}", highlight=False)
    console.print(f"已创建角色包 {pack_dir}")


@app.command(rich_help_panel=PACKS)
def build(char_id: str, config: ConfigOpt = None):
    """校验角色包并编译进数据库。"""
    engine = _engine(char_id, config)
    try:
        pack = engine.build()
    except PackError as e:
        console.print(f"[err]角色包有问题：[/err]\n{e}")
        raise typer.Exit(1) from None
    counts: dict[str, int] = {}
    for e in pack.entries:
        key = f"{e.kind}/{e.visibility}"
        counts[key] = counts.get(key, 0) + 1
    n_examples = sum(len(v) for v in pack.examples.values())
    console.print(f"[ok]已编译[/ok] {pack.character.name}（{char_id}）")
    for key, n in sorted(counts.items()):
        console.print(f"  {key}: {n}")
    console.print(f"  examples: {n_examples}")


@app.command(rich_help_panel=PACKS)
def prompt(
    char_id: str,
    chat: Annotated[str | None, typer.Option(help="同时显示该会话的历史 turns")] = None,
    dice: Annotated[bool | None, typer.Option("--dice/--no-dice", help="预览时覆盖配置中的骰子开关")] = None,
    config: ConfigOpt = None,
):
    """打印发给模型的 system prompt 和某个会话的历史，调试用。"""
    engine = _engine(char_id, config, dice)
    req = engine.request(chat) if chat else None
    console.rule("system")
    console.print(engine.system, markup=False, highlight=False)
    if req:
        if req.chat_context:
            console.rule("情景与更早的聊天")
            console.print(req.chat_context, markup=False, highlight=False)
        for t in req.turns:
            console.rule(t.role)
            console.print(t.text, markup=False, highlight=False)
        if req.recall:
            console.rule("长期记忆（附在最后一条消息后）")
            console.print(req.recall, markup=False, highlight=False)


@app.command("eval", rich_help_panel=PACKS)
def eval_scenarios(
    char_id: Annotated[str, typer.Argument(help="角色 ID")],
    only: Annotated[list[str] | None, typer.Argument(help="只跑这些场景（场景的 id）")] = None,
    files: Annotated[list[str] | None, typer.Option("--file", "-f", help="只跑 evals/ 下的这个文件，可以写多次")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="换一个回复模型来跑，比较用")] = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="报告写到哪里（默认 <data_dir>/evals/）")] = None,
    jobs: Annotated[int, typer.Option("--jobs", "-j", min=1, max=16, help="同时跑几个场景")] = 4,
    config: ConfigOpt = None,
):
    """把 evals/ 里的场景对着模型跑一遍，看角色还像不像自己。

    打印每段对话，再存一份 Markdown 报告。每个场景在临时的数据目录里跑，角色真正的聊天和笔记不受影响。会调用模型，按用量计费。
    """
    from .evals import EvalError, load_scenarios, report, run_all

    cfg = _config(config)
    pack_dir = cfg.characters_dir / char_id
    try:
        pack = load_pack(pack_dir)
        scenarios = load_scenarios(pack_dir, files)
    except (PackError, EvalError, FileNotFoundError) as e:
        console.print(f"[err]{e}[/err]")
        raise typer.Exit(1) from None
    if only:
        unknown = sorted(set(only) - {sc.id for _, sc in scenarios})
        if unknown:
            console.print(f"[err]没有这些场景：{'、'.join(unknown)}[/err]")
            raise typer.Exit(1)
        scenarios = [(f, sc) for f, sc in scenarios if sc.id in only]
    if not scenarios:
        console.print(f"[dim]{pack_dir / 'evals'} 下还没有场景，格式见 docs/characters.md[/dim]")
        return
    provider = None
    try:
        task = cfg.tasks["roleplay"] if model is None else cfg.tasks["roleplay"].model_copy(update={"model": model})
        settings = resolve(cfg, "roleplay", task)
    except (KeyError, ConfigError, ValueError) as e:
        console.print(f"[err]{e}[/err]")
        raise typer.Exit(1) from None
    if model is not None:
        from .providers import make_provider

        provider = make_provider(settings)
    name = pack.character.name
    model_name = settings.model or "echo"
    console.print(f"[bold]{name}[/bold]（{char_id}）· {len(scenarios)} 个场景 · {model_name}")
    order = {sc.id: i for i, (_, sc) in enumerate(scenarios)}
    results = []

    async def go() -> None:
        async for r in run_all(cfg, char_id, scenarios, provider, concurrency=jobs):
            results.append(r)
            _print_result(r, name)

    asyncio.run(go())
    results.sort(key=lambda r: order[r.scenario.id])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = out or cfg.data_dir / "evals" / f"{char_id}-{stamp}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report(f"{name}（{char_id}）· {stamp}", model_name, results), encoding="utf-8")
    cost = sum(r.cost for r in results)
    failed = [r for r in results if r.error]
    console.print(f"跑完了 {len(results)} 个场景 · 估算 ${cost:.3f} · 报告：{dest}", highlight=False)
    if failed:
        console.print(f"[err]{len(failed)} 个场景出错：{'、'.join(r.scenario.id for r in failed)}[/err]")
        raise typer.Exit(1)


def _print_result(r, name: str) -> None:
    console.print()
    console.print(Text.assemble(("● ", "accent"), (r.scenario.id, "bold"), (f"  {r.file}", "dim")))
    console.print(Text(f"  期望：{r.scenario.desc}", style="dim"))
    for line in r.lines:
        if line.kind == "note":
            console.print(Text(f"  （{line.text}）", style="dim"))
        else:
            style = "title" if line.kind == "reply" else "bold"
            indent = " " * (4 + len(line.who) * 2)
            console.print(Text.assemble("  ", (line.who, style), "  ", line.text.replace("\n", "\n" + indent)))
    if r.scenario.real:
        console.print(Text(f"  原话：{r.scenario.real}", style="dim"))
    if r.error:
        console.print(Text(f"  出错：{r.error}", style="err"))


@app.command(rich_help_panel=LIVE)
def code(
    ref: Annotated[str, typer.Argument(help="角色 ID 或 <角色>:<标签>")],
    minutes: Annotated[int, typer.Option("--minutes", "-m", min=1, max=1440, help="多少分钟内有效")] = 10,
    config: ConfigOpt = None,
):
    """生成一次性的系统指令码（可在服务器上运行）。

    在聊天里发一条以它开头的消息，比如「指令码 现在开始你在下雨的公园里」，后面的文字就作为系统指令交给角色：
    角色把它当作事实或要守的规矩照着做，不会提起它。码后面正好是「撤销」时，撤销这个会话最近的一条指令；
    正好是「撤销全部」时，撤销这个会话的全部指令。码只能用一次，过期或生成新码后作废。
    """
    engine = _engine(ref, config)
    secret, expires = engine.issue_code(minutes)
    console.print(secret, markup=False, highlight=False)
    console.print(f"[dim]{minutes} 分钟内有效（到本机时间 {format_ts(expires)[-5:]}），用一次就作废。"
                  "在聊天里发「码 指令」下指令，「码 撤销」撤销这个会话最近一条，「码 撤销全部」撤销全部[/dim]",
                  highlight=False)


instruction_app = typer.Typer(no_args_is_help=True, help="查看、撤销角色收到的系统指令（可在服务器上运行）。")
app.add_typer(instruction_app, name="instruction", rich_help_panel=LIVE)


@instruction_app.command("list")
def instruction_list(
    ref: Annotated[str, typer.Argument(help="角色 ID 或 <角色>:<标签>")],
    chat: Annotated[str | None, typer.Option("--chat", help="只看这个会话（会话 ID 的开头即可）")] = None,
    config: ConfigOpt = None,
):
    """列出系统指令：编号、会话、内容和收到的时间。"""
    engine = _engine(ref, config)
    rows = [m for m in engine.store.instructions(None) if not chat or m.chat_id.startswith(chat)]
    if not rows:
        console.print("[dim]没有系统指令[/dim]")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("编号", "会话", "内容", "时间"):
        table.add_column(col, overflow="fold")
    for m in rows:
        table.add_row(str(m.id), display_id(m.chat_id), m.content, format_ts(m.ts, engine.tz))
    console.print(table, highlight=False)
    console.print(f"[dim]共 {len(rows)} 条[/dim]", highlight=False)


@instruction_app.command("rm")
def instruction_rm(
    ref: Annotated[str, typer.Argument(help="角色 ID 或 <角色>:<标签>")],
    ids: Annotated[list[int], typer.Argument(help="要撤销的指令编号（rhine instruction list 里看）")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="不再确认")] = False,
    config: ConfigOpt = None,
):
    """撤销系统指令，就像从没下过；角色已经说过的话照旧留着。网关可以照常运行。"""
    engine = _engine(ref, config)
    known = {m.id: m for m in engine.store.instructions(None)}
    missing = [i for i in ids if i not in known]
    if missing:
        console.print(f"[err]没有编号为 {'、'.join(map(str, missing))} 的系统指令[/err]", highlight=False)
        raise typer.Exit(1)
    for i in ids:
        console.print(f"  {i}：{known[i].content}", markup=False, highlight=False)
    if not yes and not typer.confirm(f"撤销这 {len(ids)} 条系统指令？"):
        raise typer.Exit(1)
    engine.store.delete_messages(ids)
    console.print(f"[dim]已撤销 {len(ids)} 条[/dim]", highlight=False)


memory_app = typer.Typer(no_args_is_help=True, help="查看、改写、删除、恢复角色的笔记（可在服务器上运行）。")
app.add_typer(memory_app, name="memory", rich_help_panel=LIVE)
ACTIONS = {"add": "记下", "edit": "改写", "remove": "删除", "restore": "恢复"}
ACTORS = {"character": "角色自己", "owner": "主人"}


def _memory_rows(engine: Engine, chat: str | None, person: str | None, removed: bool = False):
    rows = engine.store.memories(None, removed=removed)
    if chat:
        rows = [m for m in rows if m.chat_id.startswith(chat)]
    if person:
        rows = [
            m for m in rows if m.subject_id
            and (person in display_id(m.subject_id) or person in (engine.store.nickname(m.subject_id) or ""))
        ]
    return rows


def _now_ms() -> int:
    return int(time.time() * 1000)


@memory_app.command("list")
def memory_list(
    ref: Annotated[str, typer.Argument(help="角色 ID 或 <角色>:<标签>")],
    chat: Annotated[str | None, typer.Option("--chat", help="只看这个会话（会话 ID 的开头即可）")] = None,
    person: Annotated[str | None, typer.Option("--person", help="只看关于这个人的（昵称或 ID 的一部分）")] = None,
    removed: Annotated[bool, typer.Option("--removed", help="看删掉的笔记（可以用 restore 恢复）")] = False,
    config: ConfigOpt = None,
):
    """列出笔记：编号、会话、关于谁、内容和日期（删掉的是删除的日期）。"""
    engine = _engine(ref, config)
    rows = _memory_rows(engine, chat, person, removed)
    if not rows:
        console.print("[dim]没有符合条件的笔记[/dim]")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("编号", "会话", "关于", "内容", "日期"):
        table.add_column(col, overflow="fold")
    for m in rows:
        who = f"{engine.store.nickname(m.subject_id) or '?'}（{display_id(m.subject_id)}）" if m.subject_id else "—"
        when = format_ts(m.removed_ts or m.updated_ts, engine.tz)[:13]
        table.add_row(str(m.id), display_id(m.chat_id), who, m.text, when)
    console.print(table, highlight=False)
    console.print(f"[dim]共 {len(rows)} 条[/dim]", highlight=False)


@memory_app.command("history")
def memory_history(
    ref: Annotated[str, typer.Argument(help="角色 ID 或 <角色>:<标签>")],
    chat: Annotated[str | None, typer.Option("--chat", help="只看这个会话（会话 ID 的开头即可）")] = None,
    memory_id: Annotated[int | None, typer.Option("--id", help="只看这一条笔记")] = None,
    config: ConfigOpt = None,
):
    """笔记的每一次改动：谁在什么时候记下、改写、删除或恢复了哪一条，改之前是什么。"""
    engine = _engine(ref, config)
    rows = [c for c in engine.store.memory_changes(None)
            if (not chat or c.chat_id.startswith(chat)) and (memory_id is None or c.memory_id == memory_id)]
    if not rows:
        console.print("[dim]没有改动记录[/dim]")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("时间", "编号", "会话", "谁", "动作", "内容"):
        table.add_column(col, overflow="fold")
    for c in rows:
        text = c.text if c.before is None else f"{c.before} → {c.text}"
        if c.reason:
            text = f"{text}（{c.reason}）"
        table.add_row(format_ts(c.ts, engine.tz)[:16], str(c.memory_id), display_id(c.chat_id),
                      ACTORS.get(c.actor, c.actor), ACTIONS.get(c.action, c.action), text)
    console.print(table, highlight=False)


def _pick(engine: Engine, ids: list[int], *, removed: bool | None = False) -> dict[int, Memory]:
    """The notes with these ids, among the live ones, the removed ones, or (None) both."""
    rows = [m for r in ((False, True) if removed is None else (removed,)) for m in engine.store.memories(None, removed=r)]
    known = {m.id: m for m in rows}
    if missing := [i for i in ids if i not in known]:
        what = "删掉的笔记" if removed else "笔记"
        console.print(f"[err]没有编号为 {'、'.join(map(str, missing))} 的{what}[/err]", highlight=False)
        raise typer.Exit(1)
    return known


@memory_app.command("rm")
def memory_rm(
    ref: Annotated[str, typer.Argument(help="角色 ID 或 <角色>:<标签>")],
    ids: Annotated[list[int], typer.Argument(help="要删除的笔记编号（rhine memory list 里看）")],
    purge: Annotated[bool, typer.Option("--purge", help="连同改动记录彻底删掉，不能恢复（不该留存的内容用）")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="不再确认")] = False,
    config: ConfigOpt = None,
):
    """删除笔记。删掉的还留在数据库里，可以用 restore 恢复；--purge 彻底删掉，删掉的也能彻底删。"""
    engine = _engine(ref, config)
    known = _pick(engine, ids, removed=None if purge else False)
    for i in ids:
        console.print(f"  {i}：{known[i].text}", markup=False, highlight=False)
    how = "彻底" if purge else ""
    if not yes and not typer.confirm(f"{how}删除这 {len(ids)} 条笔记？"):
        raise typer.Exit(1)
    for i in ids:
        if purge:
            engine.store.purge_memory(i)
        else:
            engine.store.remove_memory(None, i, _now_ms(), "owner")
    console.print(f"[dim]已{how}删除 {len(ids)} 条[/dim]", highlight=False)


@memory_app.command("restore")
def memory_restore(
    ref: Annotated[str, typer.Argument(help="角色 ID 或 <角色>:<标签>")],
    ids: Annotated[list[int], typer.Argument(help="要恢复的笔记编号（rhine memory list --removed 里看）")],
    config: ConfigOpt = None,
):
    """恢复删掉的笔记。"""
    engine = _engine(ref, config)
    _pick(engine, ids, removed=True)
    for i in ids:
        engine.store.restore_memory(i, _now_ms())
    console.print(f"[dim]已恢复 {len(ids)} 条[/dim]", highlight=False)


@memory_app.command("edit")
def memory_edit(
    ref: Annotated[str, typer.Argument(help="角色 ID 或 <角色>:<标签>")],
    memory_id: Annotated[int, typer.Argument(help="笔记编号")],
    text: Annotated[str, typer.Argument(help="改写后的内容")],
    config: ConfigOpt = None,
):
    """改写一条笔记的内容；它的日期不变，改动记在 history 里。"""
    engine = _engine(ref, config)
    m = _pick(engine, [memory_id])[memory_id]
    # Keeps its date: a note is dated by when it was written, not by when it was corrected.
    engine.store.update_memory(None, memory_id, " ".join(text.split()), _now_ms(), "owner", dated=m.updated_ts)
    console.print(f"[dim]已改写笔记 {memory_id}[/dim]", highlight=False)



def _key(settings) -> tuple[bool, str]:
    """Whether a task's API key is there, and where it comes from (never what it is)."""
    if settings.protocol == "echo":
        return True, "不需要"
    if settings.api_key_env and os.environ.get(settings.api_key_env):
        return True, f"环境变量 {settings.api_key_env}"
    if settings.api_key_file:
        path = settings.api_key_file.expanduser()
        if path.is_file() and path.stat().st_size > 0:
            return True, f"文件 {_home(path)}"
        return False, f"api_key_file 不存在或是空的：{path}"
    if settings.api_key_env:
        return False, f"没有密钥：设置环境变量 {settings.api_key_env}，或者在 providers 里写 api_key_file"
    return True, "不需要"


def _home(path: Path) -> str:
    home = str(Path.home())
    return "~" + str(path)[len(home):] if str(path).startswith(home + os.sep) else str(path)


@app.command(rich_help_panel=SETUP)
def doctor(
    ping: Annotated[bool, typer.Option("--ping", help="给每个任务的模型发一句话，确认密钥和地址真的能用（花一点点钱）")] = False,
    config: ConfigOpt = None,
):
    """检查配置、密钥、角色包和查资料用的工具是否就绪。"""
    found = config or (Path("config.yaml") if Path("config.yaml").is_file() else None)
    rows: list[tuple[str, str, str]] = []
    try:
        cfg = load_config(config)
    except (FileNotFoundError, ConfigError) as e:
        console.print(f"[err]✗ 配置读不了：{e}[/err]")
        raise typer.Exit(1) from None
    rows.append(("ok", "配置", str(found.resolve()) if found else "没有 config.yaml，用的全是默认值"))
    rows.append(("ok" if cfg.characters_dir.is_dir() else "bad", "角色目录", str(cfg.characters_dir)))
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".doctor"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        rows.append(("ok", "数据目录", str(cfg.data_dir)))
    except OSError as e:
        rows.append(("bad", "数据目录", f"{cfg.data_dir} 写不进去：{e.strerror or e}"))
    if "roleplay" not in cfg.tasks:
        rows.append(("bad", "roleplay", "没有配置：tasks.roleplay 是必须的"))
    for task in cfg.tasks:
        try:
            s = cfg.settings(task)
        except (KeyError, ConfigError) as e:
            rows.append(("bad", task, str(e)))
            continue
        ok, where = _key(s)
        rows.append(("ok" if ok else "bad", task, f"{s.provider} · {s.model or 'echo'} · 密钥：{where}"))
    missing = {"reply_judge": "没配：群聊里只在 @ 或叫到名字时回复", "vision": "没配：看不到图片的内容",
               "summary": "没配：旧消息移出窗口后就忘了"}
    for task, what in missing.items():
        if task not in cfg.tasks:
            rows.append(("info", task, what))
    for kind, tool in (("翻聊天记录", cfg.lookup.history), ("上网查资料", cfg.lookup.search)):
        if tool is None:
            rows.append(("info", kind, "没开（lookup.history / lookup.search）"))
            continue
        command = tool.command or HARNESSES[tool.harness].command
        path = shutil.which(command)
        rows.append(("ok" if path else "bad", kind, f"{tool.harness} · {path or f'找不到命令 {command}'}"))
    if cfg.lookup.reference is not None:
        ref = cfg.lookup.reference
        usable = ref.path.is_dir() and (ref.format == "text" or (ref.path / ".git").exists())
        what = " git 仓库" if ref.format == "git_html" else "目录"
        rows.append(("ok", "参考资料", _home(ref.path)) if usable else
                    ("warn", "参考资料", f"{_home(ref.path)} 不是可用的{what}：查资料时只能上网"))
    for module in cfg.plugins:
        rows.append(("ok", "插件", module))
    for char_id, name, _ in _packs(cfg):
        try:
            pack = load_pack(cfg.characters_dir / char_id)
            rows.append(("ok", f"角色 {char_id}", f"{pack.character.name} · {len(pack.entries)} 条设定"))
        except PackError as e:
            rows.append(("bad", f"角色 {char_id}", str(e).splitlines()[0]))
    if ping:
        rows += asyncio.run(_ping(cfg))
    marks = {"ok": Text("✓", style="ok"), "bad": Text("✗", style="err"), "warn": Text("!", style="warn"),
             "info": Text("·", style="dim")}
    table = Table(box=None, pad_edge=False, show_header=False)
    for status, item, detail in rows:
        table.add_row(marks[status], Text(item, style="bold"), Text(detail, style="dim" if status == "info" else ""))
    console.print(table)
    bad = sum(1 for s, _, _ in rows if s == "bad")
    if bad:
        console.print(f"[err]{bad} 项有问题[/err]")
        raise typer.Exit(1)
    console.print("[ok]都没问题[/ok]" + ("" if ping else "[dim]。要确认密钥真的能用，加 --ping[/dim]"))


async def _ping(cfg: Config) -> list[tuple[str, str, str]]:
    from .providers import ChatRequest, Turn, make_chat_provider

    rows = []
    for task in cfg.tasks:
        try:
            s = cfg.settings(task)
            if s.protocol == "echo":
                continue
            provider = make_chat_provider(cfg, task)
            started = time.monotonic()
            req = ChatRequest(system="只回答一个字：好", turns=[Turn("user", "在吗")], cache_history=False)
            result = await asyncio.wait_for(provider.complete(req), timeout=120)
            ms = round((time.monotonic() - started) * 1000)
            rows.append(("ok", f"{task} 试一下", f"{result.done.model} 回了「{result.text.strip()[:20]}」· {ms} ms"))
        except Exception as e:
            rows.append(("bad", f"{task} 试一下", f"{type(e).__name__}: {e}"))
    return rows


def _price(cfg: Config, model: str | None) -> str:
    pricing = cfg.price(model)
    if pricing is None:
        return ""
    p = pricing.at()
    return f"${p.input:g}/${p.output:g}"


@app.command("config", rich_help_panel=SETUP)
def show_config(
    character: Annotated[str | None, typer.Option("--character", "-C", help="看这个角色实际用的（叠加了 characters.<ID>）")] = None,
    config: ConfigOpt = None,
):
    """看实际生效的配置：模型、价格、查资料的工具和各项设置。"""
    cfg = _config(config)
    if character is not None:
        if character not in cfg.characters:
            console.print(f"[dim]characters 里没有 {character}，它用的就是下面这份共用的配置[/dim]")
        cfg = cfg.for_character(character)
    table = Table(box=None, pad_edge=False)
    for col in ("任务", "模型", "effort", "思考", "输出上限", "价格"):
        table.add_column(col, no_wrap=col in ("模型", "价格"))
    for task in cfg.tasks:
        role = ROLES.get(task)
        name = Text.assemble((task, "bold"), (f" {role.description}" if role else "", "dim"))
        try:
            s = cfg.settings(task)
        except (KeyError, ConfigError) as e:
            table.add_row(name, Text(str(e), style="err"))
            continue
        model = s.model or "echo"
        spec = MODELS.get(model)
        if spec is None or spec.vendor != s.provider:
            model = f"{s.provider}/{model}"
        table.add_row(name, model, s.effort or "", "关" if s.thinking is False else "开", str(s.max_tokens),
                      _price(cfg, s.model))
    console.print(table)
    console.print(Text("价格是输入/输出，每百万 token", style="dim"))
    console.print()
    other = Table(box=None, pad_edge=False, show_header=False)
    other.add_column(style="dim")
    other.add_column()
    for kind, tool in (("翻聊天记录", cfg.lookup.history), ("上网查资料", cfg.lookup.search)):
        other.add_row(kind, "没开" if tool is None else
                      f"{tool.harness} · {tool.model or HARNESSES[tool.harness].model or '默认模型'} · effort {tool.effort or '默认'}")
    if cfg.lookup.reference is not None:
        other.add_row("参考资料", f"{cfg.lookup.reference.description}（{_home(cfg.lookup.reference.path)}）")
    g = cfg.group
    other.add_row("群聊", f"筛选{'开' if g.judge and cfg.has_task('reply_judge') else '关'} · "
                         f"等下文 {g.wait_short_s:g}/{g.wait_long_s:g} 秒 · 骰子{'开' if g.dice else '关'}")
    other.add_row("历史", f"约 {cfg.history_tokens:,} token")
    other.add_row("记忆", f"摘要 {cfg.memory.summary_chars} 字 · 笔记 {cfg.memory.notes_chars} 字")
    other.add_row("数据", _home(cfg.data_dir))
    if cfg.characters:
        other.add_row("单独配置的角色", "、".join(cfg.characters))
    if cfg.plugins:
        other.add_row("插件", "、".join(cfg.plugins))
    console.print(other)


@app.command(rich_help_panel=SETUP)
def models(
    vendor: Annotated[str | None, typer.Argument(help="只看这一家：anthropic、openai、deepseek、gemini")] = None,
):
    """内置的模型表：能不能看图、用工具，输出上限和价格。

    config.yaml 的 tasks 里直接写这里的模型名。
    """
    table = Table(box=None, pad_edge=False)
    for col in ("模型", "供应商", "看图", "工具", "输出上限", "$/百万 token"):
        table.add_column(col, no_wrap=col == "模型")
    tools = {"yes": "能", "no": "不能", "no_reasoning": "关思考时能"}
    for model_id, spec in MODELS.items():
        if vendor and spec.vendor != vendor:
            continue
        price = spec.pricing.at() if getattr(spec, "pricing", None) else None
        table.add_row(model_id, spec.vendor, "能" if spec.vision else "不能", tools.get(str(spec.tools), str(spec.tools)),
                      f"{spec.max_output:,}", f"${price.input:g}/${price.output:g}" if price else "")
    console.print(table)
    console.print("[dim]价格是输入/输出，每百万 token，按现在的日期和时段；config.yaml 的 models.<模型>.price 可以改[/dim]")
