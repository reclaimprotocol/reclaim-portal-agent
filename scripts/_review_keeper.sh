#!/bin/bash
# Lightweight watchdog that keeps the review->prune daemon alive until the batch
# finishes. The heavy daemon (Chromium via residential proxy) gets group-killed
# by the OS every few minutes; this keeper spawns NO Chromium, so it isn't a
# memory target and survives those kills. Every CHECK seconds it relaunches the
# daemon if it's not running and the batch isn't done. Stops (and self-exits)
# once the driver writes portals_review_DONE.flag. Launch daemonized:
#
#   .venv/bin/python scripts/_daemonize.py <keeperlog> bash scripts/_review_keeper.sh
cd /Users/mrunomi/projects/reclaim-portal-agent || exit 1
PY=.venv/bin/python
LOG="/Users/mrunomi/projects/reclaim-portal-agent/_run_portals_review.log"
KLOG="/Users/mrunomi/projects/reclaim-portal-agent/_review_keeper.log"
FLAG="/Users/mrunomi/projects/reclaim-portal-agent/portals_review_DONE.flag"
CHECK=45
# env passed through to each daemon launch
export MAGIC_REVIEW_BATCH="${MAGIC_REVIEW_BATCH:-25}"
export MAGIC_REVIEW_WINDOW="${MAGIC_REVIEW_WINDOW:-50}"

echo "=== keeper start $(date) (batch=$MAGIC_REVIEW_BATCH) ===" >> "$KLOG"
for i in $(seq 1 5000); do
    if [ -f "$FLAG" ]; then
        echo "=== keeper: DONE flag present, exiting $(date) ===" >> "$KLOG"
        break
    fi
    if ! pgrep -f "_run_review_then_prune.sh" >/dev/null 2>&1; then
        echo "--- keeper: daemon not running, (re)launching $(date) ---" >> "$KLOG"
        $PY scripts/_daemonize.py "$LOG" bash scripts/_run_review_then_prune.sh >> "$KLOG" 2>&1
        sleep 8   # let it come up before the next check
    fi
    sleep $CHECK
done
echo "=== keeper exit $(date) ===" >> "$KLOG"
