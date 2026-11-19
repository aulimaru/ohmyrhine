"""Read and validate a character pack directory, compile it into a CompiledPack."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from .schema import (
    CanonEntry,
    Character,
    CompiledPack,
    Example,
    Relationship,
    SealedMemory,
    Secret,
    WorldEntry,
)

# Parts of the pack directory that don't affect the compiled character.
_HASH_EXCLUDE_DIRS = {"evals"}
# The compiler itself: a change to how packs compile rebuilds every pack.
_COMPILER = (Path(__file__), Path(__file__).with_name("schema.py"))


class PackError(Exception):
    """A pack failed validation; message lists every problem found."""


def pack_hash(pack_dir: Path) -> str:
    h = hashlib.sha256()
    for path in _COMPILER:
        h.update(path.read_bytes())
    for path in sorted(p for p in pack_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(pack_dir)
        if rel.parts[0] in _HASH_EXCLUDE_DIRS:
            continue
        h.update(rel.as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def load_pack(pack_dir: Path) -> CompiledPack:
    if not (pack_dir / "character.yaml").is_file():
        raise PackError(f"{pack_dir} 不是角色包：缺少 character.yaml")

    errors: list[str] = []
    character = _load_model(pack_dir / "character.yaml", Character, errors)
    if character is None:
        raise PackError("\n".join(errors))
    if character.id != pack_dir.name:
        # The directory name is how the character is found and stored; `id` is what
        # world entries' known_by refer to. They must agree.
        raise PackError(f"{pack_dir / 'character.yaml'}: id 是 {character.id}，但目录名是 {pack_dir.name}，两者要一致")

    entries: list[CanonEntry] = []
    canon = pack_dir / "canon"

    for md in sorted(canon.glob("*.md")):
        entries.extend(_backstory_entries(md))

    for rel in _load_list(canon / "relationships.yaml", Relationship, errors):
        entries.append(
            CanonEntry(
                id=f"relationship:{rel.name}",
                kind="relationship",
                category="relationships",
                title=rel.name,
                content=rel.description,
                visibility="known",
                meta={"relation": rel.relation},
            )
        )

    for sec in _load_list(canon / "secrets.yaml", Secret, errors):
        entries.append(
            CanonEntry(
                id=f"secret:{sec.id}",
                kind="secret",
                category="secrets",
                title=sec.id,
                content=sec.content,
                visibility="secret",
                meta={"reveal_when": sec.reveal_when, "when_hidden": sec.when_hidden},
            )
        )

    for mem in _load_list(canon / "sealed.yaml", SealedMemory, errors):
        entries.append(
            CanonEntry(
                id=f"sealed:{mem.id}",
                kind="sealed",
                category="sealed",
                title=mem.title,
                content=mem.content,
                visibility="secret",
                meta={"echo": mem.echo},
            )
        )

    for path in sorted((pack_dir / "world").glob("*.yaml")):
        for w in _load_list(path, WorldEntry, errors):
            entries.append(_world_entry(w, path.stem, character.id))

    examples: dict[str, list[Example]] = {}
    for path in sorted((pack_dir / "examples").glob("*.yaml")):
        examples[path.stem] = _load_list(path, Example, errors)

    seen: set[str] = set()
    for e in entries:
        if e.id in seen:
            errors.append(f"重复的条目 id：{e.id}")
        seen.add(e.id)

    if errors:
        raise PackError("\n".join(errors))
    return CompiledPack(
        character=character, entries=entries, examples=examples, pack_hash=pack_hash(pack_dir)
    )


def _world_entry(w: WorldEntry, category: str, char_id: str) -> CanonEntry:
    meta = {"truth": w.truth}
    if w.known_by is None:
        content, visibility = w.truth, "known"
    elif char_id in w.known_by:
        content, visibility = w.known_by[char_id], "known"
    else:
        content, visibility = "", "unknown"
    return CanonEntry(
        id=f"world:{w.id}",
        kind="world",
        category=category,
        title=w.title,
        content=content,
        visibility=visibility,
        meta=meta,
    )


_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _backstory_entries(md: Path) -> list[CanonEntry]:
    """Split a markdown file into one entry per `## ` section."""
    # HTML comments are notes for authors, never shown to the model.
    text = _COMMENT.sub("", md.read_text(encoding="utf-8"))
    parts = _HEADING.split(text)
    sections: list[tuple[str, str]] = []
    preamble = parts[0].strip()
    # Drop a leading `# Title` line; it names the file, not a section.
    preamble = re.sub(r"^#\s+.*\n?", "", preamble).strip()
    if preamble:
        sections.append((md.stem, preamble))
    for title, body in zip(parts[1::2], parts[2::2], strict=True):
        if body.strip():
            sections.append((title, body.strip()))
    return [
        CanonEntry(
            id=f"backstory:{md.stem}:{i}",
            kind="backstory",
            category=md.stem,
            title=title,
            content=body,
            visibility="known",
        )
        for i, (title, body) in enumerate(sections)
    ]


def _read_yaml(path: Path, errors: list[str]):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        errors.append(f"{path}: YAML 解析失败：{e}")
        return None


def _load_model[M: BaseModel](path: Path, model: type[M], errors: list[str]) -> M | None:
    raw = _read_yaml(path, errors)
    if raw is None:
        if not errors:
            errors.append(f"{path}: 文件是空的")
        return None
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        errors.append(_format_validation(path, e))
        return None


def _load_list[M: BaseModel](path: Path, model: type[M], errors: list[str]) -> list[M]:
    if not path.is_file():
        return []
    raw = _read_yaml(path, errors)
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{path}: 顶层应该是一个列表")
        return []
    out: list[M] = []
    for i, item in enumerate(raw):
        try:
            out.append(model.model_validate(item))
        except ValidationError as e:
            errors.append(_format_validation(path, e, index=i))
    return out


def _format_validation(path: Path, e: ValidationError, index: int | None = None) -> str:
    where = f"{path}" + (f" 第 {index + 1} 项" if index is not None else "")
    lines = [f"{where}:"]
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(根)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
