"""The dice component, for deployments where the character's account also runs the group's dice.

The connector marks dice traffic: a person's dice command ("roll") and the dice's answer
("result", sent from the character's own account). With the component on, a result counts
as a roll the character made for whoever asked: the transcript shows it as hers, and the judge
hands it to her only when someone talks to her about it; the roll itself, however high or low,
is never a reason to speak. Without the component dice traffic is context only, like any bot
message.
"""

from __future__ import annotations

from ..pack import Character


def roleplay_rule(c: Character) -> str:
    n, p = c.name, c.pronoun
    return (
        f"这个群里，骰子由{n}来掷。有人发出骰子指令时，那条消息会标上 dice=\"roll\"，"
        f"{n}就替他掷，结果随后以 kind=\"dice\" 的消息发出来，那就是{n}替他掷出的点数，群里人人都看得到。"
        f"点数是骰子定的，{n}改不了，也不会说自己掷出了别的结果。"
        f"掷出的点数本身不是{n}说话的理由：不管是大成功、大失败还是别的什么点数，{p}都不因为点数去评价。"
        f"有人冲着结果跟{n}说话时，{p}照常按性格回应；点数大家都看得到，不必复述。"
    )


def judge_rule(c: Character) -> str:
    n, p = c.name, c.pronoun
    return (
        f"群里的骰子由{n}来掷：dice=\"roll\" 是有人请{p}掷骰，kind=\"dice\" 是{p}替这个人掷出的结果。"
        "结果本身不交，大成功、大失败或别的极端点数也一样；"
        f"只有有人就结果跟{n}说话时才交。"
        # Dice in the group mean a tabletop game is on: its players speak in character.
        f"跑团里玩家之间、对着各自角色的台词和行动，不是在跟{n}说话。"
    )
