import io
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, BinaryIO

from bed.core.control.deps import ControlDeps
from bed.core.models import (
    AppConfig,
    CacheGrant,
    Checkpoint,
    FileChange,
    Finding,
    RunRecord,
    WorkerInput,
    WorkerResult,
)
from bed.core.ports import BotPr, PrRef, ReadToken, Summary, TicketRef


class Failing:
    def __init__(self) -> None:
        self._fail: set[str] = set()

    def fail_next(self, method: str) -> None:
        self._fail.add(method)

    def _maybe_fail(self, method: str) -> None:
        if method in self._fail:
            self._fail.discard(method)
            raise RuntimeError(f"injected failure: {method}")


class FakeVcs(Failing):
    def __init__(self, secret: dict[str, str] | None = None, options: dict[str, str] | None = None) -> None:
        super().__init__()
        self.secret = secret or {}
        self.options = options or {}
        self.bot_prs: dict[str, list[BotPr]] = {}
        self.files: dict[tuple[str, str, str], str] = {}
        self.branches: dict[tuple[str, str], str] = {}
        self.prs: dict[tuple[str, str], PrRef] = {}
        self.pushed: list[SimpleNamespace] = []
        self.opened: list[SimpleNamespace] = []
        self.labels: dict[tuple[str, int], set[str]] = {}
        self.comments: dict[tuple[str, int], str] = {}
        self.comment_calls: list[tuple[str, int]] = []
        self.reads: list[str] = []
        self.foreign_shas: set[str] = set()
        self.tokens_issued = 0
        self.revoked: list[tuple[str, str]] = []
        self.prs_by_number: dict[tuple[str, int], BotPr] = {}
        self.ancestor_checks: list[tuple[str, str, str]] = []

    def clone_url(self, repo: str) -> str:
        return f"https://example.test/{repo}.git"

    def read_token(self, repo: str) -> ReadToken:
        self._maybe_fail("read_token")
        self.tokens_issued += 1
        return ReadToken(token=f"read-token-{repo}-{self.tokens_issued}", revoke_id=f"tok-{self.tokens_issued}")

    def revoke_read_token(self, repo: str, revoke_id: str) -> None:
        self.revoked.append((repo, revoke_id))

    def read_file(self, repo: str, ref: str, path: str) -> str | None:
        self._maybe_fail("read_file")
        self.reads.append(path)
        return self.files.get((repo, ref, path))

    def list_bot_prs(self, repo: str) -> list[BotPr]:
        self._maybe_fail("list_bot_prs")
        return list(self.bot_prs.get(repo, []))

    def get_pr(self, repo: str, number: int) -> BotPr:
        return self.prs_by_number[(repo, number)]

    def close_comment(self, repo: str, number: int) -> str:
        self.comment_calls.append((repo, number))
        return self.comments.get((repo, number), "")

    def is_ancestor(self, repo: str, sha: str, branch: str) -> bool:
        self.ancestor_checks.append((repo, sha, branch))
        return sha not in self.foreign_shas

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._maybe_fail("add_labels")
        self.labels.setdefault((repo, number), set()).update(labels)

    def find_branch(self, repo: str, branch: str) -> str | None:
        return self.branches.get((repo, branch))

    def push_branch(self, repo: str, branch: str, base_sha: str, message: str, changes: list[FileChange]) -> str:
        self._maybe_fail("push_branch")
        sha = f"commit{len(self.pushed) + 1}"
        self.branches[(repo, branch)] = sha
        self.pushed.append(SimpleNamespace(repo=repo, branch=branch, base_sha=base_sha, message=message,
                                           changes=list(changes)))
        return sha

    def find_pr(self, repo: str, branch: str) -> PrRef | None:
        return self.prs.get((repo, branch))

    def open_pr(self, repo: str, base_branch: str, branch: str, title: str, body: str, labels: list[str]) -> PrRef:
        self._maybe_fail("open_pr")
        number = len(self.opened) + 1
        ref = PrRef(number=number, url=f"https://example.test/{repo}/pull/{number}")
        self.prs[(repo, branch)] = ref
        self.opened.append(SimpleNamespace(repo=repo, base_branch=base_branch, branch=branch, title=title,
                                           body=body, labels=list(labels), ref=ref))
        return ref


