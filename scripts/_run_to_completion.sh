#!/bin/bash
# Loop an idempotent, resumable Magic runner until it reports 0 remaining, so a
# killed pass just resumes and the whole batch finishes unattended. Every runner
# must support `--report-remaining` (prints the count of rows still to do, given
# the SAME args). Runs under caffeinate; meant to be launched daemonized:
#
#   .venv/bin/python scripts/_daemonize.py <log> \
#       bash scripts/_run_to_completion.sh scripts/<runner>.py [runner args...]
cd /Users/mrunomi/projects/reclaim-portal-agent || exit 1
PY=.venv/bin/python
RUNNER="$1"; shift
ARGS=("$@")
LOG="/Users/mrunomi/projects/reclaim-portal-agent/$(basename "$RUNNER" .py).log"

echo "=== driver start $(date) :: $RUNNER ${ARGS[*]} ===" >> "$LOG"
for i in $(seq 1 500); do
    rem=$($PY "$RUNNER" "${ARGS[@]}" --report-remaining 2>/dev/null | tail -1)
    echo "--- iter $i $(date) remaining=$rem ---" >> "$LOG"
    if [ "$rem" = "0" ]; then
        echo "=== ALL DONE $(date) ===" >> "$LOG"
        break
    fi
    caffeinate -dimsu $PY "$RUNNER" "${ARGS[@]}" >> "$LOG" 2>&1
    sleep 3
done
echo "=== driver exit $(date) ===" >> "$LOG"
