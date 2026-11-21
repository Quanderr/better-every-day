import pytest
from helpers import example_config

from bed.testing.fakes import FakeAgentRunner, FakeSink
from bed.worker import app


@pytest.fixture
def payload(tmp_path):
    cfg = example_config()
    return {"run_id": "run-1", "repo": cfg.repo("acme/web").model_dump(), "clone_url": f"file://{tmp_path}/none.git",
            "read_token": "t", "model": cfg.model.model_dump(), "nonce": "n" * 32}


class Tasks:
    def __init__(self):
        self.started = []
        self.completed = []

    def start(self, name):
        self.started.append(name)
        return len(self.started)

    def complete(self, task):
        self.completed.append(task)


def factory(model):
    return FakeAgentRunner([])


def invoke(payload, sink, tasks, spawn, tmp_path):
    return app.handle_invocation(payload, sink, tasks.start, tasks.complete, factory, spawn, workdir_base=tmp_path)


def test_returns_accepted_before_job_runs(payload, tmp_path):
    sink, tasks, jobs = FakeSink(), Tasks(), []
    out = invoke(payload, sink, tasks, jobs.append, tmp_path)
    assert out == {"status": "accepted", "run_id": "run-1"}
    assert sink.emitted == [] and len(jobs) == 1 and tasks.started == ["run"]
    jobs[0]()


def test_job_emits_result_and_completes_task(payload, tmp_path):
    sink, tasks, jobs = FakeSink(), Tasks(), []
    invoke(payload, sink, tasks, jobs.append, tmp_path)
    jobs[0]()
    assert sink.emitted[0].run_id == "run-1" and sink.emitted[0].status == "failed"
    assert sink.emitted[0].nonce == "n" * 32
    assert tasks.completed == [1]
    assert (tmp_path / "run-1").exists()


def test_job_completes_task_when_emit_raises(payload, tmp_path):
    sink, tasks, jobs = FakeSink(), Tasks(), []
    sink.fail_next("emit")
    invoke(payload, sink, tasks, jobs.append, tmp_path)
    jobs[0]()
    assert tasks.completed == [1]
    assert invoke(payload, sink, tasks, jobs.append, tmp_path)["status"] == "accepted"
    jobs[1]()


def test_second_invocation_while_busy(payload, tmp_path):
    sink, tasks, jobs = FakeSink(), Tasks(), []
    invoke(payload, sink, tasks, jobs.append, tmp_path)
    out = invoke(payload, sink, tasks, jobs.append, tmp_path)
    assert out["status"] == "busy" and len(jobs) == 1
    jobs[0]()


def test_invalid_payload_rejected(tmp_path):
    out = invoke({"run_id": "x"}, FakeSink(), Tasks(), [].append, tmp_path)
    assert out["status"] == "rejected" and "repo" in out["error"]


def test_run_id_with_path_chars_rejected(payload, tmp_path):
    payload["run_id"] = "../evil"
    out = invoke(payload, FakeSink(), Tasks(), [].append, tmp_path)
    assert out["status"] == "rejected"


class TooBigOnce(FakeSink):
    def emit(self, result):
        if result.status != "failed" or result.error != "result too large":
            raise ValueError("result too large for SQS")
        super().emit(result)


def test_oversize_result_replaced_by_failed(payload, tmp_path):
    sink, tasks, jobs = TooBigOnce(), Tasks(), []
    invoke(payload, sink, tasks, jobs.append, tmp_path)
    jobs[0]()
    assert [(r.status, r.error) for r in sink.emitted] == [("failed", "result too large")]


def test_rejection_error_does_not_echo_token(tmp_path):
    out = invoke({"run_id": "r", "read_token": "ghs_SECRET"}, FakeSink(), Tasks(), [].append, tmp_path)
    assert out["status"] == "rejected" and "ghs_SECRET" not in out["error"] and "clone_url" in out["error"]


def test_busy_released_when_start_task_raises(payload, tmp_path):
    def boom(name):
        raise RuntimeError("no task")
    out = app.handle_invocation(payload, FakeSink(), boom, lambda t: None, factory, [].append,
                                workdir_base=tmp_path)
    assert out["status"] == "rejected"
    jobs = []
    assert invoke(payload, FakeSink(), Tasks(), jobs.append, tmp_path)["status"] == "accepted"
    jobs[0]()


class SizeCappedSink(FakeSink):
    def __init__(self, cap):
        super().__init__()
        self.cap = cap

    def emit(self, result):
        if len(result.model_dump_json()) > self.cap:
            raise ValueError("result too large for SQS")
        super().emit(result)


def test_oversize_drops_largest_fix_first():
    from helpers import make_candidate

    from bed.core.models import FileChange, FixResult, WorkerResult

    def fix(name, size):
        return FixResult(candidate=make_candidate(title=name, files=[f"{name}.py"]), fingerprint=name,
                         status="fixed", verified=True, changes=[FileChange(path=f"{name}.py", content="x" * size)])

    result = WorkerResult(run_id="r", repo="acme/web", base_sha="a" * 40, status="ok",
                          results=[fix("small", 1000), fix("big", 50_000)])
    sink = SizeCappedSink(20_000)
    app._emit(sink, result)
    sent = sink.emitted[0]
    assert sent.status == "ok"
    assert [(r.candidate.title, r.status, len(r.changes)) for r in sent.results] == [
        ("small", "fixed", 1), ("big", "fix_failed", 0)]
    assert "too large" in sent.results[1].verify_log


def test_job_builds_cache_from_factory(payload, tmp_path):
    sink, jobs, built = FakeSink(), [], []

    def cache_factory(inp):
        built.append(inp.run_id)
        return app.NoCache()

    app.handle_invocation(payload, sink, Tasks().start, lambda task: None, factory, jobs.append,
                          workdir_base=tmp_path, cache_factory=cache_factory)
    jobs[0]()
    assert built == ["run-1"] and len(sink.emitted) == 1
