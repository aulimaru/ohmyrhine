"""Group chats: a quick, cheap filter in front of the character (the reply_judge task).

The judge runs as soon as a message arrives. It predicts whether the character would speak
up, and says whether the speaker seems to have finished, which is how long the gateway waits
for more before acting. What it passes goes to the character, which still decides for itself
whether and how to answer; a pass she answers with silence is a reply-model call wasted. When
someone asks the character not to talk, it says so (`hush`), and the gateway keeps her quiet
for a while (the speech lock).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from ..pack import Character
from ..providers import ChatProvider, ChatRequest, Done, Turn
from ..store import StoredMessage
from . import dice as dice_component
from .prompt import last_reply_index, render_message

_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "reply": {"type": "boolean"},
        "addressed": {"type": "boolean"},
        "wait": {"type": "string", "enum": ["no", "short", "long"]},
        "hush": {"type": "boolean"},
    },
    "required": ["reason", "reply", "addressed", "wait", "hush"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Decision:
    # Hand the new messages to the character.
    reply: bool
    # Someone was talking to the character (as opposed to it chiming in on its own).
    addressed: bool
    reason: str
    # The speaker doesn't seem to have finished: wait a little ("short") or a good while
    # ("long") for the rest and judge again with it; if nothing follows, `reply` stands.
    wait: Literal["", "short", "long"] = ""
    # Someone in the new messages doesn't want the character to talk now.
    hush: bool = False


def judge_system(c: Character, dice: bool = False) -> str:
    n, p = c.name, c.pronoun
    about = [f"名字：{n}"]
    known = [d for d, level in c.knowledge_domains.items() if level == "known"]
    if known:
        about.append(f"擅长和熟悉：{'、'.join(known)}")
    if c.likes:
        about.append(f"喜欢：{'、'.join(c.likes)}")
    rules = [
        f"你是群聊角色「{n}」前面的一道快速筛选：判断{n}看到新消息后会不会开口，会才交给{p}。交过去的{p}还会自己决定回不回、怎么回，"
        f"但每交一次都有代价，交过去{p}却不开口，就是白交了。你不写回复。"
        f"消息包在 <msg> 里，self=\"true\" 是{n}自己说的，bot=\"true\" 是自动程序，kind=\"system\" 是系统提示，"
        "kind=\"recall\" 是有人撤回了 <quote> 里那条消息。",
        "<character>\n" + "\n".join(about) + "\n</character>",
        f"这些时候交（reply=true）：跟{n}说话、叫到或提到{n}、向{p}或全群提问、回应或接着{p}的话，"
        f"哪怕只是一句附和、道谢或感叹；聊的就是{n}本人、{p}的事或{p}刚说的话，哪怕用「{p}」来指{p}、哪怕是对着别人说的；"
        f"{n}刚说过话，别人接着{p}说的话题往下聊，哪怕聊的是别人。"
        f"看清话是说给谁的。别人互相搭话、转述一段对话或引用一句台词，不能只凭里面有个「你」就当作在叫{n}。有人在跟{n}说话时 addressed=true。"
        f"别人之间在聊别的事、没人在跟{n}说话时，像真人在群里一样，{p}多半只是在看，"
        f"哪怕聊的是{p}擅长、喜欢的事，也不交（reply=false），除非新消息里有{p}会忍不住想接的话。"
        f"单是有人撤回了消息不用交，除非撤回的是对{n}说的话。拿不准有没有人在跟{p}说话时就交。",
        f"<new> 里有人不想让{n}现在说话，比如叫{p}先别说话、闭嘴、别插嘴、少说两句、多沉默一会儿，hush=true，这时 reply=false："
        f"系统会让{n}先不开口，直到有人 @ {p}，或者群里安静一阵。"
        f"得是冲着{n}本人说的：叫了{p}的名字、@ {p}、回{p}的话，或者{p}刚说完话就被叫停。"
        f"{n}最近没说话、也没人叫{p}时，一句「闭嘴」是冲着刚说话的人的。"
        f"冲着别人说的、只是在引用别人的原话或故事里的台词，不算；只是挑{p}哪句话的毛病、开玩笑地损{p}，也不算。",
        "wait 是最新那条消息的人说完了没有，它决定系统再等多久才处理，和交不交分开判断。"
        "这条消息自己是一句完整的话，就是 wait=\"no\"，哪怕对方之后可能还会聊别的；大多数消息都是这样。"
        "看得出他正把一段话分成几条在发，比如在列举、句子明显还有后半截、刚说了要讲一件事，wait=\"short\"。"
        "话断在半截、只起了个头，比如只发了「我跟你说」「还有」、句子停在转折或破折号上，wait=\"long\"。"
        "话没说完时，reply 按已经看到的部分来判断。",
        "只判断 <new> 里的消息，<context> 帮你理解它。reason 先写新消息是在对谁说话、说的是谁的事，再写结论，不超过三十个字。",
    ]
    if dice:
        rules.insert(-1, dice_component.judge_rule(c))
    return "\n\n".join(rules)


def judge_transcript(
    c: Character,
    history: list[StoredMessage],
    descriptions: dict[str, str | None],
    judged_upto: int | None = None,
) -> str:
    """The chat as the judge sees it. Messages after the character's last reply and after
    `judged_upto` (the newest message id already judged) go in <new>, the rest in <context>."""
    last_reply = last_reply_index(history)
    context, new = [], []
    for i, m in enumerate(history):
        target = new if i > last_reply and m.id > (judged_upto or 0) and not m.backfill else context
        target.append(render_message(m, descriptions, self_name=c.name))
    parts = []
    if context:
        parts.append("<context>\n" + "\n".join(context) + "\n</context>")
    parts.append("<new>\n" + "\n".join(new) + "\n</new>")
    return "<chat>\n" + "\n".join(parts) + f"\n</chat>\n\n{c.name}现在要发言吗？"


async def judge(
    provider: ChatProvider,
    c: Character,
    history: list[StoredMessage],
    descriptions: dict[str, str | None],
    judged_upto: int | None = None,
    dice: bool = False,
) -> tuple[Decision, Done]:
    req = ChatRequest(
        system=judge_system(c, dice),
        turns=[Turn("user", judge_transcript(c, history, descriptions, judged_upto))],
        cache_history=False,
    )
    result = await provider.complete(req, json_schema=_SCHEMA)
    try:
        data = json.loads(result.text)
        if not isinstance(data, dict) or not isinstance(data.get("reply"), bool) or not isinstance(data.get("addressed"), bool):
            raise TypeError("invalid judge decision")
        wait = data.get("wait")
        # A hush comes with reply=false; an answer contradicting itself is not taken as one,
        # since a wrong lock keeps her quiet for a good while.
        if data.get("hush") is True and not data["reply"]:
            return Decision(False, bool(data["addressed"]), str(data["reason"]), hush=True), result.done
        return (
            Decision(bool(data["reply"]), bool(data["addressed"]), str(data["reason"]), wait if wait in ("short", "long") else ""),
            result.done,
        )
    except (ValueError, KeyError, TypeError):
        # A refusal or malformed answer: stay quiet rather than guess.
        return Decision(False, False, f"判断结果无法解析：{result.text[:80]!r}"), result.done
