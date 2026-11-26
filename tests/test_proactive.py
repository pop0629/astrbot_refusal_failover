"""主动消息测试。"""
import asyncio, sys, os, types, random, time
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG)); sys.path.insert(0, PKG)

# ---- 纯逻辑 ----
from proactive import (
    is_intimate, ProactiveState, evaluate, build_proactive_prompt,
)

ok=total=0
def check(n,c,e=""):
    global ok,total; total+=1; ok+=bool(c)
    print(f"{'PASS' if c else 'FAIL'}  {n} {e}")

# 亲昵识别
for t in ["宝宝 抱抱我","想你了","亲一下","贴贴","老公 过来","hug me"]:
    check(f"亲昵识别 [{t[:8]}]", is_intimate(t))
for t in ["今天天气不错","帮我改代码","/n","/fs","这个函数不能返回空值"]:
    check(f"非亲昵不误判 [{t[:8]}]", not is_intimate(t))

# 冷却
now=1000000.0
st=ProactiveState(last_fire=now-600, last_intimacy=now-60)
d=evaluate(st,now=now,interval_minutes=10,intimacy_window_minutes=15,
           cooldown_minutes=30,probability=1.0)
check("冷却中不发送", not d.fire, d.reason)
st=ProactiveState(last_fire=now-3600, last_intimacy=now-60)
d=evaluate(st,now=now,interval_minutes=10,intimacy_window_minutes=15,
           cooldown_minutes=30,probability=1.0)
check("冷却过后发送", d.fire, d.reason)

# 亲昵过期
st=ProactiveState(last_fire=0, last_intimacy=now-3600)
d=evaluate(st,now=now,interval_minutes=10,intimacy_window_minutes=15,
           cooldown_minutes=30,probability=1.0)
check("亲昵过期不发送", not d.fire, d.reason)

# 无亲昵
st=ProactiveState(last_fire=0, last_intimacy=0)
d=evaluate(st,now=now,interval_minutes=10,intimacy_window_minutes=15,
           cooldown_minutes=30,probability=1.0)
check("无亲昵不发送", not d.fire, d.reason)

# 概率
st=ProactiveState(last_fire=0, last_intimacy=now-60)
rng=random.Random(7)
n=sum(evaluate(st,now=now,interval_minutes=10,intimacy_window_minutes=15,
               cooldown_minutes=0,probability=0.3,rng=rng).fire for _ in range(2000))
check("p=0.3 实际频率接近 30%", 520 < n < 680, f"{n}/2000")

# prompt
p=build_proactive_prompt("猫娘","短句","林渡没回头。","目标: 写第三章","对方: 抱抱\n你: 嗯")
check("prompt 含人设", "猫娘" in p)
check("prompt 含风格样本", "林渡" in p)
check("prompt 含记忆卡", "写第三章" in p)
check("prompt 要求不打招呼", "不要打招呼" in p)
check("prompt 要求不提元叙述", "作为 AI" in p)

# ---- 集成 ----
api=types.ModuleType("astrbot.api")
api.logger=types.SimpleNamespace(info=lambda *a,**k:None,warning=lambda *a,**k:None,error=lambda *a,**k:None)
api.AstrBotConfig=dict
ev=types.ModuleType("astrbot.api.event")
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
core=types.ModuleType("astrbot.core"); core.__path__=[]
pl_mod=types.ModuleType("astrbot.core.pipeline"); pl_mod.__path__=[]
prov=types.ModuleType("astrbot.core.provider"); prov.__path__=[]
star_mod=types.ModuleType("astrbot.core.star"); star_mod.__path__=[]
cu=types.ModuleType("astrbot.core.pipeline.context_utils")
sh=types.ModuleType("astrbot.core.star.star_handler")
class _ET: OnLLMResponseEvent="r"; OnLLMRequestEvent="q"
sh.EventType=_ET
async def _noop(*a,**k): return False
cu.call_event_hook=_noop
pl_mod.context_utils=cu
mer=types.ModuleType("astrbot.core.message.message_event_result")
class MessageChain:
    def __init__(self,chain=None): self.chain=chain or []
    def message(self,t):
        self.chain=[t]; return self
mer.MessageChain=MessageChain
msg=types.ModuleType("astrbot.core.message"); msg.__path__=[]
for n,m in [("astrbot",types.ModuleType("astrbot")),("astrbot.api",api),
            ("astrbot.api.event",ev),("astrbot.api.star",stm),
            ("astrbot.core",core),("astrbot.core.pipeline",pl_mod),
            ("astrbot.core.pipeline.context_utils",cu),
            ("astrbot.core.star",star_mod),("astrbot.core.star.handler",sh),
            ("astrbot.core.star.star_handler",sh),
            ("astrbot.core.provider",prov),("astrbot.core.provider.entities",ent),
            ("astrbot.core.message",msg),
            ("astrbot.core.message.message_event_result",mer)]:
    m.__dict__.setdefault("__path__",[])
    sys.modules[n]=m

from astrbot_refusal_failover.main import RefusalFailoverPlugin

MAIN=["他推开门。","林渡没回头。"]
class P:
    def __init__(self,pid,reply="晚安，抱一下"):
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
    async def update_conversation(self,**kw): pass
