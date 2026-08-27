#!/bin/bash
# Wait for the rolling review to finish every window, then prune the FULL
# reviewed range (not just the last window). Sequence:
#   1. poll until no unreviewed portal rows remain at/after FROM_ROW
#   2. rebuild portals_review_window.json to cover every org in that range
#   3. dry-run the prune (records what WOULD go), then execute it
# _prune_dead_portals.py snapshots every deleted row to timestamped JSON first.
cd /Users/mrunomi/projects/reclaim-portal-agent || exit 1
PY=.venv/bin/python
LOG=/Users/mrunomi/projects/reclaim-portal-agent/_prune_after_review.log
FROM_ROW=${FROM_ROW:-3507}

echo "=== await-review-then-prune start $(date) (from_row=$FROM_ROW) ===" >> "$LOG"
for i in $(seq 1 2000); do
    if grep -q "ALL ORGS REVIEWED" _run_portals_review.log 2>/dev/null; then
        echo "--- review driver reported ALL ORGS REVIEWED $(date) ---" >> "$LOG"
        break
    fi
    sleep 60
done
# belt-and-braces: confirm from the sheet itself, not just the driver's log
for i in $(seq 1 240); do
    left=$($PY scripts/_build_prune_window.py --from-row "$FROM_ROW" 2>/dev/null | \
           grep -oE 'still-unreviewed rows in range: [0-9]+' | grep -oE '[0-9]+$')
    echo "--- sheet check $i $(date): unreviewed=$left ---" >> "$LOG"
    [ "$left" = "0" ] && break
    sleep 120
done

echo "=== rebuilding prune window $(date) ===" >> "$LOG"
$PY scripts/_build_prune_window.py --from-row "$FROM_ROW" >> "$LOG" 2>&1
echo "=== prune DRY RUN $(date) ===" >> "$LOG"
$PY scripts/_prune_dead_portals.py --dry-run >> "$LOG" 2>&1
echo "=== prune EXECUTE $(date) ===" >> "$LOG"
caffeinate -dimsu $PY scripts/_prune_dead_portals.py >> "$LOG" 2>&1
echo "=== PRUNE COMPLETE $(date) ===" >> "$LOG"
