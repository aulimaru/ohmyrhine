"""Project configuration (config.yaml)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from . import plugins
from .catalog import (
    EFFORTS,
    HARNESSES,
    MODELS,
    ROLES,
    VENDORS,
    Effort,
    ModelSettings,
    ModelSpec,
    Price,
    Pricing,
    Protocol,
    Reasoning,
)

CONFIG_FILE = "config.yaml"


class ProviderConfig(BaseModel):
    """Where a provider is reached. The built-in vendors (catalog.VENDORS: anthropic, openai,
    deepseek, gemini, echo) need no entry at all, or only what differs (a key file, a proxy);
    any other name is a server of one of the protocols and says which (`type`)."""

    model_config = ConfigDict(extra="forbid")

    type: Protocol | None = None
    # Where the key comes from, first match wins: env var, then file, then SDK defaults.
    api_key_env: str | None = None
    api_key_file: Path | None = None
    base_url: str | None = None
    # The request field for the output cap, where a server wants max_completion_tokens.
    max_tokens_field: str | None = None


class PriceConfig(BaseModel):
    """USD per million tokens."""

    model_config = ConfigDict(extra="forbid")

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0
    cache_write_long: float | None = None


class ModelConfig(BaseModel):
    """What is known of a model, for one the built-in table (catalog.MODELS) lacks, or to
    correct one it has: every field left out keeps the table's value."""

    model_config = ConfigDict(extra="forbid")

    vision: bool | None = None
    tools: Literal["yes", "no", "no_reasoning"] | None = None
    structured: Literal["json_schema", "json_object", "prompt"] | None = None
    max_output: int | None = Field(default=None, gt=0)
    # The effort levels it takes, or ours mapped to its names ({max: high}).
    efforts: list[Effort] | dict[Effort, str] | None = None
    # Request fields that turn its thinking off (e.g. {extra_body: {enable_thinking: false}}).
    thinking_off: dict[str, Any] | None = None
    thinking_off_up_to: Effort | None = None
    fallbacks: bool | None = None
    sampling: bool | None = None
    price: PriceConfig | None = None


class TaskConfig(BaseModel):
    """One task's model. Only `provider` and `model` are needed; everything else defaults to
    what the task (catalog.ROLES) and the model (catalog.MODELS) call for."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    effort: Effort | None = None
    # False turns thinking off where the model allows it; None leaves the task's default.
    thinking: bool | None = None
    # Anthropic server-side refusal fallback; "default" lets the API pick the model.
    fallbacks: Literal["default"] | None = None
    temperature: float | None = None
    top_p: float | None = None
    # A streamed reply that sends nothing for this long has stalled (seconds).
    stream_timeout_s: float | None = Field(default=None, gt=0)
    # Retries of a request that failed before any response (the SDKs' default is 2).
    max_retries: int | None = Field(default=None, ge=0)
    # Merged into the request as they are, for what only some providers have.
    extra_body: dict[str, Any] | None = None
    extra_headers: dict[str, str] | None = None


class GroupConfig(BaseModel):
    """How the character behaves in group chats."""

    model_config = ConfigDict(extra="forbid")

    # Ask the judge when to act, and whether to speak when nobody @-ed or quoted the character.
    # Without that task configured, only @, quotes and the character's name trigger a reply.
    judge: bool = True
    # How many recent messages the judge sees.
    judge_context: int = Field(default=12, gt=0)
    wait_short_s: float = Field(default=6, ge=0)
    wait_long_s: float = Field(default=15, ge=0)
    # Attribute connector-marked rolls to the character using that account.
    dice: bool = False


class MemoryConfig(BaseModel):
    """Long-term memory: the character's own notes, and the rolling summary (needs the `summary` task)."""

    model_config = ConfigDict(extra="forbid")

    # Length cap for the rolling summary of messages that left the history window. It sits in
    # the cached part of the prompt, so a fuller summary costs little after it is written.
    summary_chars: int = 1500
    # Past this many characters of notes in a chat she is asked to merge and prune them. All of
    # them are still shown in the cached context and transcript.
    notes_chars: int = Field(default=4000, gt=0)


