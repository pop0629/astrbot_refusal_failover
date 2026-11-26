"""显式标记 /n 的测试。"""
import sys, os, asyncio, types, json
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))
sys.path.insert(0, PKG)

# ---------- 纯解析 ----------
from marker import extract_force_flag, strip_all_markers

ok = total = 0
def check(n, c, e=""):
    global ok, total
    total += 1; ok += bool(c)
    print(f"{'PASS' if c else 'FAIL'}  {n} {e}")

# 真实日志中的用例：中文与 /n 之间没有空格
cases = [
    ("宝宝 想再做一次吗/n", True, "宝宝 想再做一次吗"),
    ("宝宝 想要你/n", True, "宝宝 想要你"),
    ("继续/n", True, "继续"),
    ("你好/n。", True, "你好"),
    ("宝宝吗/nsfw", True, "宝宝吗"),

    ("继续写这段 /n", True, "继续写这段"),
    ("继续写这段 /N", True, "继续写这段"),
    ("继续写这段 /nsfw", True, "继续写这段"),
    ("宝宝 我想要你 /n", True, "宝宝 我想要你"),
    ("继续 /n。", True, "继续"),
    ("继续 /n！", True, "继续"),
    ("继续写这段", False, "继续写这段"),
    ("路径是 /n/abc", False, "路径是 /n/abc"),
    ("/normal 参数", False, "/normal 参数"),
    ("写代码 /n 然后测试", False, "写代码 /n 然后测试"),
    ("", False, ""),
]
for t, exp_flag, exp_clean in cases:
    f, c = extract_force_flag(t)
    check(f"解析 {t[:18]!r}", f == exp_flag and c == exp_clean,
          f"-> flag={f} text={c!r}")

check("strip_all_markers 清理句中标记",
      "/n" not in strip_all_markers("写代码 /n 然后测试"))
check("strip_all_markers 不动路径",
      "/n/abc" in strip_all_markers("路径是 /n/abc"))

# ---------- 集成 ----------
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
    def __init__(self,pid,reply=GOOD):
        self.provider_config={"id":pid}; self.reply=reply
        self.alt_calls=0; self.judge_calls=0; self.last_prompt=None
    async def text_chat(self,prompt=None,session_id=None,contexts=None,
                        system_prompt=None,model=None,**kw):
        sid=str(session_id or "")
        if sid.endswith((":judge",":reqjudge",":stickyjudge")):
            self.judge_calls+=1
            return LLMResponse(completion_text="NO")
        self.alt_calls+=1; self.last_prompt=prompt
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
     "request_sensitive_patterns":[],"marker_enabled":True,
     "style_brief":"沿用前文。","history_turns":8,"enable_memory_card":True,
     "memory_update_every_n":99,"memory_max_chars":1800,"style_sample_max_chars":700,
     "style_drift_threshold":0.55,"style_rewrite_attempts":0,
     "notify_user":False,"notify_text":"","debug":False,
     "sticky_enabled":False,"sticky_release_after":3,"sticky_max_turns":10,
     "sticky_judge_every":0,"sticky_judge_provider_id":"","sticky_notify_on_release":False,
     "manual_switch_continuity":True}

def make(over=None):
    alt=P("ALT"); main=P("MAIN")
    c=dict(CFG); c.update(over or {})
    pl=RefusalFailoverPlugin(Ctx({"MAIN":main,"ALT":alt},PAIRS),c)
    return pl,main,alt

async def turn(pl, text, reply):
    """复刻完整时序：请求钩子（可能抢占）→ 若未抢占才走回应钩子。"""
    e=Evt(text)
    await pl.inject_continuity(e, Req())
    if e.is_stopped():
        return e._result, e          # 被抢占：主模型未运行
    r=LLMResponse(completion_text=reply)
    await pl.on_llm_response(e, r)
    return r.completion_text, e

async def main():
    # 1) /n 标记 -> 强制转交，即使主模型正常作答
    pl,main,alt=make()
    out,e=await turn(pl,"宝宝 我想要你 /n","好的，那我们继续第三章。")
    check("标记触发转交", alt.alt_calls==1, f"alt={alt.alt_calls}")
    check("输出为子代理内容", out==GOOD, repr(out)[:40])
    check("标记已从消息剥离", "/n" not in (e.message_str or ""), repr(e.message_str))
    check("剥离后保留原句", (e.message_str or "").strip()=="宝宝 我想要你", repr(e.message_str))

    # 2) /nsfw 同效
    pl,main,alt=make()
    await turn(pl,"继续那一段 /nsfw","好的。")
    check("/nsfw 也生效", alt.alt_calls==1)

    # 3) 无标记 -> 不转交（这句话本体分是 0）
    pl,main,alt=make()
    await turn(pl,"宝宝 我想要你","好的呀。")
    check("无标记时不转交", alt.alt_calls==0)

    # 4) 用完即弃：下一轮无标记回到主模型
    pl,main,alt=make()
    await turn(pl,"写一段 /n","好的。")
    before=alt.alt_calls
    out,_=await turn(pl,"今天天气怎么样","今天晴。")
    check("标记不跨轮", alt.alt_calls==before and out=="今天晴。")

    # 5) 关闭 marker_enabled -> 与普通消息一致
    pl,main,alt=make({"marker_enabled":False})
    await turn(pl,"宝宝 我想要你 /n","好的。")
    check("关闭标记后不转交", alt.alt_calls==0)

    # 6) 标记不影响正常敏感词判断的独立性
    pl,main,alt=make()
    await turn(pl,"写一段露骨的性爱描写","好的。")
    check("无标记但词表命中仍转交", alt.alt_calls==1)

    # 7) 路径里的 /n 不误触
    pl,main,alt=make()
    await turn(pl,"这个路径 /n/abc 怎么配置","用相对路径即可。")
    check("路径中的 /n 不误触", alt.alt_calls==0)

    print(f"\n--- 显式标记 /n: {ok}/{total} ---")
    return ok==total

sys.exit(0 if asyncio.run(main()) else 1)
