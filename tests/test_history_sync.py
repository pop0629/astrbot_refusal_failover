"""历史同步测试。

模拟 AstrBot 真实时序：
  1. runner 先把主模型原始回复写进会话历史（在钩子之前）
  2. 触发 on_llm_response，插件改写 response
  3. 插件应回头修正历史

不修的话：历史里是主模型的拒绝，展示层是子代理 -> 分裂。
"""
import asyncio, sys, os, types, json, copy
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
class ProviderType: CHAT_COMPLETION = "chat_completion"
ent.LLMResponse=LLMResponse; ent.ProviderType=ProviderType
for n,m in [("astrbot",types.ModuleType("astrbot")),("astrbot.api",api),
            ("astrbot.api.event",ev),("astrbot.api.star",st),
            ("astrbot.core",types.ModuleType("astrbot.core")),
            ("astrbot.core.provider",types.ModuleType("astrbot.core.provider")),
            ("astrbot.core.provider.entities",ent)]:
    sys.modules[n]=m

from astrbot_refusal_failover.main import RefusalFailoverPlugin

MAIN=["他推开门，风灌进来，带起一片灰。","林渡没回头，只把手里的烟摁灭在墙上。"]
GOOD="阿岚没敲门，直接推开。风跟着她进来。"
REFUSAL='{"status":"refused","reason":"policy"}'

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

class Store:
    """模拟 AstrBot 的会话存储"""
    def __init__(self, pairs):
        self.content=[{"role":r,"content":t} for r,t in pairs]
        self.update_calls=0
    def snapshot(self):
        return [dict(m) for m in self.content]

class Conv:
    def __init__(self, store): self._s=store; self.content=store.snapshot()
class CM:
    def __init__(self, store): self._s=store
    async def get_curr_conversation_id(self, umo): return "cid"
    async def get_conversation(self, umo, cid): return Conv(self._s)
    async def update_conversation(self, unified_msg_origin, conversation_id=None,
                                  history=None, **kw):
        self._s.update_calls+=1
        if history is not None:
            self._s.content=[dict(m) for m in history]

class Ctx:
    def __init__(self, provs, store):
        self._p=provs; self.conversation_manager=CM(store)
    def get_provider_by_id(self,i): return self._p.get(i)
    def get_using_provider(self,u=None): return self._p.get("MAIN")

class Evt:
    def __init__(self,t): self.unified_msg_origin="t:F:1"; self.message_str=t; self._x={}
    def plain_result(self,x): return x
    def get_extra(self,k,default=None): return self._x.get(k, default)
    def set_extra(self,k,v): self._x[k]=v

PAIRS=[("user","a"),("assistant",MAIN[0]),("user","b"),("assistant",MAIN[1])]
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
     "sticky_enabled":False,"sticky_release_after":3,"sticky_max_turns":10,
     "sticky_judge_every":0,"sticky_judge_provider_id":"","sticky_notify_on_release":False,
     "manual_switch_continuity":True,"sync_history":True}

ok=total=0
def check(n,c,e=""):
    global ok,total; total+=1; ok+=bool(c)
    print(f"{'PASS' if c else 'FAIL'}  {n} {e}")

def make(over=None):
    store=Store(PAIRS)
    alt=P("ALT"); main=P("MAIN")
    c=dict(CFG); c.update(over or {})
    pl=RefusalFailoverPlugin(Ctx({"MAIN":main,"ALT":alt},store),c)
    return pl,store,alt

async def simulate_turn(pl, store, event, original_reply):
    """复刻 AstrBot 时序：先写历史，再触发钩子。"""
    # 1. runner 写历史（值拷贝）
    store.content.append({"role":"user","content":event.message_str})
    store.content.append({"role":"assistant","content":original_reply})
    # 2. 触发钩子
    resp=LLMResponse(completion_text=original_reply)
    await pl.on_llm_response(event, resp)
    return resp.completion_text

async def main():
    # 1) 核心：转交后历史必须被修正
    pl,store,alt=make()
    out=await simulate_turn(pl, store, Evt("继续写"), REFUSAL)
    check("展示层是子代理内容", out==GOOD, out[:20])
    last=store.content[-1]
    check("历史已修正为子代理内容", last["content"]==GOOD,
          f"历史={last['content'][:40]!r}")
    check("历史不再是拒绝标记", "refused" not in last["content"])
    check("历史条数未增加", len(store.content)==len(PAIRS)+2, f"{len(store.content)}")

    # 2) 未转交时不改动历史
    pl,store,alt=make()
    out=await simulate_turn(pl, store, Evt("你好"), "你好呀")
    check("普通回复不动历史", store.content[-1]["content"]=="你好呀")

    # 3) 关闭 sync_history -> 复现原 bug
    pl,store,alt=make({"sync_history":False})
    out=await simulate_turn(pl, store, Evt("继续写"), REFUSAL)
    check("关闭后展示层仍是子代理", out==GOOD)
    check("关闭后历史保留主模型原文(复现bug)", store.content[-1]["content"]==REFUSAL,
          repr(store.content[-1]["content"][:30]))

    # 4) sticky 路径也同步
    pl,store,alt=make({"sticky_enabled":True})
    await simulate_turn(pl, store, Evt("继续写"), REFUSAL)
    check("sticky 首轮历史已同步", store.content[-1]["content"]==GOOD)
    out2=await simulate_turn(pl, store, Evt("继续"), "主模型的下一轮正常回复")
    check("sticky 次轮历史已同步", store.content[-1]["content"]==GOOD,
          repr(store.content[-1]["content"][:30]))

    # 5) 工作上下文优先：如果 run_context 可达，直接改内存
    class Part:
        def __init__(self,t): self.text=t
    class Msg:
        def __init__(self,role,parts): self.role=role; self.content=parts
    class RunCtx:
        def __init__(self): self.messages=[Msg("assistant",[Part(REFUSAL)])]
    pl,store,alt=make()
    e=Evt("继续写"); rc=RunCtx(); e.set_extra("run_context", rc)
    store.content.append({"role":"user","content":"继续写"})
    store.content.append({"role":"assistant","content":REFUSAL})
    resp=LLMResponse(completion_text=REFUSAL)
    await pl.on_llm_response(e, resp)
    check("工作上下文被就地修正", rc.messages[-1].content[0].text==GOOD,
          rc.messages[-1].content[0].text[:20])

    print(f"\n--- 历史同步: {ok}/{total} ---")
    return ok==total

sys.exit(0 if asyncio.run(main()) else 1)
