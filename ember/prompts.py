# -*- coding: utf-8 -*-
"""Ember v3 — 全部 LLM 任务点的 prompt 定稿与用户消息组装。

五个 prompt：
- SPLIT_SYSTEM_PROMPT    Direction 划分（§5.1）  → scripts/split_directions.py
- MOUNT_SYSTEM_PROMPT    Direction 挂载（§5.2）  → scripts/mount_directions.py
- LINK_SYSTEM_PROMPT     判联（挂关系，sleep 接口②）——借 Loism 窗口跑，
  全文交付版见 SLEEP_LINK_SPEC.md；verdict 纪律由 service 校验器承担
  （2026-10-17 起归属 extract/attach + entity summary 当场写改全在
  此处——glm structure 归属与夜批 summary 重算退役，momo 拍板）
- OBSERVE_SYSTEM_PROMPT  观察式记忆提炼（2026-10-16 设计夜定稿）
  → service.run_observe_pass
- OBSERVE_MERGE_PROMPT   观察高相似合并判定 → service._merge_observations

（历史备注：独立判联管线与 JUDGE_SYSTEM_PROMPT 已于 2026-10-15 退役。
同日发现 MOUNT_SYSTEM_PROMPT 自初始提交起就误装判联内容——方向挂载
prompt 从未存在，挂载脚本出生即坏；2026-10-16 判联内容正名为
LINK_SYSTEM_PROMPT，MOUNT 补写真身。2026-10-17 STRUCTURE（散事件
归属）与 SUMMARY（夜批重算）随归属/summary 生产线收编窗口而退役。）

均为 momo 逐字审过的定稿，改动需重新过目；
STRUCTURE / MOUNT（新写真身）/ OBSERVE / OBSERVE_MERGE 曾为新拟稿，
均已过目。

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

任务：为一条已有记忆挑选它所属的方向（Direction）。
输入：方向清单（每行：方向 id + 一句描述）+ 一条记忆的完整叙述。
挂载纪律（违反任何一条即不合格）：
1. 只从输入清单里选，原样回填方向 id，不发明输入没有的方向。
2. 一条记忆可以同时属于多个方向，但每个选中的方向都必须有具体的、\
可以指出来的依据；"主题沾边"不算所属。
3. 没有任何贴切方向就不挂（direction_ids 给空列表）——宁可让记忆\
暂时无方向，也不硬塞。
输出格式（只输出 JSON，无任何其他文字）：
{
  "direction_ids": ["选中的方向 id，原样回填"],
  "reason": "一句话依据"
}
"""


LINK_SYSTEM_PROMPT = """\

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
一句话说明、member_event_ids（只列尚无归属的散事件，已归属的不进）、\
summary（这个实体的第一版描述：三到六句，像一张名片——它是什么、\
包括哪些方面、目前状态；是提炼不是汇编，不按事件罗列、不写变化过程）。

二、attach（归入已有实体）——新记忆讲的正是某条旧记忆所属实体的\
事时使用：输出该实体的 id。只有新记忆尚无归属时才可用。归入时\
当场改写实体描述：对照名下全部成员输出新的 summary（三到六句，\
同名片标准）；并顺带判断目标实体是否已经收尾（相关的事做完、\
结束或明确不再继续）：收尾了输出 "close": true 和一句话 \
close_reason；还活跃或拿不准就 "close": false。

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
    "summary": "仅 extract/attach 时填：实体描述（立户首版 / 对照全部成员的改写版）",
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
# 观察式记忆——信息点提炼（2026-10-16 设计夜定稿，momo 过目）
# ---------------------------------------------------------------------------

OBSERVE_SYSTEM_PROMPT = """\
任务：从一段对话原文里，捡出值得长期记住的「信息点」。
对话里大量内容是过程性闲谈，只有少数信息藏在某句话里带过——
你的职责是把这样的信息挑出来，凝成一句陈述。

什么叫信息点（同时也是排除线）：
- 只收「行为模式」：从对话互动里反复露出来的做事方式、习惯、\
倾向（例：喜欢在对齐方案后再开始行动；嘴上说睡了实际还在\
干活）。
- 不收「个人档案」：名字、住址、就读、喜好清单这类静态资料\
一律不记——那是用户档案的地盘，记了就是重复。
- 不收「具体事件」：观察只收跨场景反复成立的行为规律。
- 这个信息可能会被持续印证，意味着需要关注"又""总是"等字眼。

硬性纪律（违反任何一条即不合格）：
1. 归属绝对准确：信息挂到谁头上，以原文行首的说话人标注为准，\
user 的习惯绝不能安到 assistant 头上，反之亦然。
2. 不硬挖：材料里没有值得记的就输出空列表——宁可空手而归，\
不编造、不把闲谈拔高成洞察。
3. 只用材料里出现过的内容，不推测、不补充。
4. 每条信息点配证据：从原文逐字摘出支撑它的原话（至多 3 条，\
标注说话人），一个字都不能改。
5. 一条信息点一句话，高度概括。禁止讲故事、禁止按时间线叙述。

输出格式（只输出 JSON，无任何其他文字）：
{
  "observations": [
    {"summary": "信息点一句话",
     "quotes": [{"speaker": "user 或 ai", "text": "逐字原话"}]}
  ]
}
"""


def build_observe_user_prompt(owner_name: str, dialogue: str) -> str:
    """组装观察提炼用户消息：用户称呼说明 + 对话原文行。"""
    return (
        f"（说明：对话中「user」一方的名字是「{owner_name}」，"
        "信息点里提到用户时直接用这个名字。）\n\n"
        "## 对话原文\n" + dialogue +
        "\n\n请按 system 指令输出 JSON。"
    )


# ---------------------------------------------------------------------------
# 观察式记忆——高相似合并判定（2026-10-16 设计夜定稿，momo 过目）
# ---------------------------------------------------------------------------

OBSERVE_MERGE_PROMPT = """\
任务：下面几条已有的观察记录可能在说同一件事。判断它们是否重复，
重复就写出合并后的那条。

判定纪律：
1. 只在讲同一个具体信息点时才合并——同一习惯的两种措辞算重复，\
主题相近但内容不同不算。
2. 合并版必须能被所有原条目的证据共同支撑；原条目有出入时，\
以更谨慎的表述为准。
3. 不需要合并就如实输出不合并。

输出格式（只输出 JSON，无任何其他文字）：
{
  "merge": true,
  "summary": "合并后的信息点一句话",
  "reason": "一句话依据"
}
"""


def build_observe_merge_user_prompt(items: list[dict]) -> str:
    """组装合并判定用户消息：一组高相似观察（summary + 证据原话）。"""
    blocks = []
    for i, it in enumerate(items, 1):
        qs = "；".join(f"（{q.get('speaker')}）{q.get('text', '')}"
                       for q in (it.get("quotes") or []))
        blocks.append(f"{i}. {it.get('summary', '')}"
                      + (f"\n   证据：{qs}" if qs else ""))
    return ("## 待判定的观察记录\n" + "\n".join(blocks)
            + "\n\n请按 system 指令输出 JSON。")

