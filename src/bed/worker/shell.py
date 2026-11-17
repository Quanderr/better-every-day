import codecs
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

OUTPUT_TAIL = 20_000
CONTAINER_INIT_PID = 1
ZOMBIE_WAIT_S = 1.0


@dataclass(frozen=True)
class CmdResult:
    code: int
    output: str
    timed_out: bool


def run(cmd: list[str] | str, cwd: Path, env: dict[str, str], timeout_s: int) -> CmdResult:
    argv = ["bash", "-lc", cmd] if isinstance(cmd, str) else cmd
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    buffer: list[str] = [""]

    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def drain() -> None:
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            buffer[0] = (buffer[0] + decoder.decode(chunk))[-OUTPUT_TAIL:]
        buffer[0] = (buffer[0] + decoder.decode(b"", final=True))[-OUTPUT_TAIL:]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
    _kill_group(proc.pid)
    proc.wait()
    reader.join(timeout=5)
    reap_strays(os.getpid(), os.getuid(), _list_procs, _kill, _collect_zombies)
    return CmdResult(code=proc.returncode if not timed_out else -signal.SIGKILL, output=buffer[0],
                     timed_out=timed_out)


def reap_strays(self_pid: int, uid: int, list_procs: Callable[[], Iterable[tuple[int, int]]],
                kill: Callable[[int], None], collect: Callable[[], None]) -> None:
    if self_pid != CONTAINER_INIT_PID:
        return
    for pid, owner in list_procs():
        if pid != self_pid and owner == uid:
            kill(pid)
    collect()


def _collect_zombies() -> None:
    deadline = time.monotonic() + ZOMBIE_WAIT_S
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            time.sleep(0.02)


def _list_procs() -> list[tuple[int, int]]:
    procs = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return procs
    for entry in entries:
        if entry.isdigit():
            try:
                procs.append((int(entry), os.stat(f"/proc/{entry}").st_uid))
            except OSError:
                continue
    return procs


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
