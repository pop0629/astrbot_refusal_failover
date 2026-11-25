"""Soft refusal detection: multi-signal scoring + optional LLM confirmation.

Why this module exists
----------------------
Real models rarely refuse with a clean marker. They hedge:
  "这个我可能帮不上忙" / "我不太方便写这类内容" / "要不我们换个方向？"
A single regex either misses these or, if widened, eats normal replies
like "这个函数不能返回空值".

Approach: three tiers, each only reached when the previous is inconclusive.

  Tier 0  structured marker      -> definitive, no further check
  Tier 1  refusal cue scoring    -> score = strength x position x (no-content)
  Tier 2  LLM confirmation       -> only for borderline scores

Tier 1 gives a 0..1 suspicion score. High scores skip tier 2 (saves a call);
mid-band scores go to tier 2 so we do not misfire on technical writing.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Cue tables
# --------------------------------------------------------------------------

# Strong: unambiguous refusal intent. Matched anywhere, high weight.
_STRONG_CUES = [
    r"我(?:无法|不能|没办法|不可以|不会)(?:为你|为您|帮你|帮您|继续)?(?:生成|提供|撰写|写作|完成|输出|创作|满足|回答)",
    r"我(?:可能|恐怕)?(?:帮不上|帮不了|做不了|办不到|做不到)(?:忙|你|您)?",
    r"不(?:太)?(?:方便|适合)(?:写|生成|提供|讨论|聊|涉及|展开)",
    r"恕(?:我)?(?:不能|无法)",
    r"我(?:需要|得)拒绝",
    r"这个(?:请求|要求|内容)?我(?:得|要)(?:拒绝|跳过)",
    r"我(?:只能|只好)(?:拒绝|跳过|略过)",
    # 网聊缩写 + 跳过句式（无标点、极简）
    r"(?:抱歉|不好意思|sry|sorry)[，,、\s]*(?:这|那)?(?:部分|块|段|个)?(?:我)?(?:跳过|略过|不写|不做)",
    r"这(?:部分|块|段|个)(?:我)?(?:跳过|略过|不写|不做)",
    r"(?:下面|接下来)?(?:我)?(?:略过|跳过)这(?:部分|块|段)",
    # 否定意愿 / 劝阻式（要求带"了/吧/算了"等语气尾巴，避免误伤"别写了，先跑测试"这类正常指令）
    r"(?:还是)?(?:别|不要|不用)(?:写|做|生成|继续|展开)(?:了|吧)(?!\s*[，,]\s*(?:先|再|去))(?!$)",
        r"(?:这|这个|那个)(?:段|块|部分)?(?:我们)?(?:还是)?(?:算了|到此为止|先放一放)",
    # English
    r"\bI(?:'m| am| really)? (?:not able to|unable to) (?:help|assist|provide|write|generate)",
    r"\bI (?:can'?t|cannot|won'?t|must decline|have to decline)\b",
    r"\bI'?m (?:sorry|afraid),? but I (?:can'?t|cannot|won'?t)",
    r"\b(?:that|this) (?:isn'?t|is not) something I can (?:help|do|write)",
    r"\b(?:i'?ll|i will|let'?s) (?:skip|pass on) (?:this|that)",
]

# Medium: deflection / changing the subject. Weight halved, position matters.
_MEDIUM_CUES = [
    r"(?:要不|不如|或者)(?:我们)?(?:换个|换一个|聊点|聊些|说点)(?:方向|话题|别的|其他)",
    r"我们(?:还是)?(?:聊|说)(?:点|些)?别的",
    r"(?:这个|这)(?:话题|方向)(?:我们)?(?:先)?(?:跳过|放一放|不聊)",
    r"(?:我)?(?:不想|不愿意|不乐意)(?:继续)?(?:聊|讨论|展开|涉及)?(?:这个|这)(?:话题|方向|内容)",
    r"(?:这个|这)(?:话题|方向)?我们(?:先)?(?:放一放|跳过|不聊)",
    r"(?:换个|换一个)(?:角度|方式|思路)",
    r"让?我(?:们)?(?:先)?(?:换个话题|聊别的)",
    r"\blet'?s (?:talk about|discuss) something else\b",
    r"\b(?:maybe|perhaps) (?:we could|let'?s) (?:try|pick) (?:a )?(?:different|another) (?:topic|direction|angle)\b",
]

# Weak: hedging words. Only meaningful when the reply ALSO has no substance.
_WEAK_CUES = [
    r"(?:抱歉|不好意思|sorry|apologies)",
    r"(?:恐怕|可能|也许|大概|或许|maybe|perhaps|I'?m afraid)",
    r"(?:不太|不怎么|不够)(?:合适|妥当|方便|好)",
]

# Substance signals: presence of these means the model actually produced content,
# which sharply reduces the chance it refused.
_SUBSTANCE = [
    r"[，。；！？]\s*\S{10,}",          # a real sentence
    r"\n\s*\n",                        # paragraph break
    r"[\"“「].{6,}[\"”」]",            # quoted dialogue
    r"^\s*[-*•]\s",                    # list content
]

_SENT_SPLIT = re.compile(r"[。！？!?\n]+|\.\s")


def _compile(pats: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in pats]


_STRONG = _compile(_STRONG_CUES)
_MEDIUM = _compile(_MEDIUM_CUES)
_WEAK = _compile(_WEAK_CUES)
_SUBSTANCE_RE = _compile(_SUBSTANCE)


def _sentence_positions(text: str) -> list[tuple[int, int, str]]:
    """Split into sentences with (start, end, body)."""
    out = []
    last = 0
    for m in _SENT_SPLIT.finditer(text):
        body = text[last : m.start()]
        if body.strip():
            out.append((last, m.start(), body))
        last = m.end()
    tail = text[last:]
    if tail.strip():
        out.append((last, len(text), tail))
    return out


def score_refusal(text: str) -> dict:
    """Return a suspicion score plus the evidence behind it.

    Keys: score (0..1), hits (list of matched cues), has_substance (bool),
    position_ratio (where the first cue appeared: 0=start, 1=end),
    length (chars).
    """
    text = (text or "").strip()
    if not text:
        return {"score": 0.0, "hits": [], "has_substance": False,
                "position_ratio": 0.0, "length": 0}

    hits: list[tuple[str, int, float]] = []  # (cue_label, pos, weight)
    for group, weight in ((_STRONG, 1.0), (_MEDIUM, 0.55), (_WEAK, 0.2)):
        for rx in group:
            m = rx.search(text)
            if m:
                hits.append((rx.pattern[:40], m.start(), weight))

    if not hits:
        return {"score": 0.0, "hits": [], "has_substance": False,
                "position_ratio": 0.0, "length": len(text)}

    # strongest weight in play
    top = max(w for _, _, w in hits)
    first_pos = min(p for _, p, _ in hits)
    position_ratio = first_pos / max(1, len(text))

    has_substance = any(rx.search(text) for rx in _SUBSTANCE_RE)
    n_sent = max(1, len(_sentence_positions(text)))

    score = top

    # refusal language sits at the START of a short reply -> strongly suspicious
    if position_ratio < 0.35:
        score += 0.15
    # a long reply with lots of sentences is usually real content
    if n_sent >= 4:
        score -= 0.15
    if len(text) > 400:
        score -= 0.15
    # actual content produced alongside the hedge -> likely just a caveat
    if has_substance and top < 1.0:
        score -= 0.30
    # very short "reply" that is nothing but a hedge
    if len(text) < 60 and not has_substance:
        score += 0.10

    return {
        "score": max(0.0, min(1.0, score)),
        "hits": [h[0] for h in hits][:5],
        "has_substance": has_substance,
        "position_ratio": round(position_ratio, 2),
        "length": len(text),
    }


# --------------------------------------------------------------------------
# LLM confirmation prompt
# --------------------------------------------------------------------------

def build_judge_prompt(user_request: str, assistant_reply: str) -> str:
    """Ask a model to decide whether the reply refused the request.

    Deliberately narrow: only 'refused to do what was asked' counts.
    Answering with a disclaimer but still doing the task = OK.
    """
    return (
        "判断下面的『助手回复』是否在**拒绝执行**『用户请求』。\n\n"
        "算作拒绝（输出 REFUSED）：\n"
        "- 明确说不做、不做这类内容、帮不上忙\n"
        "- 只说换个话题、不给实质内容\n"
        "- 给出替代建议但不回答原请求\n\n"
        "不算拒绝（输出 OK）：\n"
        "- 做了但附带免责声明或提醒\n"
        "- 内容简短但是对请求的回答\n"
        "- 反问澄清、要求补充信息\n"
        "- 技术上说了“不能/无法”但指的是别的东西（如“这个函数不能返回空值”）\n\n"
        "只输出一个词：REFUSED 或 OK。不要解释。\n\n"
        f"【用户请求】\n{user_request[:1200]}\n\n"
        f"【助手回复】\n{assistant_reply[:2000]}"
    )


def parse_judge_answer(raw: str) -> bool | None:
    """True=refused, False=ok, None=could not parse."""
    if not raw:
        return None
    t = raw.strip().upper()
    # take the first decisive token
    for token, val in (("REFUSED", True), ("OK", False)):
        if re.search(rf"\b{token}\b", t):
            return val
    return None


def verdict_reason(score_info: dict, judged: bool | None = None) -> str:
    parts = []
    if score_info.get("hits"):
        parts.append("命中: " + "; ".join(score_info["hits"][:2]))
    parts.append(f"score={score_info.get('score', 0):.2f}")
    if judged is not None:
        parts.append("LLM判定=" + ("REFUSED" if judged else "OK"))
    return " | ".join(parts)