def create_fake_vcs(secret: dict[str, str], options: dict[str, str]) -> FakeVcs:
    return FakeVcs(secret, options)


class FakeTracker(Failing):
    def __init__(self) -> None:
        super().__init__()
        self.tickets: dict[tuple[str, str], TicketRef] = {}
        self.created: list[SimpleNamespace] = []
        self.links: list[SimpleNamespace] = []

    def find_ticket(self, project: str, fingerprint: str) -> TicketRef | None:
        return self.tickets.get((project, fingerprint))

    def create_ticket(self, project: str, title: str, body: str, fingerprint: str) -> TicketRef:
        self._maybe_fail("create_ticket")
        n = len(self.created) + 1
        ref = TicketRef(id=f"{project}-{n}", url=f"https://tracker.test/{project}-{n}")
        self.tickets[(project, fingerprint)] = ref
        self.created.append(SimpleNamespace(project=project, title=title, body=body, fingerprint=fingerprint,
                                            ref=ref))
        return ref

    def link_pr(self, ticket_id: str, pr_url: str, title: str) -> None:
        self._maybe_fail("link_pr")
        self.links.append(SimpleNamespace(ticket_id=ticket_id, pr_url=pr_url, title=title))


class FakeNotifier(Failing):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Summary] = []

    def send(self, summary: Summary) -> None:
        self._maybe_fail("send")
        self.sent.append(summary)


class FakeRuntime(Failing):
    def __init__(self) -> None:
        super().__init__()
        self.started: list[tuple[str, WorkerInput]] = []

    def start(self, session_id: str, payload: WorkerInput) -> None:
        self._maybe_fail("start")
        self.started.append((session_id, payload))


class FakeSink(Failing):
    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[WorkerResult | Checkpoint] = []

    def emit(self, message: WorkerResult | Checkpoint) -> None:
        self._maybe_fail("emit")
        self.emitted.append(message)


class FakeCacheGrants(Failing):
    def __init__(self) -> None:
        super().__init__()
        self.granted: list[str] = []

    def grant(self, repo: str) -> CacheGrant:
        self._maybe_fail("grant")
        self.granted.append(repo)
        return CacheGrant(bucket="cache", key=f"repos/{repo}", access_key_id="id", secret_access_key="secret",
                          session_token="session")


class FakeCacheStore(Failing):
    def __init__(self) -> None:
        super().__init__()
        self.stored: tuple[dict[str, str], bytes] | None = None
        self.uploads = 0
        self.deletes = 0

    def open(self) -> tuple[dict[str, str], BinaryIO] | None:
        self._maybe_fail("open")
        if self.stored is None:
            return None
        metadata, body = self.stored
        return dict(metadata), io.BytesIO(body)

    def upload(self, source: BinaryIO, metadata: dict[str, str]) -> None:
        self._maybe_fail("upload")
        chunks = []
        while chunk := source.read(65536):
            chunks.append(chunk)
        self.stored = (dict(metadata), b"".join(chunks))
        self.uploads += 1

    def delete(self) -> None:
        self._maybe_fail("delete")
        self.stored = None
        self.deletes += 1


class FakeSecrets:
    def __init__(self, secrets: dict[str, dict[str, str]] | None = None) -> None:
        self.secrets = secrets or {}

    def get(self, name: str) -> dict[str, str]:
        return dict(self.secrets.get(name, {}))


