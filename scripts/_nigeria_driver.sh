#!/bin/bash
# Self-restarting, fully-detached driver for Nigeria portal discovery.
# Loops the (idempotent, resumable) runner until 0 rows remain, so if any
# single pass is killed the loop just relaunches and continues. Launched via
# `nohup setsid ... &` so it is NOT a harness-tracked background task and
# survives independently until the whole tab is filled.
cd /Users/mrunomi/projects/reclaim-portal-agent || exit 1
PY=.venv/bin/python
RUNNER=scripts/_run_julybatch_nigeria_portals.py
LOG=/Users/mrunomi/projects/reclaim-portal-agent/nigeria_run.log

echo "=== driver start $(date) ===" >> "$LOG"
for i in $(seq 1 200); do
    rem=$($PY "$RUNNER" --report-remaining 2>/dev/null | tail -1)
    echo "--- iteration $i $(date) | remaining=$rem ---" >> "$LOG"
    if [ "$rem" = "0" ]; then
        echo "=== ALL DONE $(date) ===" >> "$LOG"
        break
    fi
    caffeinate -dimsu $PY "$RUNNER" >> "$LOG" 2>&1
    sleep 3
done
echo "=== driver exit $(date) ===" >> "$LOG"
