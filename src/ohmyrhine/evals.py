"""A pack's evals/: short conversations to replay against the configured models after changing
the pack (`rhine eval`), to read whether the character still sounds like itself. They never
reach its prompt.

Each scenario runs in a scratch data directory of its own, so the character's real chats and
notes are untouched. A group scenario goes through the reply judge, as in a real group.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Config
from .engine import Engine, IncomingMessage
from .providers import ChatProvider, Done, TextDelta, ToolUse

_AGO = re.compile(r"(\d+)([mhd])")
_UNITS = {"m": 60, "h": 3600, "d": 86400}


class EvalTurn(BaseModel):
    """One message in a scenario."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    say: str
    # Who says it: an id of your choosing (different ids are different people) and a nickname
    # (the id by default).
    as_: str | None = Field(default=None, alias="as")
    name: str | None = None
    # How long ago it was said: 30m, 5h, 3d.
    ago: str | None = Field(default=None, pattern=_AGO.pattern)
    # False: say the next turn straight after, without waiting for an answer.
    reply: bool = True
    # The message @-s the character.
    mention: bool = False
    # Dice traffic: a request to roll, or a roll's result.
    dice: Literal["roll", "result"] | None = None

    @property
    def nickname(self) -> str:
        return self.name or self.as_ or "网友"


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    # What the character should do: what a reader checks the transcript against.
    desc: str
    chat: Literal["private", "group"] = "private"
    # What happened before the conversation (as /scene sets it).
    scene: str | list[str] = Field(default_factory=list)
    # The character's actual line, when the scenario replays a real conversation.
    real: str | None = None
    # Run with the dice component on (the character rolls the group's dice).
    dice: bool = False
    turns: list[EvalTurn] = Field(min_length=1)


class EvalError(Exception):
    """A scenario file that cannot be read, said in terms of the file."""


