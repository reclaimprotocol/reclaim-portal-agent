#!/bin/bash
# Drive the login-endpoint audit to completion: re-verify every batch-3-7 "green
# (JS-render)" portal for false positives, resolving hub->login endpoints and
# re-flagging genuine non-logins. Batch-25 per invocation recycles the browser;
# loops until 0 suspects remain, then writes the done flag. Resumable.
cd /Users/mrunomi/projects/reclaim-portal-agent || exit 1
PY=.venv/bin/python
RUNNER=scripts/_audit_login_endpoints.py
LOG="/Users/mrunomi/projects/reclaim-portal-agent/_login_audit.log"
FLAG="/Users/mrunomi/projects/reclaim-portal-agent/login_audit_DONE.flag"
export MAGIC_REVIEW_BATCH="${MAGIC_REVIEW_BATCH:-25}"
export MAGIC_ROW_TIMEOUT="${MAGIC_ROW_TIMEOUT:-70}"
export MAGIC_RENDER_TIMEOUT="${MAGIC_RENDER_TIMEOUT:-25}"

echo "=== login-audit start $(date) ===" >> "$LOG"
for i in $(seq 1 500); do
    rem=$($PY "$RUNNER" --report-remaining 2>/dev/null | tail -1)
    echo "--- iter $i $(date) suspects-remaining=$rem ---" >> "$LOG"
    if [ "$rem" = "0" ]; then
        touch "$FLAG"
        echo "=== login-audit DONE $(date) ===" >> "$LOG"
        break
    fi
    caffeinate -dimsu $PY "$RUNNER" >> "$LOG" 2>&1
    sleep 3
done
echo "=== login-audit exit $(date) ===" >> "$LOG"
