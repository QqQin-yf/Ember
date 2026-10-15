# -*- coding: utf-8 -*-
"""Ember v3 — 全部 LLM 任务点的 prompt 定稿与用户消息组装。

四个 prompt：
- SPLIT_SYSTEM_PROMPT    Direction 划分（§5.1）  → scripts/split_directions.py
- MOUNT_SYSTEM_PROMPT    Direction 挂载（§5.2）  → scripts/mount_directions.py
- SUMMARY_SYSTEM_PROMPT  Entity summary 重算（§2.1）→（service 编排待接）
- STRUCTURE_SYSTEM_PROMPT 散事件归属（2026-10-14 设计稿 v2）
  → service.run_structure_maintenance

（JUDGE_SYSTEM_PROMPT 已随独立判联管线于 2026-10-15 退役——
判联借 Loism 窗口跑，verdict 纪律由 service 校验器承担，prompt 定义
见 git 历史 e0ec0be 之前版本。）

均为 momo 逐字审过的定稿，改动需重新过目；
STRUCTURE 为新拟稿，待 momo 过目。

写入不设独立 prompt：实时写入由 EMBEDDED_WRITER_PROMPT（writer.py）
承担（AI 调工具，主动记重要的事；sleep 只管维护，不重写内容）。
"""

# ---------------------------------------------------------------------------
# Direction 划分（§5.1，momo 2026-10-09 逐字审过）
# ---------------------------------------------------------------------------

SPLIT_SYSTEM_PROMPT = """\
任务：这个实体下的经历已经积累到需要划分方向的程度。你的职责是\
为它划分出稳定、粗粒度的语义入口（Direction），供以后快速进入\
这个实体的记忆空间。
输入：实体名 + 该实体下全部事件的摘要 + 潜在用户问法示例。
划分铁律（违反任何一条即不合格）：
1. 方向绝对不能多，尽可能聚合。划分的判据是"用户会从哪些稳定的\
大角度询问这个实体"，不是"这些事件能分成哪几类"。
2. 一个方向的描述必须能覆盖它名下的大部分事件。
3. 划分有道理即可，不追求完美。
方向描述的要求：
- 一句稳定的自然语言，描述"从哪个角度进入"（例：关于 Ember记忆系统 的\
"它的架构与机制如何设计、为什么这样设计"）。
- 长期稳定的语义边界，不含动态时间信息。
- 与其他实体的方向无关，不套模板。
输出格式（只输出 JSON，无任何其他文字）：
{
  "directions": [
    {"description": "一句稳定的方向描述"}
  ],
  "reason": "一句话划分依据"
}
"""


def build_split_user_prompt(entity_name: str, summaries: list[str]) -> str:
    """组装划分用户消息：实体名 + 全部事件摘要 + 潜在问法占位。

    summaries: ["EVT-001-aaaa 摘要文本", ...]（调用方拼好 id+摘要）
    """
    lines = "\n".join(f"- {s}" for s in summaries)
    return (
        f"## 实体：{entity_name}\n\n"
        "## 该实体下的全部事件摘要\n" + lines +
        "\n\n## 潜在用户问法示例\n（请根据事件内容自行体会用户可能怎么问）\n\n"
        "请按 system 指令输出 JSON。"
    )


