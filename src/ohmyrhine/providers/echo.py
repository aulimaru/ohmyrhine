"""Offline provider for tests and wiring checks: echoes the last user line."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from .base import (
    ChatRequest,
    Completion,
    Done,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Turn,
)


def _last_line(req: ChatRequest) -> str:
    return req.turns[-1].text.splitlines()[-1] if req.turns else ""


class EchoChat:
    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        last = _last_line(req)
        if req.show_thinking:
            yield ThinkingDelta(f"（echo 思考）对方说：{last}")
        yield TextDelta(f"（echo）{last}")
        yield Done(stop_reason="end", model="echo")

    async def complete(self, req: ChatRequest, json_schema: dict[str, Any] | None = None) -> Completion:
        if json_schema is not None:
            # Fill every required field with a placeholder of its type.
            fill = {"boolean": True, "string": "echo", "integer": 0, "number": 0}
            props = json_schema.get("properties", {})
            text = json.dumps({k: fill.get(v.get("type")) for k, v in props.items()})
        else:
            text = f"（echo）{_last_line(req)}"
        return Completion(text, Done(stop_reason="end", model="echo"))

    async def count_tokens(self, system: str | None, turns: list[Turn]) -> int | None:
        return None

    async def count_request_tokens(self, req: ChatRequest) -> int | None:
        return None
