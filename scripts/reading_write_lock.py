#!/usr/bin/env python3
"""Hold the shared Read-X/Base writer lock until the caller closes stdin."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat
import sys


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    lock_path = Path(sys.argv[1])
    parent = lock_path.parent
    if parent.is_symlink() or not parent.is_dir() or stat.S_IMODE(parent.stat().st_mode) & 0o077:
        return 2
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
            return 2
        fcntl.flock(fd, fcntl.LOCK_EX)
        sys.stdout.write("locked\n")
        sys.stdout.flush()
        sys.stdin.buffer.read()
        return 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
