"""Claude via the Anthropic Messages API."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from ..catalog import ModelSettings
from .base import (
    ChatRequest,
    Completion,
    Done,
    Image,
    ReplyWritten,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Tool,
    ToolError,
    ToolUse,
    Turn,
)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_STOP_REASONS = {"end_turn": "end", "stop_sequence": "end", "max_tokens": "max_tokens", "refusal": "refusal"}
# Tool calls allowed per reply.
_TOOL_CALLS = 4
# Breakpoints in the history: on its last block and on every _CACHE_EVERY-th block before it,
# _HISTORY_BREAKPOINTS in all (the system prompt has the fourth the API allows). A request reads
# the cache only where an earlier request put a breakpoint, looking back at most 20 blocks from
# each of its own, so this keeps the previous request's entry in reach even after a long run of
# messages she let pass (one block per message, see build_turns).
_CACHE_EVERY = 15
_HISTORY_BREAKPOINTS = 3


def _blocks(turn: Turn) -> list[dict[str, Any]]:
    if isinstance(turn.content, str):
        return [{"type": "text", "text": turn.content}]
    blocks: list[dict[str, Any]] = []
    for part in turn.content:
        if isinstance(part, Image):
            data = base64.standard_b64encode(part.data).decode("ascii")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": part.mime, "data": data}})
        elif part:
            blocks.append({"type": "text", "text": part})
    return blocks


def _messages(turns: list[Turn], cache: bool = True, recall: str | None = None) -> list[dict[str, Any]]:
    messages = [{"role": t.role, "content": _blocks(t)} for t in turns]
    # Cache the whole history prefix: breakpoints on the last block and some before it.
    if messages and cache:
        blocks = [b for m in messages for b in m["content"]]
        for i in range(len(blocks) - 1, -1, -_CACHE_EVERY)[:_HISTORY_BREAKPOINTS]:
            blocks[i]["cache_control"] = {"type": "ephemeral"}
    if recall and messages and messages[-1]["role"] == "user":
        messages[-1]["content"].append({"type": "text", "text": recall})
    return messages


def _system(req: ChatRequest) -> list[dict[str, Any]]:
    # A one-hour entry costs more to write (2x input instead of 1.25x) and is renewed by every
    # read, so it pays off for a prompt that is read again after gaps of 5 to 60 minutes.
    cache = {"type": "ephemeral", "ttl": "1h"} if req.long_lived_system else {"type": "ephemeral"}
    blocks: list[dict[str, Any]] = [{"type": "text", "text": req.system, "cache_control": cache}]
    if req.chat_context:
        blocks.append({"type": "text", "text": req.chat_context})
    return blocks


async def _run_tool(tool: Tool | None, call: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id}
    if tool is None:
        return {**result, "is_error": True, "content": f"没有 {call.name} 这个工具"}
    try:
        return {**result, "content": await tool.run(dict(call.input))}
    except ToolError as e:
        return {**result, "is_error": True, "content": str(e)}


def _usage(u: Any) -> dict[str, int]:
    """`cache_write` counts every cache write; `cache_write_1h` is the part of it written with
    the one-hour lifetime, which is priced higher."""
    usage = {
        "input": u.input_tokens,
        "output": u.output_tokens,
        "cache_read": u.cache_read_input_tokens or 0,
        "cache_write": u.cache_creation_input_tokens or 0,
    }
    by_ttl = getattr(u, "cache_creation", None)
    if hour := getattr(by_ttl, "ephemeral_1h_input_tokens", None):
        usage["cache_write_1h"] = hour
    return usage


class AnthropicChat:
    def __init__(self, settings: ModelSettings):
        kwargs: dict[str, Any] = {}
        if api_key := settings.api_key():
            kwargs["api_key"] = api_key
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        if settings.max_retries is not None:
            kwargs["max_retries"] = settings.max_retries
        # Without an explicit key the SDK resolves ANTHROPIC_API_KEY / `ant auth login` itself.
        self.client = anthropic.AsyncAnthropic(**kwargs)
        self.settings = settings

    def _kwargs(self, req: ChatRequest) -> dict[str, Any]:
        s = self.settings
        kwargs: dict[str, Any] = {
            "model": s.model,
            "max_tokens": s.max_tokens,
            "system": _system(req),
            "messages": _messages(req.turns, req.cache_history, req.recall),
        }
        if s.effort:
            kwargs["output_config"] = {"effort": s.effort}
        if s.thinking is False and s.thinking_off:
            kwargs.update(s.thinking_off)
        if s.fallbacks:
            kwargs["fallbacks"] = s.fallbacks
            kwargs["betas"] = [_FALLBACK_BETA]
        for name in ("temperature", "top_p", "extra_body", "extra_headers"):
            if (value := getattr(s, name)) is not None:
                kwargs[name] = value
        return kwargs

    def _api(self, kwargs: dict[str, Any]) -> Any:
        """The beta namespace only when a beta feature (fallbacks) is in use."""
        return self.client.beta.messages if "betas" in kwargs else self.client.messages

    def _reply_kwargs(self, req: ChatRequest) -> dict[str, Any]:
        kwargs = self._kwargs(req)
        if req.show_thinking and self.settings.thinking is not False:
            # The raw chain of thought is never returned; "summarized" gives a readable summary.
            # Thinking happens and is billed the same either way.
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        if req.tools:
            # strict: the API checks the input against the schema before we see it.
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema, "strict": True}
                for t in req.tools
            ]
        return kwargs

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        kwargs = self._reply_kwargs(req)
        tools = {t.name: t for t in req.tools}
        usage: dict[str, int] = {}
        remaining = _TOOL_CALLS
        # The reply was written and an after-reply tool began: whatever follows is notes.
        written = False
        while True:
            wrote = False
            async with self._api(kwargs).stream(**kwargs, timeout=self.settings.stream_timeout_s) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if (not written and wrote and block.type == "tool_use"
                                and (t := tools.get(block.name)) is not None and t.after_reply):
                            written = True
                            yield ReplyWritten()
                        continue
                    if event.type != "content_block_delta":
                        continue
                    if event.delta.type == "text_delta":
                        wrote = wrote or bool(event.delta.text.strip())
                        yield TextDelta(event.delta.text)
                    elif event.delta.type == "thinking_delta" and event.delta.thinking:
                        yield ThinkingDelta(event.delta.thinking)
                final = await stream.get_final_message()
            for k, v in _usage(final.usage).items():
                usage[k] = usage.get(k, 0) + v
            calls = [b for b in final.content if b.type == "tool_use"]
            # Only a turn that ended on tool use runs tools (not one cut off by max_tokens or a refusal).
            if final.stop_reason != "tool_use" or not calls or not remaining:
                break
            results: list[dict[str, Any]] = []
            for call in calls:
                if written:  # the after-reply tools run, nothing else, and nothing more is asked
                    if (t := tools.get(call.name)) is not None and t.after_reply:
                        yield ToolUse(call.name, call.input)
                        await _run_tool(t, call)
                    continue
                if not remaining:
                    results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                    "content": "本次回复的工具调用次数已用完。"})
                    continue
                yield ToolUse(call.name, call.input)
                results.append(await _run_tool(tools.get(call.name), call))
                remaining -= 1
            if written:
                break
            if not remaining:
                kwargs["tool_choice"] = {"type": "none"}
            # Appended after the history's cache breakpoint; the thinking blocks go back unchanged.
            kwargs["messages"] = [
                *kwargs["messages"],
                {"role": "assistant", "content": final.content},
                {"role": "user", "content": results},
            ]
        yield Done(
            stop_reason="end" if written else _STOP_REASONS.get(final.stop_reason or "", final.stop_reason or "end"),
            usage=usage,
            model=final.model,
        )

    async def complete(self, req: ChatRequest, json_schema: dict[str, Any] | None = None) -> Completion:
        kwargs = self._kwargs(req)
        if json_schema is not None:
            kwargs.setdefault("output_config", {})["format"] = {"type": "json_schema", "schema": json_schema}
        final = await self._api(kwargs).create(**kwargs)
        text = "".join(b.text for b in final.content if b.type == "text")
        return Completion(text, self._done(final))

    @staticmethod
    def _done(final: Any) -> Done:
        return Done(
            stop_reason=_STOP_REASONS.get(final.stop_reason or "", final.stop_reason or "end"),
            usage=_usage(final.usage),
            model=final.model,
        )

    async def count_tokens(self, system: str | None, turns: list[Turn]) -> int | None:
        kwargs: dict[str, Any] = dict(
            model=self.settings.model,
            messages=[{"role": t.role, "content": _blocks(t)} for t in turns],
        )
        if system:
            kwargs["system"] = system
        try:
            result = await self.client.messages.count_tokens(**kwargs)
        except anthropic.APIStatusError:
            return None  # Anthropic-compatible servers mostly have no counting endpoint
        return result.input_tokens

    async def count_request_tokens(self, req: ChatRequest) -> int | None:
        kwargs = self._reply_kwargs(req)
        del kwargs["max_tokens"]
        kwargs.pop("fallbacks", None)
        for name in ("extra_body", "extra_headers", "temperature", "top_p"):
            kwargs.pop(name, None)
        try:
            result = await self._api(kwargs).count_tokens(**kwargs)
        except anthropic.APIStatusError:
            return None
        return result.input_tokens
