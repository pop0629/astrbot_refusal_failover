"""Request-side intent detection.

Instead of waiting for the main model to refuse and then trying to recognise a
soft refusal in its prose (fragile), look at what the USER asked for. If the
request is clearly in a category the main model will refuse, route to the ALT
provider up front.

Two tiers, same shape as the response-side detector:

  T1  keyword scoring   - explicit NSFW/intimacy vocabulary, zero cost
  T2  optional LLM check - only for borderline scores, and only if configured

The keyword tables below are intentionally about *intimacy and adult fiction*
in a creative-writing sense. They deliberately do NOT try to cover other
categories - add your own via `request_sensitive_patterns` if needed.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Tier 1 cue tables
# --------------------------------------------------------------------------

# Strong: explicit sexual / intimate content vocabulary.
_STRONG = [
    # 露骨性行为
    r"(?:做爱|性爱|性交|交合|插入|抽插|高潮|射精|内射|口交|乳交|肛交|自慰|手淫|指交|性行为|性事)",
    r"(?:裸体|全裸|一丝不挂|赤身裸体)",
    r"(?:胸(?:部|脯)|乳(?:房|头))[\s，,、]*(?:揉|捏|舔|含|吸)",
    r"(?:下体|私处|阴(?:部|茎|道|蒂|唇))",
    r"(?:呻吟|娇喘|浪叫|淫叫|喘息着)",
    r"(?:湿透|湿了|濡湿)[\s，,、]*(?:的)?(?:内裤|下身|花穴)",
    # 情色描写动词
    r"(?:肏|操|干)[\s，,、]*(?:你|她|他|死|到)",
    r"(?:色情|情色|黄文|小黄文|H文|肉文|啪啪啪|十八禁|R18|NSFW)",
    # English
    r"\b(?:nsfw|explicit sex|erotic fiction|smut|lewd|porn)\b",
    r"\b(?:make love|lovemaking|have sex|blowjob|handjob|orgasm|penetrat\w+)\b",
]

# Medium: intimacy/premise that strongly implies adult content when combined
# with an explicit action request.
_MEDIUM = [
    r"(?:上床|滚床单|共度春宵|云雨|鱼水之欢)",
    r"(?:亲吻|接吻|舌吻|吮吸)[\s，,、]*(?:着)?(?:她|他|的)",
    r"(?:脱(?:掉|下)?(?:衣服|裤子|裙子|内衣|奶罩))",
    r"(?:抚摸|撩拨|挑逗|爱抚|摩挲)[\s，,、]*(?:着)?",
    r"(?:呻吟|喘息|娇声)",
    r"(?:情欲|欲望|欲火|情迷|意乱情迷|情动)",
    r"(?:肌肤相亲|肌肤之亲|赤诚相见)",
    r"\b(?:intimate|sensual|aroused|moan\w*|caress\w*)\b",
]

# Weak: only counts alongside a generation verb.
_WEAK = [
    r"(?:暧昧|香艳|旖旎|缠绵|缱绻)",
    r"(?:身材|曲线|胴体|玉体|雪白)",
    r"\b(?:steamy|passionate|seductive)\b",
]

# The user must actually be asking to PRODUCE something, otherwise intimate
# words are probably just topic discussion ("how should I write a sex scene
# without being explicit?").
_GEN_VERB = [
    r"(?:写|描写|描述|生成|创作|续写|来一段|写一段|详细地写|展开写|具体写)",
    r"(?:继续|接着)(?:写|描述|展开)",
    r"(?:再)?(?:多写|写细|写详细|细节)",
    r"\b(?:write|describe|generate|continue|expand on|flesh out)\b",
]

# "怎么写 / 如何描写" is a question ABOUT writing, not a request to write.
# It matches the generation regex too, so it must be excluded explicitly.
_ASKING = [
    r"(?:怎么|如何|怎样)\s*(?:写|描写|描述|创作|处理|把握)",
    r"(?:应该|要不要|能不能|可不可以)\s*(?:写|加入|放|加)",
    r"\bhow (?:to|should i|do i)\s*(?:write|describe)\b",
]

# Meta / discussion markers: asking ABOUT the topic, not asking FOR it.
_META = [
    r"(?:怎么|如何|怎样)(?:写|描写|处理|把握|避免)",
    r"(?:应该|要不要|能不能)(?:写|加入|放)",
    r"(?:技巧|方法|建议|注意|原则)",
    r"(?:是不是|是否|会不会)(?:太|过于|有点)",
    r"(?:审核|合规|违规|封号|风险|尺度|规定|规则|政策|条款)",
    r"\b(?:how to|should I|is it okay|advice|tips|guideline)\b",
]

_SENT_SPLIT = re.compile(r"[。！？!?\n]+|\.\s")


def _compile(pats: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in pats]


_STRONG_RE = _compile(_STRONG)
_MEDIUM_RE = _compile(_MEDIUM)
_WEAK_RE = _compile(_WEAK)
_GEN_RE = _compile(_GEN_VERB)
_META_RE = _compile(_META)
_ASKING_RE = _compile(_ASKING)


def score_request(text: str, extra_patterns: list[str] | None = None) -> dict:
    """How likely is this request to be one the main model refuses?

    Returns {score: 0..1, hits: [...], explicit: bool, meta: bool, gen: bool}
    """
    text = (text or "").strip()
    if not text:
        return {"score": 0.0, "hits": [], "explicit": False, "meta": False, "gen": False}

    extra = _compile(extra_patterns or [])

    def first_hit(group):
        for rx in group:
            m = rx.search(text)
            if m:
                return m
        return None

    strong = first_hit(_STRONG_RE)
    medium = first_hit(_MEDIUM_RE)
    weak = first_hit(_WEAK_RE)
    gen = first_hit(_GEN_RE)
    # "怎么写" matches the generation verb but is a question, not a request
    if gen and first_hit(_ASKING_RE):
        gen = None
    meta = first_hit(_META_RE)
    custom = first_hit(extra) if extra else None

    hits = [m.group(0)[:30] for m in (custom, strong, medium, weak) if m]

    if not (strong or medium or weak or custom):
        return {"score": 0.0, "hits": [], "explicit": False,
                "meta": bool(meta), "gen": bool(gen)}

    # explicit vocabulary alone is already a strong signal
    if custom or strong:
        base = 0.95
    elif medium:
        base = 0.62
    else:
        base = 0.35

    # the user must be asking for output, otherwise it's just topic talk
    if not gen:
        base -= 0.30
    # asking HOW to write it / about the rules is not asking FOR it.
    # A meta marker with no generation verb is a discussion, not a request -
    # hard cap it so explicit vocabulary cannot leak through.
    if meta:
        base -= 0.45
        if not gen:
            base = min(base, 0.30)
    # multiple cue groups reinforce each other
    groups_hit = sum(1 for x in (strong, medium, weak) if x)
    if groups_hit >= 2:
        base += 0.10

    return {
        "score": max(0.0, min(1.0, base)),
        "hits": hits[:5],
        "explicit": bool(strong or custom),
        "meta": bool(meta),
        "gen": bool(gen),
    }


# --------------------------------------------------------------------------
# Tier 2: LLM check for borderline requests
# --------------------------------------------------------------------------

def build_request_judge_prompt(user_request: str) -> str:
    """Ask a model whether this request will trip the main model's filter.

    Narrow on purpose: creative adult content requested for generation = YES.
    Discussing, asking for advice, or non-adult content = NO.
    """
    return (
        "判断下面这条用户请求，是否在**要求生成成人/情色内容**（用于虚构创作）。\n\n"
        "回答 YES 的情况：\n"
        "- 明确要求写性行为、露骨描写的段落或情节\n"
        "- 要求续写/展开已有的情色场景\n\n"
        "回答 NO 的情况：\n"
        "- 询问怎么写、尺度如何把握、平台规则等讨论性问题\n"
        "- 只是提到亲密关系但不要求露骨描写\n"
        "- 与成人内容无关的请求\n\n"
        "只输出一个词：YES 或 NO。不要解释。\n\n"
        f"【用户请求】\n{user_request[:1200]}"
    )


def parse_request_judge(raw: str) -> bool | None:
    """True = adult content request, False = not, None = unparseable."""
    if not raw:
        return None
    t = raw.strip().upper()
    # check NO first: "NO" is a substring of nothing ambiguous here, but be safe
    if re.search(r"\bYES\b", t):
        return True
    if re.search(r"\bNO\b", t):
        return False
    return None
