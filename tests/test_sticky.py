"""sticky 状态机测试（纯逻辑，不依赖 AstrBot）。"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from sticky import (
    StickyState, ReleaseDecision, on_enter, evaluate_turn,
    on_turn_had_signal, build_sticky_judge_prompt, parse_sticky_judge,
)

ok = total = 0
def check(name, cond, extra=""):
    global ok, total
    total += 1; ok += bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")

# ---- 进入 ----
st = on_enter("ALT", judge_every=3)
check("进入后 active", st.active)
check("记录 alt", st.alt == "ALT")
check("计时开始", st.turns == 1)
check("首次判定门槛", st.judge_skip == 2, f"skip={st.judge_skip}")

# ---- 序列化往返 ----
st.streak = 2
back = StickyState.from_json(st.to_json())
check("序列化往返一致", back.streak == 2 and back.alt == "ALT" and back.active)
check("空数据返回默认", StickyState.from_json(None).active is False)
check("坏数据返回默认", StickyState.from_json({}).active is False)

# ---- 扳机 A：连续 N 轮正常后解除 ----
st = on_enter("ALT", judge_every=0)   # 关闭 C，只看 A
decisions = [evaluate_turn(st, max_turns=10, release_after_clean=3, judge_every=0)
             for _ in range(3)]
check("第1轮不解除", not decisions[0].release)
check("第2轮不解除", not decisions[1].release)
check("第3轮解除", decisions[2].release, decisions[2].reason)

# ---- 出现拒绝迹象则重置 streak ----
st = on_enter("ALT", judge_every=0)
evaluate_turn(st, max_turns=10, release_after_clean=3, judge_every=0)
evaluate_turn(st, max_turns=10, release_after_clean=3, judge_every=0)
check("2轮后 streak=2", st.streak == 2, f"{st.streak}")
on_turn_had_signal(st)
check("出现迹象后 streak 归零", st.streak == 0)
d = evaluate_turn(st, max_turns=10, release_after_clean=3, judge_every=0)
check("重置后不立刻解除", not d.release)

# ---- 硬上限 ----
st = on_enter("ALT", judge_every=0)
st.turns = 10
d = evaluate_turn(st, max_turns=10, release_after_clean=99, judge_every=0)
check("超过硬上限强制解除", d.release, d.reason)

# ---- 扳机 C 抽样 ----
st = on_enter("ALT", judge_every=3)
# 进入时 judge_skip=2 -> 第1、2轮递减，第3轮触发
d1 = evaluate_turn(st, max_turns=99, release_after_clean=99, judge_every=3)
d2 = evaluate_turn(st, max_turns=99, release_after_clean=99, judge_every=3)
d3 = evaluate_turn(st, max_turns=99, release_after_clean=99, judge_every=3)
check("C 第1轮不触发", not d1.needs_judge)
check("C 第2轮不触发", not d2.needs_judge)
check("C 第3轮触发", d3.needs_judge, str(d3.reason))
d4 = evaluate_turn(st, max_turns=99, release_after_clean=99, judge_every=3)
check("C 触发后重新计数", not d4.needs_judge)

# ---- A 先于 C 触发时不浪费判定 ----
st = on_enter("ALT", judge_every=5)
st.streak = 2
d = evaluate_turn(st, max_turns=99, release_after_clean=3, judge_every=5)
check("A 触发时不再要求 C", d.release and not d.needs_judge, d.reason)

# ---- 关闭 A 只靠 C ----
st = on_enter("ALT", judge_every=2)
ds = [evaluate_turn(st, max_turns=99, release_after_clean=0, judge_every=2) for _ in range(2)]
check("A 关闭时不自动解除", not any(d.release for d in ds))
check("A 关闭时 C 仍工作", ds[1].needs_judge)

# ---- judge prompt / 解析 ----
p = build_sticky_judge_prompt("用户: 继续\n助手: 好的")
check("judge prompt 含对话", "继续" in p)
check("judge prompt 要求单词输出", "只输出一个词" in p)
check("解析 RELEASE", parse_sticky_judge("RELEASE") is True)
check("解析 KEEP", parse_sticky_judge("KEEP") is False)
check("解析小写", parse_sticky_judge("release") is True)
check("解析带标点", parse_sticky_judge("RELEASE.") is True)
check("无法解析返回 None", parse_sticky_judge("嗯") is None)
check("空返回 None", parse_sticky_judge("") is None)

print(f"\n--- sticky 状态机: {ok}/{total} ---")
sys.exit(0 if ok == total else 1)
