#!/bin/bash
# Rolling review+prune: for EACH 50-org window — review every portal row to
# completion, then prune that window's dead/no-login/junk rows — then advance to
# the next window, until no unreviewed orgs remain.
#
# Why per-window: _prune_dead_portals.py is scoped to portals_review_window.json,
# so pruning only once at the end would prune just the final window.
# Row colouring is off (MAGIC_REVIEW_COLOR unset -> no red tinting).
#
#   .venv/bin/python scripts/_daemonize.py <log> bash scripts/_run_review_prune_rolling.sh
cd /Users/mrunomi/projects/reclaim-portal-agent || exit 1
PY=.venv/bin/python
REVIEW=scripts/_run_portals_review.py
PRUNE=scripts/_prune_dead_portals.py
CACHE=portals_review_window.json
LOG="/Users/mrunomi/projects/reclaim-portal-agent/_run_portals_review.log"
MAXW=${MAX_WINDOWS:-100}

echo "=== rolling review+prune start $(date) (max_windows=$MAXW) ===" >> "$LOG"
for w in $(seq 1 "$MAXW"); do
    # --- review this window to completion ---
    for i in $(seq 1 200); do
        rem=$($PY "$REVIEW" --report-remaining 2>/dev/null | tail -1)
        if [ -z "$rem" ]; then           # network blip: back off, don't burn iterations
            echo "--- w$w iter $i: empty remaining (network?), sleeping 60 ---" >> "$LOG"
            sleep 60; continue
        fi
        echo "--- window $w / review iter $i $(date) remaining=$rem ---" >> "$LOG"
        [ "$rem" = "0" ] && break
        caffeinate -dimsu $PY "$REVIEW" >> "$LOG" 2>&1
        sleep 3
    done
    # --- prune this window ---
    echo "=== window $w prune $(date) ===" >> "$LOG"
    caffeinate -dimsu $PY "$PRUNE" >> "$LOG" 2>&1
    # --- advance ---
    rm -f "$CACHE"
    rem=$($PY "$REVIEW" --report-remaining 2>/dev/null | tail -1)
    echo "=== window $w done; next window remaining=$rem $(date) ===" >> "$LOG"
    if [ "$rem" = "0" ]; then
        echo "=== ALL WINDOWS REVIEWED + PRUNED $(date) ===" >> "$LOG"
        break
    fi
done
echo "=== rolling review+prune exit $(date) ===" >> "$LOG"
