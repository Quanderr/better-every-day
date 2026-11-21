import contextlib
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from bed.core.ports import CacheStore

log = logging.getLogger(__name__)
CACHE_PATHS = (
    ".local/share/mise", ".cache", ".npm", ".local/share/pnpm", ".yarn/berry/cache", ".bun/install/cache",
    ".m2/repository", ".gradle/caches", ".gradle/wrapper", "go/pkg/mod", ".cargo/registry",
)
MAX_AGE_S = 7 * 24 * 3600
MAX_ARCHIVE_BYTES = 1024 ** 3
COPY_CHUNK = 1024 * 1024
STAGING_DIR = ".bed-cache-restore"


class CacheError(Exception):
    pass


def _spawn(argv: list[str], stdin, stdout) -> subprocess.Popen:
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    return subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL, env=env)


def _stop(*procs: subprocess.Popen) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()


class _Archive:
    def __init__(self, home: Path, paths: list[str], limit: int):
        self._tar = _spawn(["tar", "-C", str(home), "-cf", "-", *paths], subprocess.DEVNULL, subprocess.PIPE)
        try:
            self._zstd = _spawn(["zstd", "-q", "-T0", "-c"], self._tar.stdout, subprocess.PIPE)
        except BaseException:
            _stop(self._tar)
            raise
        self._tar.stdout.close()
        self._limit = limit
        self.sent = 0

    def read(self, size: int = -1) -> bytes:
        data = self._zstd.stdout.read(size)
        self.sent += len(data)
        if self.sent > self._limit:
            raise CacheError(f"archive exceeds {self._limit} bytes")
        if size != 0 and not data and (self._tar.wait() or self._zstd.wait()):
            raise CacheError("archive creation failed")
        return data

    def close(self) -> None:
        _stop(self._tar, self._zstd)


def _unpack(stream: BinaryIO, target: Path) -> None:
    zstd = _spawn(["zstd", "-dqc"], subprocess.PIPE, subprocess.PIPE)
    try:
        tar = _spawn(["tar", "-C", str(target), "-xf", "-"], zstd.stdout, subprocess.DEVNULL)
    except BaseException:
        _stop(zstd)
        raise
    zstd.stdout.close()
    try:
        try:
            shutil.copyfileobj(stream, zstd.stdin, COPY_CHUNK)
        finally:
            with contextlib.suppress(BrokenPipeError):
                zstd.stdin.close()
        if zstd.wait() or tar.wait():
            raise CacheError("archive extraction failed")
    finally:
        _stop(zstd, tar)


def _is_plain_dir(root: Path, relative: str) -> bool:
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return False
    return True


def _extract(stream: BinaryIO, home: Path) -> None:
    staging = home / STAGING_DIR
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        _unpack(stream, staging)
        for path in CACHE_PATHS:
            if not _is_plain_dir(staging, path):
                continue
            target = home / path
            shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(staging / path, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


class NoCache:
    def restore(self, fingerprint: Callable[[], str]) -> None:
        return None

    def save(self) -> None:
        return None

    def discard(self) -> None:
        return None


class DependencyCache:
    def __init__(self, store: CacheStore, home: Path, now: Callable[[], float] = time.time,
                 max_bytes: int = MAX_ARCHIVE_BYTES):
        self.store = store
        self.home = Path(home)
        self.now = now
        self.max_bytes = max_bytes
        self._wanted: str | None = None
        self._restored: str | None = None
        self._created: float | None = None

    def restore(self, fingerprint: Callable[[], str]) -> None:
        try:
            self._wanted = fingerprint()
            found = self.store.open()
            if found is None:
                log.info("dependency cache miss")
                return
            metadata, stream = found
            try:
                created = float(metadata.get("created", 0))
                if not 0 <= self.now() - created <= MAX_AGE_S:
                    log.info("dependency cache expired; rebuilding from scratch")
                    return
                started = time.monotonic()
                _extract(stream, self.home)
            finally:
                stream.close()
            self._restored = metadata.get("fingerprint", "")
            self._created = created
            log.info("dependency cache restored in %.1fs; exact=%s", time.monotonic() - started,
                     self._restored == self._wanted)
        except Exception:
            log.exception("dependency cache restore failed; installing from scratch")
            self._wipe()

    def save(self) -> None:
        if self._wanted is None or self._restored == self._wanted:
            return
        paths = [p for p in CACHE_PATHS if (self.home / p).is_dir()]
        if not paths:
            return
        created = self._created if self._created is not None else self.now()
        metadata = {"fingerprint": self._wanted, "created": str(int(created))}
        started = time.monotonic()
        archive = None
        try:
            archive = _Archive(self.home, paths, self.max_bytes)
            self.store.upload(archive, metadata)
            log.info("dependency cache saved: bytes=%d in %.1fs", archive.sent, time.monotonic() - started)
        except Exception:
            log.exception("dependency cache save failed")
        finally:
            if archive is not None:
                archive.close()

    def discard(self) -> None:
        if self._created is None:
            return
        self._wanted = None
        try:
            self.store.delete()
            log.warning("dependency cache discarded after a failed setup")
        except Exception:
            log.exception("dependency cache discard failed")

    def _wipe(self) -> None:
        self._restored = None
        self._created = None
        for path in CACHE_PATHS:
            shutil.rmtree(self.home / path, ignore_errors=True)
