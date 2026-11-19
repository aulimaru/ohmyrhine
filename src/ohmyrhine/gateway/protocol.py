"""Connector protocol v1 (docs/protocol.md): turning upstream events into engine messages."""

from __future__ import annotations

import base64
import binascii
import time
from dataclasses import dataclass
from typing import Any

from ..engine import IncomingMessage
from ..store import SYSTEM_SENDER, MediaKind, Quote

PROTOCOL_VERSION = 1
@dataclass(frozen=True)
class SelfInfo:
    """The chat-app account the connector sends from."""

    id: str
    nickname: str


@dataclass(frozen=True)
class MediaRef:
    kind: MediaKind
    media_id: str | None
    available: bool
    data: bytes | None = None
    hint: str | None = None
    thumbnail: bool = False


def _ts(ev: dict[str, Any]) -> int:
    return int(time.time() * 1000 if ev.get("ts") is None else ev["ts"])


def decode_b64(b64: str | None) -> bytes | None:
    if not b64:
        return None
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None


def _as_character(text: str, me: SelfInfo, char_name: str) -> str:
    """The account *is* the character, so its account name reads as the character's name."""
    if not me.nickname or me.nickname == char_name:
        return text
    return (
        text.replace(f"@{me.nickname}", f"@{char_name}")
        .replace(f"「{me.nickname}」", f"「{char_name}」")
        .replace(f"\"{me.nickname}\"", f"\"{char_name}\"")
    )


def segments_text(segments: list[dict[str, Any]]) -> tuple[str, list[MediaRef]]:
    """Flatten segments into the message text plus its images and stickers, in order."""
    text: list[str] = []
    media: list[MediaRef] = []
    for seg in segments:
        match seg.get("type"):
            case "text":
                text.append(seg.get("text", ""))
            case "mention":
                text.append(f"@{seg.get('name') or '某人'}")
            case "emoji":
                text.append(seg.get("code", ""))
            case "image" | "sticker" as kind:
                data = decode_b64(seg.get("data_b64"))
                available = bool(seg.get("available", True)) and (data is not None or bool(seg.get("media_id")))
                media.append(
                    MediaRef(kind, seg.get("media_id"), available, data, seg.get("desc"), bool(seg.get("thumbnail")))
                )
            case "voice":
                seconds = round((seg.get("duration_ms") or 0) / 1000)
                text.append(f"[语音 {seconds} 秒]")
            case "link":
                title = seg.get("title") or seg.get("url") or ""
                desc = seg.get("desc")
                text.append(f"[链接：{title}｜{desc}]" if desc else f"[链接：{title}]")
            case "forward":
                text.append(f"[合并转发：{seg.get('title') or ''}]")
            case "file":
                text.append(f"[文件：{seg.get('name') or ''}]")
            case "video":
                text.append("[视频]")
            case "other":
                text.append(f"[{seg.get('text') or '其他消息'}]")
            case other:
                text.append(f"[{other}]")
    return "".join(text).strip(), media


def parse_message(ev: dict[str, Any], me: SelfInfo, char_name: str) -> tuple[IncomingMessage, list[MediaRef]]:
    """A `message` event as an IncomingMessage (media still unresolved) and its media refs."""
    chat, sender = ev["chat"], ev.get("sender") or {}
    text, media = segments_text(ev.get("segments") or [])
    from_self = bool(ev.get("from_self"))
    from_dice = from_self and ev.get("source") == "dice"
    dice = ("result" if from_dice else "roll") if ev.get("dice") else None
    # Lines sent from this account (the dice's, or the owner's by hand) are never answered.
    bot = from_self
    sender_id = sender.get("id") or (me.id if from_self else None)
    if not sender_id:
        raise ValueError("message missing sender.id")
    name = sender.get("nickname") or ""
    if from_self:
        name = "骰子" if from_dice else (me.nickname or name)
    quote = None
    if q := ev.get("quote"):
        quoted_name = char_name if q.get("sender_id") == me.id else (q.get("nickname") or "")
        quote = Quote(q.get("message_id"), quoted_name, _as_character(q.get("text") or "", me, char_name))
    msg = IncomingMessage(
        chat_id=chat["id"],
        chat_kind="private" if chat.get("kind") == "private" else "group",
        sender_id=sender_id,
        sender_name=name,
        text=_as_character(text, me, char_name),
        ts=_ts(ev),
        event_id=ev.get("event_id"),
        mentions_self=bool(ev.get("mention_self")) and not bot,
        message_id=ev.get("message_id"),
        quote=quote,
        bot=bot,
        dice=dice,
        backfill=bool(ev.get("backfill")),
    )
    return msg, media


def parse_system(ev: dict[str, Any], me: SelfInfo, char_name: str) -> IncomingMessage:
    """System lines (pats, recalls, joins) are stored as messages from the system sender. A
    recall that says which message it took back becomes a recall notice from whoever did it."""
    chat = ev["chat"]
    kind = "private" if chat.get("kind") == "private" else "group"
    if isinstance(r := ev.get("recall"), dict):
        by_self = r.get("sender_id") == me.id
        return IncomingMessage(
            chat_id=chat["id"],
            chat_kind=kind,
            sender_id=r.get("sender_id") or SYSTEM_SENDER,
            # Without a sender id the name still comes from the notice, if the connector has one.
            sender_name=char_name if by_self else (r.get("nickname") or ""),
            text="",
            ts=_ts(ev),
            event_id=ev.get("event_id"),
            bot=by_self,
            recall=True,
            recalled_id=r.get("message_id") or None,
            backfill=bool(ev.get("backfill")),
        )
    return IncomingMessage(
        chat_id=chat["id"],
        chat_kind=kind,
        sender_id=SYSTEM_SENDER,
        sender_name="系统",
        # System lines arrive with stray tabs and newlines; one line reads better.
        text=_as_character(" ".join((ev.get("text") or "").split()), me, char_name),
        ts=_ts(ev),
        event_id=ev.get("event_id"),
        backfill=bool(ev.get("backfill")),
    )
