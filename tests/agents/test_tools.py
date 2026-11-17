import pytest

from bed.agents.tools import build_tools
from bed.core.models import Verify
from bed.worker.workspace import Workspace


@pytest.fixture
def ws(tmp_path, tmp_bare_repo):
    w = Workspace(tmp_path / "work")
    w.clone(tmp_bare_repo, "t", "main")
    return w


def tools_for(ws, writable=True, commands=("test -f ok.txt",)):
    return {t.tool_name: t for t in build_tools(ws, Verify(commands=list(commands)), writable)}


def test_read_file_numbers_lines(ws):
    assert "1: a = 1" in tools_for(ws)["read_file"]("a.py")


def test_read_file_outside_root_returns_error(ws):
    assert "outside" in tools_for(ws)["read_file"]("../x")


def test_read_missing_returns_error(ws):
    assert "error" in tools_for(ws)["read_file"]("nope.py").lower()


def test_list_dir_and_grep(ws):
    t = tools_for(ws)
    assert "a.py" in t["list_dir"](".") and ".git" not in t["list_dir"](".").split()
    assert "a.py:1" in t["grep"]("a = 1")


def test_git_log(ws):
    assert "init" in tools_for(ws)["git_log"]()


def test_write_tools_absent_when_readonly(ws):
    names = set(tools_for(ws, writable=False))
    assert names == {"read_file", "list_dir", "grep", "git_log"}


def test_write_file_creates(ws):
    tools_for(ws)["write_file"]("pkg/new.py", "x = 1\n")
    assert (ws.root / "pkg/new.py").read_text() == "x = 1\n"


def test_write_file_outside_returns_error(ws):
    assert "outside" in tools_for(ws)["write_file"]("../evil.py", "x")


def test_run_verify_reports_exit_code(ws):
    t = tools_for(ws)
    assert "exit code: 1" in t["run_verify"]()
    t["write_file"]("ok.txt", "")
    assert "exit code: 0" in t["run_verify"]()


def test_replace_in_file_requires_unique_match(ws):
    t = tools_for(ws)
    t["write_file"]("d.py", "x\nx\n")
    assert "2 times" in t["replace_in_file"]("d.py", "x", "y")
    assert "not found" in t["replace_in_file"]("d.py", "zzz", "y")
    t["replace_in_file"]("a.py", "a = 1", "a = 2")
    assert (ws.root / "a.py").read_text() == "a = 2\n"


def test_ci_mode_has_no_run_verify(ws):
    names = {t.tool_name for t in build_tools(ws, Verify(mode="ci"), True)}
    assert "write_file" in names and "run_verify" not in names


def test_git_dir_not_writable(ws):
    t = tools_for(ws)
    assert "error" in t["write_file"](".git/hooks/pre-commit", "x")
    assert "error" in t["read_file"](".git/config")


def test_grep_pattern_not_parsed_as_option(ws):
    marker = ws.root.parent / "pwned"
    tools_for(ws)["grep"](f"-Otouch {marker}")
    assert not marker.exists()


def test_delete_file_tool(ws):
    t = tools_for(ws)
    t["delete_file"]("gone.py")
    assert [(c.path, c.content) for c in ws.changes()] == [("gone.py", None)]


def test_tool_writes_tracked_as_changes(ws):
    tools_for(ws)["write_file"]("new.py", "n\n")
    (ws.root / "junit.xml").write_text("<x/>")
    assert [c.path for c in ws.changes()] == ["new.py"]


def test_replace_preserves_crlf(ws):
    (ws.root / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    tools_for(ws)["replace_in_file"]("w.txt", "two", "TWO")
    assert (ws.root / "w.txt").read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_read_file_output_capped(ws):
    (ws.root / "min.js").write_text("x" * 3_000_000 + "\n" + "y\n" * 50_000)
    out = tools_for(ws)["read_file"]("min.js")
    assert len(out) <= 25_000 and "truncated" in out
