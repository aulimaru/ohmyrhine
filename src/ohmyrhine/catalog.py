"""The first layer of the model configuration: what the code knows about vendors, models, the
jobs models do here (roles) and the agent harnesses lookups run in.

Everything a request can be set up with is described here, so supporting another vendor, model
or harness is an entry in this file (and, for a new protocol, an adapter in providers/).
config.yaml is the second layer (config.py): it picks among these and overrides fields, and
whatever it leaves out comes from here. Prices are the vendors' list prices as of 2026-11-19,
for prompts below each model's long-context threshold unless a long price is given.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")
Protocol = Literal["anthropic_api", "openai_api", "echo"]


# --- prices ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Price:
    """USD per million tokens. `cache_write` is a write with the default cache lifetime,
    `cache_write_long` one with the longer lifetime (Anthropic's hour; twice the input price
    when unset)."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0
    cache_write_long: float | None = None

    def cost(self, usage: dict[str, int]) -> float:
        long = usage.get("cache_write_1h", 0)
        long_price = self.cache_write_long if self.cache_write_long is not None else 2 * self.input
        return (
            usage.get("input", 0) * self.input
            + usage.get("output", 0) * self.output
            + usage.get("cache_read", 0) * self.cache_read
            + (usage.get("cache_write", 0) - long) * self.cache_write
            + long * long_price
        ) / 1_000_000


@dataclass(frozen=True)
class Pricing:
    """A model's price over time: announced changes, peak hours, long prompts."""

    price: Price
    # Later prices, each from that day on (UTC).
    changes: tuple[tuple[date, Price], ...] = ()
    # Outside the peak hours (weekdays, [start, end) in UTC hours) the off-peak price applies.
    off_peak: Price | None = None
    peak_hours_utc: tuple[tuple[int, int], ...] = ()
    # A prompt of more than this many tokens is priced as `long_price` as a whole.
    long_above: int | None = None
    long_price: Price | None = None

    def at(self, when: datetime | None = None, prompt_tokens: int = 0) -> Price:
        when = (when or datetime.now(UTC)).astimezone(UTC)
        if self.long_price is not None and self.long_above is not None and prompt_tokens > self.long_above:
            return self.long_price
        price = self.price
        for start, later in self.changes:
            if when.date() >= start:
                price = later
        if self.off_peak is not None and not self._peak(when):
            return self.off_peak
        return price

    def _peak(self, when: datetime) -> bool:
        return when.weekday() < 5 and any(a <= when.hour < b for a, b in self.peak_hours_utc)

    def cost(self, usage: dict[str, int], when: datetime | None = None) -> float:
        prompt = usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_write", 0)
        return self.at(when, prompt).cost(usage)


def flat(input: float, output: float, cache_read: float = 0.0, cache_write: float = 0.0,
         cache_write_long: float | None = None) -> Pricing:
    return Pricing(Price(input, output, cache_read, cache_write, cache_write_long))


# --- models ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reasoning:
    """How a model's thinking is set through its vendor's API."""

    # The effort levels it takes: ours → what is sent. Empty: there is no effort setting.
    efforts: dict[str, str] = field(default_factory=dict)
    # Request fields that turn thinking off; None: it always thinks.
    off: dict[str, Any] | None = None
    # Thinking can be off only at this effort or below.
    off_up_to: Effort | None = None

    def nearest(self, effort: Effort) -> str | None:
        """What to send for an effort it may lack (a role's default): the nearest level it has,
        the higher one on a tie."""
        if not self.efforts:
            return None
        if effort in self.efforts:
            return self.efforts[effort]
        at = EFFORTS.index(effort)
        best = min(self.efforts, key=lambda e: (abs(EFFORTS.index(e) - at), -EFFORTS.index(e)))  # type: ignore[arg-type]
        return self.efforts[best]


def levels(*names: str, **renamed: str) -> dict[str, str]:
    """An effort table: these levels as they are, plus ours sent under another name."""
    return {**{n: n for n in names}, **renamed}


