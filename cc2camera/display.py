"""Copyable commands for Command Prompt on Windows and POSIX shells elsewhere."""
import os
import shlex
import subprocess
import sys


def invocation(*arguments: str) -> str:
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "cc2camera"]
    argv = [*prefix, *map(str, arguments)]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
