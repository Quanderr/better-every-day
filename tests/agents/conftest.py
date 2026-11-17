import os
import subprocess

import pytest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


def make_bare_repo(tmp_path, files):
    src = tmp_path / "src"
    src.mkdir()
    git("init", "-q", "-b", "main", cwd=src)
    for path, content in files.items():
        p = src / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git("add", "-A", cwd=src)
    git("commit", "-q", "-m", "init", cwd=src)
    bare = tmp_path / "bare.git"
    git("clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}"


DEFAULT_FILES = {"a.py": "a = 1\n", "gone.py": "x = 1\n", ".gitignore": "node_modules/\n"}


@pytest.fixture
def tmp_bare_repo(tmp_path):
    return make_bare_repo(tmp_path, DEFAULT_FILES)


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