class InMemoryStore:
    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], RunRecord] = {}
        self.findings: dict[tuple[str, str], Finding] = {}
        self.lessons: dict[str, str] = {}
        self.meta: dict[str, str] = {}
        self.leases: dict[str, datetime] = {}

    def put_run(self, run: RunRecord) -> None:
        self.runs[(run.repo, run.run_id)] = run

    def get_run(self, repo: str, run_id: str) -> RunRecord | None:
        return self.runs.get((repo, run_id))

    def list_runs(self, repo: str, since: datetime) -> list[RunRecord]:
        runs = [r for (rp, _), r in self.runs.items() if rp == repo and r.started_at >= since]
        return sorted(runs, key=lambda r: r.run_id)

    def get_finding(self, repo: str, fingerprint: str) -> Finding | None:
        return self.findings.get((repo, fingerprint))

    def put_finding(self, finding: Finding) -> None:
        self.findings[(finding.repo, finding.fingerprint)] = finding

    def list_findings(self, repo: str) -> list[Finding]:
        return [f for (rp, _), f in self.findings.items() if rp == repo]

    def get_lessons(self, repo: str) -> str:
        return self.lessons.get(repo, "")

    def put_lessons(self, repo: str, text: str) -> None:
        self.lessons[repo] = text

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def put_meta(self, key: str, value: str) -> None:
        self.meta[key] = value

    def update_meta(self, key: str, change: Callable[[str | None], str | None]) -> str | None:
        new = change(self.meta.get(key))
        if new is not None:
            self.meta[key] = new
        return self.meta.get(key)

    def try_lease(self, key: str, now: datetime, ttl: timedelta) -> bool:
        expires = self.leases.get(key)
        if expires is not None and expires >= now:
            return False
        self.leases[key] = now + ttl
        return True


@dataclass
class FakeClock:
    now: datetime = field(default_factory=lambda: datetime(2026, 11, 17, 5, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeDeps(ControlDeps):
    fake_vcs: FakeVcs = field(default_factory=FakeVcs)
    fake_tracker: FakeTracker = field(default_factory=FakeTracker)
    fake_notifier: FakeNotifier = field(default_factory=FakeNotifier)


def fake_deps(config: AppConfig, clock: Callable[[], datetime] | None = None) -> FakeDeps:
    deps = FakeDeps(config=config, store=InMemoryStore(), runtime=FakeRuntime(), secrets=FakeSecrets(),
                    clock=clock or FakeClock())
    connectors: dict[tuple[str, str], Any] = {}
    for name in config.vcs:
        connectors[("bed.vcs", name)] = deps.fake_vcs
    for name in config.tracker:
        connectors[("bed.tracker", name)] = deps.fake_tracker
    for name in config.notifier:
        connectors[("bed.notifier", name)] = deps.fake_notifier
    deps.connectors = connectors
    return deps


class FakeAgentRunner:
    cost_usd = 0.0
    budget_blocked = False

    def __init__(self, candidates, edits=None, lessons_text: str = "", tokens_per_call: int = 0,
                 scout_error: Exception | None = None, lessons_error: Exception | None = None,
                 summary: str = ""):
        self.candidates = list(candidates)
        self.edits = edits or {}
        self.lessons_text = lessons_text
        self.tokens_per_call = tokens_per_call
        self.scout_error = scout_error
        self.lessons_error = lessons_error
        self.summary = summary
        self.tokens_used = 0
        self.scout_calls = 0
        self.fix_calls: list[str] = []
        self.lessons_calls: list[list] = []

    def scout(self, ws, inp, discoveries=None):
        from bed.agents.runner import ScoutOutput

        self.scout_calls += 1
        self.tokens_used += self.tokens_per_call
        if self.scout_error:
            raise self.scout_error
        return ScoutOutput(candidates=list(self.candidates), summary=self.summary)

    def fix(self, ws, inp, candidate):
        from bed.agents.runner import FixerOutput

        self.fix_calls.append(candidate.title)
        self.tokens_used += self.tokens_per_call
        edit = self.edits.get(candidate.title)
        if edit is None:
            return FixerOutput(status="abandoned", summary="no edit")
        return edit(ws)

    def lessons(self, inp):
        self.lessons_calls.append(list(inp.recent_outcomes))
        self.tokens_used += self.tokens_per_call
        if self.lessons_error:
            raise self.lessons_error
        return self.lessons_text