class LookupReference(BaseModel):
    """Local material to consult first for the topics in its description."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    path: Path
    format: Literal["text", "git_html"] = "text"


class AgentToolConfig(BaseModel):
    """The agent one lookup tool runs in: a command-line agent on the account it is logged in
    with (catalog.HARNESSES), so it uses that subscription, never an API key."""

    model_config = ConfigDict(extra="forbid")

    harness: Literal["claude", "codex", "agy"]
    # The harness's model name; the harness's default model when unset.
    model: str | None = None
    effort: Effort | None = None
    # The executable, when it is not on PATH under the harness's usual name.
    command: str | None = None


class LookupConfig(BaseModel):
    """The helper agents the character hands lookups to (engine/lookup.py). Each tool is on
    when it is configured, and its harness is installed."""

    model_config = ConfigDict(extra="forbid")

    # agent_history: the chat's earlier messages.
    history: AgentToolConfig | None = None
    # agent_search: the web, and the reference below.
    search: AgentToolConfig | None = None
    # A run that hangs is killed after this long; it has no budget otherwise.
    timeout_s: float = Field(default=900, gt=0)
    # Runs at once, across chats (each is an agent process).
    concurrency: int = Field(default=2, gt=0)
    # Its working directories, one per chat and tool (data_dir/agent when unset).
    dir: Path | None = None
    reference: LookupReference | None = None


class MediaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Images are downscaled to fit this many pixels on the long edge before storing.
    max_edge: int = 1024
    # Images in the turn being answered are sent to the model as images (newest first, up to
    # this many); everything older is represented by its text description only.
    live_images: int = Field(default=4, ge=0)


class CharacterConfig(BaseModel):
    """One character's own settings over the shared ones (Config.for_character). Sections go
    field by field; a task, and a lookup tool, is replaced as a whole; null drops what is
    shared. Providers, keys, models and directories are shared only, so a key is written once."""

    model_config = ConfigDict(extra="forbid")

    history_tokens: int | None = None
    log_thinking: bool | None = None
    group: dict[str, Any] | None = None
    media: dict[str, Any] | None = None
    memory: dict[str, Any] | None = None
    lookup: dict[str, Any] | None = None
    tasks: dict[str, dict[str, Any] | None] | None = None


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    characters_dir: Path = Path("characters")
    data_dir: Path = Path("data")
    # Budget for the history sent to the model, in estimated tokens. The gateway compacts a
    # chat's window at 5/6 of it, a request at the whole of it, keeping at least a quarter
    # (Engine.compact, Engine._cut); in between the history prefix (and its cache) stays put.
    history_tokens: int = Field(default=24000, gt=0)
    # Request the model's reasoning summary on every reply and append it to
    # data/logs/<char>-thinking.jsonl. Kept constant so the prompt cache isn't disturbed;
    # the chat UI only decides whether to display it.
    log_thinking: bool = True
    group: GroupConfig = Field(default_factory=GroupConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    lookup: LookupConfig = Field(default_factory=LookupConfig)
    # Only what differs from the built-in vendors, and servers of other names.
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    tasks: dict[str, TaskConfig] = Field(
        default_factory=lambda: {"roleplay": TaskConfig(provider="anthropic", model="claude-opus-5-5")}
    )
    # Per character (by pack id; its instances share it): what differs from the above.
    characters: dict[str, CharacterConfig] = Field(default_factory=dict)
    # Modules to import at start that extend the engine (plugins.py), e.g. "my_site.history".
    plugins: list[str] = Field(default_factory=list)
    # The config a character's was made from, and those made so far.
    _root: Config | None = PrivateAttr(default=None)
    _made: dict[str, Config] = PrivateAttr(default_factory=dict)

    def for_character(self, char_id: str) -> Config:
        """This character's config: the shared one with its own section over it."""
        root = self._root or self
        own = root.characters.get(char_id)
        if own is None:
            return root
        if char_id not in root._made:
            data = root.model_dump()
            data.pop("characters")
            for key, value in own.model_dump(exclude_unset=True).items():
                if not isinstance(data.get(key), dict):
                    if value is None:
                        data.pop(key)
                    else:
                        data[key] = value
                    continue
                for name, v in (value or {}).items():
                    if v is None:
                        data[key].pop(name, None)
                    else:
                        data[key][name] = v
            try:
                made = Config.model_validate(data)
            except ValidationError as e:
                raise ConfigError(f"characters.{char_id}：{e}") from None
            made._root = root
            root._made[char_id] = made
        return root._made[char_id]

    def price(self, model: str | None) -> Pricing | None:
        if model is None:
            return None
        override = self.models.get(model)
        if override is not None and override.price is not None:
            return Pricing(Price(**override.price.model_dump()))
        spec = MODELS.get(model)
        return spec.pricing if spec else None

    def has_task(self, name: str) -> bool:
        return name in self.tasks

    def settings(self, name: str) -> ModelSettings:
        """A task's settings, resolved (raises ConfigError on what cannot work)."""
        if name not in self.tasks:
            raise KeyError(f"config 中没有配置任务 {name!r}")
        return resolve(self, name, self.tasks[name])

    def check(self) -> None:
        """Resolve every task and lookup tool, so a config that cannot work fails at start."""
        for char_id in self.characters:
            try:
                self.for_character(char_id).check()
            except ConfigError as e:
                if str(e).startswith("characters."):
                    raise
                raise ConfigError(f"characters.{char_id}.{e}") from None
        for name in self.tasks:
            self.settings(name)
        for kind, tool in (("history", self.lookup.history), ("search", self.lookup.search)):
            if tool is not None and tool.effort and tool.effort not in HARNESSES[tool.harness].efforts:
                raise ConfigError(f"lookup.{kind}：{tool.harness} 不支持 effort {tool.effort}")


