"""Provider-neutral chat interface."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class Image:
    data: bytes
    mime: str  # image/jpeg | image/png | image/gif | image/webp


# A turn's content is plain text, or text interleaved with images.
Part = str | Image


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    content: str | tuple[Part, ...]

    @property
    def text(self) -> str:
        """The text of the turn with images left out (they're described in the text too)."""
        if isinstance(self.content, str):
            return self.content
        return "".join(p for p in self.content if isinstance(p, str))

    @property
    def images(self) -> list[Image]:
        return [] if isinstance(self.content, str) else [p for p in self.content if isinstance(p, Image)]


class ToolError(Exception):
    """Raised by a tool's `run` for a bad call; the model is told the message and may retry."""


@dataclass(frozen=True)
class Tool:
    """A tool the model may call while writing its reply. The provider runs the loop: it
    calls `run` with the input and hands the result back, so the engine only sees the reply.
    Keep the definition fixed across requests: tools are the start of the cached prefix."""

    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[str]]
    # Used after the reply is written (taking a note): when the model writes its reply and then
    # calls such a tool, ReplyWritten is streamed, the after-reply tools run and the reply ends
    # there, with no request to see their results.
    after_reply: bool = False


@dataclass(frozen=True)
class ChatRequest:
    # Stable across turns (persona, rules, canon); providers put a cache breakpoint after it.
    system: str
    # Strictly alternating, starting with a user turn.
    turns: list[Turn]
    # Debug: ask the provider to stream the model's reasoning (a summary, where that's all
    # the provider exposes). Toggling it changes the request, so it costs one cache rewrite.
    show_thinking: bool = False
    # Per-chat context (scenes, the summary of earlier chat), sent after `system`. It changes
    # now and then, so it sits behind the cache breakpoint of the stable part.
    chat_context: str | None = None
    # Per-turn context (a reminder to tidy her notes, the unsent rest of a reply), added to the
    # end of the last user turn after the history's cache breakpoint, so it can change every
    # turn without costing the cached history.
    recall: str | None = None
    # Cache the conversation prefix too. Off for one-off side tasks (judging, captions,
    # memory upkeep), whose transcript is never sent twice: a cache write costs more than input.
    cache_history: bool = True
    # Tools for the reply (streamed requests only). Providers without tool support ignore them.
    tools: tuple[Tool, ...] = ()
    # `system` is the same for every chat of the character and stays in use for hours, with
    # quiet spells in between: providers that can, keep its cache for an hour instead of minutes.
    long_lived_system: bool = False


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """Model reasoning shown for debugging; never part of the reply or the stored history."""

    text: str


@dataclass(frozen=True)
class Done:
    # Normalized: "end", "max_tokens", "refusal", or the provider's raw value. The engine adds
    # "silent" for a reply the character chose not to give.
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)
    # Which model actually answered (may differ when a fallback ran).
    model: str | None = None


@dataclass(frozen=True)
class ToolUse:
    """The model called a tool (already run by the provider); for display and logs."""

    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ReplyWritten:
    """The reply's text is complete; only after-reply tools follow (see Tool.after_reply). The
    reply can be sent now while the rest of the stream finishes."""


StreamEvent = TextDelta | ThinkingDelta | ToolUse | ReplyWritten | Done


@dataclass(frozen=True)
class Completion:
    """A whole, non-streamed answer, for small side tasks (reply judging, image captions)."""

    text: str
    done: Done


class ChatProvider(Protocol):
    def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        """Yield TextDelta (and, with show_thinking, ThinkingDelta; ToolUse for each tool the
        model called) events, then exactly one Done."""
        ...

    async def complete(self, req: ChatRequest, json_schema: dict[str, Any] | None = None) -> Completion:
        """One answer in full. With json_schema the text is JSON matching it."""
        ...

    async def count_tokens(self, system: str | None, turns: list[Turn]) -> int | None:
        """Exact input token count for this model, or None if the provider can't count."""
        ...

    async def count_request_tokens(self, req: ChatRequest) -> int | None:
        """Input count for the full reply request, including tools and per-chat context."""
        ...
