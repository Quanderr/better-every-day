import logging
import os
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3

from bed.core.config import load_config
from bed.core.control.deps import ControlDeps
from bed.core.control.publish import handle_result
from bed.core.control.start_run import start_run
from bed.core.control.summary import send_summary
from bed.platform.aws.eventbus import parse_result
from bed.platform.aws.memory import AgentCoreExplorationStore
from bed.platform.aws.runtime import AgentCoreRuntime
from bed.platform.aws.secrets import SecretsManagerStore
from bed.platform.aws.state import DynamoStateStore

log = logging.getLogger()
log.setLevel(logging.INFO)


@lru_cache(maxsize=1)
def build_deps() -> ControlDeps:
    obj = boto3.client("s3").get_object(Bucket=os.environ["BED_CONFIG_BUCKET"], Key=os.environ["BED_CONFIG_KEY"])
    config = load_config(obj["Body"].read().decode())
    state = DynamoStateStore(os.environ["BED_TABLE"])
    return ControlDeps(
        config=config,
        store=state,
        runtime=AgentCoreRuntime(os.environ["BED_RUNTIME_ARN"]),
        secrets=SecretsManagerStore(),
        clock=lambda: datetime.now(UTC),
        memory=AgentCoreExplorationStore(os.environ["BED_MEMORY_ID"], state) if os.getenv("BED_MEMORY_ID") else None,
    )


def schedule_handler(event: dict, context: Any, deps: ControlDeps | None = None) -> dict:
    deps = deps or build_deps()
    action = event.get("action")
    if action == "run":
        run = start_run(event["repo"], deps)
        log.info("run %s for %s: %s", run.run_id, run.repo, run.status)
        return {"status": run.status}
    if action == "summary":
        summary = send_summary(deps)
        reportable = summary.opened or summary.skipped or summary.failed or summary.stuck
        return {"status": "sent" if reportable and deps.notifier() else "empty"}
    raise ValueError(f"unknown action: {action!r}")


def result_handler(event: dict, context: Any, deps: ControlDeps | None = None) -> dict:
    deps = deps or build_deps()
    failures = []
    for record in event.get("Records", []):
        try:
            run = handle_result(parse_result(record["body"]), deps)
            log.info("result %s for %s: %s", run.run_id, run.repo, run.status)
        except Exception:
            log.exception("failed to handle record %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
