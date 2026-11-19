"""Extension points for code kept outside the project: compatibility with one deployment's
tools, say. A plugin is a module named in config.yaml (`plugins`), imported when the config is
loaded; it adds itself to the hooks below. Everything works without any."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store


@dataclass(frozen=True)
class EarlierHistory:
    """A chat from before the engine kept it, for the history lookup: lines in export_history's
    format (numbered the plugin's own way), and what the agent should know to read them."""

    lines: list[str]
    note: str


# Called for each history lookup with (its working directory, the store, the chat, the
# character's name, her time zone, the time of the first stored message or None).
EarlierHistorySource = Callable[["Path", "Store", str, str, "tzinfo | None", "int | None"], "EarlierHistory | None"]
earlier_history: list[EarlierHistorySource] = []


def load(modules: list[str]) -> None:
    for name in modules:
        importlib.import_module(name)
