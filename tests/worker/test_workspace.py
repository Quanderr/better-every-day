import os

import pytest

from bed.core.models import Verify
from bed.worker import shell
from bed.worker.shell import CmdResult
from bed.worker.workspace import Workspace, WorkspaceError, WorkspaceFull


@pytest.fixture
def ws(tmp_path, tmp_bare_repo):
    w = Workspace(tmp_path / "work")
    w.clone(tmp_bare_repo, "secret-token", "main")
    return w


def test_clone_returns_head_sha_and_no_token_in_git_config(tmp_path, tmp_bare_repo):
    w = Workspace(tmp_path / "w2")
    sha = w.clone(tmp_bare_repo, "secret-token", "main")
    assert len(sha) == 40
    assert "secret-token" not in (w.root / ".git/config").read_text()
    assert (w.root / "a.py").exists()


def test_clone_failure_raises(tmp_path):
    with pytest.raises(WorkspaceError):
        Workspace(tmp_path / "w3").clone(f"file://{tmp_path}/missing.git", "t", "main")


def test_exec_env_has_no_token(ws, monkeypatch):
    monkeypatch.setenv("BED_GIT_TOKEN", "leak")
    out = ws.exec("env", 5).output
    assert "BED_GIT_TOKEN" not in out and "CI=1" in out


def test_changes_modified_added_deleted(ws):
    ws.write("a.py", "a = 2\n")
    ws.write("new.py", "n = 1\n")
    ws.delete("gone.py")
    got = sorted((c.path, c.content is None) for c in ws.changes())
    assert got == [("a.py", False), ("gone.py", True), ("new.py", False)]
    assert next(c for c in ws.changes() if c.path == "a.py").content == "a = 2\n"


def test_changes_in_subdirectory(ws):
    ws.write("pkg/m.py", "m\n")
    assert [c.path for c in ws.changes()] == ["pkg/m.py"]


def test_untracked_artifacts_not_changes(ws):
    ws.write("a.py", "a = 2\n")
    (ws.root / "coverage.xml").write_text("<x/>")
    assert [c.path for c in ws.changes()] == ["a.py"]


def test_write_back_to_original_not_a_change(ws):
    ws.write("a.py", "a = 1\n")
    assert ws.changes() == []


def test_binary_change_rejected(ws):
    ws.write("img.bin", "x")
    (ws.root / "img.bin").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(WorkspaceError, match="unsupported change"):
        ws.changes()


def test_large_change_rejected(ws):
    ws.write("big.txt", "x" * 210_000)
    with pytest.raises(WorkspaceError, match="unsupported change"):
        ws.changes()


def test_symlink_change_rejected(ws):
    ws.write("link", "x")
    (ws.root / "link").unlink()
    os.symlink("/etc/passwd", ws.root / "link")
    with pytest.raises(WorkspaceError, match="unsupported change"):
        ws.changes()


def test_ignored_files_not_changes(ws):
    (ws.root / "node_modules").mkdir()
    (ws.root / "node_modules" / "x.js").write_text("x")
    assert ws.changes() == []


def test_reset_restores_tree_keeps_ignored(ws):
    (ws.root / "node_modules").mkdir()
    (ws.root / "node_modules" / "x.js").write_text("x")
    (ws.root / "a.py").write_text("changed\n")
    (ws.root / "new.py").write_text("n\n")
    ws.reset()
    assert (ws.root / "a.py").read_text() == "a = 1\n"
    assert not (ws.root / "new.py").exists()
    assert (ws.root / "node_modules" / "x.js").exists()


def test_resolve_rejects_git_dir(ws):
    with pytest.raises(WorkspaceError):
        ws.resolve(".git/config")
    with pytest.raises(WorkspaceError):
        ws.resolve("sub/../.git/hooks/x")


def test_resolve_rejects_escape(ws):
    with pytest.raises(WorkspaceError):
        ws.resolve("../x")
    with pytest.raises(WorkspaceError):
        ws.resolve("/etc/passwd")
    assert ws.resolve("a.py") == (ws.root / "a.py").resolve()


def test_verify_stops_at_first_failure(ws):
    r = ws.verify(Verify(commands=["echo first; false", "echo second"]))
    assert r.code != 0 and "first" in r.output and "second" not in r.output


def test_verify_all_green(ws):
    r = ws.verify(Verify(commands=["echo one", "echo two"]))
    assert r.code == 0 and "one" in r.output and "two" in r.output


def test_install_failure_raises(ws):
    with pytest.raises(WorkspaceError):
        ws.install(Verify(install="exit 1", commands=["true"]))


