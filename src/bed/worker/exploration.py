from __future__ import annotations

from collections.abc import Callable, Collection
from threading import Lock
from typing import TYPE_CHECKING

from bed.core.fingerprint import fingerprint
from bed.core.gates import is_safe_path
from bed.core.models import (
    EXPLORATION_BACKLOG_MAX,
    EXPLORATION_MAX_BYTES,
    EXPLORATION_SUMMARY_MAX,
    Candidate,
    Checkpoint,
    ExplorationMemory,
    WorkerInput,
)

if TYPE_CHECKING:
    from bed.agents.runner import AgentRunner

CHECKPOINT_MAX_BYTES = EXPLORATION_MAX_BYTES - 5_000
CANDIDATE_FILES_MAX = 5
CANDIDATE_PATH_MAX_BYTES = 1024
CANDIDATE_TEXT_MAX = {"title": 200, "symbol": 200, "issue_kind": 100, "evidence": 2000, "rationale": 1000}


def _pending(memory: ExplorationMemory, known: Collection[str]) -> list[Candidate]:
    excluded = {*memory.attempted, *known}
    return [c for c in memory.candidates if fingerprint(c) not in excluded]


def _exhausted(memory: ExplorationMemory, known: Collection[str]) -> bool:
    return memory.complete and not _pending(memory, known)


def resumes_backlog(inp: WorkerInput) -> bool:
    previous = inp.exploration
    return (inp.memory_enabled and previous is not None and bool(previous.candidates)
            and not _exhausted(previous, inp.known_fingerprints))


def _clip(candidate: Candidate) -> Candidate:
    texts = {name: getattr(candidate, name)[:limit] for name, limit in CANDIDATE_TEXT_MAX.items()}
    return candidate.model_copy(update={**texts, "files": candidate.files[:CANDIDATE_FILES_MAX]})


def _is_storable_path(path: str) -> bool:
    return is_safe_path(path) and len(path.encode()) <= CANDIDATE_PATH_MAX_BYTES


class ExplorationJournal:
    def __init__(self, inp: WorkerInput, base_sha: str, emit: Callable[[Checkpoint], None], agents: AgentRunner):
        self.inp, self.emit, self.agents = inp, emit, agents
        self.lock = Lock()
        previous = inp.exploration
        if previous is None or _exhausted(previous, inp.known_fingerprints):
            self.memory = ExplorationMemory(generation=inp.run_id, base_sha=base_sha,
                                            summary=previous.summary if previous else "")
        else:
            self.memory = previous.model_copy(update={"base_sha": base_sha})

    @property
    def pending(self) -> list[Candidate]:
        return _pending(self.memory, self.inp.known_fingerprints)

    def remember_context(self, summary: str) -> str:
        with self.lock:
            return self._save(self.memory.model_copy(update={"summary": summary[:EXPLORATION_SUMMARY_MAX]})
                              if summary else self.memory)

    def remember_finding(self, candidate: Candidate) -> str:
        with self.lock:
            if not candidate.files or not all(_is_storable_path(p) for p in candidate.files):
                return "Invalid candidate path."
            if len(self.memory.candidates) >= EXPLORATION_BACKLOG_MAX:
                return "Backlog full. Return the current findings now."
            candidate = _clip(candidate)
            fp = fingerprint(candidate)
            if fp in self.inp.known_fingerprints:
                return "Already reported. Look for a different finding."
            if fp in {fingerprint(c) for c in self.memory.candidates}:
                return self._save(self.memory)
            return self._save(self.memory.model_copy(update={"candidates": [*self.memory.candidates, candidate]}))

    def attempted(self, candidate: Candidate) -> None:
        self._commit({"attempted": [*self.memory.attempted, fingerprint(candidate)]})

    def complete(self, summary: str) -> None:
        self.remember_context(summary)
        self._commit({"complete": True})

    def _save(self, memory: ExplorationMemory) -> str:
        if len(memory.model_dump_json().encode()) > CHECKPOINT_MAX_BYTES:
            return "Checkpoint capacity reached. Return the saved findings now."
        self.memory = memory
        self._flush()
        return f"Saved context and {len(memory.candidates)} findings."

    def _commit(self, update: dict) -> None:
        self.memory = self.memory.model_copy(update=update)
        self._flush()

    def _flush(self) -> None:
        self.emit(Checkpoint(run_id=self.inp.run_id, repo=self.inp.repo.name, base_sha=self.memory.base_sha,
                             nonce=self.inp.nonce, exploration=self.memory, tokens_used=self.agents.tokens_used,
                             cost_usd=self.agents.cost_usd))