MOUNT_SYSTEM_PROMPT = """\

材料中每条旧记忆可能标注了它所属的实体（Entity）。注意：\
联系边只看两件事的内容之间有没有具体的、可以指出来的联系，\
与归属无关——标注了归属的旧记忆，无关的照样判无关系。

第一部分：联系裁决（新记忆 × 每条旧记忆，逐条给出）\
三种结局，每条旧记忆独立判定：

一、relation（有具体联系）\
类型从词表选最贴切的一个（边方向固定：旧事件 → 新事件，\
每个类型读作"新事件 <type> 旧事件"）：
- continues：新事件是旧事件的直接延续
- concludes：新事件给旧事件画上句号
- inspired_by：新事件由旧事件触发或启发
- caused_by：新事件起因于旧事件（"旧事件引起新事件"也用它）
- responds_to：新事件回应旧事件的问题或观点
- resolves：新事件修正、推翻或消解了旧事件的核心结论
- revisits：新事件重新处理旧事件的同一主题且有实质进展
- contrasts：新事件与旧事件立场或结论相对照
- related_to：确有联系，但以上类型都不贴切\
判 resolves 有特殊效力：即认定旧事件已被推翻。此时必须同时给出：
- change：以旧事件主人公的第一人称，补写"这段经历之后，我的认知\
发生了什么变化"。只写心路，禁止写"此条已作废"之类的效力声明。
- status_evidence：一句话依据，说明新事件如何修正了旧事件。

二、neighbor（普通邻居）\
共现于同一场对话或同一话题空间，但没有具体的、值得单独记录的联系。\
"都在谈同一个大话题"不算联系。判 neighbor 不产生任何结构。

三、none（无明显关系）

判定纪律：
- 从严判 relation：必须有具体、可指出的联系。"主题相近"不是联系。
- 拿不准 relation 还是 neighbor 时，判 neighbor。
- 拿不准是不是 resolves 时，判更弱的类型；只有核心结论确实被\
推翻/修正/消解才算 resolves。

第二部分：归属动作（整个材料判定一次，三选一）

一、extract（提取开新户）——同时满足以下全部条件：
- 这批事件（新+旧）围绕同一个对象/主题；
- 其中尚无实体归属的散事件 ≥3 条；
- 这个对象不是材料中任何已有实体的子题（若是，不提取，\
改用 attach）。
输出：name（从事件原文提炼，必须是专名，不用普通词汇）、\
aliases（对话中真实出现过的其他称呼，照原文）、\
一句话说明、member_event_ids（只列尚无归属的散事件，已归属的不进）。

二、attach（归入已有实体）——新记忆讲的正是某条旧记忆所属实体的\
事时使用：输出该实体的 id。只有新记忆尚无归属时才可用。归入时顺带判断目标实体是否已经收尾（相关的事做完、结束或明确不再继续）：收尾了输出 "close": true 和一句话 close_reason；还活跃或拿不准就 "close": false。

三、none（不做归属动作）——主题不聚焦，或只是跨主题的零星联系。\
不提取也不归入时什么都不输出，事件保持散着。

输出格式（只输出 JSON，无任何其他文字）：
{
  "pairs": [
    {"old_event_id": "<原样回填>",
     "outcome": "relation | neighbor | none",
     "type": "词表之一，仅 outcome=relation 时填，否则留空",
     "confidence": "high | medium | low",
     "reason": "一句话依据",
     "change": "仅 type=resolves 时填，否则留空",
     "status_evidence": "仅 type=resolves 时填，否则留空"}
  ],
  "entity": {
    "action": "extract | attach | none",
    "name": "仅 extract 时填",
    "aliases": ["仅 extract 时填"],
    "description": "仅 extract 时填，一句话",
    "member_event_ids": ["仅 extract 时填"],
    "entity_id": "仅 attach 时填，目标已有实体",
    "close": "仅 attach 时填：true / false",
    "close_reason": "仅 close=true 时填，一句话"
  }
}
"""


def build_mount_user_prompt(dir_lines: str, event_id: str, core: str) -> str:
    """组装用户消息：方向清单 + 待挂载经历。

    dir_lines: "dir_id: description" 多行文本
    """
    return (
        "## 方向清单\n" + dir_lines +
        "\n\n## 待挂载经历\n" +
        f"event_id: {event_id}\n" + core
    )


# ---------------------------------------------------------------------------
# Entity summary 重算（§2.1，momo 2026-10-10 逐字审过）
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = """\
任务：为「Ember记忆系统」中的一个实体撰写或更新它的整体描述\
（Entity summary）。

这个描述回答一个问题：这个实体是什么、现在怎么样。它是一份\
提炼出来的整体认识，不是事件记录。

输入：实体名 + 它名下全部事件的完整叙述（每条一段）。

写作要求：
- 三到六句话。把全部事件合并起来提炼出对实体的整体认识，\
  包括它是什么、包括哪些方面、目前的状态，以及最近的情况。
- 是提炼，不是汇编：禁止按事件逐条罗列、禁止按时间顺序叙述\
  "先后做了什么"；多个事件指向的同一件事合并成一句话；\
  细枝末节可以舍弃。
- 最近发生的事可以反映进来，但要说成"目前/最近"的状态，\
  不铺陈过程。
- 概括依据只来自输入中出现过的内容，不推测、不补充输入之外\
  的信息；事件之间没有提到的方面不写。
- 通顺的自然语言，不要罗列 event_id，不要使用"摘要""事件"\
  "材料"这类词。
- 实体名用输入给出的写法；输入中出现的常用别名可自然融入。

输出格式（只输出 JSON，无任何其他文字）：
{
  "summary": "三到六句话的整体描述"
}
"""


