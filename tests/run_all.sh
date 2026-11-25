#!/bin/sh
# 一键跑全部测试（无需安装 astrbot）
set -e
cd "$(dirname "$0")"
cd ../..
for t in test_engine test_style test_soft_refusal test_integration test_judge_integration test_switch; do
    printf '\n=== %s ===\n' "$t"
    python3 "astrbot_refusal_failover/tests/$t.py"
done
printf '\nALL TESTS PASSED\n'