class Ctx:
    def __init__(self,provs,pairs):
        self._p=provs; self.conversation_manager=CM(pairs)
        self.sent=[]
    def get_provider_by_id(self,i): return self._p.get(i)
    def get_using_provider(self,u=None): return self._p.get("MAIN")
    async def send_message(self,umo,chain):
        self.sent.append((umo,chain)); return True
class Evt:
    def __init__(self,t): self.unified_msg_origin="t:F:1"; self.message_str=t
    def plain_result(self,x): return x
    def get_extra(self,k,d=None): return d
class Req:
    def __init__(self): self.system_prompt=""; self.contexts=[]; self.prompt=""

PAIRS=[("user","抱抱"),("assistant",MAIN[0]),("user","宝宝想你"),("assistant",MAIN[1])]
CFG={"enabled":True,"auto_detect_enabled":True,
     "main_provider_id":"MAIN","alt_provider_id":"ALT","alt_model":"",
     "refusal_mode":"marker_or_regex","refusal_markers":["refused"],"refusal_regex":"",
     "soft_refusal_check":True,"soft_refusal_high":0.85,"soft_refusal_low":0.45,
     "soft_refusal_llm_judge":True,"judge_provider_id":"MAIN","refusal_context_check":True,
     "request_intent_check":True,"request_intent_high":0.80,"request_intent_low":0.45,
     "request_intent_llm_judge":False,"request_intent_judge_provider_id":"",
     "request_sensitive_patterns":[],"marker_enabled":True,"marker_sticky_max_turns":100,
     "preempt_on_intent":True,
     "style_brief":"短句。","history_turns":8,"enable_memory_card":True,
     "memory_update_every_n":99,"memory_max_chars":1800,"style_sample_max_chars":700,
     "style_drift_threshold":0.55,"style_rewrite_attempts":0,
     "notify_user":False,"notify_text":"","debug":False,
     "sticky_enabled":True,"sticky_release_after":3,"sticky_max_turns":10,
     "sticky_judge_every":0,"sticky_judge_provider_id":"","sticky_notify_on_release":False,
     "manual_switch_continuity":True,"sync_history":True,
     "proactive_enabled":True,"proactive_interval_minutes":30,
     "proactive_intimacy_window_minutes":20,"proactive_cooldown_minutes":90,
     "proactive_probability":1.0,"proactive_persona_hint":"猫娘","proactive_switch_to_nsfw":True}

def mk(over=None):
    alt=P("ALT"); main=P("MAIN"); c=dict(CFG); c.update(over or {})
    ctx=Ctx({"MAIN":main,"ALT":alt},PAIRS)
    return RefusalFailoverPlugin(ctx,c), alt, ctx

async def main2():
    # 亲昵信号记录
    pl,alt,ctx=mk()
    await pl._remember_intimacy(Evt("宝宝 抱抱"))
    st=await pl._get_proactive_state("t:F:1")
    check("亲昵信号被记录", st.last_intimacy>0)

    # 非亲昵不记录
    pl,alt,ctx=mk()
    await pl._remember_intimacy(Evt("帮我改代码"))
    st=await pl._get_proactive_state("t:F:1")
    check("非亲昵不记录", st.last_intimacy==0)

    # 双条件满足 -> 发送
    pl,alt,ctx=mk()
    await pl._remember_intimacy(Evt("宝宝 抱抱"))
    await pl._maybe_fire_proactive("t:F:1")
    check("双条件满足时发送", len(ctx.sent)==1, f"sent={len(ctx.sent)}")
    check("发送内容来自子代理", alt.alt_calls==1)
    st=await pl._get_proactive_state("t:F:1")
    check("发送后记录冷却时间", st.last_fire>0)

    # 触发后进入 NSFW 接管
    s2=await pl._get_sticky("t:F:1")
    check("触发后进入接管模式", s2.active)
    check("接管不自动释放", s2.release_enabled is False)

    # 冷却期内不再发
    await pl._maybe_fire_proactive("t:F:1")
    check("冷却期内不重发", len(ctx.sent)==1, f"sent={len(ctx.sent)}")

    # 无亲昵不发
    pl,alt,ctx=mk()
    await pl._maybe_fire_proactive("t:F:1")
    check("无亲昵不发送", len(ctx.sent)==0)

    # 关闭开关不发
    pl,alt,ctx=mk({"proactive_enabled":False})
    await pl._remember_intimacy(Evt("宝宝 抱抱"))
    await pl._maybe_fire_proactive("t:F:1")
    check("关闭开关后不发送", len(ctx.sent)==0)

    # 关闭 NSFW 切换 -> 只发消息不进接管
    pl,alt,ctx=mk({"proactive_switch_to_nsfw":False})
    await pl._remember_intimacy(Evt("宝宝 抱抱"))
    await pl._maybe_fire_proactive("t:F:1")
    check("关闭切换后仍发送", len(ctx.sent)==1)
    s3=await pl._get_sticky("t:F:1")
    check("关闭切换后不进入接管", not s3.active)

    print(f"\n--- 主动消息: {ok}/{total} ---")
    return ok==total

passed = asyncio.run(main2())
sys.exit(0 if passed else 1)
