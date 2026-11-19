from types import SimpleNamespace

from helpers import make_candidate

from bed.core.models import ExplorationMemory
from bed.platform.aws.memory import AgentCoreExplorationStore
from bed.testing.fakes import InMemoryStore


def test_round_trip_uses_repo_scope_and_never_extracts_with_another_model(monkeypatch):
    saved = {}

    def create(**request):
        saved.update(request)
        return {"event": {"eventId": "123#abc"}}

    def get(**request):
        assert request["actorId"] == "acme/web" and request["eventId"] == "123#abc"
        return {"event": {"payload": saved["payload"]}}

    client = SimpleNamespace(create_event=create, get_event=get,
                             exceptions=SimpleNamespace(ResourceNotFoundException=KeyError))
    monkeypatch.setattr("bed.platform.aws.memory.boto3.client", lambda name: client)
    store = AgentCoreExplorationStore("memory", InMemoryStore())
    memory = ExplorationMemory(generation="g", base_sha="a" * 40, candidates=[make_candidate()])
    assert store.get("acme/web") is None
    store.put("acme/web", memory, "20261119T050000Z-abcd1234")
    assert saved["extractionMode"] == "SKIP" and saved["actorId"] == "acme/web"
    assert store.get("acme/web") == memory
    first_token, first_time = saved["clientToken"], saved["eventTimestamp"]
    store.put("acme/web", memory, "20261119T050000Z-abcd1234")
    assert (saved["clientToken"], saved["eventTimestamp"]) == (first_token, first_time)
