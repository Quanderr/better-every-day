from strands import tool

from bed.core.models import Verify
from bed.worker.workspace import Workspace, WorkspaceError

READ_TIMEOUT_S = 60
LIST_LIMIT = 500
GREP_LIMIT = 200
READ_CHAR_LIMIT = 20_000
LINE_CHAR_LIMIT = 1_000


def build_tools(ws: Workspace, verify: Verify, writable: bool) -> list:
    def guarded(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except WorkspaceError as e:
                return f"error: {e}"
            except OSError as e:
                return f"error: {e.strerror or e}"
        wrapper.__name__ = fn.__name__
        wrapper.__annotations__ = fn.__annotations__
        wrapper.__wrapped__ = fn
        return wrapper

    @tool(name="read_file", description="Read a text file of the repository with line numbers.")
    @guarded
    def read_file(path: str, start: int = 1, end: int = 400) -> str:
        lines = ws.resolve(path).read_text(errors="replace").splitlines()
        out: list[str] = []
        size = 0
        for i, line in enumerate(lines[start - 1:end], start):
            if len(line) > LINE_CHAR_LIMIT:
                line = line[:LINE_CHAR_LIMIT] + " …[line truncated]"
            entry = f"{i}: {line}"
            if size + len(entry) > READ_CHAR_LIMIT:
                out.append(f"…[output truncated at line {i}; read a narrower range]")
                break
            out.append(entry)
            size += len(entry) + 1
        return "\n".join(out) or "(empty)"

    @tool(name="list_dir", description="List entries of a repository directory.")
    @guarded
    def list_dir(path: str = ".") -> str:
        entries = sorted(ws.resolve(path).iterdir())
        names = [p.name + ("/" if p.is_dir() else "") for p in entries if p.name != ".git"]
        return "\n".join(names[:LIST_LIMIT])

    @tool(name="grep", description="Search tracked and untracked files for a regular expression.")
    @guarded
    def grep(pattern: str, glob: str = "**/*") -> str:
        argv = ["git", "-c", "core.fsmonitor=false", "grep", "-n", "-I", "--untracked", "-E", "-e", pattern,
                "--", f":(glob){glob}"]
        result = ws.exec_readonly(argv, READ_TIMEOUT_S)
        lines = [line[:LINE_CHAR_LIMIT] for line in result.output.splitlines()[:GREP_LIMIT]]
        return "\n".join(lines) or "no matches"

    @tool(name="git_log", description="Show recent commits, optionally for one path.")
    @guarded
    def git_log(path: str = "", n: int = 20) -> str:
        args = ["git", "-c", "core.fsmonitor=false", "log", f"-n{n}", "--format=%h %ad %s", "--date=short"]
        if path:
            ws.resolve(path)
            args += ["--", path]
        return ws.exec_readonly(args, READ_TIMEOUT_S).output or "no commits"

    readonly = [read_file, list_dir, grep, git_log]
    if not writable:
        return readonly

    @tool(name="write_file", description="Create or overwrite a repository file with the given content.")
    @guarded
    def write_file(path: str, content: str) -> str:
        ws.write(path, content)
        return f"wrote {path}"

    @tool(name="replace_in_file", description="Replace one exact, unique occurrence of text in a file.")
    @guarded
    def replace_in_file(path: str, old: str, new: str) -> str:
        target = ws.resolve(path)
        with open(target, encoding="utf-8", newline="") as fh:
            text = fh.read()
        count = text.count(old)
        if count == 0:
            return "error: text not found"
        if count > 1:
            return f"error: text found {count} times; include more context"
        ws.write(path, text.replace(old, new, 1))
        return f"replaced in {path}"

    @tool(name="delete_file", description="Delete a repository file.")
    @guarded
    def delete_file(path: str) -> str:
        ws.delete(path)
        return f"deleted {path}"

    @tool(name="run_verify", description="Run the repository's verify commands and report the result.")
    @guarded
    def run_verify() -> str:
        result = ws.verify(verify)
        return f"exit code: {result.code}\n{result.output[-4000:]}"

    writers = [write_file, replace_in_file, delete_file]
    return [*readonly, *writers, run_verify] if verify.mode == "sandbox" else [*readonly, *writers]
