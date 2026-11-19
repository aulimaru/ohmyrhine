"""Character pack schema: what authors write, and what `rhine build` compiles it into."""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

KnowledgeLevel = Literal["known", "unfamiliar", "unknown"]
OutOfWorldPolicy = Literal["confused", "reinterpret", "deflect"]
Visibility = Literal["known", "secret", "unknown"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Character(_Strict):
    """character.yaml"""

    id: str
    name: str
    # How the engine's own text speaks of the character: 她, 他, 它, TA…
    pronoun: str = Field(default="TA", min_length=1, max_length=4)
    # roleplay: full roleplay rules. raw: no behavioral rules at all, only what the pack
    # itself says — a baseline for seeing how the bare model behaves.
    prompt_mode: Literal["roleplay", "raw"] = "roleplay"
    # Whether the character sees the real-world date and clock time of each message.
    # False suits characters living in another world: only relative gaps are shown.
    perceives_clock: bool = True
    # IANA time zone the character lives in (e.g. "Asia/Tokyo"); message times are shown
    # in it. Unset: the server's local time zone.
    timezone: str | None = None
    # direct: admits being an AI when sincerely asked.
    # in_character: never breaks character to say so, but still never claims to be human.
    ai_disclosure: Literal["direct", "in_character"] = "direct"
    identity: str | None = None
    personality: str | None = None
    # The inner logic behind the persona: why the character talks and acts this way.
    # Written as prose; the model extrapolates unscripted situations from it.
    core: str | None = None
    appearance: str | None = None
    speech: list[str] = Field(default_factory=list)
    likes: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    # Free-form domain label (e.g. "现代日常生活") -> how well the character knows it.
    knowledge_domains: dict[str, KnowledgeLevel] = Field(default_factory=dict)
    out_of_world_policy: OutOfWorldPolicy = "confused"
    extra_rules: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_timezone(self) -> Character:
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"未知的时区：{self.timezone}") from None
        return self

    @model_validator(mode="after")
    def _roleplay_needs_persona(self) -> Character:
        if self.prompt_mode == "roleplay":
            missing = [f for f in ("identity", "personality") if not getattr(self, f)]
            if missing:
                raise ValueError(f"roleplay 模式需要填写：{', '.join(missing)}")
        return self


class Relationship(_Strict):
    """canon/relationships.yaml entry"""

    name: str
    relation: str
    description: str = ""


class Secret(_Strict):
    """canon/secrets.yaml entry"""

    id: str
    content: str
    # Condition for revealing; not evaluated yet (v1 keeps every secret hidden).
    reveal_when: str | None = None
    # What the character is told instead while the secret stays hidden.
    when_hidden: str


class SealedMemory(_Strict):
    """canon/sealed.yaml entry: part of a past the character lived through but cannot recall."""

    id: str
    title: str
    # What happened, for the model only: the character doesn't know it.
    content: str
    # How it surfaces when something touches it: what sets it off and the vague feeling it
    # leaves, never the memory itself.
    echo: str


class WorldEntry(_Strict):
    """world/*.yaml entry.

    No `known_by`: common knowledge, the character knows `truth`.
    With `known_by`: only characters listed there know it, and only their version.
    """

    id: str
    title: str
    truth: str
    known_by: dict[str, str] | None = None


class Example(_Strict):
    """examples/*.yaml entry"""

    user: str
    char: str
    context: str | None = None


class CanonEntry(_Strict):
    """One compiled entry in the canon index."""

    id: str
    kind: Literal["backstory", "relationship", "secret", "sealed", "world"]
    category: str
    title: str
    # What the character is allowed to see (already filtered by known_by).
    content: str
    visibility: Visibility
    meta: dict = Field(default_factory=dict)


class CompiledPack(_Strict):
    character: Character
    entries: list[CanonEntry]
    examples: dict[str, list[Example]]
    pack_hash: str
