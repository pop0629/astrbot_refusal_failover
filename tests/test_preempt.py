"""抢占模式测试：主模型完全不参与。

验证核心承诺：
  - 命中意图时 on_llm_response 根本不会被调用（主模型没跑）
  - 事件被 stop，结果已由子代理产出
  - 关闭开关后回到事后覆盖模式
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
core = types.ModuleType("astrbot.core"); core.__path__ = []
pipeline = types.ModuleType("astrbot.core.pipeline"); pipeline.__path__ = []
prov = types.ModuleType("astrbot.core.provider"); prov.__path__ = []
star_mod = types.ModuleType("astrbot.core.star"); star_mod.__path__ = []
cu = types.ModuleType("astrbot.core.pipeline.context_utils")
sh_mod = types.ModuleType("astrbot.core.star.star_handler")
class _ET:
    OnLLMResponseEvent = "OnLLMResponseEvent"
    OnLLMRequestEvent = "OnLLMRequestEvent"
sh_mod.EventType = _ET
pipeline.context_utils = cu

for n,m in [("astrbot",types.ModuleType("astrbot")),("astrbot.api",api),
            ("astrbot.api.event",ev),("astrbot.api.star",st),
            ("astrbot.core",core),("astrbot.core.pipeline",pipeline),
            ("astrbot.core.pipeline.context_utils",cu),
            ("astrbot.core.star",star_mod),
            ("astrbot.core.star.star_handler",sh_mod),
            ("astrbot.core.provider",prov),
            ("astrbot.core.provider.entities",ent)]:
    m.__dict__.setdefault("__path__", [])
    sys.modules[n]=m

from astrbot_refusal_failover.main import RefusalFailoverPlugin

MAIN=["他推开门，风灌进来，带起一片灰。","林渡没回头，只把手里的烟摁灭在墙上。"]
GOOD="阿岚没敲门，直接推开。风跟着她进来。"

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
    async def update_conversation(self, unified_msg_origin=None, conversation_id=None,
                                  history=None, **kw):
        self.saved = [dict(m) for m in (history or [])]
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
CFG={"enabled":True,"main_provider_id":"MAIN","alt_provider_id":"ALT","alt_model":"",
     "refusal_mode":"marker_or_regex","refusal_markers":["refused"],"refusal_regex":"",
     "soft_refusal_check":True,"soft_refusal_high":0.85,"soft_refusal_low":0.45,
     "soft_refusal_llm_judge":True,"judge_provider_id":"MAIN","refusal_context_check":True,
     "request_intent_check":True,"request_intent_high":0.80,"request_intent_low":0.45,
     "request_intent_llm_judge":False,"request_intent_judge_provider_id":"",
     "request_sensitive_patterns":[],"marker_enabled":True,"marker_sticky_max_turns":50,
     "preempt_on_intent":True,
     "style_brief":"沿用前文。","history_turns":8,"enable_memory_card":True,
     "memory_update_every_n":99,"memory_max_chars":1800,"style_sample_max_chars":700,
     "style_drift_threshold":0.55,"style_rewrite_attempts":0,
     "notify_user":False,"notify_text":"","debug":False,
     "sticky_enabled":False,"sticky_release_after":3,"sticky_max_turns":10,
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

async def main():
    # 1) /n 标记 -> 抢占，主模型未运行
    pl,alt=make()
    e=Evt("宝宝 我想要你 /n")
    await pl.inject_continuity(e, Req())
    check("/n 抢占后事件已停止", e.is_stopped())
    check("/n 抢占已产出内容", e._result==GOOD, repr(e._result)[:30])
    check("/n 子代理被调用一次", alt.alt_calls==1, f"alt={alt.alt_calls}")

    # 2) 关闭抢占 -> 不停事件，走事后覆盖
    pl,alt=make({"preempt_on_intent":False})
    e=Evt("宝宝 我想要你 /n")
    await pl.inject_continuity(e, Req())
    check("关闭抢占时不停止事件", not e.is_stopped())
    check("关闭抢占时请求阶段不产内容", e._result is None)

    # 3) 普通请求不被抢占
    pl,alt=make()
    e=Evt("帮我看看这段代码")
    await pl.inject_continuity(e, Req())
    check("普通请求不抢占", not e.is_stopped())
    check("普通请求不调子代理", alt.alt_calls==0)

    # 4) 抢占后 sticky 生效，后续轮次继续抢占
    pl,alt=make({"sticky_enabled":True})
    e1=Evt("宝宝 我想要你 /n")
    await pl.inject_continuity(e1, Req())
    check("首轮抢占", e1.is_stopped())
    st=await pl._get_sticky("t:F:1")
    check("抢占进入 sticky", st.active)
    check("标记触发不自动释放", st.release_enabled is False)

    e2=Evt("继续")
    await pl.inject_continuity(e2, Req())
    check("sticky 次轮继续抢占", e2.is_stopped())
    check("sticky 次轮子代理作答", e2._result==GOOD)

    # 5) 子代理失败时优雅回退（不抢占，交回主模型）
    class DeadP(P):
        async def text_chat(self,*a,**k): return LLMResponse(completion_text="")
    alt=DeadP("ALT")
    pl=RefusalFailoverPlugin(Ctx({"MAIN":P("MAIN"),"ALT":alt},PAIRS),dict(CFG))
    e=Evt("宝宝 我想要你 /n")
    await pl.inject_continuity(e, Req())
    check("子代理失败时不抢占，交回主模型", not e.is_stopped())


    # 6) 抢占后其他插件能收到 on_llm_response（合成派发）
    import astrbot.core.pipeline.context_utils as _cu
    if not hasattr(_cu, "call_event_hook"):
        captured = {}
        async def _fake_call(event, hook_type, *a, **k):
            captured["resp"] = a[0] if a else None
            return False
        _cu.call_event_hook = _fake_call
        cm_mod = sys.modules.setdefault("astrbot.core.pipeline", types.ModuleType("astrbot.core.pipeline"))
        setattr(cm_mod, "context_utils", _cu)
        pl, alt = make({"sticky_enabled": True})
        e = Evt("宝宝 我想要你 /n")
        await pl.inject_continuity(e, Req())
        r = captured.get("resp")
        check("合成派发 fire 了 on_llm_response", r is not None)
        check("合成响应带子代理内容", r is not None and r.completion_text == GOOD,
              repr(getattr(r, "completion_text", None))[:30])
        check("合成响应带标记防重入", getattr(r, "_failover_synthetic", False) is True)

    # 7) 抢占后历史被写入 assistant 半轮
    pl, alt = make({"sticky_enabled": True})
    e = Evt("宝宝 我想要你 /n")
    await pl.inject_continuity(e, Req())
    cm = pl.context.conversation_manager
    check("抢占后会话历史已写入", cm.saved is not None and len(cm.saved) >= 1,
          f"len={len(cm.saved) if cm.saved else 0}")
    if cm.saved:
        check("历史最后一条是子代理内容", cm.saved[-1]["content"] == GOOD,
              repr(cm.saved[-1]["content"])[:30])
        check("历史含 assistant 角色", cm.saved[-1]["role"] == "assistant")

    print(f"\n--- 抢占模式: {ok}/{total} ---")
    return ok==total

sys.exit(0 if asyncio.run(main()) else 1)