class ConfigError(ValueError):
    """A config that cannot work, said in terms of config.yaml."""


def _spec(cfg: Config, model: str) -> ModelSpec | None:
    """The built-in entry for a model with config.yaml's corrections, or only those."""
    base = MODELS.get(model)
    override = cfg.models.get(model)
    if override is None:
        return base
    fields = override.model_dump(exclude_none=True)
    reasoning = base.reasoning if base else Reasoning()
    if "efforts" in fields:
        efforts = fields.pop("efforts")
        reasoning = replace(reasoning, efforts=efforts if isinstance(efforts, dict) else {e: e for e in efforts})
    if "thinking_off" in fields:
        reasoning = replace(reasoning, off=fields.pop("thinking_off"))
    if "thinking_off_up_to" in fields:
        reasoning = replace(reasoning, off_up_to=fields.pop("thinking_off_up_to"))
    if "price" in fields:
        fields["pricing"] = Pricing(Price(**fields.pop("price")))
    if base is None:
        if "vision" not in fields:
            fields["vision"] = None
        return ModelSpec(vendor="", max_output=fields.pop("max_output", 0), reasoning=reasoning, **fields)
    return replace(base, reasoning=reasoning, **fields)


def resolve(cfg: Config, name: str, task: TaskConfig) -> ModelSettings:
    """Layer two over layer one: a task's config.yaml entry, with the defaults of its role, its
    provider and its model filled in, checked against what the model can do."""
    where = f"tasks.{name}"
    role = ROLES.get(name)
    if role is None:
        raise ConfigError(f"{where}：没有这个任务，可用的有 {'、'.join(ROLES)}")
    vendor = VENDORS.get(task.provider)
    entry = cfg.providers.get(task.provider)
    if vendor is None and entry is None:
        raise ConfigError(f"{where}：provider {task.provider!r} 既不是内置的（{'、'.join(VENDORS)}），也没在 providers 里配置")
    protocol = (entry.type if entry and entry.type else None) or (vendor.protocol if vendor else None)
    if protocol is None:
        raise ConfigError(f"providers.{task.provider}：要写 type（anthropic_api / openai_api / echo）")
    base: dict[str, Any] = {
        "task": name, "provider": task.provider, "protocol": protocol, "model": task.model,
        "base_url": (entry.base_url if entry and entry.base_url else None) or (vendor.base_url if vendor else None),
        # A key file given for a built-in vendor stands in for its usual environment variable.
        "api_key_env": (entry.api_key_env if entry and entry.api_key_env else None)
        or (vendor.api_key_env if vendor and not (entry and entry.api_key_file) else None),
        "api_key_file": entry.api_key_file if entry else None,
        "max_tokens_field": (entry.max_tokens_field if entry and entry.max_tokens_field else None)
        or (vendor.max_tokens_field if vendor else "max_tokens"),
        "temperature": task.temperature, "top_p": task.top_p, "max_retries": task.max_retries,
        "extra_body": task.extra_body, "extra_headers": task.extra_headers,
    }
    if task.stream_timeout_s is not None:
        base["stream_timeout_s"] = task.stream_timeout_s
    if protocol == "echo":
        return ModelSettings(spec=None, max_tokens=task.max_tokens or role.max_tokens, **base)
    if not task.model:
        raise ConfigError(f"{where}：要写 model")
    model = task.model
    spec = _spec(cfg, model)
    known = spec is not None and spec.vendor in VENDORS and VENDORS[spec.vendor].protocol == protocol
    if spec is None:
        spec = ModelSpec(vendor="", vision=None, max_output=0)  # type: ignore[arg-type]
    # The way thinking is set belongs to the vendor's API; through another protocol (a proxy
    # serving the model) it is not known.
    reasoning = spec.reasoning if known or not spec.vendor else Reasoning()

    needs_images = role.images is True or (role.images == "live" and cfg.media.live_images > 0)
    if needs_images and spec.vision is not True:
        if spec.vision is None:
            raise ConfigError(f"{where}：不知道 {model} 能不能看图，在 models.{model} 里写明 vision")
        hint = "；或者把 media.live_images 设为 0，只给它看图片的文字描述" if role.images == "live" else ""
        raise ConfigError(f"{where}：{role.description}要看图，{model} 不能读图{hint}")

    # Thinking: off where asked, or where the role would rather have it off and the model can.
    thinking = task.thinking if task.thinking is not None else role.thinking
    if thinking is False and reasoning.off is None:
        if task.thinking is False:
            raise ConfigError(f"{where}：{model} 的思考关不掉，去掉 thinking: false")
        thinking = None
    if task.effort is not None:
        if task.effort not in reasoning.efforts:
            if not reasoning.efforts and not spec.vendor:
                effort: str | None = task.effort  # a model nothing is known of: sent as it is
            else:
                took = "、".join(reasoning.efforts) or "没有 effort 这一项"
                raise ConfigError(f"{where}：{model} 不支持 effort {task.effort}（{took}）")
        else:
            effort = reasoning.efforts[task.effort]
        asked: Effort | None = task.effort
    else:
        asked = role.effort
        effort = reasoning.nearest(role.effort) if role.effort else None
    if thinking is False and reasoning.off_up_to and asked and EFFORTS.index(asked) > EFFORTS.index(reasoning.off_up_to):
        if task.thinking is False and task.effort is not None:
            raise ConfigError(f"{where}：{model} 只有 effort 不超过 {reasoning.off_up_to} 时才能关掉思考")
        effort = reasoning.efforts.get(reasoning.off_up_to)
    if thinking is False and protocol == "openai_api":
        effort = None  # the off switch is the effort ("none") or a field of its own

    if role.tools:
        if spec.tools == "no":
            raise ConfigError(f"{where}：{role.description}要用工具，{model} 在这个接口上不支持函数调用")
        if spec.tools == "no_reasoning" and thinking is not False:
            raise ConfigError(f"{where}：{model} 只有关掉思考才能用工具，写上 thinking: false")
    if task.fallbacks and (protocol != "anthropic_api" or spec.fallbacks is False):
        raise ConfigError(f"{where}：{model} 不支持 fallbacks")
    if (task.temperature is not None or task.top_p is not None) and spec.sampling is False:
        raise ConfigError(f"{where}：{model} 不接受 temperature 和 top_p")

    max_tokens = task.max_tokens or (
        role.max_tokens_quick if thinking is False and role.max_tokens_quick else role.max_tokens
    )
    if spec.max_output:
        if task.max_tokens and task.max_tokens > spec.max_output:
            raise ConfigError(f"{where}：{model} 最多输出 {spec.max_output} token，max_tokens 太大了")
        max_tokens = min(max_tokens, spec.max_output)
    return ModelSettings(
        spec=spec, max_tokens=max_tokens, effort=effort, thinking=thinking,
        thinking_off=reasoning.off if thinking is False else None, fallbacks=task.fallbacks, **base,
    )


