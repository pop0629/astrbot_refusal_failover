"""手动切换连续性注入 测试。

验证 /fa 之后每次请求都会被注入：
  - 主模型真实文风样本
  - 风格要求
  - 记忆卡
  - 连续性要求
且 /fm 之后不再注入。
"""
import asyncio, sys, os, types
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
class ProviderType: CHAT_COMPLETION = "chat_completion"
ent.LLMResponse=LLMResponse; ent.ProviderType=ProviderType
for n,m in [("astrbot",types.ModuleType("astrbot")),("astrbot.api",api),
            ("astrbot.api.event",ev),("astrbot.api.star",st),
            ("astrbot.core",types.ModuleType("astrbot.core")),
            ("astrbot.core.provider",types.ModuleType("astrbot.core.provider")),
            ("astrbot.core.provider.entities",ent)]:
    sys.modules[n]=m

from astrbot_refusal_failover.main import RefusalFailoverPlugin

MAIN_REPLIES = ["他推开门，风灌进来，带起一片灰。",
                "林渡没回头，只把手里的烟摁灭在墙上。",
                "“你迟了。”他说。声音没什么起伏。"]

class Prov:
    def __init__(self, pid): self.provider_config={"id":pid}
class PM:
    def __init__(self, provs, cur): self.provs=set(provs); self.current=cur
    async def set_provider(self, pid, ptype, umo=None):
        if pid not in self.provs: raise ValueError("no such provider")
        self.current=pid
class Conv:
    def __init__(self, pairs): self.content=[{"role":r,"content":t} for r,t in pairs]
class CM:
    def __init__(self, pairs): self.pairs=pairs
    async def get_curr_conversation_id(self, umo): return "cid"
    async def get_conversation(self, umo, cid): return Conv(self.pairs)
class Ctx:
    def __init__(self, provs, cur, pairs):
        self._p={k:Prov(k) for k in provs}
        self.provider_manager=PM(provs,cur)
        self.conversation_manager=CM(pairs)
    def get_provider_by_id(self,pid): return self._p.get(pid)
    def get_using_provider(self,umo=None): return self._p.get(self.provider_manager.current)
class Evt:
    def __init__(self): self.unified_msg_origin="t:F:1"; self.message_str="继续写"
    def plain_result(self,t): return t
class Req:
    def __init__(self):
        self.system_prompt="原有系统提示"
        self.contexts=[{"role":"user","content":"继续写"},
                       {"role":"assistant","content":MAIN_REPLIES[2]}]
        self.prompt="继续写"

PAIRS=[("user","a"),("assistant",MAIN_REPLIES[0]),
       ("user","b"),("assistant",MAIN_REPLIES[1]),
       ("user","c"),("assistant",MAIN_REPLIES[2]),("user","继续写")]
CFG={"alt_provider_id":"ALT","main_provider_id":"MAIN",
     "manual_switch_continuity":True,"style_brief":"沿用前文，短句。",
     "enable_memory_card":True,"memory_max_chars":1800,
     "style_sample_max_chars":700,"debug":False}

ok=total=0
def check(name,cond,extra=""):
    global ok,total
    total+=1; ok+=bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")

async def run_cmd(cmd,evt):
    return [r async for r in cmd(evt)]

async def main():
    ctx=Ctx(["MAIN","ALT"],"MAIN",PAIRS)
    p=RefusalFailoverPlugin(ctx,dict(CFG))
    e=Evt()

    # 未切换时不应注入
    req0=Req()
    await p.inject_continuity(e,req0)
    check("主模型状态下不注入", req0.system_prompt=="原有系统提示", req0.system_prompt[:20])

    # 切到子代理
    await run_cmd(p.switch_to_alt,e)

    # 存一张记忆卡
    from astrbot_refusal_failover.failover import MemoryCard
    await p._save_card(e.unified_msg_origin, MemoryCard.parse(
        "目标: 写第三章\n关键事实:\n- 主角: 林渡\n当前状态: 对峙"))

    req=Req()
    await p.inject_continuity(e,req)
    sp=req.system_prompt
    check("注入后保留原有提示", sp.startswith("原有系统提示"))
    check("注入了文风样本", "林渡" in sp and "烟摁灭" in sp)
    check("注入了风格要求", "沿用前文，短句" in sp)
    check("注入了记忆卡", "主角: 林渡" in sp)
    check("注入了连续性要求", "连续性要求" in sp)
    check("强调不要提及切换", "不要提及模型切换" in sp)
    check("标记了接缝", req.contexts[-1].get("_seam_here") is True)

    # 重复调用不重复注入
    req2=Req()
    await p.inject_continuity(e,req2)
    n1=req2.system_prompt.count("连续性要求")
    await p.inject_continuity(e,req2)
    n2=req2.system_prompt.count("连续性要求")
    check("同一请求不重复注入", n1==n2==1, f"{n1}->{n2}")

    # 切回后不再注入
    await run_cmd(p.switch_to_main,e)
    req3=Req()
    await p.inject_continuity(e,req3)
    check("切回后停止注入", req3.system_prompt=="原有系统提示", req3.system_prompt[:20])

    # 关闭配置后不注入
    ctx2=Ctx(["MAIN","ALT"],"MAIN",PAIRS)
    cfg2=dict(CFG); cfg2["manual_switch_continuity"]=False
    p2=RefusalFailoverPlugin(ctx2,cfg2)
    e2=Evt()
    await run_cmd(p2.switch_to_alt,e2)
    req4=Req()
    await p2.inject_continuity(e2,req4)
    check("关闭开关后不注入", req4.system_prompt=="原有系统提示")

    print(f"\n--- 连续性注入: {ok}/{total} ---")
    return ok==total

sys.exit(0 if asyncio.run(main()) else 1)
