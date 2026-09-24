"""
Ghost killer — runs at the very start of bot.py before anything else.

If two instances of bot.py are running simultaneously:
  - Telegram throws Conflict errors (two bots polling same token)
  - The DB singleton lock gets confused
  - Trades can double-fire

This module finds and kills any OTHER bot.py process before startup.
Import it as the very first thing in bot.py.
"""

import os
import signal
import logging
import time

log = logging.getLogger("ghost_kill")


def kill_ghosts():
    """Kill any other bot.py processes. Keep only this one (current PID)."""
    my_pid = os.getpid()
    killed = []

    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "python.*bot\\.py"],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.strip().split()
                if p and int(p) != my_pid]

        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
                log.warning(f"Ghost killed: PID {pid}")
            except ProcessLookupError:
                pass  # already gone

        if killed:
            log.warning(f"Killed {len(killed)} ghost instance(s): {killed}")
            time.sleep(3)  # let them finish dying before we take the Telegram token
        else:
            log.info("No ghost instances found")

    except Exception as e:
        # Never let this crash the bot startup
        log.debug(f"Ghost kill check failed (non-fatal): {e}")
