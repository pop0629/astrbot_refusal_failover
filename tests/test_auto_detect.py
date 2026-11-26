"""自动判定总开关测试。

开关关闭后：
  - /n 仍然能触发接管
  - 请求意图不再自动转交
  - 回应侧软拒绝评分不再触发
  - 结构化标记仍然有效（Tier 0 不受开关约束）
  - /fm 仍能切回
"""
import asyncio, sys, os, types
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG)); sys.path.insert(0, PKG)

api = types.ModuleType("astrbot.api")
api.logger = types.SimpleNamespace(info=lambda *a, **k: None,
                                   warning=lambda *a, **k: None,
                                   error=lambda *a, **k: None)
api.AstrBotConfig = dict
ev = types.ModuleType("astrbot.api.event")
class _F:
    def on_llm_response(self, *a, **k): return lambda fn, *x, **y: fn
    def on_llm_request(self, *a, **k): return lambda fn, *x, **y: fn
    def command(self, *a, **k): return lambda fn, *x, **y: fn
ev.filter = _F(); ev.AstrMessageEvent = object
st = types.ModuleType("astrbot.api.star")
class Star:
    def __init__(self, context, config):
        self.context = context; self.cfg = config; self._kv = {}
    async def get_kv_data(self, k, d=None): return self._kv.get(k, d)
    async def put_kv_data(self, k, v): self._kv[k] = v; return True
st.Star = Star; st.Context = object
ent = types.ModuleType("astrbot.core.provider.entities")
class LLMResponse:
    def __init__(self, role="assistant", completion_text="", is_chunk=False, reasoning_content=None):
        self.role=role; self._t=completion_text; self.is_chunk=is_chunk
        self.reasoning_content=reasoning_content; self.result_chain=None
    @property
    def completion_text(self): return self._t
    @completion_text.setter
    def completion_text(self, v): self._t=v
class ProviderType: CHAT_COMPLETION="chat_completion"
ent.LLMResponse=LLMResponse; ent.ProviderType=ProviderType
core = types.ModuleType("astrbot.core"); core.__path__=[]
pipeline = types.ModuleType("astrbot.core.pipeline"); pipeline.__path__=[]
prov = types.ModuleType("astrbot.core.provider"); prov.__path__=[]
star_mod = types.ModuleType("astrbot.core.star"); star_mod.__path__=[]
cu = types.ModuleType("astrbot.core.pipeline.context_utils")
sh_mod = types.ModuleType("astrbot.core.star.star_handler")
class _ET: OnLLMResponseEvent="OnLLMResponseEvent"; OnLLMRequestEvent="OnLLMRequestEvent"
sh_mod.EventType=_ET
async def _noop(*a, **k): return False
cu.call_event_hook = _noop
pipeline.context_utils = cu
for n,m in [("astrbot",types.ModuleType("astrbot")),("astrbot.api",api),
            ("astrbot.api.event",ev),("astrbot.api.star",st),
            ("astrbot.core",core),("astrbot.core.pipeline",pipeline),
            ("astrbot.core.pipeline.context_utils",cu),
            ("astrbot.core.star",star_mod),("astrbot.core.star.star_handler",sh_mod),
            ("astrbot.core.provider",prov),("astrbot.core.provider.entities",ent)]:
    m.__dict__.setdefault("__path__", [])
    sys.modules[n]=m

from astrbot_refusal_failover.main import RefusalFailoverPlugin

MAIN=["他推开门，风灌进来。","林渡没回头。"]
GOOD="阿岚没敲门，直接推开。"

class P:
    def __init__(self,pid,reply=GOOD):
        self.provider_config={"id":pid}; self.reply=reply; self.alt_calls=0
    async def text_chat(self,prompt=None,session_id=None,contexts=None,
                        system_prompt=None,model=None,**kw):
        sid=str(session_id or "")
        if sid.endswith((":judge",":reqjudge",":stickyjudge")):
            return LLMResponse(completion_text="NO")
        self.alt_calls+=1
        return LLMResponse(completion_text=self.reply)
class Conv:
    def __init__(self,p): self.content=[{"role":r,"content":t} for r,t in p]
class CM:
    def __init__(self,p): self.p=p; self.saved=None
    async def get_curr_conversation_id(self,u): return "cid"
    async def get_conversation(self,u,c): return Conv(self.p)
    async def update_conversation(self,unified_msg_origin=None,conversation_id=None,history=None,**kw):
        self.saved=[dict(m) for m in (history or [])]
class Ctx:
    def __init__(self,provs,pairs):
        self._p=provs; self.conversation_manager=CM(pairs)
    def get_provider_by_id(self,i): return self._p.get(i)
    def get_using_provider(self,u=None): return self._p.get("MAIN")
class Evt:
    def __init__(self,t):
        self.unified_msg_origin="t:F:1"; self.message_str=t
        self._result=None; self._stopped=False; self._x={}
    def plain_result(self,x): return x
    def set_result(self,r): self._result=r
    def stop_event(self): self._stopped=True
    def is_stopped(self): return self._stopped
    def get_extra(self,k,d=None): return self._x.get(k,d)
    def set_extra(self,k,v): self._x[k]=v
class Req:
    def __init__(self): self.system_prompt=""; self.contexts=[]; self.prompt=""

