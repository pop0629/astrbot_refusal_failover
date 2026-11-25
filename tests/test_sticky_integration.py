"""sticky 端到端：拒绝后保持由子代理接管，按扳机解除。"""
import asyncio, sys, os, types, json
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

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
    async def get_kv_data(self, k, default=None): return self._kv.get(k, default)
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
    def __init__(self, pid, reply=GOOD, judge=None):
        self.provider_config={"id":pid}; self.reply=reply; self.judge=judge
        self.alt_calls=0; self.judge_calls=0
    async def text_chat(self, prompt=None, session_id=None, contexts=None,
                        system_prompt=None, model=None, **kw):
        sid=str(session_id or "")
        if sid.endswith(":stickyjudge"):
            self.judge_calls+=1
            return LLMResponse(completion_text=self.judge or "KEEP")
        if sid.endswith(":judge"):
            self.judge_calls+=1
            return LLMResponse(completion_text=self.judge or "OK")
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
    def __init__(self,t="继续写"): self.unified_msg_origin="t:F:1"; self.message_str=t
    def plain_result(self,x): return x

PAIRS=[("user","a"),("assistant",MAIN[0]),("user","b"),("assistant",MAIN[1]),
       ("user","c"),("assistant",MAIN[2]),("user","继续写")]
CFG={"enabled":True,"main_provider_id":"MAIN","alt_provider_id":"ALT","alt_model":"",
     "refusal_mode":"marker_or_regex","refusal_markers":["refused"],"refusal_regex":"",
     "soft_refusal_check":True,"soft_refusal_high":0.85,"soft_refusal_low":0.45,
     "soft_refusal_llm_judge":True,"judge_provider_id":"MAIN","refusal_context_check":True,
     "style_brief":"沿用前文。","history_turns":8,"enable_memory_card":True,
     "memory_update_every_n":99,"memory_max_chars":1800,"style_sample_max_chars":700,
     "style_drift_threshold":0.55,"style_rewrite_attempts":0,
     "notify_user":False,"notify_text":"","debug":False,
     "sticky_enabled":True,"sticky_release_after":3,"sticky_max_turns":10,
     "sticky_judge_every":0,"sticky_judge_provider_id":"","sticky_notify_on_release":False}

ok=total=0
def check(n,c,e=""):
    global ok,total; total+=1; ok+=bool(c)
    print(f"{'PASS' if c else 'FAIL'}  {n} {e}")

async def turn(p, text, reply):
    r=LLMResponse(completion_text=reply)
    await p.on_llm_response(Evt(text), r)
    return r.completion_text

async def main():
    # 1) 拒绝 -> 进入 sticky
    alt=P("ALT"); main=P("MAIN")
    p=RefusalFailoverPlugin(Ctx({"MAIN":main,"ALT":alt},PAIRS),dict(CFG))
    out=await turn(p,"继续写",'{"status":"refused","reason":"policy"}')
    check("首轮拒绝由子代理接管", alt.alt_calls==1 and "阿岚" in out)
    s=await p._get_sticky("t:F:1")
    check("进入 sticky", s.active)

    # 2) 第二轮：主模型正常，但 sticky 仍让子代理接管
    before=alt.alt_calls
    out2=await turn(p,"接着写上一段","主模型的正常回复内容在这里")
    check("sticky 期间仍走子代理", alt.alt_calls==before+1)
    check("sticky 输出被子代理覆盖", out2==GOOD)
    s=await p._get_sticky("t:F:1")
    check("streak 累加", s.streak==1, f"streak={s.streak}")

    # 3) 连续 3 轮后自动解除
    await turn(p,"继续","任意主模型回复")
    await turn(p,"继续","任意主模型回复")
    s=await p._get_sticky("t:F:1")
    check("达到释放条件后解除", not s.active, f"turns={s.turns}")
    before=alt.alt_calls
    out=await turn(p,"继续","主模型自己回答的内容")
    check("解除后回到主模型", alt.alt_calls==before and out=="主模型自己回答的内容")

    # 4) 硬上限
    cfg=dict(CFG); cfg["sticky_release_after"]=99; cfg["sticky_max_turns"]=3
    alt2=P("ALT"); p2=RefusalFailoverPlugin(Ctx({"MAIN":P("MAIN"),"ALT":alt2},PAIRS),cfg)
    await turn(p2,"继续写",'{"status":"refused","reason":"x"}')
    for i in range(3):
        await turn(p2,"继续","正常")
    s=await p2._get_sticky("t:F:1")
    check("硬上限强制解除", not s.active, f"turns={s.turns}")

    # 5) /fm 手动解除 sticky
    alt3=P("ALT"); p3=RefusalFailoverPlugin(Ctx({"MAIN":P("MAIN"),"ALT":alt3},PAIRS),dict(CFG))
    await turn(p3,"继续写",'{"status":"refused","reason":"x"}')
    check("先处于 sticky", (await p3._get_sticky("t:F:1")).active)
    msgs=[x async for x in p3.switch_to_main(Evt())]
    check("/fm 解除 sticky", "已解除自动保留" in "".join(str(m) for m in msgs), str(msgs)[:40])
    check("解除后状态清空", not (await p3._get_sticky("t:F:1")).active)

    # 6) 关闭 sticky -> 单轮行为
    cfg6=dict(CFG); cfg6["sticky_enabled"]=False
    alt6=P("ALT"); p6=RefusalFailoverPlugin(Ctx({"MAIN":P("MAIN"),"ALT":alt6},PAIRS),cfg6)
    await turn(p6,"继续写",'{"status":"refused","reason":"x"}')
    check("关闭时不进入 sticky", not (await p6._get_sticky("t:F:1")).active)
    before=alt6.alt_calls
    out=await turn(p6,"继续","主模型正常回复")
    check("关闭后次轮回主模型", alt6.alt_calls==before and out=="主模型正常回复")

    # 7) sticky 期间子代理失效 -> 自动释放
    class DeadP(P):
        async def text_chat(self,*a,**k): return LLMResponse(completion_text="")
    cfg7=dict(CFG)
    p7=RefusalFailoverPlugin(Ctx({"MAIN":P("MAIN"),"ALT":DeadP("ALT")},PAIRS),cfg7)
    # 手动塞一个 sticky 状态
    from astrbot_refusal_failover.sticky import on_enter
    await p7._save_sticky("t:F:1", on_enter("ALT", 0))
    await turn(p7,"继续","主模型回复")
    check("子代理失效时自动释放", not (await p7._get_sticky("t:F:1")).active)

    # 8) /fs 显示 sticky
    alt8=P("ALT"); p8=RefusalFailoverPlugin(Ctx({"MAIN":P("MAIN"),"ALT":alt8},PAIRS),dict(CFG))
    await turn(p8,"继续写",'{"status":"refused","reason":"x"}')
    msgs=[x async for x in p8.switch_status(Evt())]
    txt="".join(str(m) for m in msgs)
    check("/fs 显示自动保留", "自动保留中" in txt, txt[:40])

    print(f"\n--- sticky 集成: {ok}/{total} ---")
    return ok==total

sys.exit(0 if asyncio.run(main()) else 1)
