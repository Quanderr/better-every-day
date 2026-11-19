from collections.abc import Callable
from threading import RLock

from bed.core.fingerprint import fingerprint
from bed.core.gates import is_safe_path
from bed.core.models import Candidate, ExplorationMemory, WorkerInput, WorkerResult


class ExplorationJournal:
    def __init__(self, inp: WorkerInput, base_sha: str, emit: Callable[[WorkerResult], None], agents):
        self.inp, self.emit, self.agents = inp, emit, agents
        self.lock = RLock()
        previous = inp.exploration
        self.memory = previous.model_copy(update={"base_sha": base_sha}) if previous else ExplorationMemory(
            generation=inp.run_id, base_sha=base_sha)
        if previous and previous.complete and not self.pending:
            self.memory = ExplorationMemory(generation=inp.run_id, base_sha=base_sha, summary=previous.summary)

    @property
    def pending(self) -> list[Candidate]:
        excluded = set(self.memory.attempted) | set(self.inp.known_fingerprints)
        return [c for c in self.memory.candidates if fingerprint(c) not in excluded]

    def flush(self):
        self.emit(WorkerResult(run_id=self.inp.run_id, repo=self.inp.repo.name,
            base_sha=self.memory.base_sha, status="started", kind="checkpoint", nonce=self.inp.nonce,
            exploration=self.memory, tokens_used=self.agents.tokens_used,
            cost_usd=getattr(self.agents, "cost_usd", 0)))

    def remember(self, candidate: Candidate | None, summary: str) -> str:
        with self.lock:
            return self._remember(candidate, summary)

    def _remember(self, candidate: Candidate | None, summary: str) -> str:
        previous = self.memory
        if summary:
            self.memory = self.memory.model_copy(update={"summary": summary[:8000]})
        if candidate is not None:
            if not candidate.files or any(not is_safe_path(p) or len(p.encode()) > 1024 for p in candidate.files):
                return "Invalid candidate path."
            if len(self.memory.candidates) >= 12:
                return "Backlog full. Return the current findings now."
            candidate = candidate.model_copy(update={
                "title": candidate.title[:200], "files": candidate.files[:5], "symbol": candidate.symbol[:200],
                "issue_kind": candidate.issue_kind[:100], "evidence": candidate.evidence[:2000],
                "rationale": candidate.rationale[:1000],
            })
            if fingerprint(candidate) in self.inp.known_fingerprints:
                return "Already reported. Look for a different finding."
            if fingerprint(candidate) not in {fingerprint(c) for c in self.memory.candidates}:
                self.memory = self.memory.model_copy(update={"candidates": [*self.memory.candidates, candidate]})
        if len(self.memory.model_dump_json().encode()) > 55_000:
            self.memory = previous
            return "Checkpoint capacity reached. Return the saved findings now."
        self.flush()
        return f"Saved context and {len(self.memory.candidates)} findings."

    def attempted(self, candidate: Candidate):
        fp = fingerprint(candidate)
        self.memory = self.memory.model_copy(update={"attempted": [*self.memory.attempted, fp]})
        self.flush()