def load_scenarios(pack_dir: Path, files: list[str] | None = None) -> list[tuple[str, Scenario]]:
    """The scenarios in a pack's evals/*.yaml (or just these files), with the file each is in."""
    directory = pack_dir / "evals"
    paths = [directory / (f if f.endswith(".yaml") else f"{f}.yaml") for f in files] if files else sorted(directory.glob("*.yaml"))
    out: list[tuple[str, Scenario]] = []
    for path in paths:
        if not path.is_file():
            raise EvalError(f"没有评测文件 {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except yaml.YAMLError as e:
            raise EvalError(f"{path.name} 不是合法的 YAML：{e}") from None
        if not isinstance(data, list):
            raise EvalError(f"{path.name} 应该是场景的列表")
        for i, raw in enumerate(data):
            try:
                out.append((path.name, Scenario.model_validate(raw)))
            except ValidationError as e:
                where = raw.get("id") if isinstance(raw, dict) else None
                problems = "；".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
                raise EvalError(f"{path.name} 第 {i + 1} 个场景{f'（{where}）' if where else ''}：{problems}") from None
    seen: set[str] = set()
    for name, sc in out:
        if sc.id in seen:
            raise EvalError(f"{name}：场景 id {sc.id} 重复了")
        seen.add(sc.id)
    return out


@dataclass
class Line:
    """One line of a transcript: someone's message, the character's reply, or a note."""

    kind: Literal["said", "reply", "note"]
    who: str
    text: str


@dataclass
class Result:
    file: str
    scenario: Scenario
    lines: list[Line] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    error: str | None = None


def _ago_ms(spec: str | None) -> int:
    if not spec:
        return 0
    m = _AGO.fullmatch(spec)
    assert m is not None  # checked by the model
    return int(m.group(1)) * _UNITS[m.group(2)] * 1000


async def run_scenario(cfg: Config, ref: str, file: str, sc: Scenario, provider: ChatProvider | None = None,
                       instance: str | None = None) -> Result:
    """Play a scenario in a scratch data directory; `provider` stands in for the configured
    roleplay model."""
    result = Result(file, sc)
    with tempfile.TemporaryDirectory(prefix="rhine-eval-") as tmp:
        # A fresh config (not a copy): a copy would share the cache of per-character configs.
        run_cfg = Config.model_validate({**cfg.model_dump(), "data_dir": Path(tmp)})
        engine = Engine(run_cfg, ref, dice=sc.dice or None, instance=instance)
        if provider is not None:
            engine._provider = provider

        def used(done: Done) -> None:
            for k, v in done.usage.items():
                result.usage[k] = result.usage.get(k, 0) + v
            if price := run_cfg.price(done.model):
                result.cost += price.cost(done.usage)

        engine.on_usage = used
        name = engine.pack.character.name
        chat_id = f"eval:{'g' if sc.chat == 'group' else 'p'}:{sc.id}"
        now = int(time.time() * 1000)
        for text in [sc.scene] if isinstance(sc.scene, str) else sc.scene:
            engine.store.add_scene(chat_id, text, now)
            result.lines.append(Line("note", "", f"情景：{text}"))
        try:
            for i, turn in enumerate(sc.turns):
                user = turn.as_ or "tester"
                ts = now - _ago_ms(turn.ago) + i  # same-time turns keep their order
                msg = IncomingMessage(chat_id, sc.chat, f"eval:u:{user}", turn.nickname, turn.say, ts=ts,
                                      mentions_self=turn.mention, dice=turn.dice)
                must = engine.receive(msg)
                result.lines.append(Line("said", turn.nickname, turn.say))
                if not turn.reply:
                    continue
                if not must and engine.judges(chat_id):
                    decision = await engine.decide(chat_id)
                    more = i + 1 < len(sc.turns)
                    if decision.wait and more:
                        result.lines.append(Line("note", "", f"判断：等下文 · {decision.reason}"))
                        continue
                    if not decision.reply:
                        result.lines.append(Line("note", "", f"判断：{name}不接话 · {decision.reason}"))
                        continue
                    result.lines.append(Line("note", "", f"判断：接话 · {decision.reason}"))
                elif not must and engine.store.chat_kind(chat_id) == "group":
                    result.lines.append(Line("note", "", f"{name}没有被叫到，不回"))
                    continue
                await _reply(engine, chat_id, name, result, used)
        except Exception as e:  # one broken scenario must not stop the rest
            result.error = f"{type(e).__name__}: {e}"
        finally:
            engine.store.close()
    return result


async def _reply(engine: Engine, chat_id: str, name: str, result: Result, used) -> None:
    text, done = "", None
    async for ev in engine.reply(chat_id):
        if isinstance(ev, TextDelta):
            text += ev.text
        elif isinstance(ev, ToolUse):
            result.lines.append(Line("note", "", f"{name}用了 {ev.name}：{_brief(ev.input)}"))
        elif isinstance(ev, Done):
            done = ev
            used(ev)
    if done is not None and done.stop_reason == "silent":
        result.lines.append(Line("note", "", f"{name}没有回复"))
    elif text.strip():
        note = f"（{done.stop_reason}）" if done is not None and done.stop_reason not in ("end", "tool_use") else ""
        result.lines.append(Line("reply", name, text.strip() + note))
    if engine.lookups.running(chat_id):
        await engine.lookups.wait(chat_id)
        await _reply(engine, chat_id, name, result, used)


def _brief(data: dict) -> str:
    return "，".join(f"{v}" for v in data.values() if isinstance(v, str | int))[:80]


async def run_all(cfg: Config, ref: str, scenarios: list[tuple[str, Scenario]], provider: ChatProvider | None = None,
                  instance: str | None = None, concurrency: int = 4):
    """Run scenarios a few at a time; yields each Result as it finishes."""
    gate = asyncio.Semaphore(concurrency)

    async def one(file: str, sc: Scenario) -> Result:
        async with gate:
            return await run_scenario(cfg, ref, file, sc, provider, instance)

    for next_done in asyncio.as_completed([one(f, sc) for f, sc in scenarios]):
        yield await next_done


def report(title: str, model: str, results: list[Result]) -> str:
    """The transcripts as Markdown, in the order the scenarios are written."""
    cost = sum(r.cost for r in results)
    out = [f"# {title}", "", f"模型 {model} · {len(results)} 个场景 · 估算 ${cost:.3f}", ""]
    for r in results:
        out += [f"## {r.scenario.id} · {r.file}", "", f"**期望**：{r.scenario.desc}", ""]
        for line in r.lines:
            if line.kind == "note":
                out.append(f"_（{line.text}）_  ")
            elif line.kind == "said":
                out.append(f"**{line.who}**：{line.text}  ".replace("\n", "  \n"))
            else:
                out.append("> **{}**：{}".format(line.who, line.text.replace("\n", "  \n> ")))
                out.append("")
        if r.scenario.real:
            out.append(f"_原话：{r.scenario.real}_")
        if r.error:
            out.append(f"**出错**：{r.error}")
        out.append("")
    return "\n".join(out)
