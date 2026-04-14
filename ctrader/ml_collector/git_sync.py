"""
One-shot daily git sync: pull --rebase, add ml_data_clean, commit, push.

Triggered by glitch-ml-gitsync.timer at 00:05 UTC. Exit codes:
  0 — success, or nothing to commit
  1 — repo missing or misconfigured
  2 — pull/rebase conflict (human intervention required)
  3 — push failed after retries
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import Config, configure_logging, get_config

logger = logging.getLogger("ml_collector.git_sync")


def _run(repo: Path, args: List[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo)] + args
    logger.debug("$ %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        capture_output=capture,
    )


def main() -> int:
    cfg: Config = get_config()
    configure_logging(cfg)

    repo = cfg.ml_repo_path
    if not (repo / ".git").exists():
        logger.error("Repo path %s is not a git clone. Run bootstrap first.", repo)
        return 1

    data_dir = repo / cfg.ml_data_subdir
    if not data_dir.exists():
        logger.info("No %s dir yet — nothing to sync", data_dir)
        return 0

    # 1. Make sure we're on main
    try:
        _run(repo, ["checkout", "main"])
    except subprocess.CalledProcessError:
        logger.exception("git checkout main failed")
        return 1

    # 2. Pull with rebase — abort on conflict, don't risk a force push
    try:
        _run(repo, ["pull", "--rebase", "--autostash", "origin", "main"])
    except subprocess.CalledProcessError:
        logger.error(
            "git pull --rebase failed — likely a conflict. "
            "Inspect manually: git -C %s status", repo,
        )
        # Best-effort cleanup of any partial rebase state
        _run(repo, ["rebase", "--abort"], check=False)
        return 2

    # 3. Stage only the clean data subdir — explicit path, never `-A`
    try:
        _run(repo, ["add", "--", cfg.ml_data_subdir])
    except subprocess.CalledProcessError:
        logger.exception("git add failed")
        return 1

    # 4. Noop if nothing changed (weekends when markets are closed)
    diff = _run(repo, ["diff", "--cached", "--name-only"], capture=True)
    if not (diff.stdout or "").strip():
        logger.info("Nothing to commit — noop")
        return 0

    # 5. Commit with bot identity (scoped to this repo via -c, not global)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msg = f"ml_data_clean: daily sync {date}"
    try:
        _run(
            repo,
            [
                "-c", f"user.email={cfg.git_user_email}",
                "-c", f"user.name={cfg.git_user_name}",
                "commit", "-m", msg,
            ],
        )
    except subprocess.CalledProcessError:
        logger.exception("git commit failed")
        return 1

    # 6. Push with 3 retries and backoff
    last_err: Optional[subprocess.CalledProcessError] = None
    for attempt in range(3):
        try:
            _run(repo, ["push", "origin", "main"])
            logger.info("Push OK on attempt %d", attempt + 1)
            return 0
        except subprocess.CalledProcessError as e:
            last_err = e
            wait = 10 * (attempt + 1)
            logger.warning(
                "Push attempt %d/3 failed: %s — sleeping %ds",
                attempt + 1, e, wait,
            )
            time.sleep(wait)

    logger.error("All 3 push attempts failed: %s", last_err)
    return 3


if __name__ == "__main__":
    sys.exit(main())
