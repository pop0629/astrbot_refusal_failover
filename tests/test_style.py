import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, PKG_PARENT)

from astrbot_refusal_failover.style_check import (
    build_style_sample, style_drift, drift_report,
)
from astrbot_refusal_failover.failover import (
    MemoryCard, build_rewrite_prompt, drift_reasons,
    build_failover_system_prompt, build_failover_user_prompt,
)

MAIN = [
    "他推开门，风灌进来，带起一片灰。",
    "林渡没回头，只把手里的烟摁灭在墙上。",
    "“你迟了。”他说。声音没什么起伏。",
]

# --- style sample ---
s = build_style_sample(MAIN)
assert "林渡" in s and "烟摁灭" in s
print("PASS build_style_sample keeps real text")
s2 = build_style_sample(MAIN, max_chars=30)
assert len(s2) <= 30 or s2.count("\n---\n") == 0, s2
print(f"PASS style sample truncation -> {len(s2)} chars")

# --- drift: same-voice vs alien-voice ---
same_voice = "他抬手，指节蹭过门框上的灰，没说话。"
alien = ("好的！下面我将为您继续撰写这段故事。\n\n"
         "- 首先，我们需要理解人物动机；\n"
         "- 其次，要注意氛围营造；\n"
         "- 最后，希望对您有帮助 😊\n\n"
         "希望以上回答对您有帮助，如需进一步调整请随时告知。")

sim_same = style_drift(same_voice, MAIN)
sim_alien = style_drift(alien, MAIN)
print(f"PASS same-voice similarity = {sim_same:.3f}")
print(f"PASS alien-voice similarity = {sim_alien:.3f}")
assert sim_same > sim_alien, (sim_same, sim_alien)
assert sim_alien < 0.55

# --- no samples -> never trigger rewrite ---
assert style_drift(alien, []) == 1.0
print("PASS no samples => no rewrite")

# --- reasons ---
rs = drift_reasons(alien, MAIN)
print("reasons:", rs)
assert any("元叙述" in r for r in rs)
assert any("列表" in r for r in rs)

# --- rewrite prompt ---
rp = build_rewrite_prompt(alien, "短句，第二人称，不用列表。", s, rs)
assert "待重写正文" in rp and "前文样本" in rp and "偏离点" in rp
print("PASS rewrite prompt assembly")

# --- failover system prompt has continuity block + sample ---
sp = build_failover_system_prompt("短句。", "目标: x", "policy", s)
assert "连续性要求" in sp and "真实前文" in sp and "林渡" in sp
print("PASS system prompt includes style sample + continuity rules")

up = build_failover_user_prompt([("user", "你来了"), ("assistant", "嗯。")], "继续")
assert "从它之后开始写" in up
print("PASS user prompt marks the seam")

# --- memory card with threads ---
raw = "目标: x\n关键事实:\n- 主角: 林渡\n未闭合线索:\n- 门后的信\n当前状态: 推门"
c = MemoryCard.parse(raw)
assert c.threads == ["门后的信"]
assert "未闭合线索" in c.render()
assert MemoryCard.parse(c.render()).threads == c.threads
print("PASS memory card threads roundtrip")

print("\nALL STYLE TESTS PASSED")
