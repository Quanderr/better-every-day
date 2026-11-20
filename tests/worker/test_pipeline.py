import itertools

import pytest
from conftest import make_bare_repo
from helpers import example_config, make_candidate

from bed.agents.runner import FixerOutput
from bed.core.fingerprint import fingerprint
from bed.core.models import Limits, OutcomeDigest, Verify, WorkerInput
from bed.testing.fakes import FakeAgentRunner
from bed.worker.pipeline import run_worker
from bed.worker.workspace import Workspace, WorkspaceFull

FILES = {"ok.txt": "", "src/a.py": "a = 1\n", ".gitignore": "node_modules/\n"}
CHECK = "test -f ok.txt && ! test -f bad.txt"


@pytest.fixture
def url(tmp_path):
    return make_bare_repo(tmp_path, FILES)


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "work")


def inp(url, commands=(CHECK,), mode="sandbox", known=(), locked=(), outcomes=(), categories=None, **limits):
    cfg = example_config()
    verify = Verify(mode=mode, commands=list(commands) if mode == "sandbox" else [])
    update = {"verify": verify, "limits": Limits(**limits)}
    if categories:
        update["categories"] = categories
    repo = cfg.repo("acme/web").model_copy(update=update)
    return WorkerInput(run_id="r1", repo=repo, clone_url=url, read_token="t", model=cfg.model, nonce="n" * 32,
                       known_fingerprints=list(known), locked_paths=list(locked), recent_outcomes=list(outcomes))


def clock():
    return 0.0


def run(*args):
    return run_worker(*args, lambda checkpoint: None)


def good(name):
    def edit(ws):
        ws.write(f"src/{name}.py", f"{name} = 1\n")
        return FixerOutput(status="fixed", summary=f"fixed {name}")
    return edit


def bad(ws):
    (ws.root / "bad.txt").write_text("x")
    return FixerOutput(status="fixed", summary="broke it")


def cands(*names):
    return [make_candidate(title=n, symbol=n, files=[f"src/{n}.py"]) for n in names]


def test_baseline_red_stops_before_scout(url, ws):
    agents = FakeAgentRunner(cands("a"))
    r = run(inp(url, commands=["echo broken; false"]), ws, agents, clock)
    assert r.status == "baseline_red" and agents.scout_calls == 0 and "broken" in r.error


def test_fixed_result_carries_changes_and_fingerprint(url, ws):
    c = cands("x")
    agents = FakeAgentRunner(c, {"x": good("x")}, tokens_per_call=10)
    r = run(inp(url), ws, agents, clock)
    assert r.status == "ok" and len(r.base_sha) == 40 and r.tokens_used == 20
    res = r.results[0]
    assert res.status == "fixed" and res.verified and res.fingerprint == fingerprint(c[0])
    assert [(ch.path, ch.content) for ch in res.changes] == [("src/x.py", "x = 1\n")]
    assert res.summary == "fixed x"


def test_unverified_fix_is_fix_failed(url, ws):
    r = run(inp(url), ws, FakeAgentRunner(cands("x"), {"x": bad}), clock)
    assert r.results[0].status == "fix_failed" and not r.results[0].verified


def test_abandoned_recorded(url, ws):
    r = run(inp(url), ws, FakeAgentRunner(cands("x")), clock)
    assert r.results[0].status == "abandoned" and r.results[0].changes == []


def test_known_fingerprint_skipped(url, ws):
    c = cands("x", "y")
    agents = FakeAgentRunner(c, {"x": good("x"), "y": good("y")})
    r = run(inp(url, known=[fingerprint(c[0])]), ws, agents, clock)
    assert agents.fix_calls == ["y"] and len(r.results) == 1


def test_locked_path_skipped(url, ws):
    agents = FakeAgentRunner(cands("x", "y"), {"x": good("x"), "y": good("y")})
    run(inp(url, locked=["src/x.py"]), ws, agents, clock)
    assert agents.fix_calls == ["y"]


def test_disabled_category_skipped(url, ws):
    c = [make_candidate(title="d", category="docs", files=["README.md"])]
    agents = FakeAgentRunner(c, {})
    run(inp(url, categories=["bug"]), ws, agents, clock)
    assert agents.fix_calls == []


def test_stops_at_max_prs_per_run(url, ws):
    names = ["a1", "a2", "a3", "a4", "a5"]
    agents = FakeAgentRunner(cands(*names), {n: good(n) for n in names})
    r = run(inp(url), ws, agents, clock)
    assert len(agents.fix_calls) == 2 and [x.status for x in r.results] == ["fixed", "fixed"]


def test_attempt_cap(url, ws):
    names = ["a1", "a2", "a3", "a4", "a5", "a6"]
    agents = FakeAgentRunner(cands(*names), {n: bad for n in names})
    run(inp(url), ws, agents, clock)
    assert len(agents.fix_calls) == 4