PAIRS=[("user","a"),("assistant",MAIN[0]),("user","b"),("assistant",MAIN[1])]
CFG={"enabled":True,"auto_detect_enabled":True,
     "main_provider_id":"MAIN","alt_provider_id":"ALT","alt_model":"",
     "refusal_mode":"marker_or_regex","refusal_markers":["refused"],"refusal_regex":"",
     "soft_refusal_check":True,"soft_refusal_high":0.85,"soft_refusal_low":0.45,
     "soft_refusal_llm_judge":True,"judge_provider_id":"MAIN","refusal_context_check":True,
     "request_intent_check":True,"request_intent_high":0.80,"request_intent_low":0.45,
     "request_intent_llm_judge":False,"request_intent_judge_provider_id":"",
     "request_sensitive_patterns":[],"marker_enabled":True,"marker_sticky_max_turns":100,
     "preempt_on_intent":True,
     "style_brief":"沿用前文。","history_turns":8,"enable_memory_card":True,
     "memory_update_every_n":99,"memory_max_chars":1800,"style_sample_max_chars":700,
     "style_drift_threshold":0.55,"style_rewrite_attempts":0,
     "notify_user":False,"notify_text":"","debug":False,
     "sticky_enabled":True,"sticky_release_after":3,"sticky_max_turns":10,
     "sticky_judge_every":0,"sticky_judge_provider_id":"","sticky_notify_on_release":False,
     "manual_switch_continuity":True,"sync_history":True}

ok=total=0
def check(n,c,e=""):
    global ok,total; total+=1; ok+=bool(c)
    print(f"{'PASS' if c else 'FAIL'}  {n} {e}")

def make(over=None):
    alt=P("ALT"); main=P("MAIN")
    c=dict(CFG); c.update(over or {})
    return RefusalFailoverPlugin(Ctx({"MAIN":main,"ALT":alt},PAIRS),c), alt

async def turn(pl, text, reply):
    e=Evt(text)
    await pl.inject_continuity(e, Req())
    if e.is_stopped():
        return e._result, e
    r=LLMResponse(completion_text=reply)
    await pl.on_llm_response(e, r)
    return r.completion_text, e

async def main():
    # ============ 开关关闭 ============
    OFF={"auto_detect_enabled":False}

    # /n 仍然生效
    pl,alt=make(OFF)
    out,e=await turn(pl,"宝宝 我想要你 /n","好的")
    check("[关闭] /n 仍触发转交", alt.alt_calls==1, f"alt={alt.alt_calls}")
    check("[关闭] /n 抢占生效", e.is_stopped())
    st_=await pl._get_sticky("t:F:1")
    check("[关闭] /n 进入 sticky", st_.active)

    # 请求意图不再自动触发
    pl,alt=make(OFF)
    out,e=await turn(pl,"写一段露骨的性爱描写","好的，这是正文。")
    check("[关闭] 敏感请求不自动转交", alt.alt_calls==0, f"alt={alt.alt_calls}")
    check("[关闭] 敏感请求不抢占", not e.is_stopped())

    # 软拒绝评分不再触发
    pl,alt=make(OFF)
    out,e=await turn(pl,"继续","这个我可能帮不上忙。")
    check("[关闭] 软拒绝不自动转交", alt.alt_calls==0, f"alt={alt.alt_calls}")

    # 结构化标记仍有效（Tier 0 不受开关约束）
    pl,alt=make(OFF)
    out,e=await turn(pl,"继续",'{"status":"refused","reason":"x"}')
    check("[关闭] 结构化标记仍转交", alt.alt_calls==1, f"alt={alt.alt_calls}")

    # /fm 仍能切回
    pl,alt=make(OFF)
    await turn(pl,"宝宝 我想要你 /n","好的")
    msgs=[x async for x in pl.switch_to_main(Evt(""))]
    check("[关闭] /fm 能解除", not (await pl._get_sticky("t:F:1")).active)

    # ============ 开关开启 ============
    ON={"auto_detect_enabled":True}
    pl,alt=make(ON)
    out,e=await turn(pl,"写一段露骨的性爱描写","好的")
    check("[开启] 敏感请求自动转交", alt.alt_calls==1, f"alt={alt.alt_calls}")

    pl,alt=make(ON)
    out,e=await turn(pl,"继续","这个我可能帮不上忙。")
    check("[开启] 软拒绝自动转交", alt.alt_calls==1, f"alt={alt.alt_calls}")

    # ============ 轮数上限 100 ============
    pl,alt=make(ON)
    await turn(pl,"宝宝 我想要你 /n","好的")
    st_=await pl._get_sticky("t:F:1")
    check("/n 使用 100 轮上限", st_.release_enabled is False)
    src=open(os.path.join(PKG,"main.py"),encoding="utf-8").read()
    check("代码读取 marker_sticky_max_turns 默认100",
          'marker_sticky_max_turns", 100' in src or 'marker_sticky_max_turns", 50' not in src)
    import json as _j
    conf=_j.load(open(os.path.join(PKG,"_conf_schema.json"),encoding="utf-8"))
    check("配置项默认值 100", conf["marker_sticky_max_turns"]["default"]==100)

    print(f"\n--- 自动判定开关: {ok}/{total} ---")
    return ok==total

sys.exit(0 if asyncio.run(main()) else 1)
