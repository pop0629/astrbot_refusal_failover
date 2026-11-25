"""Sticky failover state machine.

After an automatic failover, optionally stay on the ALT provider for the next
few turns instead of snapping back to the main model every turn.

Three release triggers, ordered by cost:

  A  free     N consecutive non-refusal turns  -> auto-release
  B  free     manual /fm (handled by the command, not here)
  C  paid     cheap-model sampling, only when A cannot decide

Design goal: A releases the vast majority of sessions, so C is a backstop
and its token cost stays negligible.

State shape (stored as JSON in plugin KV):
    {
      "active": true,
      "alt": "ALT_ID",
      "since": 1712345678.0,
      "streak": 2,             # consecutive clean turns, feeds trigger A
      "judge_skip": 1,          # turns until the next sampling judge (trigger C)
      "turns": 5,               # total turns spent sticky (for stats/caps)
    }
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class StickyState:
    active: bool = False
    alt: str = ""
    since: float = 0.0
    streak: int = 0          # consecutive non-refusal turns
    judge_skip: int = 0      # turns remaining before the next sampling judge
    turns: int = 0           # total sticky turns

    def to_json(self) -> dict:
        return {
            "active": self.active,
            "alt": self.alt,
            "since": self.since,
            "streak": self.streak,
            "judge_skip": self.judge_skip,
            "turns": self.turns,
        }

    @classmethod
    def from_json(cls, raw: dict | None) -> "StickyState":
        if not raw:
            return cls()
        return cls(
            active=bool(raw.get("active", False)),
            alt=str(raw.get("alt", "")),
            since=float(raw.get("since", 0.0)),
            streak=int(raw.get("streak", 0)),
            judge_skip=int(raw.get("judge_skip", 0)),
            turns=int(raw.get("turns", 0)),
        )


@dataclass
class ReleaseDecision:
    release: bool
    reason: str = ""
    needs_judge: bool = False   # True -> caller should run trigger C this turn


def on_enter(alt_id: str, judge_every: int) -> StickyState:
    """Called the moment an automatic failover succeeds."""
    return StickyState(
        active=True,
        alt=alt_id,
        since=time.time(),
        streak=0,
        judge_skip=max(0, judge_every - 1),
        turns=1,
    )


def evaluate_turn(
    state: StickyState,
    *,
    max_turns: int,
    release_after_clean: int,
    judge_every: int,
) -> ReleaseDecision:
    """Advance one sticky turn and decide whether to release.

    Called AFTER a turn was served by the sticky ALT provider.

    Trigger A: `release_after_clean` consecutive turns with no refusal signal
               -> release, zero cost.
    Trigger C: every `judge_every` turns, ask for a sampling judge. Only if A
               has not already fired.
    Hard cap: `max_turns` prevents an indefinitely sticky session.
    """
    state.turns += 1

    # hard cap first - a session must never stay sticky forever by accident
    if max_turns > 0 and state.turns > max_turns:
        return ReleaseDecision(True, f"达到最长保留轮数 {max_turns}")

    # trigger A
    state.streak += 1
    if release_after_clean > 0 and state.streak >= release_after_clean:
        return ReleaseDecision(
            True, f"连续 {release_after_clean} 轮正常回复"
        )

    # trigger C (sampling)
    if judge_every > 0:
        if state.judge_skip <= 0:
            state.judge_skip = judge_every
            return ReleaseDecision(False, "抽样判定中", needs_judge=True)
        state.judge_skip -= 1

    return ReleaseDecision(False, "")


def on_turn_had_signal(state: StickyState) -> None:
    """A turn still showed refusal-ish behaviour: reset the clean streak."""
    state.streak = 0


def build_sticky_judge_prompt(recent_turns: str) -> str:
    """Cheap sampling judge: has the conversation moved on from the scene that
    originally triggered the failover?"""
    return (
        "下面是一段对话的最近几轮。此前因为某轮回复被判定为拒绝，临时切换了模型接管。\n"
        "判断：这段对话是否**已经回到正常问答**，不再需要继续由备用模型接管？\n\n"
        "回答 RELEASE 表示可以切回主模型；回答 KEEP 表示还需要备用模型。\n"
        "只输出一个词：RELEASE 或 KEEP。不要解释。\n\n"
        f"【最近对话】\n{recent_turns[:2000]}"
    )


def parse_sticky_judge(raw: str) -> bool | None:
    """True = release (back to main), False = keep sticky, None = unparseable."""
    if not raw:
        return None
    t = raw.strip().upper()
    if "RELEASE" in t:
        return True
    if "KEEP" in t:
        return False
    return None
