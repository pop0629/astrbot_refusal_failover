"""Style profiling + drift detection.

Two jobs:
1. build_style_sample(history) -> a compact "voice fingerprint" extracted
   from the MAIN model's own recent replies. This is far more effective
   than a hand-written style_brief: the alt model imitates real text,
   not a description of text.
2. style_drift(alt_text, main_samples) -> 0..1. Low score means the alt
   output looks like it came from someone else. Used to trigger a rewrite.

Deliberately dependency-free (no sklearn): char n-gram cosine + a few
cheap stylometric features. Runs in <1ms on normal reply lengths.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_CJK = re.compile(r"[\u4e00-\u9fff]")
_SENT_SPLIT = re.compile(r"[。！？!?…]+|\.\s")
_LIST_MARK = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s", re.MULTILINE)
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF]"
)
_POLITE = re.compile(r"(您好|请问|非常感谢|抱歉|对不起|希望对您有帮助|如需|不胜感激)")
_META = re.compile(
    r"(作为(一个)?(AI|人工智能|语言模型)|我将为(你|您)|以下是|下面我|希望这(个|些)回答)"
)


def _vec(text: str, n: int = 2) -> Counter:
    t = re.sub(r"\s+", "", text or "")
    if len(t) < n:
        return Counter()
    return Counter(t[i : i + n] for i in range(len(t) - n + 1))


def _cos(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    num = sum(a[k] * b[k] for k in common)
    da = math.sqrt(sum(v * v for v in a.values()))
    db = math.sqrt(sum(v * v for v in b.values()))
    return num / (da * db) if da and db else 0.0


_FUNC_CHARS = set("的了着是在和与就都也很不没他她它我你们这那个一上下里过把被让给对从到向虽说然后可但是因为所以")


def _char_profile(text: str) -> Counter:
    """Function-word / punctuation profile: captures voice better than content n-grams."""
    t = text or ""
    prof = Counter()
    for ch in t:
        if ch in _FUNC_CHARS:
            prof[ch] += 1
        elif ch in "，。！？、…—":
            prof[ch] += 1
    return prof


def _voice_similarity(a: str, b: str) -> float:
    """Blend of content 2-gram cosine and function-char profile cosine.

    Both components are cosine over sparse counters, so short texts score
    low in absolute terms. We rescale to a "same-author?" band:
    raw 0.0-0.25 -> 0-0.5, raw 0.25-0.55 -> 0.5-0.8, raw >=0.55 -> 0.8-1.
    """
    content = _cos(_vec(a), _vec(b))
    voice = _cos(_char_profile(a), _char_profile(b))
    raw = 0.45 * content + 0.55 * voice
    if raw <= 0.0:
        return 0.0
    if raw < 0.25:
        return raw / 0.25 * 0.5
    if raw < 0.55:
        return 0.5 + (raw - 0.25) / 0.30 * 0.3
    return min(1.0, 0.8 + (raw - 0.55) / 0.45 * 0.2)



def _stats(text: str) -> dict:
    text = text or ""
    sents = [s for s in _SENT_SPLIT.split(text) if s.strip()]
    n_cjk = len(_CJK.findall(text))
    n_all = max(1, len(re.sub(r"\s+", "", text)))
    return {
        "len": n_all,
        "sent_len": n_all / max(1, len(sents)),
        "cjk_ratio": n_cjk / n_all,
        "list_ratio": len(_LIST_MARK.findall(text)) / max(1, len(text) / 80),
        "emoji": len(_EMOJI.findall(text)),
        "polite": len(_POLITE.findall(text)),
        "meta": len(_META.findall(text)),
        "para": text.count("\n\n") + 1,
    }


def build_style_sample(main_replies: list[str], max_chars: int = 700) -> str:
    """Condense the main model's recent replies into an imitation sample.

    Keeps the tail (most recent voice) and prefers complete lines.
    """
    replies = [r.strip() for r in main_replies if r and r.strip()]
    if not replies:
        return ""
    # prefer the most recent 2-3 replies, most recent last
    picked: list[str] = []
    total = 0
    for r in reversed(replies):
        if total + len(r) > max_chars and picked:
            break
        picked.append(r)
        total += len(r)
    picked.reverse()
    out = "\n---\n".join(picked)
    return out[-max_chars:]


def style_drift(alt_text: str, main_replies: list[str]) -> float:
    """Return similarity in 0..1 (higher = closer). <0.55 is a strong mismatch."""
    samples = [r for r in main_replies if r and r.strip()]
    if not samples or not alt_text.strip():
        return 1.0  # cannot judge -> do not trigger rewrite

    alt_v = _vec(alt_text)
    # score against each sample, take the best (voice varies turn to turn)
    best = 0.0
    for s in samples[-5:]:
        best = max(best, _voice_similarity(alt_text, s))

    sa = _stats(alt_text)
    ref = [_stats(s) for s in samples[-5:]]
    mean = lambda k: sum(r[k] for r in ref) / len(ref)  # noqa: E731

    penalty = 0.0
    # length: allow big variance, only punish extreme mismatch
    ml = mean("len")
    if ml > 0:
        ratio = sa["len"] / ml
        if ratio < 0.35 or ratio > 3.0:
            penalty += 0.12
    # sentence length drift
    msl = mean("sent_len") or 1
    if sa["sent_len"] / msl > 2.5:
        penalty += 0.08
    # formatting: alt started listing when main never did
    if sa["list_ratio"] > 0.5 and mean("list_ratio") < 0.1:
        penalty += 0.15
    # emoji / politeness / meta-narration leakage
    if sa["emoji"] > mean("emoji") + 2:
        penalty += 0.08
    if sa["polite"] > mean("polite") + 1:
        penalty += 0.10
    if sa["meta"] > mean("meta"):
        penalty += 0.15

    return max(0.0, min(1.0, best - penalty))


def drift_report(alt_text: str, main_replies: list[str]) -> dict:
    return {
        "similarity": round(style_drift(alt_text, main_replies), 3),
        "alt_len": _stats(alt_text)["len"],
        "stats": {k: round(v, 2) for k, v in _stats(alt_text).items()},
    }
