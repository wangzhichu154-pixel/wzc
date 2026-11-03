"""Minimal Linux subprocess resource boundary, run outside the Celery process.

No shell, user command, environment credentials or dynamically imported plugin is
passed to the market-data parser. Container/user isolation remains necessary.
"""
import os
import resource
import ctypes
import signal
import sys


def main():
    if len(sys.argv) < 7:
        raise SystemExit(64)
    memory, cpu, output, parent_pid = map(int, sys.argv[1:5])
    if memory <= 0 or cpu <= 0 or output <= 0 or parent_pid <= 1:
        raise SystemExit(64)
    # SIGKILL follows worker death across exec. The expected PID comes from the
    # actual spawning worker, catching death before this wrapper even starts.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "Cannot establish worker-death protection")
    if os.getppid() != parent_pid:
        raise SystemExit(70)
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output, output))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.execve(sys.argv[5], sys.argv[5:], {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})


if __name__ == "__main__":
    main()
