import pytest

from bed.core.gates import changed_lines, evaluate
from bed.core.models import Candidate, FileChange, FixResult, Limits, RepoConfig, Verify

FP = "fp0000000000000a"


def change(path, lines=1):
    return FileChange(path=path, content="".join(f"line{i}\n" for i in range(lines)))


def result(**kw):
    cand = Candidate(title="T", category="bug", files=["src/a.py"], issue_kind="k", evidence="e", rationale="r")
    base = dict(candidate=cand, fingerprint=FP, status="fixed", changes=[change("src/a.py")], verified=True)
    return FixResult(**{**base, **kw})


def repo(mode="sandbox", **kw):
    verify = Verify(mode=mode, commands=["true"] if mode == "sandbox" else [])
    base = dict(name="acme/web", vcs="g", tracker="l", tracker_project="WEB", schedule="0 5 * * *",
                timezone="UTC", verify=verify, limits=Limits())
    return RepoConfig(**{**base, **kw})


def base_for(res):
    return {c.path: None for c in res.changes}


@pytest.mark.parametrize(
    "result_kw, repo_kw, known, locked, reason",
    [
        (dict(status="fix_failed"), {}, set(), set(), "not_fixed"),
        (dict(verified=False), {}, set(), set(), "not_verified"),
        (dict(changes=[]), {}, set(), set(), "empty"),
        ({}, {}, {FP}, set(), "duplicate"),
        (dict(changes=[change("../etc/passwd")]), {}, set(), set(), "unsafe_path"),
        (dict(changes=[change("/abs/x.py")]), {}, set(), set(), "unsafe_path"),
        (dict(changes=[change("src/../../x.py")]), {}, set(), set(), "unsafe_path"),
        (dict(changes=[change("gen/generated/a.py")]), dict(exclude_paths=["**/generated/**"]), set(), set(),
         "excluded_path"),
        (dict(changes=[change("infra/main.tf")]), dict(exclude_paths=["infra/**"]), set(), set(), "excluded_path"),
        (dict(changes=[change(".github/workflows/ci.yml")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("pnpm-lock.yaml")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("config/.env.local")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("go.sum")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("Makefile")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("web/package.json")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("pyproject.toml")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("setup.py")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change(".npmrc")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("Jenkinsfile")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change(".gitlab/ci/test.yml")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change(".buildkite/pipeline.yml")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("scripts/deploy.sh")]), dict(protected_paths=["scripts/**"]), set(), set(),
         "protected_path"),
        (dict(changes=[change(".github/workflows/a\nb.yml")]), {}, set(), set(), "unsafe_path"),
        (dict(changes=[change("src/a\tb.py")]), {}, set(), set(), "unsafe_path"),
        (dict(changes=[change("web/npm-shrinkwrap.json")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("bun.lockb")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("CODEOWNERS")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("docs/CODEOWNERS")]), {}, set(), set(), "protected_path"),
        (dict(changes=[change("src/a.py")]), {}, set(), {"src/a.py"}, "overlaps_open_pr"),
        (dict(changes=[change(f"src/{i}.py") for i in range(6)]), {}, set(), set(), "too_many_files"),
        (dict(changes=[change("src/a.py", lines=151)]), {}, set(), set(), "too_many_lines"),
    ],
)
def test_gate_rejects(result_kw, repo_kw, known, locked, reason):
    res = result(**result_kw)
    decision = evaluate(res, repo(**repo_kw), known, locked, base_for(res))
    assert not decision.ok and decision.reason == reason


def test_gate_accepts_small_verified_fix():
    res = result()
    assert evaluate(res, repo(), set(), set(), base_for(res)).ok


def test_ci_mode_accepts_unverified():
    res = result(verified=False)
    assert evaluate(res, repo(mode="ci"), set(), set(), base_for(res)).ok


def test_lines_counted_against_base():
    res = result(changes=[FileChange(path="src/a.py", content="a\nB\nc\n")])
    assert evaluate(res, repo(), set(), set(), {"src/a.py": "a\nb\nc\n"}).ok


def test_deleted_file_counts_its_lines():
    assert changed_lines("a\nb\n", None) == 2


def test_modified_line_counts_two():
    assert changed_lines("a\nb\n", "a\nc\n") == 2


def test_unchanged_counts_zero():
    assert changed_lines("a\n", "a\n") == 0


def test_line_ending_change_counts_every_line():
    assert changed_lines("a\r\nb\r\n", "a\nb\n") == 4


def test_glob_matches_across_newline():
    from bed.core.gates import matches
    assert matches(".github/workflows/a\nb.yml", (".github/**",))


def test_changed_lines_large_crlf_rewrite_fast_and_counted():
    import time
    before = "".join(f"line {i} with some text\r\n" for i in range(3000))
    after = before.replace("\r\n", "\n")
    start = time.monotonic()
    assert changed_lines(before, after) == 6000
    assert time.monotonic() - start < 2
