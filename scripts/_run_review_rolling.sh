#!/bin/bash
# Rolling review driver: finish the CURRENT window (portals_review_window.json),
# then CLEAR the cache so the reviewer builds the next 50-org window, up to
# MAX_ADVANCES times (default 1 => current window + one more batch of 50), then
# stop. Resumable and idempotent: a killed pass just resumes the same window, so
# it's safe to run daemonized. Runs each pass under caffeinate so laptop sleep
# can't drop it.
#
#   MAX_ADVANCES=1 .venv/bin/python scripts/_daemonize.py <log> \
#       bash scripts/_run_review_rolling.sh
cd /Users/mrunomi/projects/reclaim-portal-agent || exit 1
PY=.venv/bin/python
RUNNER=scripts/_run_portals_review.py
CACHE=portals_review_window.json
LOG="/Users/mrunomi/projects/reclaim-portal-agent/_run_portals_review.log"
MAX=${MAX_ADVANCES:-1}
advances=0

echo "=== rolling review start $(date) (max_advances=$MAX) ===" >> "$LOG"
for i in $(seq 1 500); do
    rem=$($PY "$RUNNER" --report-remaining 2>/dev/null | tail -1)
    echo "--- iter $i $(date) window-remaining=$rem advances=$advances/$MAX ---" >> "$LOG"
    if [ "$rem" = "0" ]; then
        if [ "$advances" -ge "$MAX" ]; then
            echo "=== DONE: current window + $advances extra window(s) reviewed $(date) ===" >> "$LOG"
            break
        fi
        # current window fully reviewed -> advance to the next 50
        rm -f "$CACHE"
        advances=$((advances + 1))
        rem=$($PY "$RUNNER" --report-remaining 2>/dev/null | tail -1)   # builds next window
        echo "--- advanced to window #$advances $(date) new-remaining=$rem ---" >> "$LOG"
        if [ "$rem" = "0" ]; then
            echo "=== ALL ORGS REVIEWED $(date) ===" >> "$LOG"
            break
        fi
    fi
    caffeinate -dimsu $PY "$RUNNER" >> "$LOG" 2>&1
    sleep 3
done
echo "=== rolling review exit $(date) ===" >> "$LOG"
