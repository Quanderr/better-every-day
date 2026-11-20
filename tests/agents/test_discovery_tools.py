import asyncio
from types import SimpleNamespace

from helpers import example_config, make_candidate

from bed.agents.runner import ScoutOutput, StrandsAgentRunner
from bed.core.models import WorkerInput


def test_sdk_tool_validation_reconstructs_candidate_before_checkpointing():
    runner = object.__new__(StrandsAgentRunner)
    saved = []
    discoveries = SimpleNamespace(remember_finding=lambda candidate: saved.append(candidate) or "saved",
                                  remember_context=lambda summary: "saved")
    cfg = example_config()
    inp = WorkerInput(run_id="r", repo=cfg.repos[0], clone_url="u", read_token="t", model=cfg.model)
    candidate = make_candidate()

    def invoke(system, tools, prompt, output):
        remember = next(t for t in tools if t.tool_name == "remember_finding")

        async def call():
            return [event async for event in remember.stream(
                {"toolUseId": "checkpoint", "input": {"candidate": candidate.model_dump()}}, {})]

        asyncio.run(call())
        return SimpleNamespace(structured_output=ScoutOutput(candidates=[candidate]))

    runner._invoke = invoke
    assert runner.scout(object(), inp, discoveries).candidates == [candidate]
    assert saved == [candidate]
