from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BOT_LABEL = "better-every-day"

Category = Literal["bug", "error-handling", "test-gap", "dead-code", "perf", "docs"]
ALL_CATEGORIES: list[Category] = ["bug", "error-handling", "test-gap", "dead-code", "perf", "docs"]


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Limits(Model):
    max_prs_per_run: int = Field(2, ge=1)
    max_open_bot_prs: int = Field(5, ge=1)
    max_diff_lines: int = Field(150, ge=1)
    max_files: int = Field(5, ge=1)
    token_budget: int = Field(2_000_000, ge=1)
    timeout_minutes: int = Field(60, ge=1)


class Verify(Model):
    mode: Literal["sandbox", "ci"] = "sandbox"
    install: str | None = None
    commands: list[str] = []


class RepoConfig(Model):
    name: str
    vcs: str
    tracker: str
    tracker_project: str
    schedule: str
    timezone: str
    base_branch: str = "main"
    verify: Verify = Verify()
    exclude_paths: list[str] = []
    protected_paths: list[str] = []
    prompt: str = ""
    categories: list[Category] = ALL_CATEGORIES
    limits: Limits = Limits()


class ConnectorConfig(Model):
    type: str
    auth_secret: str
    options: dict[str, str] = {}


class Notifications(Model):
    notifier: str
    summary_schedule: str
    timezone: str


class ModelConfig(Model):
    provider: str
    id: str


class AppConfig(Model):
    model: ModelConfig
    vcs: dict[str, ConnectorConfig] = {}
    tracker: dict[str, ConnectorConfig] = {}
    notifier: dict[str, ConnectorConfig] = {}
    notifications: Notifications | None = None
    repos: list[RepoConfig]
    prompt: str = ""

    def repo(self, name: str) -> RepoConfig:
        for repo in self.repos:
            if repo.name == name:
                return repo
        raise KeyError(name)


class Candidate(Model):
    title: str
    category: Category
    files: list[str]
    symbol: str = ""
    issue_kind: str
    evidence: str
    rationale: str


class FileChange(Model):
    path: str
    content: str | None


class FixResult(Model):
    candidate: Candidate
    fingerprint: str
    status: Literal["fixed", "fix_failed", "abandoned"]
    changes: list[FileChange] = []
    summary: str = ""
    verify_log: str = ""
    verified: bool = False


class OutcomeDigest(Model):
    title: str
    category: Category
    state: Literal["merged", "closed"]
    close_comment: str = ""


class WorkerInput(Model):
    run_id: str
    repo: RepoConfig
    clone_url: str
    read_token: str
    model: ModelConfig
    deployment_prompt: str = ""
    known_fingerprints: list[str] = []
    locked_paths: list[str] = []
    lessons: str = ""
    recent_outcomes: list[OutcomeDigest] = []
    nonce: str = ""


RunStatus = Literal[
    "started",
    "ok",
    "failed",
    "baseline_red",
    "workspace_full",
    "timeout",
    "skipped_backpressure",
    "skipped_running",
]


class WorkerResult(Model):
    run_id: str
    repo: str
    base_sha: str = ""
    status: RunStatus
    results: list[FixResult] = []
    lessons: str | None = None
    tokens_used: int = 0
    error: str | None = None
    nonce: str = ""


FindingStatus = Literal["publishing", "publish_failed", "published", "merged", "closed", "rejected_by_gate", "fix_failed"]
BLOCKING_STATUSES: frozenset[str] = frozenset({"publishing", "published", "merged", "closed"})


class Finding(Model):
    repo: str
    fingerprint: str
    title: str
    category: Category
    files: list[str]
    status: FindingStatus
    reason: str | None = None
    ticket_id: str | None = None
    ticket_url: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    close_comment: str | None = None
    changes_hash: str | None = None
    run_id: str
    created_at: datetime
    updated_at: datetime


class RunRecord(Model):
    run_id: str
    repo: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    tokens_used: int = 0
    nonce: str = ""
    read_token_id: str | None = None
