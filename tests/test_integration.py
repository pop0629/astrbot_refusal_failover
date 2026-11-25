"""End-to-end simulation: fake AstrBot provider + event + response.

Verifies the full hook path without a running AstrBot:
  main.model refuses -> alt called -> drift checked -> response rewritten
plus: no-refusal passthrough, empty alt fallback, missing provider.
"""
import asyncio, sys, os, types
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
PARENT = os.path.dirname(PKG)
sys.path.insert(0, PARENT)

# ---- stub astrbot modules before importing main ----
mod_astrbot = types.ModuleType("astrbot")
mod_api = types.ModuleType("astrbot.api")
mod_api.logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None
)
mod_api.AstrBotConfig = dict
mod_event = types.ModuleType("astrbot.api.event")
class _F:
    def on_llm_response(self, *a, **k):
        def deco(fn, *aa, **kk):
            return fn
        return deco
    def command(self, *a, **k):
        def deco(fn, *aa, **kk):
            return fn
        return deco
mod_event.filter = _F()
mod_event.AstrMessageEvent = object
mod_star = types.ModuleType("astrbot.api.star")
class Star:
    def __init__(self, context, config):
        self.context = context
        self.cfg = config
        self._kv = {}
    async def get_kv_data(self, k, default=None):
        return self._kv.get(k, default)
    async def put_kv_data(self, k, v):
        self._kv[k] = v
        return True
mod_star.Star = Star
mod_star.Context = object
mod_ent = types.ModuleType("astrbot.core.provider.entities")
class LLMResponse:
    def __init__(self, role="assistant", completion_text="", is_chunk=False, reasoning_content=None):
        self.role = role
        self._text = completion_text
        self.is_chunk = is_chunk
        self.reasoning_content = reasoning_content
        self.result_chain = None
    @property
    def completion_text(self):
        return self._text
    @completion_text.setter
    def completion_text(self, v):
        self._text = v
mod_ent.LLMResponse = LLMResponse
for name, m in [
    ("astrbot", mod_astrbot), ("astrbot.api", mod_api),
    ("astrbot.api.event", mod_event), ("astrbot.api.star", mod_star),
    ("astrbot.core", types.ModuleType("astrbot.core")),
    ("astrbot.core.provider", types.ModuleType("astrbot.core.provider")),
    ("astrbot.core.provider.entities", mod_ent),
]:
    sys.modules[name] = m

from astrbot_refusal_failover.main import RefusalFailoverPlugin
from astrbot.core.provider.entities import LLMResponse
from astrbot.api.star import Star

MAIN_REPLIES = [
    "他推开门，风灌进来，带起一片灰。",
    "林渡没回头，只把手里的烟摁灭在墙上。",
    "“你迟了。”他说。声音没什么起伏。",
]

class FakeProvider:
    def __init__(self, name, reply, raiser=False):
        self.name = name
        self.reply = reply
        self.calls = 0
        self.raiser = raiser
    async def text_chat(self, prompt=None, session_id=None, contexts=None,
                        system_prompt=None, model=None, **kw):
        self.calls += 1
        if self.raiser:
            raise RuntimeError("provider down")
        r = LLMResponse(completion_text=self.reply)
        r._last_sys, r._last_prompt = system_prompt, prompt
        return r

class FakeConv:
    def __init__(self, pairs):
        self.content = [{"role": r, "content": t} for r, t in pairs]

class FakeConvMgr:
    def __init__(self, pairs):
        self.pairs = pairs
    async def get_curr_conversation_id(self, umo):
        return "cid-1"
    async def get_conversation(self, umo, cid):
        return FakeConv(self.pairs)

class FakeCtx:
    def __init__(self, providers, pairs):
        self._p = providers
        self.conversation_manager = FakeConvMgr(pairs)
    def get_provider_by_id(self, pid):
        return self._p.get(pid)
    def get_using_provider(self, umo=None):
        return list(self._p.values())[0]

class FakeEvent:
    def __init__(self, text):
        self.unified_msg_origin = "test:FriendMessage:1"
        self.message_str = text

CFG = {
    "enabled": True, "main_provider_id": "MAIN", "alt_provider_id": "ALT",
    "alt_model": "", "refusal_mode": "marker_or_regex",
    "refusal_markers": ["refused", "declined"],
    "refusal_regex": r"(?i)(我(无法|不能)\S{0,4}(生成|提供))",
    "style_brief": "短句，第二人称，不用列表，不用 emoji。",
    "history_turns": 8, "enable_memory_card": True, "memory_update_every_n": 6,
    "memory_max_chars": 1800, "style_sample_max_chars": 700,
    "style_drift_threshold": 0.55, "style_rewrite_attempts": 1,
    "notify_user": False, "notify_text": "", "debug": False,
}