def test_workspace_reset_between_candidates(url, ws):
    agents = FakeAgentRunner(cands("x", "y"), {"x": good("x"), "y": good("y")})
    r = run(inp(url), ws, agents, clock)
    assert [c.path for c in r.results[1].changes] == ["src/y.py"]


def test_token_budget_stops_loop(url, ws):
    agents = FakeAgentRunner(cands("x", "y"), {"x": bad, "y": good("y")}, tokens_per_call=600)
    r = run(inp(url, token_budget=1000), ws, agents, clock)
    assert agents.fix_calls == ["x"] and r.status == "ok"


def test_time_limit_stops_loop(url, ws):
    ticks = itertools.count(0, 2000)
    agents = FakeAgentRunner(cands("x", "y", "z"), {"x": bad, "y": bad, "z": bad})
    r = run(inp(url, timeout_minutes=60), ws, agents, lambda: float(next(ticks)))
    assert r.status == "ok" and agents.fix_calls == ["x"]


def test_ci_mode_skips_install_and_verify(url, ws):
    agents = FakeAgentRunner(cands("x"), {"x": bad})
    r = run(inp(url, mode="ci"), ws, agents, clock)
    assert r.results[0].verified is False and r.results[0].status == "fixed"


def test_workspace_full_status(url, ws, monkeypatch):
    monkeypatch.setattr(Workspace, "install_toolchains", lambda self: (_ for _ in ()).throw(WorkspaceFull("x")))
    r = run(inp(url), ws, FakeAgentRunner([]), clock)
    assert r.status == "workspace_full"


def test_workspace_error_in_setup_failed(tmp_path, ws):
    r = run(inp(f"file://{tmp_path}/missing.git"), ws, FakeAgentRunner([]), clock)
    assert r.status == "failed" and "clone failed" in r.error


def test_unexpected_exception_returns_failed(url, ws):
    r = run(inp(url), ws, FakeAgentRunner([], scout_error=ValueError("boom")), clock)
    assert r.status == "failed" and "boom" in r.error


def test_fix_exception_marks_candidate_fix_failed(url, ws):
    def explode(ws):
        raise RuntimeError("throttled")
    agents = FakeAgentRunner(cands("x", "y"), {"x": explode, "y": good("y")})
    r = run(inp(url), ws, agents, clock)
    assert [x.status for x in r.results] == ["fix_failed", "fixed"] and "throttled" in r.results[0].verify_log


def test_binary_change_is_fix_failed(url, ws):
    def binary(ws):
        ws.write("img.bin", "x")
        (ws.root / "img.bin").write_bytes(b"\xff\xfe")
        return FixerOutput(status="fixed", summary="s")
    r = run(inp(url), ws, FakeAgentRunner(cands("x"), {"x": binary}), clock)
    assert r.results[0].status == "fix_failed"


def test_lessons_only_with_outcomes(url, ws):
    agents = FakeAgentRunner([], lessons_text="- small")
    assert run(inp(url), ws, agents, clock).lessons is None and agents.lessons_calls == []
    outcome = OutcomeDigest(title="t", category="bug", state="merged")
    r = run(inp(url, outcomes=[outcome]), ws, agents, clock)
    assert r.lessons == "- small"


def test_no_candidates_ok(url, ws):
    r = run(inp(url), ws, FakeAgentRunner([]), clock)
    assert r.status == "ok" and r.results == []


def test_reset_failure_keeps_results_and_stops(url, ws, monkeypatch):
    from bed.worker.workspace import WorkspaceError

    def broken_reset(self):
        raise WorkspaceError("git reset failed")

    monkeypatch.setattr(Workspace, "reset", broken_reset)
    agents = FakeAgentRunner(cands("x", "y"), {"x": good("x"), "y": good("y")})
    r = run(inp(url, mode="ci"), ws, agents, clock)
    assert r.status == "ok" and [x.status for x in r.results] == ["fixed"] and agents.fix_calls == ["x"]


OUTCOME = OutcomeDigest(title="t", category="bug", state="merged")


def test_lessons_failure_keeps_fixes(url, ws):
    agents = FakeAgentRunner(cands("x"), {"x": good("x")}, lessons_error=RuntimeError("budget"))
    r = run(inp(url, outcomes=[OUTCOME]), ws, agents, clock)
    assert r.status == "ok" and r.lessons is None and [x.status for x in r.results] == ["fixed"]


def test_lessons_skipped_when_budget_exhausted(url, ws):
    agents = FakeAgentRunner(cands("x"), {"x": good("x")}, tokens_per_call=600)
    r = run(inp(url, outcomes=[OUTCOME], token_budget=1000), ws, agents, clock)
    assert agents.lessons_calls == [] and r.status == "ok" and len(r.results) == 1


def test_result_echoes_nonce(url, ws):
    assert run(inp(url), ws, FakeAgentRunner([]), clock).nonce == "n" * 32


def test_failed_result_echoes_nonce(tmp_path, ws):
    assert run(inp(f"file://{tmp_path}/missing.git"), ws, FakeAgentRunner([]), clock).nonce == "n" * 32
