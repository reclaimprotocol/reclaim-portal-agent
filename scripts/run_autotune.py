"""Autonomous self-improving eval loop.

The pipeline this script drives:

    1. Run `scripts/run_finetune_eval.py` against rows --start..--end.
    2. Read the resulting fix prompt (`fixes_needed.md`) + stats sidecar.
    3. Pipe the prompt into the Claude Code CLI; let it implement the fixes.
    4. Re-run the eval with --skip-discovery to verify improvement.
    5. If accuracy gained >= --min-improvement, `git commit`. Otherwise
       `git checkout -- .` to revert.
    6. If --max-iterations > 1, repeat steps 2-5 using the cached discovery
       results until either the target accuracy is reached, no patterns
       remain, or the iteration budget is exhausted.

Contract with scripts/run_finetune_eval.py
------------------------------------------
This script does not yet exist in the repo. `run_autotune.py` calls it
via subprocess with:

    python scripts/run_finetune_eval.py \
        --start <int> --end <int> \
        [--skip-discovery] \
        --output <path-to-fixes_needed.md>

It is expected to:

  * Exit 0 on success, non-zero on failure.
  * Write the human/Claude-readable fix prompt to <output>.
  * Write a machine-readable sidecar at <output>.json (same basename,
    `.json` extension) containing:

        {
          "accuracy":          float,       # 0..100
          "correct":           int,
          "evaluated":         int,
          "patterns_detected": list[str],   # systemic patterns (may be empty)
          "errors":            list[dict],  # per-row diagnostics (opaque)
          "domain_overrides_proposed": int  # informational; may be 0
        }

If the sidecar is missing or malformed, this autotune treats the eval
as failed for that iteration and bails out gracefully — it does not
attempt to scrape stats from stdout.

Examples
--------
    python scripts/run_autotune.py --start 100 --end 120
    python scripts/run_autotune.py --start 100 --end 120 --max-iterations 3
    python scripts/run_autotune.py --start 100 --end 120 --dry-run
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401  -- must come before agent.* imports
import click

from agent.config import load_config
from agent.sheets_client import SheetsClient

logger = logging.getLogger(__name__)

REPO_ROOT = Path(_bootstrap.ROOT)
EVAL_SCRIPT = REPO_ROOT / "scripts" / "run_finetune_eval.py"
EVAL_LOGS_DIR = REPO_ROOT / "eval_logs"
CLAUDE_CODE_TIMEOUT_SECONDS = 900


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    accuracy: float
    correct: int
    evaluated: int
    patterns_detected: list[str]
    fixes_file: Path
    sidecar_file: Path


# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------


def _check_git_clean() -> bool:
    """Return True iff the working tree has no uncommitted changes.

    Warn-but-continue if dirty so the user can still run autotune on
    a WIP branch — the autotune commit will then bundle their WIP with
    its own fixes, which is rarely what they want.
    """
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as err:
        logger.warning("git status failed: %s — continuing anyway", err)
        return True
    if out.stdout.strip():
        logger.warning(
            "Working tree is dirty. Autotune commits will bundle your "
            "uncommitted changes with the generated fixes. Continuing."
        )
        return False
    return True


def _check_claude_cli() -> str | None:
    path = shutil.which("claude")
    if not path:
        logger.error(
            "Claude Code CLI not found on PATH. Install with:\n"
            "    npm install -g @anthropic-ai/claude-code"
        )
        return None
    return path


def _check_eval_script() -> bool:
    if not EVAL_SCRIPT.exists():
        logger.error(
            "Eval driver missing: %s. Create it before running autotune; "
            "see the module docstring of run_autotune.py for the expected "
            "interface.", EVAL_SCRIPT,
        )
        return False
    return True


def _check_oauth_token() -> bool:
    token_path = REPO_ROOT / "token.json"
    if not token_path.exists():
        logger.error(
            "Google OAuth token missing (%s). Run:\n"
            "    rm -f token.json && python scripts/run_single.py "
            "--orgid test", token_path,
        )
        return False
    return True


def _check_logs_dir() -> bool:
    try:
        EVAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        logger.error("Cannot create %s: %s", EVAL_LOGS_DIR, err)
        return False
    return True


def _check_column_e() -> bool:
    """Verify the Universities sheet has a populated column E (ground truth)."""
    try:
        config = load_config()
        sheets = SheetsClient.from_config(config)
        rows = sheets.read_universities()
    except Exception as err:
        logger.error("Could not read Universities sheet: %s", err)
        return False
    if not rows:
        logger.error("Universities sheet is empty.")
        return False
    headers = list(rows[0].keys())
    if len(headers) < 5:
        logger.error(
            "Universities sheet has fewer than 5 columns; column E "
            "(ground truth) is missing. Headers: %s", headers,
        )
        return False
    return True


def _run_prereq_checks(claude_only: bool = False) -> str | None:
    """Run all startup checks. Returns the Claude CLI path on success,
    None on hard failure. `claude_only=True` skips checks that aren't
    needed in --dry-run mode (Claude CLI, OAuth token)."""
    if not _check_eval_script():
        return None
    if not _check_logs_dir():
        return None
    if not claude_only:
        if not _check_oauth_token():
            return None
        if not _check_column_e():
            return None
    _check_git_clean()  # warn-only
    claude_path = _check_claude_cli() if not claude_only else "dry-run"
    return claude_path


# ---------------------------------------------------------------------------
# Eval invocation
# ---------------------------------------------------------------------------


def _sidecar_path(fixes_file: Path) -> Path:
    return fixes_file.with_suffix(".json")


def _read_sidecar(sidecar: Path) -> dict[str, Any] | None:
    if not sidecar.exists():
        logger.error("Eval sidecar missing: %s", sidecar)
        return None
    try:
        return json.loads(sidecar.read_text())
    except (OSError, json.JSONDecodeError) as err:
        logger.error("Could not parse eval sidecar %s: %s", sidecar, err)
        return None


def _run_eval(
    *,
    start: int,
    end: int,
    output: Path,
    skip_discovery: bool,
) -> EvalResult | None:
    cmd: list[str] = [
        sys.executable, str(EVAL_SCRIPT),
        "--start", str(start),
        "--end", str(end),
        "--output", str(output),
    ]
    if skip_discovery:
        cmd.append("--skip-discovery")

    label = "verify" if skip_discovery else "initial"
    logger.info("Running eval (%s): %s", label, " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=False, text=True,
        )
    except FileNotFoundError as err:
        logger.error("Could not launch eval: %s", err)
        return None
    if result.returncode != 0:
        logger.error(
            "Eval exited %d — see its stdout/stderr above.",
            result.returncode,
        )
        return None

    sidecar = _sidecar_path(output)
    data = _read_sidecar(sidecar)
    if data is None:
        return None
    try:
        return EvalResult(
            accuracy=float(data["accuracy"]),
            correct=int(data["correct"]),
            evaluated=int(data["evaluated"]),
            patterns_detected=list(data.get("patterns_detected", [])),
            fixes_file=output,
            sidecar_file=sidecar,
        )
    except (KeyError, TypeError, ValueError) as err:
        logger.error("Sidecar %s missing required fields: %s", sidecar, err)
        return None


# ---------------------------------------------------------------------------
# Claude Code CLI invocation
# ---------------------------------------------------------------------------


def call_claude_code(fixes_file: Path, claude_path: str) -> bool:
    """Pipe the fix prompt to Claude Code CLI and wait for it to finish."""
    logger.info("Passing %s to Claude Code CLI…", fixes_file.name)
    try:
        prompt_content = fixes_file.read_text()
    except OSError as err:
        logger.error("Could not read %s: %s", fixes_file, err)
        return False

    full_prompt = (
        "You are fixing bugs in the reclaim-portal-agent codebase.\n"
        "Read the following auto-generated fix prompt carefully and "
        "implement ALL changes described. Make the exact code changes "
        "specified. Do not ask for clarification — implement everything."
        "\n\n" + prompt_content
    )

    try:
        result = subprocess.run(
            [
                claude_path, "--print",
                # Autotune runs non-interactively: without bypass, Claude
                # blocks on Edit/Write approval prompts. Acceptable here
                # because the entire purpose of this subprocess is to let
                # Claude modify the repo.
                "--permission-mode", "bypassPermissions",
            ],
            input=full_prompt,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=CLAUDE_CODE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "Claude Code timed out after %ds — skipping commit.",
            CLAUDE_CODE_TIMEOUT_SECONDS,
        )
        return False
    except FileNotFoundError as err:
        logger.error("Claude Code CLI vanished mid-run: %s", err)
        return False

    if result.returncode != 0:
        logger.error("Claude Code failed: %s", (result.stderr or "")[:500])
        return False

    logger.info("Claude Code completed successfully.")
    preview = (result.stdout or "").strip().splitlines()
    if preview:
        logger.info("Output preview: %s", preview[0][:200])
    return True


# ---------------------------------------------------------------------------
# Git commit / revert
# ---------------------------------------------------------------------------


def _git_commit(start: int, end: int, before: float, after: float) -> bool:
    msg = (
        f"fix: autotune eval rows {start}-{end} "
        f"accuracy {before:.0f}%→{after:.0f}%"
    )
    try:
        subprocess.run(["git", "add", "-A"], cwd=REPO_ROOT, check=True)
        subprocess.run(
            ["git", "commit", "-m", msg], cwd=REPO_ROOT, check=True,
        )
    except subprocess.CalledProcessError as err:
        logger.error("git commit failed: %s", err)
        return False
    logger.info("Committed: %s", msg)
    return True


def _git_revert() -> None:
    try:
        subprocess.run(
            ["git", "checkout", "--", "."], cwd=REPO_ROOT, check=True,
        )
    except subprocess.CalledProcessError as err:
        logger.error("git revert failed: %s", err)
        return
    logger.warning("Changes reverted — no improvement detected.")


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------


def _print_report(
    *,
    start: int,
    end: int,
    iterations_run: int,
    initial_pct: float,
    final_pct: float,
    patterns_fixed: int,
    overrides_added: int,
    committed: bool,
) -> None:
    delta = final_pct - initial_pct
    delta_str = f"{'+' if delta >= 0 else ''}{delta:.1f}%"
    width = 50  # inner width between the two ║ chars

    def row(text: str) -> str:
        return "║" + text.ljust(width) + "║"

    border_top = "╔" + "═" * width + "╗"
    border_mid = "╠" + "═" * width + "╣"
    border_bot = "╚" + "═" * width + "╝"

    lines = [
        border_top,
        row(f"  AUTOTUNE COMPLETE: rows {start}-{end}"),
        border_mid,
        row(f"  Iterations run:        {iterations_run}"),
        border_mid,
        row(f"  Initial accuracy:      {initial_pct:.1f}%"),
        row(f"  Final accuracy:        {final_pct:.1f}%"),
        row(f"  Improvement:           {delta_str}"),
        border_mid,
        row(f"  Patterns fixed:        {patterns_fixed}"),
        row(f"  domain_overrides added:{overrides_added}"),
        row(f"  Committed:             {'yes' if committed else 'no'}"),
        border_mid,
        row("  Logs saved → eval_logs/"),
        row("  Fix prompts → fixes_needed*.md"),
        border_bot,
    ]
    click.echo("\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.option(
    "--start", type=int, required=True,
    help="First sheet row number to process (row 2 = first data row).",
)
@click.option(
    "--end", type=int, required=True,
    help="Last sheet row number to process (inclusive).",
)
@click.option(
    "--max-iterations", type=int, default=1, show_default=True,
    help="How many fix-and-verify cycles to run before giving up.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Generate fixes_needed.md but do NOT call Claude Code or commit.",
)
@click.option(
    "--min-improvement", type=float, default=5.0, show_default=True,
    help="Minimum accuracy gain (percentage points) to accept a fix cycle.",
)
@click.option(
    "--no-commit", is_flag=True,
    help="Implement fixes but skip git commit (still revert on regression).",
)
def main(
    start: int,
    end: int,
    max_iterations: int,
    dry_run: bool,
    min_improvement: float,
    no_commit: bool,
) -> None:
    if start > end:
        raise click.ClickException(
            f"--start ({start}) must be <= --end ({end})"
        )
    if start < 2:
        raise click.ClickException(
            "Row 2 is the first data row (row 1 is the header)."
        )
    if max_iterations < 1:
        raise click.ClickException("--max-iterations must be >= 1")

    _bootstrap.setup_logging("INFO")

    claude_path = _run_prereq_checks(claude_only=dry_run)
    if claude_path is None:
        sys.exit(1)

    initial_fixes = EVAL_LOGS_DIR / f"fixes_needed_{start}_{end}.md"
    initial = _run_eval(
        start=start, end=end, output=initial_fixes, skip_discovery=False,
    )
    if initial is None:
        logger.error("Initial eval failed — cannot continue.")
        sys.exit(1)

    logger.info(
        "Initial accuracy: %.1f%% (%d/%d) — %d patterns detected",
        initial.accuracy, initial.correct, initial.evaluated,
        len(initial.patterns_detected),
    )

    if initial.evaluated > 0 and initial.accuracy >= 100.0:
        click.echo("Already at 100% accuracy — nothing to fix.")
        _print_report(
            start=start, end=end, iterations_run=0,
            initial_pct=initial.accuracy, final_pct=initial.accuracy,
            patterns_fixed=0, overrides_added=0, committed=False,
        )
        return

    if not initial.patterns_detected and initial.accuracy >= 90.0:
        click.echo(
            f"Accuracy {initial.accuracy:.1f}% with no systemic patterns "
            f"— only one-off fixes needed."
        )

    if dry_run:
        click.echo("\n=== DRY RUN: fixes_needed.md contents ===\n")
        try:
            click.echo(initial_fixes.read_text())
        except OSError as err:
            logger.error("Could not read %s: %s", initial_fixes, err)
        click.echo("\n=== (no Claude Code invocation, no commit) ===")
        return

    current_accuracy = initial.accuracy
    current_fixes_file = initial_fixes
    patterns_before = set(initial.patterns_detected)
    patterns_fixed = 0
    overrides_added = 0
    committed_any = False
    iterations_run = 0

    for iteration in range(1, max_iterations + 1):
        logger.info("Iteration %d/%d", iteration, max_iterations)
        iterations_run = iteration

        # The fix prompt for iteration 1 is the initial eval output. For
        # later iterations, regenerate it against the freshly-modified
        # codebase. `skip_discovery=False` is required for the new code
        # to actually take effect — the Portals sheet only changes when
        # discovery re-runs, and that's how the eval reads agent output.
        if iteration > 1:
            iter_fixes = (
                EVAL_LOGS_DIR
                / f"fixes_needed_{start}_{end}_iter{iteration}.md"
            )
            regen = _run_eval(
                start=start, end=end, output=iter_fixes, skip_discovery=False,
            )
            if regen is None:
                logger.error("Iteration %d eval failed — stopping.", iteration)
                break
            current_accuracy = regen.accuracy
            current_fixes_file = iter_fixes
            patterns_before = set(regen.patterns_detected)
            if not regen.patterns_detected:
                logger.info("No more systemic patterns — stopping.")
                break
            if current_accuracy >= 90.0:
                logger.info(
                    "Target accuracy reached: %.1f%% — stopping.",
                    current_accuracy,
                )
                break

        if not call_claude_code(current_fixes_file, claude_path):
            logger.error("Claude Code step failed — stopping at iteration %d.",
                         iteration)
            break

        verify_out = (
            EVAL_LOGS_DIR
            / f"fixes_verification_{start}_{end}_iter{iteration}.md"
        )
        verified = _run_eval(
            start=start, end=end, output=verify_out, skip_discovery=False,
        )
        if verified is None:
            logger.error(
                "Verification eval failed — reverting iteration %d.",
                iteration,
            )
            _git_revert()
            break

        gained = verified.accuracy - current_accuracy
        logger.info(
            "Iteration %d: %.1f%% → %.1f%% (Δ %+.1f, threshold %+.1f)",
            iteration, current_accuracy, verified.accuracy,
            gained, min_improvement,
        )

        if gained >= min_improvement:
            patterns_fixed += len(
                patterns_before - set(verified.patterns_detected)
            )
            if no_commit:
                logger.info("--no-commit set; keeping changes uncommitted.")
            else:
                if _git_commit(start, end, current_accuracy, verified.accuracy):
                    committed_any = True
            current_accuracy = verified.accuracy
            patterns_before = set(verified.patterns_detected)
        else:
            logger.warning(
                "Improvement %+.1f below threshold %+.1f — reverting.",
                gained, min_improvement,
            )
            _git_revert()
            break

        if current_accuracy >= 90.0:
            logger.info(
                "Target accuracy reached: %.1f%% — stopping.",
                current_accuracy,
            )
            break

    _print_report(
        start=start, end=end,
        iterations_run=iterations_run,
        initial_pct=initial.accuracy,
        final_pct=current_accuracy,
        patterns_fixed=patterns_fixed,
        overrides_added=overrides_added,
        committed=committed_any,
    )


if __name__ == "__main__":
    main()
