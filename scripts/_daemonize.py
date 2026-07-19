#!/usr/bin/env python3
"""Daemonize a command via double-fork so it survives independently of the
launching shell/session (reparented to launchd, new session, no controlling
terminal). Used to run batch Magic jobs to completion without the harness's
background-task lifecycle killing them mid-run.

Usage:
    .venv/bin/python scripts/_daemonize.py <logfile> <cmd> [args...]
"""
import os
import sys

ROOT = "/Users/mrunomi/projects/reclaim-portal-agent"


def main() -> None:
    if len(sys.argv) < 3:
        sys.stderr.write("usage: _daemonize.py <logfile> <cmd> [args...]\n")
        sys.exit(2)
    log = sys.argv[1]
    cmd = sys.argv[2:]
    if os.fork() > 0:
        os._exit(0)          # parent exits -> caller's shell returns immediately
    os.setsid()              # new session (detached from controlling terminal)
    if os.fork() > 0:
        os._exit(0)          # ensure we can't reacquire a terminal
    os.chdir(ROOT)
    with open("/dev/null", "rb") as devnull:
        os.dup2(devnull.fileno(), 0)
    lf = open(log, "ab")
    os.dup2(lf.fileno(), 1)
    os.dup2(lf.fileno(), 2)
    os.execvp(cmd[0], cmd)   # grandchild (PPID 1) becomes the command


if __name__ == "__main__":
    main()
