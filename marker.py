"""Explicit user marker parsing.

The most reliable signal is the user saying so. Instead of guessing intent
from vocabulary, support a trailing marker that the user appends to a message.

    /n       generate NSFW - route this turn to the ALT provider
    /nsfw    same, spelled out
    /c       cancel the force flag (in case a previous turn left one)

Design notes:
- The marker is PER-TURN. It is consumed on use and never persists, so a
  forgotten marker cannot silently redirect future turns.
- The marker is stripped from the text before the request hits the model,
  otherwise the model sees an instruction it has no vocabulary for and may
  echo it back.
- A marker only applies when it is a standalone token, so a path like
  "/n/n" inside code, or "/normal", is not matched.
"""

from __future__ import annotations

import re

# Trailing marker: may be preceded by whitespace, punctuation, CJK, or nothing.
# Chinese users habitually type "吗/n" with no space - requiring \s missed it.
# Still anchored to end-of-string so "/n/abc" and mid-sentence "/n" are ignored.
_N_MARKER = re.compile(
    r"^(?:/(?:n|nsfw)\s*[。.!！?？]*\s*$)"          # message is only the marker
    r"|(?:(?<=[\s。，、；：！？…,;:!?])"
    r"|(?<=[\u4e00-\u9fff\u3040-\u30ff0-9A-Za-z]))"
    r"/(?:n|nsfw)\s*[。.!！?？]*\s*$",              # trailing marker
    re.IGNORECASE,
)


def extract_force_flag(text: str) -> tuple[bool, str]:
    """Return (has_flag, cleaned_text).

    >>> extract_force_flag("继续写这段 /n")
    (True, '继续写这段')
    >>> extract_force_flag("继续写这段")
    (False, '继续写这段')
    """
    if not text:
        return False, text or ""

    m = _N_MARKER.search(text)
    if not m:
        return False, text

    cleaned = text[: m.start()].rstrip()
    return True, cleaned


def strip_all_markers(text: str) -> str:
    """Remove the marker anywhere it appears as a standalone token.

    Used defensively before sending text to a model, in case the marker shows
    up mid-message (quoted from context, etc.).
    """
    if not text:
        return text or ""
    return re.sub(r"(?:^|\s)/(?:n|nsfw)(?=\s|$)", " ", text, flags=re.IGNORECASE).strip()
