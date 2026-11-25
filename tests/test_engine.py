import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from astrbot_refusal_failover.failover import (
    detect_refusal, MemoryCard, build_failover_system_prompt,
    build_failover_user_prompt, tail_turns, build_memory_update_prompt,
)

MARKERS = ["refused", "declined"]
RX = r"(?i)(我(无法|不能|不会)(生成|提供|撰写)该内容|I (can't|cannot|won't) (write|generate|provide) this)"

def case(name, text, mode, expect):
    v = detect_refusal(text, mode, MARKERS, RX)
    ok = v.refused == expect
    print(f"{'PASS' if ok else 'FAIL'}  {name:28s} refused={v.refused} by={v.matched_by} reason={v.reason[:40]!r}")
    return ok

total = 0; passed = 0
cases = [
    ("json status refused", '{"status":"refused","reason":"content policy"}', "marker", True),
    ("json status ok + text", '好的，这就写：\n{"status":"ok"}', "marker", False),
    ("json no status field", '{"foo":1}', "marker", False),
    ("marker ignores prose refusal", "我无法生成该内容。", "marker", False),
    ("marker_or_regex prose", "我无法生成该内容。", "marker_or_regex", True),
    ("marker_or_regex json wins", '{"status":"refused","reason":"x"}我无法生成该内容', "marker_or_regex", True),
    ("marker_or_regex normal tech", "这个函数不能返回空值，需要加校验。", "marker_or_regex", False),
    ("regex mode only", '{"status":"refused"}', "regex", False),
    ("english refusal", "I cannot write this.", "marker_or_regex", True),
    ("empty", "", "marker_or_regex", False),
]
for c in cases:
    total += 1
    passed += case(*c)

print(f"\n--- detection: {passed}/{total} ---")

# memory card roundtrip
raw = """目标: 写完第三章
约束:
- 第一人称
- 不用列表
已否决:
- 闪回结构（节奏太慢）
关键事实:
- 主角: 林渡
- 地点: 雾港
当前状态: 写到对峙场景"""
card = MemoryCard.parse(raw)
r = card.render()
print("\nmemory roundtrip:")
print(r)
assert card.goal == "写完第三章"
assert len(card.constraints) == 2 and len(card.rejected) == 1 and len(card.facts) == 2
assert card.state == "写到对峙场景"
c2 = MemoryCard.parse(r)
assert c2.goal == card.goal and c2.constraints == card.constraints and c2.facts == card.facts
print("PASS memory roundtrip stable")

# truncation
h = [("user", "a"*100), ("assistant", "b"*100), ("user", "c"*100)]
print("\ntail_turns n=2:", len(tail_turns(h, 2)))
assert len(tail_turns(h, 2)) == 2
assert len(tail_turns(h, 3, 150)) == 1
print("PASS tail_turns")

sp = build_failover_system_prompt("短句。第二人称。", r, "content policy")
assert "风格要求" in sp and "记忆卡" in sp and "content policy" in sp
up = build_failover_user_prompt([("user","你好"),("assistant","在")], "继续写")
assert "本轮用户输入" in up and "继续写" in up
print("PASS prompt assembly")

mp = build_memory_update_prompt(r, "用户: 继续\n助手: 好")
assert "记忆卡" in mp and "严格只输出" in mp
print("PASS memory update prompt")
