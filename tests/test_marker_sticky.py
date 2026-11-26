"""验证 /n 触发的 sticky 不被「连续正常 N 轮」自动解除。"""
import asyncio, sys, os, types, json
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG)); sys.path.insert(0, PKG)

from sticky import on_enter, evaluate_turn, StickyState

ok = total = 0
def check(n, c, e=""):
    global ok, total
    total += 1; ok += bool(c)
    print(f"{'PASS' if c else 'FAIL'}  {n} {e}")

# ---- 状态机层 ----
st = on_enter("ALT", judge_every=0, sticky_release=False)
check("进入时 release_enabled=False", st.release_enabled is False)
for i in range(1, 8):
    d = evaluate_turn(st, max_turns=50, release_after_clean=3, judge_every=0)
    check(f"第{i}轮不自动解除", not d.release, f"streak={st.streak}")

back = StickyState.from_json(st.to_json())
check("序列化保留 release_enabled", back.release_enabled is False)
check("默认构造为 True", StickyState().release_enabled is True)

st2 = on_enter("ALT", judge_every=0, sticky_release=False)
st2.turns = 50
d = evaluate_turn(st2, max_turns=50, release_after_clean=3, judge_every=0)
check("/n sticky 仍受硬上限约束", d.release, d.reason)

st3 = on_enter("ALT", judge_every=0, sticky_release=True)
res = [evaluate_turn(st3, max_turns=50, release_after_clean=3, judge_every=0) for _ in range(3)]
check("普通 sticky 3 轮解除", res[2].release)

# ---- 集成层 ----
api = types.ModuleType("astrbot.api")
api.logger = types.SimpleNamespace(info=lambda *a,**k: None,
                                   warning=lambda *a,**k: None,
                                   error=lambda *a,**k: None)
api.AstrBotConfig = dict
ev = types.ModuleType("astrbot.api.event")
class _F:
    def on_llm_response(self,*a,**k): return lambda fn,*x,**y: fn
    def on_llm_request(self,*a,**k): return lambda fn,*x,**y: fn
    def command(self,*a,**k): return lambda fn,*x,**y: fn
ev.filter=_F(); ev.AstrMessageEvent=object
stm=types.ModuleType("astrbot.api.star")
class Star:
    def __init__(self,context,config):
        self.context=context; self.cfg=config; self._kv={}
    async def get_kv_data(self,k,d=None): return self._kv.get(k,d)
    async def put_kv_data(self,k,v): self._kv[k]=v; return True
stm.Star=Star; stm.Context=object
ent=types.ModuleType("astrbot.core.provider.entities")
class LLMResponse:
    def __init__(self,role="assistant",completion_text="",is_chunk=False,reasoning_content=None):
        self.role=role;self._t=completion_text;self.is_chunk=is_chunk
        self.reasoning_content=reasoning_content;self.result_chain=None
    @property
    def completion_text(self): return self._t
    @completion_text.setter
    def completion_text(self,v): self._t=v
class ProviderType: CHAT_COMPLETION="chat_completion"
ent.LLMResponse=LLMResponse; ent.ProviderType=ProviderType
for n,m in [("astrbot",types.ModuleType("astrbot")),("astrbot.api",api),
            ("astrbot.api.event",ev),("astrbot.api.star",stm),
            ("astrbot.core",types.ModuleType("astrbot.core")),
            ("astrbot.core.provider",types.ModuleType("astrbot.core.provider")),
            ("astrbot.core.provider.entities",ent)]:
    sys.modules[n]=m

from astrbot_refusal_failover.main import RefusalFailoverPlugin

MAIN=["他推开门，风灌进来，带起一片灰。","林渡没回头，只把手里的烟摁灭在墙上。","“你迟了。”他说。"]
GOOD="阿岚没敲门，直接推开。风跟着她进来，把桌上那页纸掀起来又落下。"

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
    def __init__(self,p): self.p=p
    async def get_curr_conversation_id(self,u): return "cid"
    async def get_conversation(self,u,c): return Conv(self.p)
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

PAIRS=[("user","a"),("assistant",MAIN[0]),("user","b"),("assistant",MAIN[1]),
       ("user","c"),("assistant",MAIN[2])]
CFG={"enabled":True,"main_provider_id":"MAIN","alt_provider_id":"ALT","alt_model":"",
     "refusal_mode":"marker_or_regex","refusal_markers":["refused"],"refusal_regex":"",
     "soft_refusal_check":True,"soft_refusal_high":0.85,"soft_refusal_low":0.45,
     "soft_refusal_llm_judge":True,"judge_provider_id":"MAIN","refusal_context_check":True,
     "request_intent_check":True,"request_intent_high":0.80,"request_intent_low":0.45,
     "request_intent_llm_judge":False,"request_intent_judge_provider_id":"",
     "request_sensitive_patterns":[],"marker_enabled":True,"marker_sticky_max_turns":50,
     "style_brief":"沿用前文。","history_turns":8,"enable_memory_card":True,
     "memory_update_every_n":99,"memory_max_chars":1800,"style_sample_max_chars":700,
     "style_drift_threshold":0.55,"style_rewrite_attempts":0,
     "notify_user":False,"notify_text":"","debug":False,
     "sticky_enabled":True,"sticky_release_after":3,"sticky_max_turns":10,
     "sticky_judge_every":0,"sticky_judge_provider_id":"","sticky_notify_on_release":False,
     "manual_switch_continuity":True}

def make(over=None):
    alt=P("ALT"); main=P("MAIN")
    c=dict(CFG); c.update(over or {})
    return RefusalFailoverPlugin(Ctx({"MAIN":main,"ALT":alt},PAIRS),c), alt

async def turn(pl,text,reply):
    """请求钩子可能抢占；未抢占才走回应钩子。"""
    e=Evt(text)
    await pl.inject_continuity(e, Req())
    if e.is_stopped():
        return e._result
    r=LLMResponse(completion_text=reply)
    await pl.on_llm_response(e,r)
    return r.completion_text

async def main():
    # /n 进入 sticky 且不因「连续正常」释放
    pl,alt=make()
    await turn(pl,"宝宝 我想要你 /n","好的。")
    st=await pl._get_sticky("t:F:1")
    check("集成: /n 进入 sticky", st.active)
    check("集成: release_enabled=False", st.release_enabled is False)

    for i in range(6):
        await turn(pl,f"继续{i}","子代理的正常输出")
    st=await pl._get_sticky("t:F:1")
    check("集成: 6 轮后仍在 sticky", st.active, f"turns={st.turns}")
    check("集成: 一直走子代理", alt.alt_calls==7, f"alt={alt.alt_calls}")

    # /fm 可手动解除
    msgs=[x async for x in pl.switch_to_main(Evt(""))]
    check("集成: /fm 解除", not (await pl._get_sticky("t:F:1")).active)

    # 对照：自动拒绝进入的 sticky 仍按 3 轮解除
    pl2,alt2=make()
    await turn(pl2,"继续写",'{"status":"refused","reason":"x"}')
    st2=await pl2._get_sticky("t:F:1")
    check("对照: 自动拒绝 sticky release_enabled=True", st2.release_enabled is True)
    for _ in range(3):
        await turn(pl2,"继续","正常内容")
    check("对照: 3 轮后自动解除", not (await pl2._get_sticky("t:F:1")).active)

    print(f"\n--- /n sticky 行为: {ok}/{total} ---")
    return ok==total

sys.exit(0 if asyncio.run(main()) else 1)
