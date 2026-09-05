"""Ownership checks for process tests, including Windows venv redirectors."""
import psutil


def assert_owned_pid(case, process, pid):
    root = psutil.Process(process.pid)
    owned = {root.pid, *(child.pid for child in root.children(recursive=True))}
    case.assertIn(pid, owned, "runtime identity must belong to this test's launch")


def stop_owned_process(process):
    try:
        root = psutil.Process(process.pid)
        owned = root.children(recursive=True) + [root]
    except psutil.NoSuchProcess:
        owned = []
    for child in owned:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(owned, timeout=10)
    process.wait(timeout=10)
    if alive:
        raise AssertionError(f"test children survived: {[p.pid for p in alive]}")
