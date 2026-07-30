#!/bin/bash
# Two-phase batch cleanup for the active review window:
#   Phase 1 — review every portal row in the window to completion (proxy-enabled
#             _run_portals_review.py; resumable, loops until 0 remaining).
#   Phase 2 — prune: delete dead / no-login / junk rows, keeping only working
#             login portals + their T&Cs (snapshots deletions first).
# Runs under caffeinate; launch daemonized so it survives sleep / harness kills.
#
#   .venv/bin/python scripts/_daemonize.py <log> bash scripts/_run_review_then_prune.sh
cd /Users/mrunomi/projects/reclaim-portal-agent || exit 1
PY=.venv/bin/python
REVIEW=scripts/_run_portals_review.py
PRUNE=scripts/_prune_dead_portals.py
LOG="/Users/mrunomi/projects/reclaim-portal-agent/_run_portals_review.log"

echo "=== review+prune start $(date) ===" >> "$LOG"

# --- Phase 1: review to completion (single window, no advance) ---
for i in $(seq 1 500); do
    rem=$($PY "$REVIEW" --report-remaining 2>/dev/null | tail -1)
    echo "--- review iter $i $(date) remaining=$rem ---" >> "$LOG"
    [ "$rem" = "0" ] && break
    caffeinate -dimsu $PY "$REVIEW" >> "$LOG" 2>&1
    sleep 3
done
echo "=== review complete $(date) ===" >> "$LOG"

# --- Phase 2: prune dead/no-login/junk rows ---
echo "=== prune start $(date) ===" >> "$LOG"
caffeinate -dimsu $PY "$PRUNE" >> "$LOG" 2>&1
# completion flag — the keeper stops relaunching once this exists
touch /Users/mrunomi/projects/reclaim-portal-agent/portals_review_DONE.flag
echo "=== review+prune exit $(date) ===" >> "$LOG"
