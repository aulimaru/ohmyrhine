"""Breakdown of what goes into the model's context, for `/context`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..providers import Turn
from .engine import Engine
from .prompt import render_sections


@dataclass(frozen=True)
class Part:
    label: str
    chars: int
    # None when the provider can't count tokens.
    tokens: int | None


@dataclass(frozen=True)
class ContextReport:
    sections: list[Part]
    history: Part
    messages_in_window: int
    messages_total: int
    history_tokens: int
    # Exact count of the full request; None when the provider can't count.
    total_tokens: int | None


async def context_report(engine: Engine, chat_id: str) -> ContextReport:
    sections = render_sections(engine.pack, engine.dice)
    window = engine.window(chat_id)
    req = engine.request(chat_id, show_thinking=engine.cfg.log_thinking)
    blocks = engine.chat_context(chat_id, window)
    if req.recall and req.turns and req.turns[-1].role == "user":
        blocks.append(("末尾附注", req.recall))
    provider = engine.provider

    async def count(text: str) -> int | None:
        return await provider.count_tokens(None, [Turn("user", text)])

    # Each standalone count carries a little fixed message overhead; measure and subtract it.
    probe, *counts = await asyncio.gather(count("."), *(count(text) for _, text in [*sections, *blocks]))
    overhead = probe - 1 if probe is not None else 0

    def net(n: int | None) -> int | None:
        return None if n is None else max(n - overhead, 0)

    parts = [Part(label, len(text), net(n)) for (label, text), n in zip([*sections, *blocks], counts, strict=True)]
    history_tokens: int | None = 0
    total: int | None
    if req.turns:
        history_tokens, total = await asyncio.gather(
            provider.count_tokens(None, req.turns), provider.count_request_tokens(req)
        )
        history_tokens = net(history_tokens)
    else:
        total = None if probe is None else sum(p.tokens or 0 for p in parts)

    return ContextReport(
        sections=parts,
        history=Part("历史消息", sum(len(t.text) for t in req.turns), history_tokens),
        messages_in_window=len(window),
        messages_total=engine.store.count_messages(chat_id),
        history_tokens=engine.cfg.history_tokens,
        total_tokens=total,
    )