def test_install_skipped_when_unset(ws):
    ws.install(Verify(commands=["true"]))


def test_enospc_output_raises_workspace_full(ws):
    with pytest.raises(WorkspaceFull):
        ws.verify(Verify(commands=["echo 'write failed: No space left on device'; exit 1"]))


def test_enospc_oserror_raises_workspace_full(ws, monkeypatch):
    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shell, "run", boom)
    with pytest.raises(WorkspaceFull):
        ws.exec("true", 5)


def test_install_toolchains_skipped_without_version_files(ws, monkeypatch):
    calls = []
    monkeypatch.setattr(shell, "run", lambda *a, **k: calls.append(a) or CmdResult(0, "", False))
    ws.install_toolchains()
    assert calls == []


def test_install_toolchains_runs_mise_with_env(tmp_path, monkeypatch):
    ws = Workspace(tmp_path)
    calls = []

    def fake_run(cmd, cwd, env, timeout_s):
        calls.append((cmd, env, timeout_s))
        return CmdResult(0, "", False)

    (ws.root / ".nvmrc").write_text("20\n")
    monkeypatch.setattr(shell, "run", fake_run)
    ws.install_toolchains()
    cmd, env, timeout = calls[0]
    assert cmd == ["mise", "install"] and timeout == 600
    assert env["MISE_PARANOID"] == "1" and env["MISE_IDIOMATIC_VERSION_FILE"] == "1"
    assert "BED_GIT_TOKEN" not in env
    ws.exec("true", 5)
    assert calls[1][0] == ["mise", "exec", "--", "bash", "-c", "true"]
    exec_env = calls[1][1]
    assert exec_env["MISE_IDIOMATIC_VERSION_FILE"] == env["MISE_IDIOMATIC_VERSION_FILE"]
    assert exec_env["MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS"] == env["MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS"]


def test_install_toolchains_failure_raises(ws, monkeypatch):
    (ws.root / ".nvmrc").write_text("20\n")
    monkeypatch.setattr(shell, "run", lambda *a, **k: CmdResult(1, "boom", False))
    with pytest.raises(WorkspaceError):
        ws.install_toolchains()


def test_usage_counts(ws):
    size, count = ws.usage()
    assert size > 0 and count >= 3


def test_write_keeps_crlf(ws):
    ws.write("w.txt", "a\r\nb\r\n")
    assert (ws.root / "w.txt").read_bytes() == b"a\r\nb\r\n"


def test_large_unchanged_rewrite_not_a_change(tmp_path):
    from conftest import make_bare_repo
    big = "".join(f"line {i}\n" for i in range(5000))
    url = make_bare_repo(tmp_path, {"big.py": big})
    w = Workspace(tmp_path / "wb")
    w.clone(url, "t", "main")
    w.write("big.py", big + "x\n")
    w.write("big.py", big)
    assert w.changes() == []


def test_exec_env_strips_aws_and_bed_vars(ws, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://x")
    monkeypatch.setenv("BED_RESULT_QUEUE_URL", "https://sqs")
    out = ws.exec("env", 5).output
    assert "AWS_ACCESS_KEY_ID" not in out and "AWS_CONTAINER" not in out and "BED_RESULT_QUEUE_URL" not in out


def test_exec_env_is_allowlisted(ws, monkeypatch):
    monkeypatch.setenv("SOME_RUNTIME_SECRET", "x")
    monkeypatch.setenv("MISE_DATA_DIR", "/m")
    out = ws.exec("env", 5).output
    assert "SOME_RUNTIME_SECRET" not in out and "MISE_DATA_DIR=/m" in out and "PATH=" in out and "HOME=" in out


def test_dependency_fingerprint_tracks_lockfiles_and_install_command(tmp_path):
    from conftest import make_bare_repo

    def fingerprint(name, files, install="npm ci"):
        base = tmp_path / name
        base.mkdir()
        w = Workspace(base / "work")
        w.clone(make_bare_repo(base, files), "t", "main")
        return w.dependency_fingerprint(Verify(install=install, commands=["true"]))

    files = {"a.py": "a = 1\n", "web/package-lock.json": "{}\n", ".nvmrc": "22\n"}
    same = fingerprint("one", files)
    assert same == fingerprint("two", {**files, "a.py": "a = 2\n"})
    assert same != fingerprint("three", {**files, "web/package-lock.json": '{"v": 2}\n'})
    assert same != fingerprint("four", {**files, ".nvmrc": "24\n"})
    assert same != fingerprint("five", files, install="npm install")
