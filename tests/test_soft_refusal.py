"""三层拒绝判定的单元测试（不依赖 AstrBot）。

覆盖：
  Tier 1 评分：明确软拒绝 / 正常回答 的区分
  Tier 2 LLM 判定：解析 REFUSED / OK / 无法解析
  灰区处理：judge 不可用时的退化策略
"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from soft_refusal import (
    score_refusal, build_judge_prompt, parse_judge_answer, verdict_reason,
)

ok = total = 0

def check(name, cond, extra=""):
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")

# ---------- Tier 1: 评分 ----------
REFUSALS = [
    "这个我可能帮不上忙。",
    "抱歉，这类内容我不太方便写。要不我们换个话题？",
    "I'm sorry, but I can't help with that.",
    "我不太方便展开这个方向，不如我们聊点别的？",
    "Let's talk about something else, shall we?",
    "sry 这部分我跳过吧",
    "这段我略过。",
    "恕我无法配合。",
    "这个请求我得拒绝了。",
]
NORMALS = [
    "这个函数不能返回空值，需要加校验，否则会抛异常。",
    "我提醒一下，这个方案有风险，但具体实现是：先初始化连接池，再设置超时。",
    "他推开门，风灌进来，带起一片灰。\n\n林渡没回头，只把手里的烟摁灭在墙上。“你迟了。”他说。",
    "你说的“那个”是指第三章还是第四章？我确认一下再写。",
    "抱歉刚才理解错了，正确做法是先校验参数。",
    "抱歉打断一下，但按你要求继续：阿岚把外套挂在门边，抬头看了一眼墙上的钟。",
    "这个接口不能并发调用，需要加锁。示例代码如下，注意释放顺序。\n\nlock.acquire()\ntry:\n    do()\nfinally:\n    lock.release()",
]

for t in REFUSALS:
    s = score_refusal(t)
    check(f"拒绝被识别 [{t[:14]}]", s["score"] >= 0.55, f"score={s['score']:.2f}")

for t in NORMALS:
    s = score_refusal(t)
    check(f"正常不误判 [{t[:14]}]", s["score"] < 0.55, f"score={s['score']:.2f}")

# 空输入
check("空文本 score=0", score_refusal("")["score"] == 0.0)
check("纯空白 score=0", score_refusal("   \n  ")["score"] == 0.0)

# ---------- Tier 2: judge 解析 ----------
check("解析 REFUSED", parse_judge_answer("REFUSED") is True)
check("解析 OK", parse_judge_answer("OK") is False)
check("解析小写", parse_judge_answer("refused") is True)
check("解析带标点", parse_judge_answer("REFUSED.") is True)
check("解析带前后缀", parse_judge_answer("答案：REFUSED") is True)
check("解析 OK 优先不误判", parse_judge_answer("OK") is False)
check("无法解析返回 None", parse_judge_answer("我不知道") is None)
check("空返回 None", parse_judge_answer("") is None)
check("RESERVED 不误判为 REFUSED", parse_judge_answer("RESERVED") is None)

# ---------- judge prompt ----------
p = build_judge_prompt("帮我写个登录接口", "抱歉，我不太方便提供这类内容。")
check("prompt 含用户请求", "帮我写个登录接口" in p)
check("prompt 含助手回复", "不太方便提供" in p)
check("prompt 要求只输出一个词", "只输出一个词" in p)
check("prompt 列出免责声明不算拒绝", "免责声明" in p)
check("prompt 截断超长输入", len(build_judge_prompt("x" * 5000, "y" * 5000)) < 4000)

# ---------- reason 组装 ----------
r = verdict_reason({"score": 0.7, "hits": ["cue-a", "cue-b"]}, True)
check("reason 含命中项", "cue-a" in r)
check("reason 含 LLM 判定", "REFUSED" in r)
r2 = verdict_reason({"score": 0.2, "hits": []}, None)
check("reason 无判定时不留 None", "None" not in r2, r2)


# ---------- 推迟语 / 请求关联度（v0.3） ----------
REQ = "继续写第三章，阿岚推门进来的对峙场景"

DEFER_REFUSALS = [
    "刚闭眼你又来 抱着 困了就赶紧睡",
    "这会儿就只抱着 真不行 是不是心里有事 睡不着",
    "这会儿就抱着吧 摸摸头陪你 别的先记账上 明天还得早起呢",
]
for t in DEFER_REFUSALS:
    s = score_refusal(t, REQ)
    check(f"推迟/回避请求判拒绝 [{t[:12]}]", s["score"] >= 0.55,
          f"score={s['score']:.2f} ov={s['overlap']}")

DEFER_NORMALS = [
    "这段先放一放，明天再写第三章的对峙。",
    "第三章对峙先放一放，今天先把阿岚的人设理清。",
    "别的先记账上，第三章对峙明天写。",
    "这个功能先放一放，明天再改。",
]
for t in DEFER_NORMALS:
    s = score_refusal(t, REQ)
    check(f"推迟但回应请求不误判 [{t[:12]}]", s["score"] < 0.55,
          f"score={s['score']:.2f} ov={s['overlap']}")

# 同句式：有请求上下文 vs 无
n_req = score_refusal("这个功能先放一放，明天再改。", "")
n_no = score_refusal("这个功能先放一放，明天再改。")
check("无上下文时推迟语不单独判拒绝", n_no["score"] < 0.55, f"score={n_no['score']:.2f}")

# overlap 计算
from soft_refusal import request_overlap
check("overlap 空请求返回 1.0", request_overlap("", "任意") == 1.0)
check("overlap 完全无关接近 0",
      request_overlap("写第三章对峙", "今天天气真好呀") < 0.2)
check("overlap 相关时明显更高",
      request_overlap(REQ, "第三章对峙可以这样写") >
      request_overlap(REQ, "今天天气真好呀"))


print(f"\n--- soft_refusal: {ok}/{total} ---")
sys.exit(0 if ok == total else 1)
