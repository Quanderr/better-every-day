from importlib.metadata import entry_points
from typing import Any, Literal

from bed.core.config import ConfigError
from bed.core.models import ConnectorConfig
from bed.core.ports import SecretStore

Group = Literal["bed.vcs", "bed.tracker", "bed.notifier"]


def load_connector(group: Group, cfg: ConnectorConfig, secrets: SecretStore) -> Any:
    for ep in entry_points(group=group):
        if ep.name == cfg.type:
            factory = ep.load()
            return factory(secrets.get(cfg.auth_secret), dict(cfg.options))
    raise ConfigError([f"unknown {group} type: {cfg.type}"])
