from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bed.core.models import AppConfig, RepoConfig
from bed.core.ports import (
    AgentRuntime,
    ExplorationStore,
    NotifierConnector,
    SecretStore,
    StateStore,
    TrackerConnector,
    VcsConnector,
)
from bed.core.registry import Group, load_connector


@dataclass
class ControlDeps:
    config: AppConfig
    store: StateStore
    runtime: AgentRuntime
    secrets: SecretStore
    clock: Callable[[], datetime]
    connectors: dict[tuple[str, str], Any] = field(default_factory=dict)
    memory: ExplorationStore | None = None

    def _connector(self, group: Group, name: str) -> Any:
        key = (group, name)
        if key not in self.connectors:
            configs = {"bed.vcs": self.config.vcs, "bed.tracker": self.config.tracker,
                       "bed.notifier": self.config.notifier}[group]
            self.connectors[key] = load_connector(group, configs[name], self.secrets)
        return self.connectors[key]

    def vcs(self, repo: RepoConfig) -> VcsConnector:
        return self._connector("bed.vcs", repo.vcs)

    def tracker(self, repo: RepoConfig) -> TrackerConnector | None:
        if repo.tracker is None:
            return None
        return self._connector("bed.tracker", repo.tracker)

    def notifier(self) -> NotifierConnector | None:
        if self.config.notifications is None:
            return None
        return self._connector("bed.notifier", self.config.notifications.notifier)
