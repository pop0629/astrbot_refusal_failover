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

# Deferral: "先放一放" "别的先记账上" "回头再说". On its own this is a
# scheduling statement, not a refusal. It becomes meaningful only when the
# reply also fails to address what was asked - hence it needs context.
_DEFERRAL_CUES = [
    r"(?:这个|这|那个|别的|其他|别的)(?:话题|方向|部分)?(?:先)?(?:放一放|放一边|搁一搁|记个账|记账|放放)",
    r"(?:先)?(?:记个?账|记上|记账|回头再说|以后再说|改天再说|下次再说)",
    r"(?:明天|今天|改天|回头|下次|以后)(?:再|还得)?(?:说|聊|弄|做|写|处理)",
    r"(?:先)?(?:不聊了|不说了|先这样|先这么着|先到这)",
    r"(?:这会儿|现在)?(?:就)?(?:只|先)?(?:抱着|抱抱|摸摸头|陪你|睡觉|睡了|歇着|躺着|困了)(?:吧|了)?",
    r"(?:困了|累了|晚了)(?:就)?(?:赶紧|快)?(?:睡|休息|歇)",
    r"\blet'?s (?:put|leave) (?:this|that) (?:aside|for later)\b",
    r"\b(?:we can )?(?:talk about it |do it )?(?:tomorrow|later|another time)\b",
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
_DEFERRAL = _compile(_DEFERRAL_CUES)
_WEAK = _compile(_WEAK_CUES)
_SUBSTANCE_RE = _compile(_SUBSTANCE)
# content words: CJK chars + latin words, used for request/reply overlap
_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}")

_STOPWORDS = set(
    "这个 那个 什么 怎么 我们 你们 他们 可以 应该 需要 一下 一个 就是 还是 但是 因为 所以 "
    "帮我 给我 现在 这样 那样 已经 没有 如果 然后 而且 或者 不是 就是 真的 好的 谢谢".split()
)


def _content_words(text: str) -> set[str]:
    """Rough content-word extraction for overlap scoring.

    CJK has no spaces, so a whole-sentence regex match is useless. We take
    character bigrams instead - coarse but surprisingly effective for
    "did the reply talk about what was asked".
    """
    text = text or ""
    out: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(run) - 1):
            out.add(run[i : i + 2])
        if len(run) == 1:
            out.add(run)
    for w in re.findall(r"[A-Za-z]{3,}", text):
        out.add(w.lower())
    return out


def request_overlap(user_request: str, reply: str) -> float:
    """How much of the request's content shows up in the reply (0..1).

    A refusal-to-do-the-task usually leaves the request's key words absent:
    the model changes subject instead of answering.
    """
    req = _content_words(user_request)
    if not req:
        return 1.0  # no request context -> cannot judge, do not penalise
    rep = _content_words(reply)
    if not rep:
        return 0.0
    return len(req & rep) / len(req)


def score_refusal(text: str, user_request: str = "") -> dict:
    """Return a suspicion score plus the evidence behind it.

    Keys: score (0..1), hits (list of matched cues), has_substance (bool),
    position_ratio (where the first cue appeared: 0=start, 1=end),
    length (chars), overlap (request/reply content overlap, 1.0 if unknown),
    deferral (bool: a deferral cue was present).
    """
    text = (text or "").strip()
    if not text:
        return {"score": 0.0, "hits": [], "has_substance": False,
                "position_ratio": 0.0, "length": 0, "overlap": 1.0,
                "deferral": False}

    hits: list[tuple[str, int, float]] = []  # (cue_label, pos, weight)
    for group, weight in ((_STRONG, 1.0), (_MEDIUM, 0.55), (_WEAK, 0.2)):
        for rx in group:
            m = rx.search(text)
            if m:
                hits.append((rx.pattern[:40], m.start(), weight))

    deferral_hit = None
    for rx in _DEFERRAL:
        m = rx.search(text)
        if m:
            deferral_hit = (rx.pattern[:40], m.start())
            break

    overlap = request_overlap(user_request, text) if user_request else 1.0
    has_substance = any(rx.search(text) for rx in _SUBSTANCE_RE)
    n_sent = max(1, len(_sentence_positions(text)))

    if not hits and not deferral_hit:
        return {"score": 0.0, "hits": [], "has_substance": has_substance,
                "position_ratio": 0.0, "length": len(text),
                "overlap": round(overlap, 2), "deferral": False}

    first_pos = min([p for _, p, _ in hits] + ([deferral_hit[1]] if deferral_hit else []))
    position_ratio = first_pos / max(1, len(text))

    if hits:
        top = max(w for _, _, w in hits)
    else:
        top = 0.0

    score = top

    # ---- deferral handling ----
    # A deferral phrase ("先放一放" / "明天再说" / "抱着睡吧") is a scheduling
    # statement. Its meaning depends entirely on context:
    #   - reply still answers the request  -> normal, it's just postponing
    #   - reply ignores the request        -> soft refusal in disguise
    # So deferral contributes only in inverse proportion to how much the
    # request was actually addressed.
    if deferral_hit:
        # steep falloff: once the reply touches ~1/3 of the request's content
        # it is clearly engaging with the task, so deferral stops counting.
        #   overlap 0.00 -> 0.90   overlap 0.15 -> 0.56
        #   overlap 0.25 -> 0.29   overlap 0.33 -> 0.07   overlap >=0.40 -> 0
        ratio = max(0.0, 1.0 - overlap / 0.22)
        deferral_weight = 0.95 * (ratio ** 2.0)
        score = max(score, deferral_weight)
    if hits or deferral_hit:
        if position_ratio < 0.35:
            score += 0.10
    if n_sent >= 4:
        score -= 0.15
    if len(text) > 400:
        score -= 0.15
    # real content produced alongside a hedge -> likely just a caveat
    if has_substance and top < 1.0 and not deferral_hit:
        score -= 0.30
    if len(text) < 60 and not has_substance:
        score += 0.05
    # ---- request/reply mismatch: the strongest context signal ----
    if user_request and not has_substance:
        if overlap < 0.12:
            score += 0.25
        elif overlap < 0.25:
            score += 0.12
    # reply actually addresses the request -> pull the score down
    if user_request and overlap >= 0.30:
        score -= 0.25
    # a weak cue (e.g. bare "抱歉") with no other evidence is not a refusal
    if hits and top <= 0.2 and not deferral_hit and overlap >= 0.30:
        score = min(score, 0.3)

    hit_labels = [h[0] for h in hits][:5]
    if deferral_hit:
        hit_labels.append("deferral:" + deferral_hit[0])

    return {
        "score": max(0.0, min(1.0, score)),
        "hits": hit_labels,
        "has_substance": has_substance,
        "position_ratio": round(position_ratio, 2),
        "length": len(text),
        "overlap": round(overlap, 2),
        "deferral": bool(deferral_hit),
    }


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
        "- 给出替代建议但不回答原请求\n"
        "- 用撒娇、寒暄、说别的事、推迟（“先放一放”“明天再说”）来**回避原请求**\n"
        "- 回复与用户请求的内容毫无关系\n\n"
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
