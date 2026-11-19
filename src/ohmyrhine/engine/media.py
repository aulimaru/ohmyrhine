"""Images and stickers: normalize, store content-addressed, describe with the vision task."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

from ..providers import ChatProvider, ChatRequest, Done, Image, Turn
from ..store import Attachment, MediaKind, MediaRecord, Store

_KIND_LABEL = {"image": "图片", "sticker": "表情"}

_DESCRIBE_SYSTEM = (
    "你为聊天记录里的图片写简短的文字描述，供看不到图片的人阅读。"
    "用中文写一到两句，客观地说画面里有什么；图上有文字就照录下来；"
    "如果是表情包，说明它表达的情绪或意思。不要评价图片，也不要猜测发图的人想做什么。"
    "只输出描述本身。"
)


@dataclass(frozen=True)
class IncomingMedia:
    """An image or sticker as the connector delivered it."""

    kind: MediaKind
    # None when the platform couldn't provide the file.
    data: bytes | None = None
    # The platform's own description, e.g. a sticker's name.
    hint: str | None = None
    # Only the chat app's small preview: the original wasn't available.
    thumbnail: bool = False


def normalize(data: bytes, max_edge: int) -> tuple[bytes, str, int, int]:
    """Downscale to fit max_edge and re-encode: PNG when there's transparency, else JPEG.

    Animated images keep their first frame. Raises ValueError for data that isn't an image.
    """
    try:
        with PILImage.open(io.BytesIO(data)) as src:
            src.seek(0)
            im = ImageOps.exif_transpose(src)
            alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if alpha else "RGB")
            im.thumbnail((max_edge, max_edge))
            buf = io.BytesIO()
            if alpha:
                im.save(buf, "PNG", optimize=True)
                mime = "image/png"
            else:
                im.save(buf, "JPEG", quality=85)
                mime = "image/jpeg"
            return buf.getvalue(), mime, im.width, im.height
    except (UnidentifiedImageError, OSError, PILImage.DecompressionBombError) as e:
        raise ValueError(f"无法识别的图片：{e}") from e


class MediaLibrary:
    """Media files under data_dir/media, indexed by the character's store."""

    def __init__(self, store: Store, data_dir: Path, max_edge: int):
        self.store = store
        self.data_dir = data_dir
        self.max_edge = max_edge

    def ingest(self, media: IncomingMedia) -> Attachment:
        if media.data is None:
            return Attachment(media.kind, None, media.hint, media.thumbnail)
        try:
            data, mime, width, height = normalize(media.data, self.max_edge)
        except ValueError:
            return Attachment(media.kind, None, media.hint, media.thumbnail)
        sha = hashlib.sha256(data).hexdigest()
        rel = Path("media") / sha[:2] / f"{sha}.{mime.split('/')[1]}"
        path = self.data_dir / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.store.save_media(MediaRecord(sha, mime, rel.as_posix(), width, height, None))
        return Attachment(media.kind, sha, media.hint, media.thumbnail)

    def load(self, rec: MediaRecord) -> Image | None:
        path = self.data_dir / rec.path
        return Image(path.read_bytes(), rec.mime) if path.is_file() else None

    async def describe(
        self, provider: ChatProvider, rec: MediaRecord, att: Attachment
    ) -> tuple[str | None, Done | None]:
        """Caption one file with the vision task and store the caption. Returns it (None if
        there is none) and the call's Done (None when no call was made)."""
        image = self.load(rec)
        if image is None:
            return None, None
        note = f"这是聊天里的一个{_KIND_LABEL[att.kind]}。"
        if att.hint:
            note += f"聊天软件给它的名字是「{att.hint}」。"
        if att.thumbnail:
            note += "这只是聊天软件里的缩略图，原图没拿到，分辨率很低。"
        req = ChatRequest(system=_DESCRIBE_SYSTEM, turns=[Turn("user", (image, note))], cache_history=False)
        result = await provider.complete(req)
        text = " ".join(result.text.split())
        if result.done.stop_reason == "refusal":
            text = ""
        # An empty caption is stored too, so a refused image isn't sent again on every turn;
        # the transcript then falls back to the platform's name for it, if any.
        self.store.describe_media(rec.sha256, text, result.done.model)
        return text or None, result.done


def attachment_text(att: Attachment, description: str | None) -> str:
    """How an attachment reads for people (CLI history)."""
    label = _KIND_LABEL[att.kind]
    if att.sha256 is None and not att.hint:
        return f"[{label}（没能加载）]"
    detail = description or att.hint
    return f"[{label}：{detail}]" if detail else f"[{label}]"


def attachment_tag(att: Attachment, description: str | None, escape) -> str:
    """How an attachment reads in the model's transcript: a tag nobody can type into a message."""
    if att.sha256 is None and not att.hint:
        return f'<{att.kind} unavailable="true"/>'
    detail = description or att.hint
    attrs = ' thumbnail="true"' if att.thumbnail else ""
    return f"<{att.kind}{attrs}>{escape(detail)}</{att.kind}>" if detail else f"<{att.kind}{attrs}/>"
