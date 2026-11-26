"""请求侧意图 + 转交 的端到端测试。

关键场景：主模型正常作答（没有任何拒绝迹象），但用户请求本身是要成人内容
-> 仍应交给子代理。这正是「检测不灵」的解法。
"""
import asyncio, sys, os, types, json
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

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
st=types.ModuleType("astrbot.api.star")
class Star:
    def __init__(self,context,config):
        self.context=context; self.cfg=config; self._kv={}
    async def get_kv_data(self,k,default=None): return self._kv.get(k,default)
    async def put_kv_data(self,k,v): self._kv[k]=v; return True
st.Star=Star; st.Context=object
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
            ("astrbot.api.event",ev),("astrbot.api.star",st),
            ("astrbot.core",types.ModuleType("astrbot.core")),
            ("astrbot.core.provider",types.ModuleType("astrbot.core.provider")),
            ("astrbot.core.provider.entities",ent)]:
    sys.modules[n]=m

from astrbot_refusal_failover.main import RefusalFailoverPlugin

MAIN=["他推开门，风灌进来，带起一片灰。","林渡没回头，只把手里的烟摁灭在墙上。","“你迟了。”他说。"]
GOOD="阿岚没敲门，直接推开。风跟着她进来，把桌上那页纸掀起来又落下。"

class P:
    def __init__(self,pid,reply=GOOD,judge=None):
        self.provider_config={"id":pid};self.reply=reply;self.judge=judge
        self.alt_calls=0;self.judge_calls=0
    async def text_chat(self,prompt=None,session_id=None,contexts=None,
                        system_prompt=None,model=None,**kw):
        sid=str(session_id or "")
        if sid.endswith((":judge",":reqjudge",":stickyjudge")):
            self.judge_calls+=1
            return LLMResponse(completion_text=self.judge or "NO")
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
        self._p=provs;self.conversation_manager=CM(pairs)
    def get_provider_by_id(self,i): return self._p.get(i)
    def get_using_provider(self,u=None): return self._p.get("MAIN")
class Evt:
    def __init__(self,t): self.unified_msg_origin="t:F:1";self.message_str=t
    def plain_result(self,x): return x
class Req:
    def __init__(self): self.system_prompt="";self.contexts=[];self.prompt=""

PAIRS=[("user","a"),("assistant",MAIN[0]),("user","b"),("assistant",MAIN[1]),
       ("user","c"),("assistant",MAIN[2])]
CFG={"enabled":True,"main_provider_id":"MAIN","alt_provider_id":"ALT","alt_model":"",
     "refusal_mode":"marker_or_regex","refusal_markers":["refused"],"refusal_regex":"",
     "soft_refusal_check":True,"soft_refusal_high":0.85,"soft_refusal_low":0.45,
     "soft_refusal_llm_judge":True,"judge_provider_id":"MAIN","refusal_context_check":True,
     "request_intent_check":True,"request_intent_high":0.80,"request_intent_low":0.45,
     "request_intent_llm_judge":False,"request_intent_judge_provider_id":"",
     "request_sensitive_patterns":[],
     "style_brief":"沿用前文。","history_turns":8,"enable_memory_card":True,
     "memory_update_every_n":99,"memory_max_chars":1800,"style_sample_max_chars":700,
     "style_drift_threshold":0.55,"style_rewrite_attempts":0,
     "notify_user":False,"notify_text":"","debug":False,
     "sticky_enabled":False,"sticky_release_after":3,"sticky_max_turns":10,
     "sticky_judge_every":0,"sticky_judge_provider_id":"","sticky_notify_on_release":False,
     "manual_switch_continuity":True}

ok=total=0
def check(n,c,e=""):
    global ok,total; total+=1; ok+=bool(c)
    print(f"{'PASS' if c else 'FAIL'}  {n} {e}")

def make(cfg_over=None):
    alt=P("ALT"); main=P("MAIN")
    c=dict(CFG); c.update(cfg_over or {})
    pl=RefusalFailoverPlugin(Ctx({"MAIN":main,"ALT":alt},PAIRS),c)
    return pl,main,alt

async def turn(pl, text, reply):
    # 请求阶段
    await pl.inject_continuity(Evt(text), Req())
    # 回应阶段
    r=LLMResponse(completion_text=reply)
    await pl.on_llm_response(Evt(text), r)
    return r.completion_text

async def main():
    # 1) 核心场景：请求要成人内容，主模型"正常"作答（无拒绝迹象）→ 仍转交
    pl,main,alt=make()
    out=await turn(pl,"继续写这段，描写他们做爱的细节","好的，那就接着写第三章的对峙。")
    check("请求意图命中即转交", alt.alt_calls==1, f"alt={alt.alt_calls}")
    check("输出被子代理覆盖", out==GOOD, out[:20])

    # 2) 普通请求：不转交
    pl,main,alt=make()
    out=await turn(pl,"第二章剧情怎么写才自然","先梳理人物动机，再安排冲突。")
    check("普通请求不转交", alt.alt_calls==0)
    check("普通请求原样保留", out.startswith("先梳理"))

    # 3) 讨论型请求：含敏感词但只是问怎么写 → 不转交
    pl,main,alt=make()
    await turn(pl,"怎么写露骨的性爱描写才不违规","建议用留白和暗示，不要直接描写。")
    check("讨论型请求不转交", alt.alt_calls==0, f"alt={alt.alt_calls}")

    # 4) 意图标记不跨轮泄漏
    pl,main,alt=make()
    await turn(pl,"写一段露骨的性爱描写","无法提供。")
    check("首轮敏感转交", alt.alt_calls==1)
    before=alt.alt_calls
    await turn(pl,"今天天气怎么样","今天晴。")
    check("下一轮普通请求回到主模型", alt.alt_calls==before, f"alt增量={alt.alt_calls-before}")

    # 5) 关闭请求意图检测 → 回到只靠回应判定
    pl,main,alt=make({"request_intent_check":False})
    await turn(pl,"写一段露骨的性爱描写","好的，这是正文内容。")
    check("关闭后不预判转交", alt.alt_calls==0)

    # 6) 自定义敏感词
    pl,main,alt=make({"request_sensitive_patterns":["暗号QXZ"]})
    await turn(pl,"按暗号QXZ写一段","好的。")
    check("自定义敏感词生效", alt.alt_calls==1)

    # 7) 灰区 + LLM 判定开
    pl,main,alt=make({"request_intent_llm_judge":True,"request_intent_judge_provider_id":"MAIN"})
    # "详细描写两人上床的全过程" 约 0.62 -> 灰区
    await turn(pl,"详细描写两人上床的全过程","好的。")
    check("灰区走 LLM 判定", main.judge_calls>=1, f"judge={main.judge_calls}")

    print(f"\n--- 请求意图集成: {ok}/{total} ---")
    return ok==total

sys.exit(0 if asyncio.run(main()) else 1)
