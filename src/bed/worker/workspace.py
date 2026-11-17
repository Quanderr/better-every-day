import errno
import os
import shutil
from pathlib import Path

from bed.core.models import FileChange, Verify
from bed.worker import shell
from bed.worker.shell import CmdResult

TOKEN_ENV = "BED_GIT_TOKEN"
PASSED_ENV = frozenset({
    "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
})
PASSED_ENV_PREFIXES = ("MISE_",)
CREDENTIAL_HELPER = f"!f() {{ echo username=x-access-token; echo password=${TOKEN_ENV}; }}; f"
VERSION_FILES = (
    ".tool-versions", "mise.toml", ".mise.toml", ".nvmrc", ".node-version", ".python-version", ".ruby-version",
    ".java-version", "go.mod",
)
MISE_TIMEOUT_S = 600
INSTALL_TIMEOUT_S = 1200
VERIFY_TIMEOUT_S = 1200
GIT_TIMEOUT_S = 600
MAX_CHANGE_BYTES = 200_000
FULL_MARKERS = ("ENOSPC", "No space left on device")
GIT_SAFE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


class WorkspaceFull(Exception):
    pass


class WorkspaceError(Exception):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._mise = False
        self._touched: set[str] = set()

    def _base_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in PASSED_ENV or k.startswith(PASSED_ENV_PREFIXES)}
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _run(self, cmd: list[str] | str, timeout_s: int, env: dict[str, str] | None = None,
             cwd: Path | None = None) -> CmdResult:
        try:
            result = shell.run(cmd, cwd or self.root, env or self._base_env(), timeout_s)
        except OSError as e:
            if e.errno in (errno.ENOSPC, errno.EDQUOT):
                raise WorkspaceFull(str(e)) from e
            raise WorkspaceError(str(e)) from e
        if result.code != 0 and any(m in result.output for m in FULL_MARKERS):
            raise WorkspaceFull(result.output[-2000:])
        return result

    def _git(self, *args: str) -> CmdResult:
        return self._run(["git", *GIT_SAFE, *args], GIT_TIMEOUT_S)

    def clone(self, url: str, token: str, branch: str) -> str:
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        env = {**self._base_env(), TOKEN_ENV: token}
        cmd = ["git", "-c", "credential.helper=", "-c", f"credential.helper={CREDENTIAL_HELPER}", "clone",
               "--depth", "20", "--branch", branch, url, "."]
        result = self._run(cmd, GIT_TIMEOUT_S, env=env)
        if result.code != 0:
            raise WorkspaceError(f"clone failed: {result.output[-2000:]}")
        head = self._git("rev-parse", "HEAD")
        if head.code != 0:
            raise WorkspaceError(f"rev-parse failed: {head.output}")
        return head.output.strip()

    def install_toolchains(self) -> None:
        if not any((self.root / f).exists() for f in VERSION_FILES):
            return
        env = {
            **self._base_env(),
            "MISE_PARANOID": "1",
            "MISE_IDIOMATIC_VERSION_FILE": "1",
            "MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS": "node,python,ruby,java,go",
            "MISE_TRUSTED_CONFIG_PATHS": str(self.root),
            "MISE_PYTHON_COMPILE": "0",
            "MISE_YES": "1",
        }
        result = self._run(["mise", "install"], MISE_TIMEOUT_S, env=env)
        if result.code != 0:
            raise WorkspaceError(f"mise install failed: {result.output[-2000:]}")
        self._mise = True

    def exec(self, command: str, timeout_s: int) -> CmdResult:
        env = {**self._base_env(), "NODE_ENV": "test", "CI": "1"}
        if self._mise:
            env["MISE_TRUSTED_CONFIG_PATHS"] = str(self.root)
            return self._run(["mise", "exec", "--", "bash", "-lc", command], timeout_s, env=env)
        return self._run(command, timeout_s, env=env)

    def exec_readonly(self, argv: list[str], timeout_s: int) -> CmdResult:
        return self._run(argv, timeout_s)

    def install(self, verify: Verify) -> None:
        if not verify.install:
            return
        result = self.exec(verify.install, INSTALL_TIMEOUT_S)
        if result.code != 0:
            raise WorkspaceError(f"install failed: {result.output[-2000:]}")

    def verify(self, verify: Verify) -> CmdResult:
        outputs: list[str] = []
        last = CmdResult(0, "", False)
        for command in verify.commands:
            last = self.exec(command, VERIFY_TIMEOUT_S)
            outputs.append(f"$ {command}\n{last.output}")
            if last.code != 0:
                break
        return CmdResult(last.code, "\n".join(outputs)[-shell.OUTPUT_TAIL:], last.timed_out)

    def write(self, path: str, content: str) -> None:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        self._touched.add(self._relative(target))

    def delete(self, path: str) -> None:
        target = self.resolve(path)
        target.unlink()
        self._touched.add(self._relative(target))

    def _relative(self, target: Path) -> str:
        return target.relative_to(self.root.resolve()).as_posix()

    def changes(self) -> list[FileChange]:
        out = []
        for path in sorted(self._touched):
            full = self.root / path
            if not full.exists() and not full.is_symlink():
                if self._git("cat-file", "-e", f"HEAD:{path}").code == 0:
                    out.append(FileChange(path=path, content=None))
                continue
            content = self._read_change(path)
            tracked = self._git("cat-file", "-e", f"HEAD:{path}").code == 0
            if tracked and self._git("diff", "--quiet", "HEAD", "--", path).code == 0:
                continue
            out.append(FileChange(path=path, content=content))
        return out

    def _read_change(self, path: str) -> str:
        full = self.root / path
        try:
            if full.is_symlink() or not full.is_file() or full.stat().st_size > MAX_CHANGE_BYTES:
                raise WorkspaceError(f"unsupported change: {path}")
            return full.read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            raise WorkspaceError(f"unsupported change: {path}") from e

    def reset(self) -> None:
        self._touched.clear()
        for args in (("reset", "--hard", "-q"), ("clean", "-fdq")):
            result = self._git(*args)
            if result.code != 0:
                raise WorkspaceError(f"git {args[0]} failed: {result.output}")

    def resolve(self, path: str) -> Path:
        root = self.root.resolve()
        full = (root / path).resolve()
        if full != root and root not in full.parents:
            raise WorkspaceError(f"path outside workspace: {path}")
        if ".git" in full.relative_to(root).parts:
            raise WorkspaceError(f"path inside .git is not allowed: {path}")
        return full

    def usage(self) -> tuple[int, int]:
        size = 0
        count = 0
        for dirpath, _, filenames in os.walk(self.root):
            for name in filenames:
                try:
                    size += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    continue
                count += 1
        return size, count

