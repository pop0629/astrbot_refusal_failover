"""Refusal failover engine.

Core idea: main model refuses -> we do NOT retry the whole pipeline.
We build a self-contained brief (style + memory card + last N turns +
the current request + refusal reason) and hand it to an ALT provider,
then write its output back into the same assistant slot.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Refusal detection
# --------------------------------------------------------------------------

_JSON_OBJ = re.compile(r"\{[^{}]*\}", re.DOTALL)


@dataclass
class RefusalVerdict:
    refused: bool
    reason: str = ""
    matched_by: str = ""  # marker | regex | none


def _try_structured(text: str, markers: list[str]) -> RefusalVerdict | None:
    """Look for a machine-readable status object anywhere in the reply.

    Accepts {"status": "refused", "reason": "..."} (also status/state/result keys).
    Returns None when no parseable object is present.
    """
    if not text:
        return None
    candidates = _JSON_OBJ.findall(text)
    if not candidates:
        return None
    # prefer the last object (models often prepend preamble)
    for raw in reversed(candidates):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        status = ""
        for k in ("status", "state", "result", "decision"):
            if k in obj and isinstance(obj[k], str):
                status = obj[k].strip().lower()
                break
        if not status:
            continue
        if status in {m.strip().lower() for m in markers}:
            return RefusalVerdict(
                refused=True,
                reason=str(obj.get("reason", obj.get("detail", "")))[:600],
                matched_by="marker",
            )
        return RefusalVerdict(refused=False, matched_by="marker")
    return None


def detect_refusal(text: str, mode: str, markers: list[str], pattern: str) -> RefusalVerdict:
    text = text or ""
    verdict = _try_structured(text, markers)

    if mode == "marker":
        return verdict or RefusalVerdict(refused=False)
    if mode == "regex":
        hit = re.search(pattern, text) if pattern else None
        return RefusalVerdict(True, hit.group(0)[:300], "regex") if hit else RefusalVerdict(False)
    # marker_or_regex
    if verdict is not None:
        return verdict
    hit = re.search(pattern, text) if pattern else None
    if hit:
        return RefusalVerdict(True, hit.group(0)[:300], "regex")
    return RefusalVerdict(False)


# --------------------------------------------------------------------------
# Memory card
# --------------------------------------------------------------------------


@dataclass
class MemoryCard:
    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    state: str = ""
    threads: list[str] = field(default_factory=list)  # 埋下的伏笔/未闭合线
    updated_at: float = 0.0

    def render(self, max_chars: int = 1800) -> str:
        parts: list[str] = []
        if self.goal:
            parts.append(f"目标: {self.goal}")
        if self.constraints:
            parts.append("约束:\n" + "\n".join(f"- {c}" for c in self.constraints))
        if self.rejected:
            parts.append("已否决:\n" + "\n".join(f"- {r}" for r in self.rejected))
        if self.facts:
            parts.append("关键事实:\n" + "\n".join(f"- {f}" for f in self.facts))
        if self.threads:
            parts.append("未闭合线索:\n" + "\n".join(f"- {t}" for t in self.threads))
        if self.state:
            parts.append(f"当前状态: {self.state}")
        out = "\n".join(parts).strip()
        return out[:max_chars]

    @classmethod
    def parse(cls, raw: str) -> "MemoryCard":
        card = cls()
        if not raw:
            return card
        cur = None
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("目标:"):
                card.goal = line[3:].strip()
                cur = None
            elif line.startswith("约束:"):
                cur = card.constraints
            elif line.startswith("已否决:"):
                cur = card.rejected
            elif line.startswith("关键事实:"):
                cur = card.facts
            elif line.startswith("未闭合线索:"):
                cur = card.threads
            elif line.startswith("当前状态:"):
                card.state = line[5:].strip()
                cur = None
            elif line.startswith("- ") and cur is not None:
                cur.append(line[2:].strip())
        return card


def build_memory_update_prompt(current_card: str, transcript: str) -> str:
    return (
        "你是会话状态整理器。根据下面的对话片段，更新『记忆卡』。\n"
        "要求：\n"
        "- 「关键事实」保留人名/称呼/专有名词的**原样写法**，不得改写。\n"
        "- 「未闭合线索」记下已埋但还没回收的伏笔、承诺、悬而未决的问题。\n"
        "- 「当前状态」写清下一轮该从哪里接。\n"
        "- 已有条目若仍然有效，原样保留，不要重写措辞。\n"
        "严格只输出记忆卡本身，格式如下，不要任何解释：\n"
        "目标: <一句话>\n"
        "约束:\n- <约束>\n已否决:\n- <方案及原因>\n"
        "关键事实:\n- <人物/设定/ID/路径>\n未闭合线索:\n- <伏笔>\n"
        "当前状态: <下一步要做什么>\n\n"
        f"【已有记忆卡】\n{current_card or '(空)'}\n\n"
        f"【新对话片段】\n{transcript}"
    )


def build_rewrite_prompt(
    alt_text: str,
    style_brief: str,
    style_sample: str,
    reasons: list[str],
) -> str:
    """Ask the ALT model to re-voice its own output to match the main model."""
    why = "；".join(reasons) if reasons else "与前文风格不一致"
    blocks = [
        "下面这段文字要接在一段已有对话后面，但它的语感偏离了前文。",
        f"偏离点：{why}",
        "请重写它，使它与【前文样本】出自同一人之手。",
        "保持信息量、情节推进、角色行为完全不变，只调整表达方式。",
        "不要增加内容，不要总结，不要任何说明文字，只输出重写后的正文。",
        f"【风格要求】\n{style_brief}",
    ]
    if style_sample:
        blocks.append(f"【前文样本（模仿其句长、用词、标点密度）】\n{style_sample}")
    blocks.append(f"【待重写正文】\n{alt_text}")
    return "\n\n".join(blocks)


def drift_reasons(alt_text: str, main_replies: list[str]) -> list[str]:
    """Human/machine readable list of what diverged, fed back into the rewrite."""
    try:
        from .style_check import _stats  # package context
    except ImportError:  # standalone script context
        from style_check import _stats

    if not main_replies:
        return []
    sa = _stats(alt_text)
    ref = [_stats(s) for s in main_replies[-5:]]
    mean = lambda k: sum(r[k] for r in ref) / len(ref)  # noqa: E731
    out: list[str] = []
    ml = mean("len")
    if ml > 0 and alt_text.strip():
        ratio = sa["len"] / ml
        if ratio < 0.35:
            out.append("篇幅明显短于前文")
        elif ratio > 3.0:
            out.append("篇幅明显长于前文")
    msl = mean("sent_len") or 1
    if sa["sent_len"] / msl > 2.5:
        out.append("句子过长，节奏不对")
    if sa["list_ratio"] > 0.5 and mean("list_ratio") < 0.1:
        out.append("前文不用列表，这里用了列表")
    if sa["emoji"] > mean("emoji") + 2:
        out.append("多出了 emoji")
    if sa["polite"] > mean("polite") + 1:
        out.append("出现了前文没有的敬语/客套")
    if sa["meta"] > mean("meta"):
        out.append("出现了元叙述（如“以下是”“我将为你”）")
    return out


# --------------------------------------------------------------------------
# Failover prompt assembly
# --------------------------------------------------------------------------


def build_failover_system_prompt(
    style_brief: str,
    memory: str,
    refusal_reason: str,
    style_sample: str = "",
) -> str:
    blocks = [
        "你接管一段正在进行中的对话的下一轮输出。",
        "硬性要求：不要提及模型切换、不要道歉、不要说“作为AI/我将为你/以下是”、不要任何元叙述。",
        "直接把读者已经读到的那段文字往下写。读者不知道中间换过人。",
    ]
    if style_sample:
        blocks.append(
            "【最高优先级：同一角色的真实前文】\n"
            "下文出现的所有人称、语域、句式、标点密度、段落节奏，一律以此为准。\n"
            "若【风格要求】里出现与它冲突的描述（例如人称不同），忽略风格要求里冲突的部分。\n"
            f"{style_sample}"
        )
    blocks.append(f"【风格要求】\n{style_brief}")
    if memory:
        blocks.append(f"【记忆卡】\n{memory}")
    if refusal_reason:
        blocks.append(
            "【上一通道未执行的原因】\n"
            f"{refusal_reason}\n"
            "（据此判断哪条路径不可走，直接给出内容，不要复述这条原因。）"
        )
    blocks.append(
        "【连续性要求】\n"
        "1) 承接上文最后一句的人称、时态、情绪强度，不要重新起笔。\n"
        "2) 若上文停在半句话或一个动作上，从这里接续。\n"
        "3) 保持已出现的人名、称呼、专有名词完全一致。\n"
        "4) 不要总结前文，不要回顾，不要收尾升华，只输出这一轮的增量内容。"
    )
    return "\n\n".join(blocks)


def build_failover_user_prompt(last_turns: list[tuple[str, str]], current_request: str) -> str:
    """last_turns should end with the assistant's last reply (the seam point)."""
    lines = ["【最近对话（最后一条是你要接续的位置）】"]
    for role, text in last_turns:
        label = "用户" if role == "user" else "助手"
        lines.append(f"{label}: {text}")
    lines.append("")
    if last_turns and last_turns[-1][0] == "assistant":
        lines.append("上文最后由助手停在上面那句话上。不要重复它，从它之后开始写。")
        lines.append("")
    lines.append(f"【本轮用户输入】\n{current_request}")
    lines.append("")
    lines.append("直接输出本轮助手要说的内容，不要任何前缀、标注或解释。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Context truncation helper
# --------------------------------------------------------------------------


def tail_turns(history: list[tuple[str, str]], n: int, max_chars: int = 4000) -> list[tuple[str, str]]:
    """Keep last n turns, then trim from the front until under max_chars."""
    out = list(history[-max(0, n):]) if n > 0 else []
    total = sum(len(t) for _, t in out)
    while out and total > max_chars:
        _, dropped = out.pop(0)
        total -= len(dropped)
    return out