ALL_EFFORTS = levels(*EFFORTS)

ToolSupport = Literal["yes", "no", "no_reasoning"]
Structured = Literal["json_schema", "json_object", "prompt"]


@dataclass(frozen=True)
class ModelSpec:
    vendor: str
    vision: bool
    max_output: int
    reasoning: Reasoning = field(default_factory=Reasoning)
    # Function calling through this vendor's protocol: "no_reasoning" only with thinking off.
    tools: ToolSupport = "yes"
    # The strictest structured output it takes; weaker ones are fallen back to if refused.
    structured: Structured = "json_schema"
    pricing: Pricing | None = None
    # Anthropic's server-side refusal fallback (None: not known either way).
    fallbacks: bool | None = None
    # temperature / top_p (None: not known either way).
    sampling: bool | None = None


@dataclass(frozen=True)
class VendorSpec:
    protocol: Protocol
    base_url: str | None = None
    api_key_env: str | None = None
    # Its name for the output cap (OpenAI's reasoning models refuse max_tokens).
    max_tokens_field: str = "max_tokens"


VENDORS: dict[str, VendorSpec] = {
    "anthropic": VendorSpec("anthropic_api", api_key_env="ANTHROPIC_API_KEY"),
    "openai": VendorSpec("openai_api", api_key_env="OPENAI_API_KEY", max_tokens_field="max_completion_tokens"),
    "deepseek": VendorSpec("openai_api", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "gemini": VendorSpec("openai_api", "https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
    "echo": VendorSpec("echo"),
}

_ANTHROPIC_ALWAYS = Reasoning(ALL_EFFORTS)
# OpenAI's GPT-6 line: effort "none" turns reasoning off where the model allows it.
_OPENAI_NONE = Reasoning(ALL_EFFORTS, off={"reasoning_effort": "none"})
_OPENAI_ALWAYS = Reasoning(ALL_EFFORTS)
# DeepSeek takes low, high and max; thinking is on unless switched off.
_DEEPSEEK = Reasoning(levels("low", "high", "max"), off={"extra_body": {"thinking": {"type": "disabled"}}})
# Gemini 3 and 4 always think.
_GEMINI = Reasoning(levels("low", "medium", "high"))
_GEMINI_XHIGH = Reasoning(levels("low", "medium", "high", "xhigh"))
# DeepSeek is half price off-peak: outside 01:00–04:00 and 06:00–10:00 UTC on weekdays
# (Chinese public holidays, also off-peak, are not counted, so those days are overestimated).
_DEEPSEEK_PEAK = ((1, 4), (6, 10))
_DEC_1 = date(2026, 12, 1)
_JAN_1 = date(2027, 1, 1)

MODELS: dict[str, ModelSpec] = {
    # Anthropic. 5-minute cache writes are 1.25x input, one-hour writes 2x.
    "claude-fable-5-2": ModelSpec(
        "anthropic", True, 128_000, Reasoning(ALL_EFFORTS), pricing=flat(8, 40, 0.20, 10, 16), sampling=False),
    "claude-opus-5-5": ModelSpec(
        "anthropic", True, 128_000, _ANTHROPIC_ALWAYS, pricing=flat(4, 20, 0.20, 5, 8), fallbacks=True),
    "claude-sonnet-5-5": ModelSpec(
        "anthropic", True, 128_000,
        Reasoning(ALL_EFFORTS, off={"thinking": {"type": "between_tools"}}, off_up_to="high"),
        pricing=flat(2, 10, 0.10, 2.5, 4), fallbacks=True),
    "claude-haiku-5-5": ModelSpec(
        "anthropic", True, 128_000,
        Reasoning(ALL_EFFORTS, off={"thinking": {"type": "disabled"}}, off_up_to="high"),
        pricing=Pricing(Price(0.10, 0.50, 0.01, 0.125, 0.20), long_above=100_000,
                        long_price=Price(0.50, 2.50, 0.05, 0.625, 1.0)),
        fallbacks=False, sampling=False),
    # OpenAI, Chat Completions. Cache writes (30 minutes) are 1.25x input from GPT-5.6 on.
    # Above 272K prompt tokens input and cache cost twice as much, output 1.5x.
    "gpt-6.2-astra": ModelSpec(
        "openai", True, 128_000, _OPENAI_ALWAYS,
        pricing=Pricing(Price(8, 40, 0.40, 10), long_above=272_000, long_price=Price(16, 60, 0.80, 20))),
    "gpt-6.2-sol": ModelSpec(
        "openai", True, 128_000, _OPENAI_ALWAYS,
        pricing=Pricing(Price(2, 10, 0.10, 2.5), long_above=272_000, long_price=Price(4, 15, 0.20, 5))),
    "gpt-6.1-sol": ModelSpec(
        "openai", True, 128_000, _OPENAI_ALWAYS, tools="no",  # tools only through the Responses API
        pricing=Pricing(Price(2, 10, 0.10, 2.5), long_above=272_000, long_price=Price(4, 15, 0.20, 5))),
    "gpt-6.1-terra": ModelSpec(
        "openai", True, 128_000, _OPENAI_NONE, tools="no",
        pricing=Pricing(Price(1, 5, 0.05, 1.25), long_above=272_000, long_price=Price(2, 7.5, 0.10, 2.5))),
    "gpt-6-astra": ModelSpec(
        "openai", True, 128_000, _OPENAI_ALWAYS,
        pricing=Pricing(Price(10, 50, 1.00, 12.5), long_above=272_000, long_price=Price(20, 75, 2.00, 25))),
    "gpt-6-sol": ModelSpec(
        "openai", True, 128_000, _OPENAI_NONE, tools="no_reasoning",
        pricing=Pricing(Price(2, 10, 0.20, 2.5), long_above=272_000, long_price=Price(4, 15, 0.40, 5))),
    "gpt-6-luna": ModelSpec(
        "openai", True, 128_000, _OPENAI_NONE, tools="no_reasoning",
        pricing=Pricing(Price(0.10, 0.50, 0.01, 0.125), long_above=272_000, long_price=Price(0.20, 0.75, 0.02, 0.25))),
    "gpt-5.4-mini": ModelSpec(
        "openai", True, 128_000, Reasoning(levels("low", "medium", "high", "xhigh"), off={"reasoning_effort": "none"}),
        pricing=flat(0.75, 4.50, 0.075)),
    # DeepSeek: JSON output is json_object only. Cache hits are the cache_read price.
    "deepseek-flash": ModelSpec(
        "deepseek", True, 384_000, _DEEPSEEK, structured="json_object",
        pricing=Pricing(Price(0.30, 1.20, 0.006), off_peak=Price(0.15, 0.60, 0.003), peak_hours_utc=_DEEPSEEK_PEAK)),
    "deepseek-v4-pro": ModelSpec(
        "deepseek", False, 384_000, _DEEPSEEK, structured="json_object",
        pricing=Pricing(Price(1.32, 3.96, 0.044), off_peak=Price(0.66, 1.98, 0.022), peak_hours_utc=_DEEPSEEK_PEAK)),
    # Gemini, through its OpenAI-compatible API (implicit caching only).
    "gemini-4-carbon": ModelSpec(
        "gemini", True, 65_536, _GEMINI,
        pricing=Pricing(Price(2, 10, 0.10), changes=((_DEC_1, Price(4, 20, 0.20)),))),
    "gemini-4-argon": ModelSpec(
        "gemini", True, 65_536, _GEMINI,
        pricing=Pricing(Price(2, 10, 0.10), changes=((_DEC_1, Price(4, 20, 0.20)),))),
    "gemini-4-neon": ModelSpec("gemini", True, 65_536, _GEMINI_XHIGH, pricing=flat(3, 15, 0.15)),
    "gemini-4-hydrogen": ModelSpec("gemini", True, 65_536, _GEMINI_XHIGH, pricing=flat(1.5, 7.5, 0.075)),
    "gemini-3.8-flash": ModelSpec(
        "gemini", True, 65_536, _GEMINI,
        pricing=Pricing(Price(0.75, 3.75, 0.075), changes=((_JAN_1, Price(1.5, 7.5, 0.15)),))),
    "gemini-3.6-flash": ModelSpec(
        "gemini", True, 65_536, _GEMINI,
        pricing=Pricing(Price(0.75, 3.75, 0.075), changes=((_JAN_1, Price(1.5, 7.5, 0.15)),))),
    "gemini-3.5-flash-lite": ModelSpec("gemini", True, 65_536, _GEMINI, pricing=flat(0.30, 2.50, 0.03)),
}


# --- roles ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Role:
    """A job a model does here (a task in config.yaml), and what it needs from the model."""

    description: str
    # It calls tools (her notes, reading long messages, lookups).
    tools: bool = False
    # It is sent images: always, or only while media.live_images > 0 ("live").
    images: bool | Literal["live"] = False
    # Defaults where config.yaml says nothing.
    effort: Effort | None = None
    # False: thinking off where the model allows it (a quick yes or no needs none).
    thinking: bool | None = None
    max_tokens: int = 8000
    # The cap with thinking off.
    max_tokens_quick: int | None = None


ROLES: dict[str, Role] = {
    "roleplay": Role("角色回复", tools=True, images="live", effort="medium", max_tokens=8000),
    "reply_judge": Role("群聊筛选", effort="low", thinking=False, max_tokens=4000, max_tokens_quick=150),
    "vision": Role("看图写描述", images=True, effort="low", thinking=False, max_tokens=4000, max_tokens_quick=500),
    "summary": Role("滚动摘要和长消息摘要", effort="medium", max_tokens=8000),
}


# --- agent harnesses ------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessSpec:
    """A command-line agent lookups can run in, on the account it is logged in with."""

    command: str
    # The file in its working directory it reads its instructions from.
    instructions: str
    # Effort levels it takes (ours → its own).
    efforts: dict[str, str]
    # Its model when the config names none (None: the harness's own default).
    model: str | None = None


HARNESSES: dict[str, HarnessSpec] = {
    "claude": HarnessSpec("claude", "CLAUDE.md", ALL_EFFORTS, "sonnet"),
    "codex": HarnessSpec("codex", "AGENTS.md", ALL_EFFORTS),
    "agy": HarnessSpec("agy", "AGENTS.md", ALL_EFFORTS),
}


# --- what a task's requests are made with ---------------------------------------------------


@dataclass(frozen=True)
class ModelSettings:
    """One task's model, fully resolved from config.yaml over the tables above: everything a
    provider adapter (providers/) builds its requests from."""

    task: str
    provider: str
    protocol: Protocol
    model: str | None
    # What is known of the model; for one outside MODELS, what config.yaml says of it.
    spec: ModelSpec | None
    base_url: str | None = None
    api_key_env: str | None = None
    api_key_file: Path | None = None
    max_tokens: int = 8000
    max_tokens_field: str = "max_tokens"
    # The effort as the API takes it; None sends none.
    effort: str | None = None
    # False: thinking off, with `thinking_off` merged into the request.
    thinking: bool | None = None
    thinking_off: dict[str, Any] | None = None
    fallbacks: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    # A streamed reply that sends nothing for this long has stalled.
    stream_timeout_s: float = 60.0
    max_retries: int | None = None
    extra_body: dict[str, Any] | None = None
    extra_headers: dict[str, str] | None = None

    @property
    def structured(self) -> Structured:
        return self.spec.structured if self.spec else "json_schema"

    def api_key(self) -> str | None:
        if self.api_key_env and os.environ.get(self.api_key_env):
            return os.environ[self.api_key_env]
        if self.api_key_file:
            path = self.api_key_file.expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"api_key_file 不存在：{path}")
            return path.read_text(encoding="utf-8").strip()
        return None