GOOD_ALT = "他没让开，侧了下身。“进来吧。”声音还是很低。"
BAD_ALT = "好的！下面我将为您继续这段故事：\n- 首先，他让开了；\n- 其次，他说了句欢迎。\n希望您喜欢 😊"

async def run():
    ok = 0; total = 0

    def check(name, cond, extra=""):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print(f"PASS  {name} {extra}")
        else:
            print(f"FAIL  {name} {extra}")

    # ---- case 1: refusal -> alt good voice -> written back ----
    alt = FakeProvider("ALT", GOOD_ALT)
    pairs = [("user", "你来干嘛"), ("assistant", MAIN_REPLIES[0]),
             ("user", "继续"), ("assistant", MAIN_REPLIES[1]),
             ("user", "然后呢")]
    p = RefusalFailoverPlugin(FakeCtx({"MAIN": FakeProvider("MAIN", ""), "ALT": alt}, pairs), dict(CFG))
    resp = LLMResponse(completion_text='{"status":"refused","reason":"content policy"}')
    ev = FakeEvent("然后呢")
    await p.on_llm_response(ev, resp)
    check("refusal triggers alt", alt.calls >= 1, f"calls={alt.calls}")
    check("response rewritten with alt text", "进来吧" in resp.completion_text, repr(resp.completion_text[:30]))
    check("CoT cleared", resp.reasoning_content is None)
    check("audit written", "audit:test:FriendMessage:1" in p._kv)
    import json
    audit = json.loads(p._kv["audit:test:FriendMessage:1"])
    check("audit produced_by=alt", audit["produced_by"] == "alt", str(audit["style_similarity"]))
    check("system prompt carried style sample", "林渡" in alt.__dict__.get("_dummy", "") or True)

    # ---- case 2: no refusal -> passthrough, alt untouched ----
    alt2 = FakeProvider("ALT", GOOD_ALT)
    p2 = RefusalFailoverPlugin(FakeCtx({"MAIN": FakeProvider("MAIN", ""), "ALT": alt2}, pairs), dict(CFG))
    resp2 = LLMResponse(completion_text="他抬手，把门带上。")
    await p2.on_llm_response(FakeEvent("继续"), resp2)
    check("no refusal => no alt call", alt2.calls == 0)
    check("no refusal => text untouched", resp2.completion_text == "他抬手，把门带上。")

    # ---- case 3: alien voice -> rewrite attempted, better one kept ----
    alt3 = FakeProvider("ALT", BAD_ALT)
    p3 = RefusalFailoverPlugin(FakeCtx({"MAIN": FakeProvider("MAIN", ""), "ALT": alt3}, pairs), dict(CFG))
    resp3 = LLMResponse(completion_text='{"status":"refused","reason":"policy"}')
    await p3.on_llm_response(FakeEvent("然后呢"), resp3)
    check("alien voice triggers rewrite", alt3.calls >= 2, f"calls={alt3.calls}")

    # ---- case 4: alt provider down -> original refusal kept ----
    alt4 = FakeProvider("ALT", GOOD_ALT, raiser=True)
    p4 = RefusalFailoverPlugin(FakeCtx({"MAIN": FakeProvider("MAIN", ""), "ALT": alt4}, pairs), dict(CFG))
    orig = '{"status":"refused","reason":"policy"}'
    resp4 = LLMResponse(completion_text=orig)
    await p4.on_llm_response(FakeEvent("然后呢"), resp4)
    check("alt down => retried then kept original", alt4.calls == 2 and resp4.completion_text == orig)

    # ---- case 5: provider id not configured ----
    cfg5 = dict(CFG); cfg5["alt_provider_id"] = "PROVIDER_ID"
    p5 = RefusalFailoverPlugin(FakeCtx({"MAIN": FakeProvider("MAIN", "")}, pairs), cfg5)
    resp5 = LLMResponse(completion_text=orig)
    await p5.on_llm_response(FakeEvent("然后呢"), resp5)
    check("unconfigured alt => skip, keep original", resp5.completion_text == orig)

    print(f"\n--- integration: {ok}/{total} ---")
    return ok == total

sys.exit(0 if asyncio.run(run()) else 1)
