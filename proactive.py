"""Proactive message engine.

Two conditions must BOTH hold before the sub-agent speaks on its own:

  1. a timer fires (every `interval_minutes`)
  2. the recent conversation shows intimacy (亲昵) within `intimacy_window_minutes`

Plus a probability roll and a cooldown, so it never feels mechanical.

State machine (per session, stored in plugin KV):
    last_fire      timestamp of the last proactive message
    last_intimacy  timestamp of the most recent intimacy signal
    last_seen_turn monotonically increasing counter, used to avoid firing
                   right after the user just spoke
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Intimacy detection
# --------------------------------------------------------------------------

# Physical / affectionate actions, not explicit vocabulary - this is the
# "亲昵" layer that precedes anything adult.
_INTIMACY = [
    # 身体接触
    r"(?:抱|搂|拥|靠)(?:着|住|紧|一下)?(?:你|我|他|她|怀里|肩膀)",
    r"(?:摸|抚|蹭|贴)(?:摸|头|脸|发|着)(?:你|我|他|她)?",
    r"(?:亲|吻)(?:你|我|他|她|一下|一口|嘴|脸|额头)",
    r"(?:牵手|十指相扣|握(?:着|住)(?:手|你|我))",
    r"(?:枕|躺)(?:在)?(?:你|我)(?:的)?(?:肩|怀|腿|臂)",
    r"(?:撒娇|黏(?:着|人)|蹭蹭|贴贴)",
    # 称呼
    r"(?:宝宝|宝贝|亲爱的|老公|老婆|乖乖)",
    # 情感表达
    r"(?:喜欢你|想你|爱你|想你抱|要你陪)",
    # English
    r"\b(?:hug|cuddle|kiss|snuggle|hold (?:me|you)|miss you)\b",
]

_INTIMACY_RE = [re.compile(p, re.IGNORECASE) for p in _INTIMACY]

# A message that is a pure command should not count as intimacy.
_COMMAND_RE = re.compile(r"^\s*/[A-Za-z]+\s*$")


def is_intimate(text: str) -> bool:
    """True when the text carries an affectionate/physical signal."""
    if not text or not text.strip():
        return False
    if _COMMAND_RE.match(text):
        return False
    return any(rx.search(text) for rx in _INTIMACY_RE)


# --------------------------------------------------------------------------
# Active hours
# --------------------------------------------------------------------------


def parse_hour(text: str, default: int) -> int:
    """Parse "HH:MM" or "HH" into an hour (0-23). Falls back to `default`."""
    if not text:
        return default
    s = str(text).strip()
    head = s.split(":")[0].split("：")[0]
    try:
        h = int(head)
    except ValueError:
        return default
    if h < 0 or h > 23:
        return default
    return h


def in_active_hours(hour: int, start: int, end: int) -> bool:
    """Is `hour` inside [start, end)?

    Handles the wrap-around case (e.g. 22 -> 2 means 22,23,0,1 are active).
    start == end means "always on".
    """
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    # wraps midnight
    return hour >= start or hour < end


def describe_hours(start: int, end: int) -> str:
    if start == end:
        return "全天"
    return f"{start:02d}:00 - {end:02d}:00"


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


@dataclass
class ProactiveState:
    last_fire: float = 0.0
    last_intimacy: float = 0.0

    def to_json(self) -> dict:
        return {"last_fire": self.last_fire, "last_intimacy": self.last_intimacy}

    @classmethod
    def from_json(cls, raw: dict | None) -> "ProactiveState":
        if not raw:
            return cls()
        return cls(
            last_fire=float(raw.get("last_fire", 0.0)),
            last_intimacy=float(raw.get("last_intimacy", 0.0)),
        )


@dataclass
class ProactiveDecision:
    fire: bool
    reason: str = ""


def evaluate(
    state: ProactiveState,
    *,
    now: float | None = None,
    interval_minutes: float,
    intimacy_window_minutes: float,
    cooldown_minutes: float,
    probability: float,
    active_start: int = 0,
    active_end: int = 0,
    rng: random.Random | None = None,
) -> ProactiveDecision:
    """Decide whether to send a proactive message right now.

    Conditions, in cheap-to-expensive order:
      0. current hour inside the active window (0==0 means always)
      1. recent intimacy inside the window
      2. cooldown since the last proactive message
      3. probability roll
    """
    now = time.time() if now is None else now
    rng = rng or random

    # ---- 0. active hours ----
    hour = time.localtime(now).tm_hour
    if not in_active_hours(hour, active_start, active_end):
        return ProactiveDecision(
            False, f"不在活跃时段（{describe_hours(active_start, active_end)}）"
        )

    if cooldown_minutes > 0 and state.last_fire > 0:
        elapsed_min = (now - state.last_fire) / 60.0
        if elapsed_min < cooldown_minutes:
            return ProactiveDecision(False, f"冷却中（{elapsed_min:.0f}/{cooldown_minutes:.0f} 分钟）")

    if intimacy_window_minutes > 0:
        if state.last_intimacy <= 0:
            return ProactiveDecision(False, "没有亲昵信号")
        age_min = (now - state.last_intimacy) / 60.0
        if age_min > intimacy_window_minutes:
            return ProactiveDecision(False, f"亲昵信号已过期（{age_min:.0f} 分钟前）")

    if probability < 1.0 and rng.random() > probability:
        return ProactiveDecision(False, f"概率未命中（p={probability:.2f}）")

    return ProactiveDecision(True, "定时 + 亲昵 双条件满足")


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------


def build_proactive_prompt(
    persona_hint: str,
    style_brief: str,
    style_sample: str,
    memory: str,
    recent_turns: str,
) -> str:
    """Prompt for a self-initiated message.

    Key constraints that make proactive messages feel natural instead of
    robotic: short, no re-greeting, no recap, no assistant-speak, and it must
    continue an existing mood rather than start a conversation from nothing.
    """
    parts = [
        "你现在要**主动**发一条消息给对方，不是回复对方。",
        "写作要求：",
        "1. 一两句就够，像真的在想对方时随手发的，不要长篇。",
        "2. 不要打招呼（不许出现「在吗」「你好」），不要复述刚才聊过什么。",
        "3. 不要说「作为 AI」「我来找你」这类元叙述。",
        "4. 延续最近对话里的情绪和距离感，不要突然变得生分。",
        "5. 可以带一点主动的亲近，但要和已有的关系程度相称。",
    ]
    if persona_hint:
        parts.append(f"【人设】\n{persona_hint}")
    if style_brief:
        parts.append(f"【风格要求】\n{style_brief}")
    if style_sample:
        parts.append(f"【你平时说话的样子（模仿语气，不要照抄内容）】\n{style_sample}")
    if memory:
        parts.append(f"【记忆卡】\n{memory}")
    if recent_turns:
        parts.append(f"【最近对话（只用来延续情绪，不要复述）】\n{recent_turns}")
    parts.append("只输出要发出去的那句话本身，不需要任何前缀或引号。")
    return "\n\n".join(parts)
