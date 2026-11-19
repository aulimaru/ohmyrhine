"""OpenAI Chat Completions, and every endpoint that speaks it (`base_url`): DeepSeek, Qwen
(DashScope), Kimi, GLM, Doubao, Gemini's OpenAI-compatible API, OpenRouter, Groq, Mistral, xAI,
SiliconFlow, and local servers such as Ollama, vLLM and LM Studio.

What a request carries comes from the task's settings (catalog.ModelSettings): the model table
says how each known model takes structured output, effort and the off switch for thinking.
Servers still differ, so a request adapts when a server turns part of it down (a 400 or 422
naming that part): structured output falls back from json_schema to json_object to a plain
instruction, the output cap moves between `max_tokens` and `max_completion_tokens`, and
`reasoning_effort` and usage reporting are dropped. Each change is logged once and kept for the
rest of the process. Images are never dropped: a model that cannot read them is a config error.
Reasoning is read from `reasoning_content` or `reasoning`, where a server streams it.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import openai

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

log = logging.getLogger(__name__)

_STOP_REASONS = {"stop": "end", "length": "max_tokens", "content_filter": "refusal", "tool_calls": "end"}
# Tool calls allowed per reply, as with the Anthropic API.
_TOOL_CALLS = 4


def _content(turn: Turn) -> str | list[dict[str, Any]]:
    # Text stays one string (some servers take nothing else, and they cache by prefix anyway);
    # with images, the text between them is one part each.
    if isinstance(turn.content, str) or not turn.images:
        return turn.text
    parts: list[dict[str, Any]] = []
    for p in turn.content:
        if isinstance(p, Image):
            url = f"data:{p.mime};base64,{base64.standard_b64encode(p.data).decode('ascii')}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif p and parts and parts[-1]["type"] == "text":
            parts[-1]["text"] += p
        elif p:
            parts.append({"type": "text", "text": p})
    return parts


def _messages(req: ChatRequest) -> list[dict[str, Any]]:
    system = f"{req.system}\n\n{req.chat_context}" if req.chat_context else req.system
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages += [{"role": t.role, "content": _content(t)} for t in req.turns]
    if req.recall and req.turns and req.turns[-1].role == "user":
        # After the prefix the provider caches by itself.
        last = messages[-1]
        if isinstance(last["content"], str):
            last["content"] += "\n\n" + req.recall
        else:
            last["content"].append({"type": "text", "text": req.recall})
    return messages


def _usage(u: Any) -> dict[str, int]:
    if u is None:
        return {}
    details = getattr(u, "prompt_tokens_details", None)
    # OpenAI and most others report cached input here; DeepSeek as prompt_cache_hit_tokens.
    cached = (getattr(details, "cached_tokens", None) if details else None) or getattr(u, "prompt_cache_hit_tokens", 0) or 0
    # What went into the cache, where a server says (OpenAI bills these writes from GPT-5.6 on).
    written = (getattr(details, "cache_write_tokens", None) if details else None) or 0
    usage = {"input": (u.prompt_tokens or 0) - cached - written, "output": u.completion_tokens or 0, "cache_read": cached}
    if written:
        usage["cache_write"] = written
    return usage


def _add(usage: dict[str, int], more: dict[str, int]) -> None:
    for k, v in more.items():
        usage[k] = usage.get(k, 0) + v


def _json_text(text: str) -> str:
    """The JSON object in an answer that may wrap it in prose or a code fence."""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text


async def _run_tool(tool: Tool | None, name: str, args: dict[str, Any] | None) -> str:
    """A tool's result as the model reads it; there is no error flag, so errors say so."""
    if tool is None:
        return f"错误：没有 {name} 这个工具"
    if args is None:
        return "错误：参数不是一个 JSON 对象"
    try:
        return await tool.run(args)
    except ToolError as e:
        return f"错误：{e}"


def _parse(arguments: str) -> dict[str, Any] | None:
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return None
    return args if isinstance(args, dict) else None


