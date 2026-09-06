"""Publish a complete directory without replacing an existing destination."""

from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import Path
import sys
import tempfile


def rename_new(source: Path, destination: Path) -> None:
    # POSIX rename alone can replace an existing empty directory. Use the
    # platform's exclusive rename so a concurrent creator is also protected.
    if sys.platform == "win32":
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(source), os.fsencode(destination), 4)
    else:
        raise OSError(errno.ENOTSUP, "Atomic exclusive directory publication is unavailable")
    rename.restype = ctypes.c_int
    if rename(*args) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


@contextmanager
def staged_directory(destination: Path):
    """Yield a private staging directory, then publish it only on success."""
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, "Output already exists", str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cc2flash-build-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        yield staging
        rename_new(staging, destination)
