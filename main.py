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

        verdict: RefusalVerdict = await self._judge_refusal(event, text)

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

        await self._maybe_refresh_memory(event, umo, card, alt_text, current)

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

    async def terminate(self):
        pass
