"""Validated exploration snapshots in AgentCore Memory; no model-driven extraction."""
import hashlib
import json
from datetime import UTC, datetime

import boto3

from bed.core.models import ExplorationMemory
from bed.core.ports import StateStore


class AgentCoreExplorationStore:
    def __init__(self, memory_id: str, state: StateStore):
        self.memory_id = memory_id
        self.state = state
        self.client = boto3.client("bedrock-agentcore")

    def get(self, repo: str) -> ExplorationMemory | None:
        pointer = self.state.get_meta(f"exploration#{repo}")
        if not pointer:
            return None
        ref = json.loads(pointer)
        try:
            response = self.client.get_event(memoryId=self.memory_id, actorId=repo,
                                            sessionId=ref["session"], eventId=ref["event"])
        except self.client.exceptions.ResourceNotFoundException:
            # An expired snapshot is a cache miss, not permission to skip validation.
            return None
        data = response["event"]["payload"][0]["json"]["content"]
        return ExplorationMemory.model_validate(data)

    def put(self, repo: str, memory: ExplorationMemory, run_id: str) -> None:
        data = memory.model_dump(mode="json")
        canonical = json.dumps(data, sort_keys=True)
        digest = hashlib.sha256(f"{repo}:{run_id}:{canonical}".encode()).hexdigest()
        timestamp = datetime.strptime(run_id.split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        response = self.client.create_event(
            memoryId=self.memory_id, actorId=repo, sessionId=run_id, clientToken=digest,
            eventTimestamp=timestamp, extractionMode="SKIP", payload=[{"json": {"content": data}}],
        )
        self.state.put_meta(f"exploration#{repo}", json.dumps({"session": run_id,
                                                             "event": response["event"]["eventId"]}))
