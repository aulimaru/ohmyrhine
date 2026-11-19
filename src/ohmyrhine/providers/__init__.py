from ..catalog import ModelSettings
from ..config import Config
from .base import (
    ChatProvider,
    ChatRequest,
    Completion,
    Done,
    Image,
    Part,
    ReplyWritten,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Tool,
    ToolError,
    ToolUse,
    Turn,
)


def make_chat_provider(cfg: Config, task_name: str) -> ChatProvider:
    return make_provider(cfg.settings(task_name))


def make_provider(settings: ModelSettings) -> ChatProvider:
    match settings.protocol:
        case "anthropic_api":
            from .anthropic_api import AnthropicChat

            return AnthropicChat(settings)
        case "openai_api":
            from .openai_api import OpenAIChat

            return OpenAIChat(settings)
        case "echo":
            from .echo import EchoChat

            return EchoChat()
    raise ValueError(f"未知的 provider 类型：{settings.protocol}")


__all__ = [
    "ChatProvider",
    "ChatRequest",
    "Completion",
    "Done",
    "Image",
    "Part",
    "ReplyWritten",
    "StreamEvent",
    "TextDelta",
    "ThinkingDelta",
    "Tool",
    "ToolError",
    "ToolUse",
    "Turn",
    "make_chat_provider",
    "make_provider",
]