def load_config(path: Path | None = None) -> Config:
    """Load config.yaml; relative paths inside it resolve against its directory.

    Without a path, ./config.yaml is used if it exists, else the defaults. A path that was
    given must exist: a typo shouldn't quietly fall back to the defaults.
    """
    if path is None:
        path = Path.cwd() / CONFIG_FILE
        if not path.exists():
            return Config()
    elif not path.is_file():
        raise FileNotFoundError(f"找不到配置文件：{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(raw)
    base = path.parent
    cfg.characters_dir = base / cfg.characters_dir
    cfg.data_dir = base / cfg.data_dir
    if cfg.lookup.dir is not None:
        cfg.lookup.dir = base / cfg.lookup.dir.expanduser()
    if cfg.lookup.reference is not None:
        cfg.lookup.reference.path = base / cfg.lookup.reference.path.expanduser()
    for provider in cfg.providers.values():
        if provider.api_key_file is not None:
            provider.api_key_file = base / provider.api_key_file.expanduser()
    for own in cfg.characters.values():
        lookup = own.lookup or {}
        if isinstance(lookup.get("dir"), str):
            lookup["dir"] = base / Path(lookup["dir"]).expanduser()
        if isinstance(ref := lookup.get("reference"), dict) and isinstance(ref.get("path"), str):
            ref["path"] = base / Path(ref["path"]).expanduser()
    cfg.check()
    plugins.load(cfg.plugins)
    return cfg
