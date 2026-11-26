#!/bin/sh
set -e
cd "$(dirname "$0")"
cd ../..
for t in test_engine test_style test_soft_refusal test_request_intent test_marker \
         test_marker_sticky test_history_sync test_preempt test_auto_detect test_proactive \
         test_integration test_judge_integration test_request_intent_integration \
         test_switch test_continuity test_sticky test_sticky_integration; do
    printf '\n=== %s ===\n' "$t"
    python3 "astrbot_refusal_failover/tests/$t.py"
done
printf '\nALL TESTS PASSED\n'
