import difflib
import re
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel

from bed.core.fingerprint import normalize_path
from bed.core.models import FixResult, RepoConfig

PROTECTED_PATHS: tuple[str, ...] = (
    ".github/**",
    ".gitlab-ci.yml",
    ".circleci/**",
    "**/*.lock",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/.env*",
    "**/*.pem",
    "**/*.key",
    "**/go.sum",
    "**/npm-shrinkwrap.json",
    "**/bun.lockb",
    "**/bun.lock",
    "**/CODEOWNERS",
    "**/Makefile",
    "**/GNUmakefile",
    "**/*.mk",
    "**/package.json",
    "**/pyproject.toml",
    "**/setup.py",
    "**/setup.cfg",
    "**/tox.ini",
    "**/noxfile.py",
    "**/.npmrc",
    "**/.yarnrc",
    "**/.yarnrc.yml",
    "**/.pypirc",
    "**/Jenkinsfile",
    ".gitlab/**",
    "**/*.gitlab-ci.yml",
    ".buildkite/**",
    ".drone.yml",
    ".travis.yml",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    ".husky/**",
    ".pre-commit-config.yaml",
)

GateReason = Literal[
    "not_fixed",
    "not_verified",
    "empty",
    "duplicate",
    "unsafe_path",
    "excluded_path",
    "protected_path",
    "overlaps_open_pr",
    "too_many_files",
    "too_many_lines",
]


class GateDecision(BaseModel):
    ok: bool
    reason: GateReason | None = None
    detail: str = ""


def _accept() -> GateDecision:
    return GateDecision(ok=True)


def _reject(reason: GateReason, detail: str = "") -> GateDecision:
    return GateDecision(ok=False, reason=reason, detail=detail)


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def matches(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)


def is_safe_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return False
    parts = PurePosixPath(path).parts
    return ".." not in parts and ".git" not in parts


def changed_lines(before: str | None, after: str | None) -> int:
    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    opcodes = difflib.SequenceMatcher(None, a, b).get_opcodes()
    return sum((i2 - i1) + (j2 - j1) for tag, i1, i2, j1, j2 in opcodes if tag != "equal")


def evaluate(
    result: FixResult,
    repo: RepoConfig,
    known_fingerprints: set[str],
    locked_paths: set[str],
    base_contents: dict[str, str | None],
) -> GateDecision:
    if result.status != "fixed":
        return _reject("not_fixed", result.status)
    if repo.verify.mode != "ci" and not result.verified:
        return _reject("not_verified")
    if not result.changes:
        return _reject("empty")
    if result.fingerprint in known_fingerprints:
        return _reject("duplicate", result.fingerprint)
    unsafe = [c.path for c in result.changes if not is_safe_path(c.path)]
    if unsafe:
        return _reject("unsafe_path", ", ".join(unsafe))
    paths = [normalize_path(c.path) for c in result.changes]
    excluded = [p for p in paths if matches(p, repo.exclude_paths)]
    if excluded:
        return _reject("excluded_path", ", ".join(excluded))
    protected = [p for p in paths if matches(p, PROTECTED_PATHS) or matches(p, repo.protected_paths)]
    if protected:
        return _reject("protected_path", ", ".join(protected))
    overlapping = [p for p in paths if p in locked_paths]
    if overlapping:
        return _reject("overlaps_open_pr", ", ".join(overlapping))
    if len(paths) > repo.limits.max_files:
        return _reject("too_many_files", f"{len(paths)} > {repo.limits.max_files}")
    lines = sum(changed_lines(base_contents.get(c.path), c.content) for c in result.changes)
    if lines > repo.limits.max_diff_lines:
        return _reject("too_many_lines", f"{lines} > {repo.limits.max_diff_lines}")
    return _accept()