class OpenAIChat:
    # What this server turned down, left out or replaced from then on (see _adapt), and how it
    # takes the output cap and structured output.
    _refused: frozenset[str] = frozenset()
    _cap = "max_tokens"
    _format = "json_schema"

    def __init__(self, settings: ModelSettings):
        kwargs: dict[str, Any] = {}
        if api_key := settings.api_key():
            kwargs["api_key"] = api_key
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        if settings.max_retries is not None:
            kwargs["max_retries"] = settings.max_retries
        self.client = openai.AsyncOpenAI(**kwargs)
        self.settings = settings
        # Either name of the output cap is switched to the other if refused.
        self._cap = settings.max_tokens_field
        self._format = settings.structured

    def _kwargs(self, req: ChatRequest) -> dict[str, Any]:
        s = self.settings
        kwargs: dict[str, Any] = {"model": s.model, "messages": _messages(req), self._cap: s.max_tokens}
        if s.effort and "reasoning_effort" not in self._refused:
            kwargs["reasoning_effort"] = s.effort
        for name in ("temperature", "top_p", "extra_headers"):
            if (value := getattr(s, name)) is not None:
                kwargs[name] = value
        extra = dict(s.extra_body or {})
        if s.thinking is False and s.thinking_off:
            off = dict(s.thinking_off)
            extra |= off.pop("extra_body", {})
            kwargs |= off
        if extra:
            kwargs["extra_body"] = extra
        return kwargs

    def _adapt(self, error: openai.APIStatusError, kwargs: dict[str, Any]) -> bool:
        """Change the request after the server refused part of it; False if nothing to change."""
        said = str(error).lower()
        change = None
        if self._cap in said and self._cap in kwargs:
            other = "max_tokens" if self._cap == "max_completion_tokens" else "max_completion_tokens"
            kwargs[other] = kwargs.pop(self._cap)
            change, self._cap = f"输出上限改用 {other}", other
        elif "response_format" in kwargs and ("response_format" in said or "json_schema" in said or "json_object" in said):
            self._format = "json_object" if self._format == "json_schema" else "prompt"
            change = f"结构化输出改用 {self._format}"
        elif "reasoning_effort" in kwargs and "reasoning" in said:
            del kwargs["reasoning_effort"]
            self._refused = self._refused | {"reasoning_effort"}
            change = "不再传 reasoning_effort"
        elif "stream_options" in kwargs and "stream_options" in said:
            del kwargs["stream_options"]
            self._refused = self._refused | {"stream_options"}
            change = "不再要求流式返回用量"
        if change:
            log.warning("%s（模型 %s）：服务端拒绝了请求的一部分，%s：%s", type(self).__name__, self.settings.model, change, error)
        return change is not None

    async def _create(self, kwargs: dict[str, Any], json_schema: dict[str, Any] | None = None) -> Any:
        for _ in range(5):
            if json_schema is not None:
                kwargs = self._structured(kwargs, json_schema)
            try:
                return await self.client.chat.completions.create(**kwargs)
            except (openai.BadRequestError, openai.UnprocessableEntityError) as e:
                if not self._adapt(e, kwargs):
                    raise
        return await self.client.chat.completions.create(**kwargs)

    def _structured(self, kwargs: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        """Ask for JSON in the way this server takes."""
        kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}
        if self._format == "json_schema":
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}}
            return kwargs
        if self._format == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        # Without a schema the server can enforce, it goes in the instructions (json_object also
        # needs the word JSON in them).
        rule = f"只输出一个 JSON 对象，不要别的文字，格式符合这个 JSON Schema：{json.dumps(schema, ensure_ascii=False)}"
        system = kwargs["messages"][0]
        if rule not in system["content"]:
            kwargs["messages"] = [{"role": "system", "content": f"{system['content']}\n\n{rule}"}, *kwargs["messages"][1:]]
        return kwargs

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        kwargs = self._kwargs(req) | {"stream": True, "timeout": self.settings.stream_timeout_s}
        if "stream_options" not in self._refused:
            kwargs["stream_options"] = {"include_usage": True}
        tools = {t.name: t for t in req.tools}
        if req.tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in req.tools
            ]
        usage: dict[str, int] = {}
        remaining = _TOOL_CALLS
        # The reply was written and an after-reply tool began: whatever follows is notes.
        written = False
        model, finish = None, "stop"
        # A server that ignores tool_choice "none" must not keep the loop going.
        for _ in range(_TOOL_CALLS + 2):
            text: list[str] = []
            reasoning: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            wrote, finish = False, "stop"
            async for chunk in await self._create(kwargs):
                model = getattr(chunk, "model", None) or model
                if getattr(chunk, "usage", None):
                    _add(usage, _usage(chunk.usage))
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is not None:
                    # OpenAI's own Chat Completions never returns reasoning; some compatible
                    # servers stream it, as `reasoning_content` (DeepSeek, Qwen) or `reasoning`.
                    thought = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                    if isinstance(thought, str) and thought:
                        reasoning.append(thought)
                        if req.show_thinking:
                            yield ThinkingDelta(thought)
                    if delta.content:
                        text.append(delta.content)
                        wrote = wrote or bool(delta.content.strip())
                        yield TextDelta(delta.content)
                    for tc in getattr(delta, "tool_calls", None) or ():
                        index = getattr(tc, "index", None)
                        if index is None:  # a server that leaves it out: a new id starts a new call
                            index = len(calls) if tc.id or not calls else max(calls)
                        call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        call["id"] = tc.id or call["id"]
                        if tc.function is not None:
                            if tc.function.name and not call["name"]:
                                call["name"] = tc.function.name
                                t = tools.get(call["name"])
                                if not written and wrote and t is not None and t.after_reply:
                                    written = True
                                    yield ReplyWritten()
                            call["arguments"] += tc.function.arguments or ""
                    if getattr(delta, "refusal", None):
                        finish = "content_filter"
                if choice.finish_reason and finish != "content_filter":
                    finish = choice.finish_reason
            # Only a turn that called tools runs them (not one cut off by the cap or refused).
            ran = [calls[i] for i in sorted(calls) if calls[i]["name"]]
            if not ran or finish in ("length", "content_filter"):
                break
            results: list[dict[str, Any]] = []
            for n, call in enumerate(ran):
                call["id"] = call["id"] or f"call_{n}"
                tool, args = tools.get(call["name"]), _parse(call["arguments"])
                if written:  # the after-reply tools run, nothing else, and nothing more is asked
                    if tool is not None and tool.after_reply:
                        yield ToolUse(call["name"], args or {})
                        await _run_tool(tool, call["name"], args)
                    continue
                if not remaining:
                    results.append({"role": "tool", "tool_call_id": call["id"], "content": "错误：本次回复的工具调用次数已用完。"})
                    continue
                yield ToolUse(call["name"], args or {})
                results.append({"role": "tool", "tool_call_id": call["id"], "content": await _run_tool(tool, call["name"], args)})
                remaining -= 1
            if written:
                break
            asked: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text) or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                    for c in ran
                ],
            }
            if reasoning:
                # DeepSeek's thinking mode wants the reasoning back within one tool loop; servers
                # that never send it never get it.
                asked["reasoning_content"] = "".join(reasoning)
            kwargs["messages"] = [*kwargs["messages"], asked, *results]
            if not remaining:
                kwargs["tool_choice"] = "none"
        yield Done(stop_reason="end" if written else _STOP_REASONS.get(finish, finish), usage=usage, model=model)

    async def complete(self, req: ChatRequest, json_schema: dict[str, Any] | None = None) -> Completion:
        resp = await self._create(self._kwargs(req), json_schema)
        choice = resp.choices[0]
        finish = "content_filter" if getattr(choice.message, "refusal", None) else choice.finish_reason or "stop"
        done = Done(stop_reason=_STOP_REASONS.get(finish, finish), usage=_usage(resp.usage), model=resp.model)
        text = choice.message.content or ""
        return Completion(_json_text(text) if json_schema is not None else text, done)

    async def count_tokens(self, system: str | None, turns: list[Turn]) -> int | None:
        return None

    async def count_request_tokens(self, req: ChatRequest) -> int | None:
        return None
