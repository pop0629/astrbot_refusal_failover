"""手动切换主模型/子代理 的单元测试。

用伪 Context / provider_manager 验证：
  切换成功、重复切换提示、未配置提示、切回、无记录时切回、状态查询
"""
import asyncio, sys, os, types, json
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(PKG))

# ---- stub astrbot ----
api = types.ModuleType("astrbot.api")
api.logger = types.SimpleNamespace(info=lambda *a, **k: None,
                                   warning=lambda *a, **k: None,
                                   error=lambda *a, **k: None)
api.AstrBotConfig = dict
ev = types.ModuleType("astrbot.api.event")
class _F:
    def on_llm_response(self, *a, **k): return lambda fn, *x, **y: fn
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
    def completion_text(self, v): self._t = v
class ProviderType:
    CHAT_COMPLETION = "chat_completion"
ent.LLMResponse = LLMResponse
ent.ProviderType = ProviderType
for n, m in [("astrbot", types.ModuleType("astrbot")), ("astrbot.api", api),
             ("astrbot.api.event", ev), ("astrbot.api.star", st),
             ("astrbot.core", types.ModuleType("astrbot.core")),
             ("astrbot.core.provider", types.ModuleType("astrbot.core.provider")),
             ("astrbot.core.provider.entities", ent)]:
    sys.modules[n] = m

from astrbot_refusal_failover.main import RefusalFailoverPlugin

class Prov:
    def __init__(self, pid): self.provider_config = {"id": pid}
class PM:
    def __init__(self, provs, current): 
        self.provs = provs; self.current = current; self.calls = []
    async def set_provider(self, pid, ptype, umo=None):
        if pid not in self.provs:
            raise ValueError(f"Provider {pid} does not exist")
        self.calls.append((pid, umo)); self.current = pid
class Ctx:
    def __init__(self, provs, current):
        self._p = {k: Prov(k) for k in provs}
        self.provider_manager = PM(set(provs), current)
        self._cur = current
    def get_provider_by_id(self, pid): return self._p.get(pid)
    def get_using_provider(self, umo=None): return self._p.get(self.provider_manager.current)
class Ev:
    def __init__(self): self.unified_msg_origin = "t:F:1"; self.message_str = ""
    def plain_result(self, t): return t

class R:
    """收集 plain_result 文本"""
    def __init__(self, text): self.text = text
    def plain_result(self, t): return t

async def run_cmd(cmd, pl, evt):
    out = []
    async for r in cmd(evt):
        out.append(r)
    return "\n".join(str(x) for x in out)

CFG = {"alt_provider_id": "ALT", "main_provider_id": "MAIN"}

ok = total = 0
def check(name, cond, extra=""):
    global ok, total
    total += 1; ok += bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")

async def main():
    # 1) 切到子代理
    ctx = Ctx(["MAIN", "ALT"], "MAIN")
    p = RefusalFailoverPlugin(ctx, dict(CFG))
    evt = Ev()
    msg = await run_cmd(p.switch_to_alt, p, evt)
    check("切到子代理成功", ctx.provider_manager.current == "ALT", msg[:30])
    check("提示包含切回指令", "/切换主模型" in msg)
    st = await p._get_switch_state(evt.unified_msg_origin)
    check("记录了原 provider", st.get("prev") == "MAIN", str(st))

    # 2) 重复切到子代理
    msg2 = await run_cmd(p.switch_to_alt, p, evt)
    check("重复切换有提示", "已经在子代理" in msg2, msg2[:30])

    # 3) 切回主模型
    msg3 = await run_cmd(p.switch_to_main, p, evt)
    check("切回主模型成功", ctx.provider_manager.current == "MAIN", msg3[:30])
    st2 = await p._get_switch_state(evt.unified_msg_origin)
    check("状态标记为 main", st2.get("active") == "main", str(st2))

    # 4) 没切换时切回
    msg4 = await run_cmd(p.switch_to_main, p, evt)
    check("未切换时切回有提示", "没有在子代理" in msg4, msg4[:30])

    # 5) 未配置 alt
    ctx5 = Ctx(["MAIN"], "MAIN")
    p5 = RefusalFailoverPlugin(ctx5, {"alt_provider_id": "", "main_provider_id": "MAIN"})
    msg5 = await run_cmd(p5.switch_to_alt, p5, Ev())
    check("未配置备用有提示", "还没配置" in msg5, msg5[:30])
    check("未配置时不改动 provider", ctx5.provider_manager.current == "MAIN")

    # 6) alt 不存在
    ctx6 = Ctx(["MAIN"], "MAIN")
    p6 = RefusalFailoverPlugin(ctx6, {"alt_provider_id": "GHOST", "main_provider_id": "MAIN"})
    msg6 = await run_cmd(p6.switch_to_alt, p6, Ev())
    check("备用不存在有提示", "找不到提供商" in msg6, msg6[:30])

    # 7) 状态查询
    ctx7 = Ctx(["MAIN", "ALT"], "MAIN")
    p7 = RefusalFailoverPlugin(ctx7, dict(CFG))
    evt7 = Ev()
    await run_cmd(p7.switch_to_alt, p7, evt7)
    msg7 = await run_cmd(p7.switch_status, p7, evt7)
    check("状态显示子代理", "子代理" in msg7 and "ALT" in msg7, msg7[:40])

    # 8) 主模型已不可用时切回
    ctx8 = Ctx(["MAIN", "ALT"], "MAIN")
    p8 = RefusalFailoverPlugin(ctx8, dict(CFG))
    evt8 = Ev()
    await run_cmd(p8.switch_to_alt, p8, evt8)
    del ctx8._p["MAIN"]          # 模拟主模型被删除
    ctx8.provider_manager.provs.discard("MAIN")
    msg8 = await run_cmd(p8.switch_to_main, p8, evt8)
    check("主模型不可用有提示", "已不可用" in msg8, msg8[:40])

    print(f"\n--- 手动切换: {ok}/{total} ---")
    return ok == total

sys.exit(0 if asyncio.run(main()) else 1)
