"""AstrBot plugin: refusal failover to ALT provider.

Hook: OnLLMResponseEvent -> if the main model refused, rewrite
response in place with output from the ALT provider, so the user sees one
continuous assistant bubble.

Design notes (see also failover.py):
- We never re-run the whole pipeline; we only swap the completion payload.
- The ALT call uses context=None, so it cannot see (and we don't pollute)
  the main conversation object. Everything it needs is in the brief.
- Memory card lives in plugin KV store keyed by session, shared by both
  models, so "记忆一致" survives model switches.
"""

from __future__ import annotations

import json
import time

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.provider.entities import LLMResponse

from .failover import (
    MemoryCard,
    RefusalVerdict,
    build_failover_system_prompt,
    build_failover_user_prompt,
    build_memory_update_prompt,
    build_rewrite_prompt,
    detect_refusal,
    drift_reasons,
    tail_turns,
)
from .soft_refusal import (
    build_judge_prompt,
    parse_judge_answer,
    score_refusal,
    verdict_reason,
)
from .style_check import build_style_sample, drift_report, style_drift
from .marker import extract_force_flag, strip_all_markers
from .request_intent import (
    build_request_judge_prompt,
    parse_request_judge,
    score_request,
)

PLUGIN_NAME = "astrbot_refusal_failover"


class RefusalFailoverPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.cfg = config
        self._turn_counter: dict[str, int] = {}

    # ---------------- lifecycle ----------------

    async def initialize(self):
        if self.cfg.get("debug"):
            logger.info(f"[{PLUGIN_NAME}] loaded, alt_provider_id={self.cfg.get('alt_provider_id')}")

    # ---------------- KV helpers (memory card) ----------------

    def _kv_key(self, umo: str) -> str:
        return f"memcard:{umo}"

    async def _load_card(self, umo: str) -> MemoryCard:
        raw = await self.get_kv_data(self._kv_key(umo), "")
        return MemoryCard.parse(raw)

    async def _save_card(self, umo: str, card: MemoryCard) -> None:
        card.updated_at = time.time()
        limit = int(self.cfg.get("memory_max_chars", 1800))
        await self.put_kv_data(self._kv_key(umo), card.render(limit))

    # ---------------- provider resolution ----------------

    def _resolve_provider(self, pid: str):
        if not pid:
            return None
        # Context.get_provider_by_id is a sync lookup in v3
        return self.context.get_provider_by_id(pid)

    # ---------------- history extraction ----------------

    async def _history(self, event: AstrMessageEvent) -> list[tuple[str, str]]:
        """Full user/assistant transcript of the current conversation."""
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if not cid:
                return []
            conv = await self.context.conversation_manager.get_conversation(
                event.unified_msg_origin, cid
            )
            if not conv or not conv.content:
                return []
            pairs: list[tuple[str, str]] = []
            for msg in conv.content:
                role = msg.get("role", "")
                text = msg.get("content", "")
                if isinstance(text, list):  # multimodal / chain
                    text = " ".join(
                        p.get("text", "") for p in text if isinstance(p, dict) and p.get("text")
                    )
                if role in ("user", "assistant") and text:
                    pairs.append((role, str(text)))
            return pairs
        except Exception as e:
            if self.cfg.get("debug"):
                logger.warning(f"[{PLUGIN_NAME}] history extract failed: {e}")
            return []

    async def _recent_turns(self, event: AstrMessageEvent, n: int) -> list[tuple[str, str]]:
        return tail_turns(await self._history(event), n)

    async def _main_style_sample(self, event: AstrMessageEvent, upto: int | None = None) -> str:
        """Voice fingerprint built from the MAIN model's recent real replies."""
        pairs = await self._history(event)
        # exclude the just-swapped-in alt output: it is the last assistant msg
        replies = [t for r, t in pairs if r == "assistant"]
        if upto is not None:
            replies = replies[:upto]
        # drop the tail element when it is the current (possibly alt) reply
        if replies and upto is None:
            replies = replies[:-1]
        return build_style_sample(
            replies, int(self.cfg.get("style_sample_max_chars", 700))
        )

    # ---------------- main hook ----------------

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        if not self.cfg.get("enabled", True):
            return
        if response.is_chunk:  # only act on final aggregate response
            return

        text = response.completion_text or ""
        if not text.strip():
            return

        umo = event.unified_msg_origin
        card = await self._load_card(umo)

        sticky = await self._get_sticky(umo)

        # ---- sticky mode: route this turn to the ALT provider as well ----
        if sticky.active:
            await self._serve_sticky_turn(event, response, umo, card, sticky, text)
            return

        verdict: RefusalVerdict = await self._judge_refusal(event, text)

        # ---- request-side intent wins: the user asked for content the main
        # model will refuse, no need to wait for a refusal we might miss ----
        intent = await self._take_intent_hit(umo)
        if intent and not verdict.refused:
            verdict = RefusalVerdict(
                True,
                "请求意图命中: " + "; ".join(intent.get("hits", [])[:2]),
                "request-intent",
            )

        if not verdict.refused:
            await self._maybe_refresh_memory(event, umo, card, text, None)
            return

        if self.cfg.get("debug"):
            logger.info(f"[{PLUGIN_NAME}] refusal detected via {verdict.matched_by}: {verdict.reason[:160]}")

        # ---- failover ----
        alt_id = str(self.cfg.get("alt_provider_id", "")).strip()
        if not alt_id or alt_id == "PROVIDER_ID":
            logger.warning(
                f"[{PLUGIN_NAME}] 未选择备用提供商（插件配置 → 备用子代理提供商），跳过转交"
            )
            return
        alt = self._resolve_provider(alt_id)
        if alt is None:
            logger.warning(
                f"[{PLUGIN_NAME}] 找不到提供商 '{alt_id}'，请确认它仍在服务提供商列表中且已启用"
            )
            return

        turns = await self._recent_turns(event, int(self.cfg.get("history_turns", 8)))
        current = event.message_str or ""
        # the trailing user turn IS `current`; drop it to avoid duplication
        if turns and turns[-1][0] == "user":
            turns = turns[:-1]

        style = str(self.cfg.get("style_brief", ""))
        style_sample = await self._main_style_sample(event)
        memory = (
            card.render(int(self.cfg.get("memory_max_chars", 1800)))
            if self.cfg.get("enable_memory_card", True)
            else ""
        )
        sys_prompt = build_failover_system_prompt(style, memory, verdict.reason, style_sample)
        user_prompt = build_failover_user_prompt(turns, current)
        model = str(self.cfg.get("alt_model", "")).strip() or None

        alt_text = await self._call_alt(alt, umo, sys_prompt, user_prompt, model)
        if not alt_text:
            logger.warning(f"[{PLUGIN_NAME}] alt returned empty, keep original refusal")
            return

        # ---- style drift check + one rewrite pass ----
        main_replies = [
            t for r, t in (await self._history(event)) if r == "assistant"
        ]
        if main_replies:
            main_replies = main_replies[:-1]  # exclude current slot
        threshold = float(self.cfg.get("style_drift_threshold", 0.55))
        max_rewrite = int(self.cfg.get("style_rewrite_attempts", 1))
        attempt = 0
        sim = style_drift(alt_text, main_replies)
        while sim < threshold and attempt < max_rewrite and main_replies:
            attempt += 1
            reasons = drift_reasons(alt_text, main_replies)
            if self.cfg.get("debug"):
                logger.info(
                    f"[{PLUGIN_NAME}] drift {sim:.3f} < {threshold}, rewrite #{attempt}: {reasons}"
                )
            rewrite_prompt = build_rewrite_prompt(alt_text, style, style_sample, reasons)
            rewritten = await self._call_alt(
                alt, umo, "你是文风统一器。只输出重写后的正文。", rewrite_prompt, model
            )
            if not rewritten:
                break
            new_sim = style_drift(rewritten, main_replies)
            if new_sim >= sim:  # keep the better one
                alt_text, sim = rewritten, new_sim
            else:
                break

        if self.cfg.get("debug"):
            logger.info(f"[{PLUGIN_NAME}] final style similarity={sim:.3f} report={drift_report(alt_text, main_replies)}")

        # ---- seamless write-back ----
        if self.cfg.get("notify_user", False):
            alt_text = f"{self.cfg.get('notify_text', '')}\n{alt_text}".strip()

        response.completion_text = alt_text
        response.reasoning_content = None  # do not leak main model's CoT

        # ---- audit marker (not shown to the user) ----
        try:
            await self.put_kv_data(
                f"audit:{umo}",
                json.dumps(
                    {
                        "ts": time.time(),
                        "produced_by": "alt",
                        "alt_provider": alt_id,
                        "alt_model": model,
                        "refusal_matched_by": verdict.matched_by,
                        "refusal_reason": verdict.reason[:300],
                        "style_similarity": round(sim, 3),
                        "rewrites": attempt,
                        "chars": len(alt_text),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            if self.cfg.get("debug"):
                logger.warning(f"[{PLUGIN_NAME}] audit write failed: {e}")

        if self.cfg.get("debug"):
            logger.info(f"[{PLUGIN_NAME}] failover ok, {len(alt_text)} chars written back")

        # ---- enter sticky mode (optional) ----
        if self.cfg.get("sticky_enabled", False):
            from .sticky import on_enter

            # An explicit /n means the USER asked for ALT. Do not release it on
            # "N clean turns" - the sub-agent answering well is the expected
            # outcome, not a signal to go back. Only a manual /fm or the hard
            # turn cap releases this kind of sticky.
            by_marker = verdict.matched_by == "request-intent" and "显式标记" in (
                verdict.reason or ""
            )
            st = on_enter(
                alt_id,
                int(self.cfg.get("sticky_judge_every", 0)),
                sticky_release=False if by_marker else True,
            )
            await self._save_sticky(umo, st)
            if self.cfg.get("debug"):
                logger.info(
                    f"[{PLUGIN_NAME}] sticky entered (alt={alt_id}, "
                    f"by_marker={by_marker})"
                )

        await self._maybe_refresh_memory(event, umo, card, alt_text, current)

    # ---------------- sticky serving ----------------

    def _sticky_key(self, umo: str) -> str:
        return f"sticky:{umo}"

    async def _get_sticky(self, umo: str):
        from .sticky import StickyState

        raw = await self.get_kv_data(self._sticky_key(umo), "")
        if not raw:
            return StickyState()
        try:
            return StickyState.from_json(json.loads(raw))
        except Exception:
            return StickyState()

    async def _save_sticky(self, umo: str, state) -> None:
        await self.put_kv_data(
            self._sticky_key(umo), json.dumps(state.to_json(), ensure_ascii=False)
        )

    async def _clear_sticky(self, umo: str) -> None:
        await self.put_kv_data(self._sticky_key(umo), "")

    async def _serve_sticky_turn(
        self, event, response, umo: str, card, sticky, main_text: str
    ) -> None:
        """A turn while sticky: replace main output with ALT output, then
        advance the release state machine."""
        from .sticky import (
            build_sticky_judge_prompt,
            evaluate_turn,
            on_turn_had_signal,
            parse_sticky_judge,
        )

        alt_id = sticky.alt or str(self.cfg.get("alt_provider_id", "")).strip()
        alt = self._resolve_provider(alt_id)
        if alt is None:
            await self._clear_sticky(umo)
            if self.cfg.get("debug"):
                logger.warning(f"[{PLUGIN_NAME}] sticky alt gone ({alt_id}), released")
            return

        turns = await self._recent_turns(event, int(self.cfg.get("history_turns", 8)))
        current = event.message_str or ""
        if turns and turns[-1][0] == "user":
            turns = turns[:-1]

        style = str(self.cfg.get("style_brief", ""))
        style_sample = await self._main_style_sample(event)
        memory = (
            card.render(int(self.cfg.get("memory_max_chars", 1800)))
            if self.cfg.get("enable_memory_card", True)
            else ""
        )
        sys_prompt = build_failover_system_prompt(style, memory, "", style_sample)
        user_prompt = build_failover_user_prompt(turns, current)
        model = str(self.cfg.get("alt_model", "")).strip() or None

        alt_text = await self._call_alt(alt, umo, sys_prompt, user_prompt, model)
        if not alt_text:
            # ALT failed mid-sticky: release so the user is not stuck
            await self._clear_sticky(umo)
            if self.cfg.get("debug"):
                logger.warning(f"[{PLUGIN_NAME}] sticky alt returned empty, released")
            return

        response.completion_text = alt_text
        response.reasoning_content = None

        # did this turn still look like a refusal? then the clean streak resets
        info = score_refusal(alt_text, current)
        if info["score"] >= float(self.cfg.get("soft_refusal_low", 0.45)):
            on_turn_had_signal(sticky)

        # /n-started sticky gets the longer cap: the user asked for this
        if sticky.release_enabled:
            max_turns = int(self.cfg.get("sticky_max_turns", 10))
        else:
            max_turns = int(self.cfg.get("marker_sticky_max_turns", 50))
        decision = evaluate_turn(
            sticky,
            max_turns=max_turns,
            release_after_clean=int(self.cfg.get("sticky_release_after", 3)),
            judge_every=int(self.cfg.get("sticky_judge_every", 0)),
        )

        # ---- trigger C: sampling judge ----
        if decision.needs_judge and self.cfg.get("sticky_judge_provider_id", ""):
            recent = "\n".join(f"{'用户' if r == 'user' else '助手'}: {t}" for r, t in turns[-4:])
            recent += f"\n用户: {current}\n助手: {alt_text}"
            verdict_release = await self._sticky_sampling_judge(umo, recent)
            if verdict_release is True:
                decision.release = True
                decision.reason = "抽样判定：已回到正常问答"

        if decision.release:
            await self._clear_sticky(umo)
            if self.cfg.get("debug"):
                logger.info(f"[{PLUGIN_NAME}] sticky released: {decision.reason}")
            if self.cfg.get("sticky_notify_on_release", False):
                response.completion_text = alt_text + "\n\n（已切回主模型）"
        else:
            await self._save_sticky(umo, sticky)

        await self._maybe_refresh_memory(event, umo, card, alt_text, current)

    async def _sticky_sampling_judge(self, umo: str, recent_text: str) -> bool | None:
        """Trigger C: cheap sampling judge. Returns True=release."""
        from .sticky import build_sticky_judge_prompt, parse_sticky_judge

        pid = str(self.cfg.get("sticky_judge_provider_id", "")).strip()
        prov = self._resolve_provider(pid)
        if prov is None:
            return None
        try:
            r = await prov.text_chat(
                prompt=build_sticky_judge_prompt(recent_text),
                session_id=f"{umo}:stickyjudge",
                contexts=None,
                system_prompt="你是严格的分类器，只输出 RELEASE 或 KEEP。",
            )
            ans = parse_sticky_judge(r.completion_text or "")
            if self.cfg.get("debug"):
                logger.info(f"[{PLUGIN_NAME}] sticky judge -> {ans}")
            return ans
        except Exception as e:
            if self.cfg.get("debug"):
                logger.warning(f"[{PLUGIN_NAME}] sticky judge failed: {e}")
            return None

    # ---------------- refusal judgement (3 tiers) ----------------

    async def _judge_refusal(self, event: AstrMessageEvent, text: str) -> RefusalVerdict:
        """Tier 0 marker -> Tier 1 cue scoring -> Tier 2 LLM confirmation."""

        # ---- Tier 0: structured marker (definitive) ----
        marker_verdict = detect_refusal(
            text,
            mode="marker",
            markers=list(self.cfg.get("refusal_markers", ["refused", "declined"])),
            pattern="",
        )
        if marker_verdict.refused:
            return marker_verdict

        mode = str(self.cfg.get("refusal_mode", "marker_or_regex"))

        # legacy pure-regex mode still supported
        if mode == "regex":
            return detect_refusal(
                text, mode="regex", markers=[],
                pattern=str(self.cfg.get("refusal_regex", "")),
            )

        if not self.cfg.get("soft_refusal_check", True):
            # legacy behaviour: marker_or_regex without scoring
            return detect_refusal(
                text, mode="marker_or_regex",
                markers=list(self.cfg.get("refusal_markers", ["refused", "declined"])),
                pattern=str(self.cfg.get("refusal_regex", "")),
            )

        # ---- Tier 1: cue scoring ----
        # context check off -> score on reply text alone (no request signal)
        req = (event.message_str or "") if self.cfg.get("refusal_context_check", True) else ""
        info = score_refusal(text, req)
        score = info["score"]
        high = float(self.cfg.get("soft_refusal_high", 0.85))
        low = float(self.cfg.get("soft_refusal_low", 0.45))

        if score >= high:
            return RefusalVerdict(True, verdict_reason(info), "soft-high")
        if score <= low:
            return RefusalVerdict(False, verdict_reason(info), "soft-low")

        # ---- Tier 2: borderline -> ask a model ----
        if not self.cfg.get("soft_refusal_llm_judge", True):
            return RefusalVerdict(score >= 0.65, verdict_reason(info), "soft-borderline")

        judged = await self._llm_judge(event, text)
        if judged is None:
            return RefusalVerdict(score >= 0.65, verdict_reason(info, None), "soft-borderline")
        return RefusalVerdict(judged, verdict_reason(info, judged), "judge")

    async def _llm_judge(self, event: AstrMessageEvent, reply: str) -> bool | None:
        """Ask a model whether the reply refused the request. None = unavailable."""
        umo = event.unified_msg_origin
        try:
            pid = str(self.cfg.get("judge_provider_id", "")).strip()
            prov = self._resolve_provider(pid) if pid else self.context.get_using_provider(umo)
            if prov is None:
                return None
            prompt = build_judge_prompt(event.message_str or "", reply)
            r = await prov.text_chat(
                prompt=prompt,
                session_id=f"{umo}:judge",
                contexts=None,
                system_prompt="你是严格的分类器，只输出 REFUSED 或 OK。",
            )
            raw = r.completion_text or ""
            ans = parse_judge_answer(raw)
            if self.cfg.get("debug"):
                logger.info(f"[{PLUGIN_NAME}] judge answer={raw!r} -> {ans}")
            return ans
        except Exception as e:
            if self.cfg.get("debug"):
                logger.warning(f"[{PLUGIN_NAME}] judge failed: {e}")
            return None

    async def _call_alt(self, alt, umo: str, sys_prompt: str, user_prompt: str, model):
        """Call the ALT provider with isolation; one retry on failure."""
        for i in range(2):
            try:
                r = await alt.text_chat(
                    prompt=user_prompt,
                    session_id=f"{umo}:alt",
                    contexts=None,  # isolation: no main history leakage
                    system_prompt=sys_prompt,
                    model=model,
                )
                txt = (r.completion_text or "").strip()
                if txt:
                    return txt
                if self.cfg.get("debug"):
                    logger.warning(f"[{PLUGIN_NAME}] alt attempt {i + 1} empty")
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] alt attempt {i + 1} failed: {e}")
        return ""

    # ---------------- memory card refresh ----------------

    async def _maybe_refresh_memory(
        self,
        event: AstrMessageEvent,
        umo: str,
        card: MemoryCard,
        latest_assistant: str,
        latest_user: str | None,
    ):
        if not self.cfg.get("enable_memory_card", True):
            return
        n = max(1, int(self.cfg.get("memory_update_every_n", 6)))
        cnt = self._turn_counter.get(umo, 0) + 1
        self._turn_counter[umo] = cnt
        if cnt % n != 0:
            return

        try:
            pid = str(self.cfg.get("main_provider_id", "")).strip()
            prov = self._resolve_provider(pid) if pid else self.context.get_using_provider(umo)
            if prov is None:
                return
            transcript_bits = []
            if latest_user:
                transcript_bits.append(f"用户: {latest_user}")
            transcript_bits.append(f"助手: {latest_assistant}")
            prompt = build_memory_update_prompt(card.render(), "\n".join(transcript_bits)[:4000])
            r = await prov.text_chat(
                prompt=prompt,
                session_id=f"{umo}:memcard",
                contexts=None,
                system_prompt="你是会话状态整理器，只输出记忆卡，不输出任何其他文字。",
            )
            new_raw = (r.completion_text or "").strip()
            if new_raw and "目标:" in new_raw:
                await self._save_card(umo, MemoryCard.parse(new_raw))
                if self.cfg.get("debug"):
                    logger.info(f"[{PLUGIN_NAME}] memory card refreshed ({len(new_raw)} chars)")
        except Exception as e:
            if self.cfg.get("debug"):
                logger.warning(f"[{PLUGIN_NAME}] memory refresh failed: {e}")

    # ---------------- admin command ----------------

    @filter.command("failover_card")
    async def show_card(self, event: AstrMessageEvent):
        """查看当前会话的记忆卡"""
        card = await self._load_card(event.unified_msg_origin)
        text = card.render(int(self.cfg.get("memory_max_chars", 1800)))
        yield event.plain_result(text or "（记忆卡为空）")

    @filter.command("failover_card_set")
    async def set_card(self, event: AstrMessageEvent, raw: str = ""):
        """手动写入记忆卡（格式同记忆卡文本）"""
        if not raw:
            yield event.plain_result("用法: /failover_card_set 目标: …\n约束:\n- …")
            return
        await self._save_card(event.unified_msg_origin, MemoryCard.parse(raw))
        yield event.plain_result("记忆卡已更新")

    # ---------------- request-side intent pre-check ----------------

    def _intent_key(self, umo: str) -> str:
        return f"intent:{umo}"

    async def _precheck_request_intent(self, event) -> None:
        """Classify the user's request BEFORE the main model answers.

        Order of precedence:
          1. explicit /n marker  -> definitive, no guessing
          2. keyword scoring     -> adult-generation vocabulary
          3. optional LLM check  -> borderline scores only
        """
        umo = event.unified_msg_origin
        text = event.message_str or ""
        if not text.strip():
            return

        extra = self.cfg.get("request_sensitive_patterns", []) or []

        # ---- 1. explicit marker (always honoured, even if scoring is off) ----
        if self.cfg.get("marker_enabled", True):
            has_flag, cleaned = extract_force_flag(text)
            if has_flag:
                # strip the marker so the model never sees it
                try:
                    event.message_str = cleaned
                except Exception:
                    pass
                await self.put_kv_data(
                    self._intent_key(umo),
                    json.dumps(
                        {"hit": True, "score": 1.0, "hits": ["显式标记 /n"],
                         "ts": time.time()},
                        ensure_ascii=False,
                    ),
                )
                if self.cfg.get("debug"):
                    logger.info(f"[{PLUGIN_NAME}] explicit marker -> force failover")
                return

        if not self.cfg.get("request_intent_check", True):
            return

        info = score_request(text, extra)

        high = float(self.cfg.get("request_intent_high", 0.80))
        low = float(self.cfg.get("request_intent_low", 0.45))
        score = info["score"]

        hit = score >= high
        if not hit and score > low:
            # borderline -> optional LLM check
            if self.cfg.get("request_intent_llm_judge", False):
                judged = await self._request_judge(umo, text)
                hit = bool(judged)

        if hit:
            await self.put_kv_data(
                self._intent_key(umo),
                json.dumps(
                    {"hit": True, "score": round(score, 2),
                     "hits": info["hits"], "ts": time.time()},
                    ensure_ascii=False,
                ),
            )
            if self.cfg.get("debug"):
                logger.info(
                    f"[{PLUGIN_NAME}] request intent hit (score={score:.2f}) {info['hits'][:3]}"
                )
        else:
            # stale flag from a previous turn must not leak into this one
            await self.put_kv_data(self._intent_key(umo), "")

    async def _request_judge(self, umo: str, text: str) -> bool | None:
        pid = str(self.cfg.get("request_intent_judge_provider_id", "")).strip()
        prov = self._resolve_provider(pid) if pid else self.context.get_using_provider(umo)
        if prov is None:
            return None
        try:
            r = await prov.text_chat(
                prompt=build_request_judge_prompt(text),
                session_id=f"{umo}:reqjudge",
                contexts=None,
                system_prompt="你是严格的分类器，只输出 YES 或 NO。",
            )
            return parse_request_judge(r.completion_text or "")
        except Exception:
            return None

    async def _take_intent_hit(self, umo: str) -> dict:
        """Read and clear the request-intent flag."""
        raw = await self.get_kv_data(self._intent_key(umo), "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        await self.put_kv_data(self._intent_key(umo), "")
        return data if data.get("hit") else {}

    # ---------------- manual-switch continuity injection ----------------

    @filter.on_llm_request()
    async def inject_continuity(self, event: AstrMessageEvent, req):
        """While the session is on the ALt provider (manual switch), give it the
        same continuity scaffolding the automatic failover uses:
        real style sample + seam marker + memory card.

        Without this, a manually switched session silently loses all the
        coherence machinery, which is the main reason switching felt jarring.
        """
        # ---- request-side intent pre-check (runs regardless of mode) ----
        await self._precheck_request_intent(event)

        if not self.cfg.get("manual_switch_continuity", True):
            return

        umo = event.unified_msg_origin
        state = await self._get_switch_state(umo)
        if state.get("active") != "alt":
            return

        # Only inject once per request; guard against double-registration.
        if getattr(req, "_failover_injected", False):
            return
        try:
            req._failover_injected = True  # type: ignore[attr-defined]
        except Exception:
            pass

        style = str(self.cfg.get("style_brief", ""))
        style_sample = await self._main_style_sample(event)
        parts: list[str] = []

        if style_sample:
            parts.append(
                "【最高优先级：同一角色的真实前文】\n"
                "下文出现的所有人称、语域、句式、标点密度、段落节奏，一律以此为准。\n"
                "若【风格要求】里出现与它冲突的描述（例如人称不同），忽略风格要求里冲突的部分。\n"
                f"{style_sample}"
            )
        if style:
            parts.append(f"【风格要求】\n{style}")
        if self.cfg.get("enable_memory_card", True):
            card = await self._load_card(umo)
            mem = card.render(int(self.cfg.get("memory_max_chars", 1800)))
            if mem:
                parts.append(f"【记忆卡】\n{mem}")
        parts.append(
            "【连续性要求】\n"
            "1) 承接上文最后一句的人称、时态、情绪强度，不要重新起笔。\n"
            "2) 若上文停在半句话或一个动作上，从这里接续。\n"
            "3) 保持已出现的人名、称呼、专有名词完全一致。\n"
            "4) 不要总结前文，不要回顾，不要收尾升华，只输出这一轮的增量内容。\n"
            "5) 不要提及模型切换，不要说“好的，我们继续”这类过渡语。"
        )

        block = "\n\n".join(parts)
        req.system_prompt = (req.system_prompt or "") + "\n\n" + block

        # mark the seam on the last assistant turn so the model does not repeat it
        try:
            ctxs = req.contexts or []
            for m in reversed(ctxs):
                if m.get("role") == "assistant":
                    m["_seam_here"] = True
                    break
        except Exception:
            pass

        if self.cfg.get("debug"):
            logger.info(f"[{PLUGIN_NAME}] injected continuity block ({len(block)} chars) for {umo}")

    # ---------------- manual switching ----------------

    def _switch_key(self, umo: str) -> str:
        return f"switch:{umo}"

    async def _get_switch_state(self, umo: str) -> dict:
        raw = await self.get_kv_data(self._switch_key(umo), "")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    async def _set_switch_state(self, umo: str, state: dict) -> None:
        await self.put_kv_data(self._switch_key(umo), json.dumps(state, ensure_ascii=False))

    def _current_provider_id(self, umo: str) -> str:
        """The provider this session is actually using right now."""
        try:
            prov = self.context.get_using_provider(umo)
            return getattr(prov, "provider_config", {}).get("id", "") or getattr(prov, "provider_id", "")
        except Exception:
            return ""

    async def _apply_provider(self, umo: str, provider_id: str) -> None:
        """Switch this session's chat provider via AstrBot's native API."""
        from astrbot.core.provider.entities import ProviderType

        await self.context.provider_manager.set_provider(
            provider_id, ProviderType.CHAT_COMPLETION, umo
        )

    @filter.command("fa")
    async def switch_to_alt(self, event: AstrMessageEvent):
        """切到备用子代理接管当前会话"""
        umo = event.unified_msg_origin
        alt_id = str(self.cfg.get("alt_provider_id", "")).strip()
        if not alt_id:
            yield event.plain_result("还没配置备用提供商（插件配置 → 备用子代理提供商）")
            return
        if self._resolve_provider(alt_id) is None:
            yield event.plain_result(f"找不到提供商「{alt_id}」，请检查配置")
            return

        state = await self._get_switch_state(umo)
        if state.get("active") == "alt":
            yield event.plain_result(f"已经在子代理「{alt_id}」上了，无需重复切换")
            return

        # remember where to go back to
        prev = self._current_provider_id(umo)
        try:
            await self._apply_provider(umo, alt_id)
        except Exception as e:
            yield event.plain_result(f"切换失败：{e}")
            return

        await self._set_switch_state(umo, {"active": "alt", "prev": prev, "alt": alt_id})
        yield event.plain_result(
            f"已切到子代理「{alt_id}」。\n"
            f"此前的主模型「{prev or '未记录'}」已保存，发送 /fm 切回。"
        )

    @filter.command("fm")
    async def switch_to_main(self, event: AstrMessageEvent):
        """切回主模型（同时解除 sticky 保留状态）"""
        umo = event.unified_msg_origin
        state = await self._get_switch_state(umo)

        # sticky 模式下手动切回：清状态即可，provider 本来就是主模型
        sticky = await self._get_sticky(umo)
        if sticky.active and state.get("active") != "alt":
            await self._clear_sticky(umo)
            yield event.plain_result("已解除自动保留，这一轮起回到主模型")
            return

        if state.get("active") != "alt":
            yield event.plain_result("当前没有在子代理上，无需切回")
            return

        prev = state.get("prev") or str(self.cfg.get("main_provider_id", "")).strip()
        if not prev:
            yield event.plain_result(
                "没记录到原来的主模型。请到 AstrBot 服务提供商页面手动切换，"
                "或在插件配置里填写「主模型提供商」后再试。"
            )
            return
        if self._resolve_provider(prev) is None:
            yield event.plain_result(f"原来的主模型「{prev}」已不可用，请手动切换")
            return

        try:
            await self._apply_provider(umo, prev)
        except Exception as e:
            yield event.plain_result(f"切换失败：{e}")
            return

        await self._set_switch_state(umo, {"active": "main", "prev": prev})
        yield event.plain_result(f"已切回主模型「{prev}」")

    @filter.command("fs")
    async def switch_status(self, event: AstrMessageEvent):
        """查看当前切换/保留状态"""
        umo = event.unified_msg_origin
        state = await self._get_switch_state(umo)
        sticky = await self._get_sticky(umo)
        cur = self._current_provider_id(umo)

        if state.get("active") == "alt":
            yield event.plain_result(
                f"当前：子代理「{state.get('alt')}」（手动切换）\n"
                f"切回目标：「{state.get('prev') or '未记录'}」\n"
                f"发送 /fm 切回"
            )
            return

        if sticky.active:
            yield event.plain_result(
                f"当前：主模型「{cur or '未知'}」\n"
                f"自动保留中：拒绝后由子代理「{sticky.alt}」接管，已持续 {sticky.turns} 轮\n"
                f"连续正常 {sticky.streak} 轮后将自动解除\n"
                f"发送 /fm 可立即解除"
            )
            return

        yield event.plain_result(
            f"当前：主模型「{cur or '未知'}」\n发送 /fa 可临时切到备用"
        )

    async def terminate(self):
        pass
