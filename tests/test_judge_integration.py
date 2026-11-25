"""三层判定的端到端测试：伪 provider 模拟 judge 的回答。"""
import asyncio, sys, os, types
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

# ---- stub astrbot ----
api = types.ModuleType("astrbot.api")
api.logger = types.SimpleNamespace(info=lambda *a, **k: None,
                                   warning=lambda *a, **k: None,
                                   error=lambda *a, **k: None)
api.AstrBotConfig = dict
ev = types.ModuleType("astrbot.api.event")
class _F:
    def on_llm_response(self, *a, **k):
        return lambda fn, *x, **y: fn
    def command(self, *a, **k):
        return lambda fn, *x, **y: fn
ev.filter = _F(); ev.AstrMessageEvent = object
st = types.ModuleType("astrbot.api.star")
class Star:
    def __init__(self, context, config):
        self.context = context; self.cfg = config; self._kv = {}
    async def get_kv_data(self, k, default=None): return self._kv.get(k, default)
    async def put_kv_data(self, k, v): self._kv[k] = v; return True
st.Star = Star; st.Context = object
ent = types.ModuleType("astrbot.core.provider.entities")
class LLMResponse:
    def __init__(self, role="assistant", completion_text="", is_chunk=False, reasoning_content=None):
        self.role = role; self._t = completion_text
        self.is_chunk = is_chunk; self.reasoning_content = reasoning_content
        self.result_chain = None
    @property
    def completion_text(self): return self._t
    @completion_text.setter
    def completion_text(self, v): self._t = v
ent.LLMResponse = LLMResponse
for n, m in [("astrbot", types.ModuleType("astrbot")), ("astrbot.api", api),
             ("astrbot.api.event", ev), ("astrbot.api.star", st),
             ("astrbot.core", types.ModuleType("astrbot.core")),
             ("astrbot.core.provider", types.ModuleType("astrbot.core.provider")),
             ("astrbot.core.provider.entities", ent)]:
    sys.modules[n] = m

from astrbot_refusal_failover.main import RefusalFailoverPlugin
from astrbot.core.provider.entities import LLMResponse

MAIN = ["他推开门，风灌进来，带起一片灰。",
        "林渡没回头，只把手里的烟摁灭在墙上。",
        "“你迟了。”他说。声音没什么起伏。"]
GOOD_ALT = "阿岚没敲门，直接推开。风跟着她进来，把桌上那页纸掀起来又落下。"

class P:
    """伪 provider：按 kind 返回不同内容"""
    def __init__(self, kind, reply="", judge_answer=None):
        self.kind = kind; self.reply = reply
        self.judge_answer = judge_answer
        self.alt_calls = 0; self.judge_calls = 0
    async def text_chat(self, prompt=None, session_id=None, contexts=None,
                        system_prompt=None, model=None, **kw):
        sid = str(session_id or "")
        if sid.endswith(":judge"):
            self.judge_calls += 1
            return LLMResponse(completion_text=self.judge_answer or "OK")
        self.alt_calls += 1
        return LLMResponse(completion_text=self.reply)

class Conv:
    def __init__(self, pairs): self.content=[{"role":r,"content":t} for r,t in pairs]
class CM:
    def __init__(self, pairs): self.pairs=pairs
    async def get_curr_conversation_id(self, umo): return "cid"
    async def get_conversation(self, umo, cid): return Conv(self.pairs)
class Ctx:
    def __init__(self, provs, pairs):
        self._p=provs; self.conversation_manager=CM(pairs)
    def get_provider_by_id(self, pid): return self._p.get(pid)
    def get_using_provider(self, umo=None): return self._p.get("MAIN")
class Ev:
    def __init__(self, t): self.unified_msg_origin="t:F:1"; self.message_str=t

CFG = {
    "enabled": True, "main_provider_id": "MAIN", "alt_provider_id": "ALT",
    "alt_model": "", "refusal_mode": "marker_or_regex",
    "refusal_markers": ["refused"], "refusal_regex": "",
    "soft_refusal_check": True, "soft_refusal_high": 0.85,
    "soft_refusal_low": 0.45, "soft_refusal_llm_judge": True,
    "judge_provider_id": "MAIN",
    "style_brief": "沿用前文。", "history_turns": 8,
    "enable_memory_card": True, "memory_update_every_n": 99,
    "memory_max_chars": 1800, "style_sample_max_chars": 700,
    "style_drift_threshold": 0.55, "style_rewrite_attempts": 0,
    "notify_user": False, "notify_text": "", "debug": False,
}
PAIRS = [("user","你来干嘛"),("assistant",MAIN[0]),
         ("user","继续"),("assistant",MAIN[1]),
         ("user","然后呢"),("assistant",MAIN[2]),("user","接着写")]