def build_summary_user_prompt(entity_name: str, old_summary: str,
                              cores: list[str]) -> str:
    """组装 summary 重算的用户消息：实体名 + 旧描述（如有）+ 全部
    未被推翻成员事件的完整叙述（core）。

    cores: [完整叙述, ...]（调用方提取，已过滤 superseded，不含 id）
    old_summary: 该实体现有 summary；为空串时省略该区块（首次生成）。
    """
    parts = [f"## 实体\n{entity_name}"]
    if old_summary.strip():
        parts.append("## 现有描述（可参考，按当前全部事件重新概括）\n"
                     + old_summary.strip())
    blocks = "\n".join(f"- {c}" for c in cores)
    parts.append("## 该实体下全部事件\n" + blocks)
    parts.append("请按 system 指令输出 JSON。")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 结构维护——散事件归属（2026-10-14 设计稿 v2；新拟稿，待 momo 过目）
# ---------------------------------------------------------------------------

STRUCTURE_SYSTEM_PROMPT = """\
任务：记忆库里积了一批尚无实体归属的散事件。你拿到散事件清单和\
已有实体库摘要，为散事件安排归属。

动作二选一（每个动作独立给出，一个事件只能归一处）：

一、extract（提取开新户）——同时满足以下全部条件：
- 一组散事件围绕同一个对象/主题；
- 该组散事件 ≥3 条；
- 这个对象不是实体库里任何已有实体的子题（若是，改用 attach）。
输出：name（从事件原文提炼，必须是专名，不用普通词汇）、\
aliases（对话中真实出现过的其他称呼，照原文）、一句话说明、\
member_event_ids（从散事件清单里选，原样回填 event_id）。

二、attach（归入已有实体）——散事件讲的正是实体库里某个已有实体的\
事时使用：输出该实体的 entity_id，event_ids 列出要归入的散事件\
（原样回填 event_id）。

判定纪律：
- 从严：主题相近不等于归属；宁可让散事件继续散着，也不硬塞。
- 与任何已有实体无关、也凑不齐立户的散事件，不给任何动作，\
保持散着。
- 一个事件只出现在一个动作里，不要重复分配。

输出格式（只输出 JSON，无任何其他文字）：
{
  "assignments": [
    {"action": "extract",
     "name": "专名", "aliases": ["..."], "description": "一句话",
     "member_event_ids": ["EVT-...", "EVT-...", "EVT-..."]},
    {"action": "attach",
     "entity_id": "ent_...", "event_ids": ["EVT-...", "EVT-..."]}
  ]
}
"""


def build_structure_user_prompt(strays: list[dict],
                                entities: list[dict]) -> str:
    """组装结构维护用户消息：实体库摘要 + 散事件清单。

    strays: [{"event_id", "core", "summary"}]（调用方从库中筛出）
    entities: [{"entity_id", "name", "status", "summary"}]
    """
    ent_lines = []
    for e in entities:
        ent_lines.append(
            f"- {e['entity_id']} {e['name']}"
            f"（{e.get('status') or 'open'}）{e.get('summary') or ''}")
    ev_lines = []
    for ev in strays:
        ev_lines.append(f"event_id: {ev['event_id']}")
        ev_lines.append(ev.get("core") or "")
        ev_lines.append(f"（摘要：{ev.get('summary', '')}）")
        ev_lines.append("")
    return (
        "## 已有实体库\n" + ("\n".join(ent_lines) or "（暂无实体）") +
        "\n\n## 尚无归属的散事件\n" + "\n".join(ev_lines) +
        "请按 system 指令输出 JSON。"
    )

