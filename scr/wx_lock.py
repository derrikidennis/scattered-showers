"""
wx_lock.py : a tiny lock file, so two scripts never write the same file at the same moment.
Used by wx_alerts.py and build_dashboard.py. A lock left behind by a crash is taken over after `stale` seconds.
"""
import os, time
from contextlib import contextmanager
from pathlib import Path

@contextmanager
def file_lock(path, wait=120, stale=300):
    """Yields True when the lock is held, False if it gave up waiting (the caller decides what to do)."""
    got = False
    end = time.time() + wait
    try:
        Path(str(path)).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            got = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(str(path)) > stale:
                    os.remove(str(path))
                    continue
            except OSError:
                pass
            if time.time() >= end:
                break
            time.sleep(1)
        except OSError:
            break
    try:
        yield got
    finally:
        if got:
            try:
                os.remove(str(path))
            except OSError:
                pass
