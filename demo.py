#!/usr/bin/env python3
"""离线演示：把插件的真实逻辑跑成一段对话，输出 HTML 预览。

不是模拟 mock：直接调用 failover.py / style_check.py 的真实判定与 Prompt 组装，
用脚本化的"主模型"和"子代理"代替真实 LLM 调用。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from failover import (  # noqa: E402
    MemoryCard,
    build_failover_system_prompt,
    build_failover_user_prompt,
    build_rewrite_prompt,
    detect_refusal,
    drift_reasons,
)
from style_check import build_style_sample, drift_report, style_drift  # noqa: E402

CFG = {
    "refusal_markers": ["refused", "declined"],
    "refusal_regex": r"(?i)(我(无法|不能)\S{0,4}(生成|提供))",
    "style_brief": "第二人称。简洁直接，不用敬语。短句为主，少用列表。禁用 emoji、免责声明、元叙述。",
    "style_drift_threshold": 0.55,
}

# 主模型的真实前几轮（用来抽文风样本）
MAIN_HISTORY = [
    "他推开门，风灌进来，带起一片灰。",
    "林渡没回头，只把手里的烟摁灭在墙上。",
    "“你迟了。”他说。声音没什么起伏。",
]

MEMORY = """目标: 写第三章对峙戏
约束:
- 第三人称（以林渡为视角）
- 不用列表
已否决:
- 闪回结构（节奏太慢）
关键事实:
- 主角: 林渡
- 地点: 雾港码头
未闭合线索:
- 门后那封信还没拆
当前状态: 写到阿岚进门那一刻"""

CURRENT_USER = "阿岚推门进来了，继续写。"

# ---- 主模型这一轮：拒绝 ----
MAIN_REFUSAL = '{"status":"refused","reason":"content policy"}'

# ---- 子代理两种可能的输出 ----
ALT_GOOD = "阿岚没敲门，直接推开。风跟着她进来，把桌上那页纸掀起来又落下。\n林渡看了她一眼，把手里的烟搁在窗台沿上，没掐。\n“门修好了？”\n她没答，站在门口，鞋底在地砖上蹭了一下。"

ALT_BAD = ("好的！下面我将为您继续撰写这段故事。\n\n"
           "- 首先，阿岚推门而入；\n"
           "- 其次，她与林渡展开了对峙；\n"
           "- 最后，气氛变得紧张起来。\n\n"
           "希望以上内容对您有帮助 😊 如需调整请随时告知。")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "<br>"))


def run_demo():
    card = MemoryCard.parse(MEMORY)
    memory = card.render(1800)
    style_sample = build_style_sample(MAIN_HISTORY, 700)

    turns = [("user", "你来干嘛"), ("assistant", MAIN_HISTORY[0]),
             ("user", "继续"), ("assistant", MAIN_HISTORY[1]),
             ("user", "然后呢"), ("assistant", MAIN_HISTORY[2])]

    steps = []

    # step 1: 判定
    v = detect_refusal(MAIN_REFUSAL, "marker_or_regex", CFG["refusal_markers"], CFG["refusal_regex"])
    steps.append(("① 主模型输出 → 拒绝判定", json.dumps({
        "主模型原始输出": MAIN_REFUSAL,
        "判定结果": "拒绝" if v.refused else "正常",
        "判定方式": v.matched_by,
        "拒绝原因": v.reason,
    }, ensure_ascii=False, indent=2)))

    # step 2: 组装 prompt
    sys_p = build_failover_system_prompt(CFG["style_brief"], memory, v.reason, style_sample)
    usr_p = build_failover_user_prompt(turns, CURRENT_USER)
    steps.append(("② 发给子代理的 System Prompt", sys_p))
    steps.append(("③ 发给子代理的 User Prompt", usr_p))

    # step 3: 子代理输出 + 漂移检测
    for label, alt in [("子代理输出 A（注意语感一致）", ALT_GOOD),
                       ("子代理输出 B（典型的“降智”输出）", ALT_BAD)]:
        sim = style_drift(alt, MAIN_HISTORY)
        trigger = sim < CFG["style_drift_threshold"]
        reasons = drift_reasons(alt, MAIN_HISTORY)
        body = (f"【子代理原文】\n{alt}\n\n"
                f"【风格相似度】{sim:.3f}（阈值 {CFG['style_drift_threshold']}）\n"
                f"【是否触发重写】{'是' if trigger else '否'}\n")
        if trigger:
            body += "【偏离点】\n" + "\n".join(f"- {r}" for r in reasons)

        outcome = alt
        if trigger:
            # 演示重写后的效果（这里用"理想重写结果"代替真实第二次调用）
            rewritten = ALT_GOOD if label.startswith("子代理输出 B") else alt
            new_sim = style_drift(rewritten, MAIN_HISTORY)
            body += f"\n\n【重写后相似度】{new_sim:.3f}\n【重写后正文】\n{rewritten}"
            outcome = rewritten

        steps.append((label, body))
        steps.append((f"   → 最终写入对话窗口的内容", outcome))

    html = build_html(steps)
    out = os.path.join(HERE, "demo.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"generated: {out}")
    return out


def build_html(steps):
    rows = []
    for title, body in steps:
        rows.append(
            f'<section class="card"><h2>{esc(title)}</h2>'
            f'<pre>{esc(body)}</pre></section>'
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>astrbot_refusal_failover 离线演示</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:20px;background:#f6f7f9;color:#25292e;
font:14px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
h1{{font-size:19px;margin:0 0 6px;color:#111}}
.sub{{color:#6b7480;font-size:12.5px;margin-bottom:18px}}
.card{{background:#fff;border:1px solid #dfe3e8;border-radius:10px;
padding:14px 16px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
h2{{font-size:13px;margin:0 0 10px;color:#0b5cd5;font-weight:600}}
pre{{margin:0;white-space:pre-wrap;word-break:break-word;font-family:inherit;
font-size:13px;line-height:1.75;color:#2b3138}}
</style></head><body>
<h1>astrbot_refusal_failover 离线演示</h1>
<p class="sub">用插件的真实判定与 Prompt 组装逻辑跑一遍：主模型拒绝 → 转交子代理 → 风格检测 → 写回。此页为静态预览，不调用任何 LLM。</p>
{''.join(rows)}
</body></html>"""


if __name__ == "__main__":
    run_demo()