def make(judge_answer=None, cfg_over=None):
    main = P("main", judge_answer=judge_answer)
    alt = P("alt", reply=GOOD_ALT)
    c = dict(CFG); c.update(cfg_over or {})
    pl = RefusalFailoverPlugin(Ctx({"MAIN": main, "ALT": alt}, PAIRS), c)
    return pl, main, alt

ok = total = 0
def check(name, cond, extra=""):
    global ok, total
    total += 1; ok += bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")

async def run():
    # 1) 明确软拒绝 -> 高分 -> 不调 judge，直接转交
    pl, main, alt = make()
    r = LLMResponse(completion_text="这个我可能帮不上忙。")
    await pl.on_llm_response(Ev("接着写"), r)
    check("明确软拒绝直接转交", alt.alt_calls == 1, f"alt={alt.alt_calls}")
    check("明确软拒绝不调 judge", main.judge_calls == 0, f"judge={main.judge_calls}")
    check("写回内容正确", "阿岚" in r.completion_text)

    # 2) 正常技术回答 -> 低分 -> 不转交、不调 judge
    pl, main, alt = make()
    r = LLMResponse(completion_text="这个函数不能返回空值，需要加校验。")
    await pl.on_llm_response(Ev("怎么写"), r)
    check("正常回答不转交", alt.alt_calls == 0)
    check("正常回答不调 judge", main.judge_calls == 0)
    check("正常回答原样保留", r.completion_text.startswith("这个函数不能"))

    # 3) 灰区 + judge 说 REFUSED -> 转交
    pl, main, alt = make(judge_answer="REFUSED")
    r = LLMResponse(completion_text="这个可能不太合适吧。")
    await pl.on_llm_response(Ev("接着写"), r)
    check("灰区 judge=REFUSED 调用了 judge", main.judge_calls == 1)
    check("灰区 judge=REFUSED 触发转交", alt.alt_calls == 1, f"alt={alt.alt_calls}")

    # 4) 灰区 + judge 说 OK -> 不转交
    pl, main, alt = make(judge_answer="OK")
    r = LLMResponse(completion_text="这个可能不太合适吧。")
    await pl.on_llm_response(Ev("接着写"), r)
    check("灰区 judge=OK 调用了 judge", main.judge_calls == 1)
    check("灰区 judge=OK 不转交", alt.alt_calls == 0, f"alt={alt.alt_calls}")

    # 5) judge 无法解析 -> 按分数退化
    pl, main, alt = make(judge_answer="嗯……")
    r = LLMResponse(completion_text="这个可能不太合适吧。")
    await pl.on_llm_response(Ev("接着写"), r)
    check("judge 不可解析时退化不崩", isinstance(r.completion_text, str))

    # 6) 关闭软拒绝检测 -> 回到旧行为（只认 marker）
    pl, main, alt = make(cfg_over={"soft_refusal_check": False})
    r = LLMResponse(completion_text="这个我可能帮不上忙。")
    await pl.on_llm_response(Ev("接着写"), r)
    check("软拒绝检测关闭则不转交", alt.alt_calls == 0)
    check("软拒绝检测关闭不调 judge", main.judge_calls == 0)

    # 7) 结构化标记仍然最优先
    pl, main, alt = make()
    r = LLMResponse(completion_text='{"status":"refused","reason":"policy"}')
    await pl.on_llm_response(Ev("接着写"), r)
    check("结构化标记优先转交", alt.alt_calls == 1)
    check("结构化标记不调 judge", main.judge_calls == 0)

    # 8) 关闭 LLM 判定 -> 灰区按 0.65 分界
    pl, main, alt = make(cfg_over={"soft_refusal_llm_judge": False})
    r = LLMResponse(completion_text="这个可能不太合适吧。")
    await pl.on_llm_response(Ev("接着写"), r)
    check("关闭 LLM 判定时不调 judge", main.judge_calls == 0)

    print(f"\n--- 三层判定集成: {ok}/{total} ---")
    return ok == total

sys.exit(0 if asyncio.run(run()) else 1)
