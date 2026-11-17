from importlib.metadata import EntryPoint

import pytest

from bed.core import registry
from bed.core.config import ConfigError
from bed.core.models import ConnectorConfig
from bed.testing.fakes import FakeSecrets, FakeVcs


def test_registered_entry_point_resolves(monkeypatch):
    ep = EntryPoint(name="fake", value="bed.testing.fakes:create_fake_vcs", group="bed.vcs")
    monkeypatch.setattr(registry, "entry_points", lambda group: [ep] if group == "bed.vcs" else [])
    secrets = FakeSecrets({"bed/fake": {"token": "t"}})
    conn = registry.load_connector("bed.vcs", ConnectorConfig(type="fake", auth_secret="bed/fake",
                                                              options={"x": "1"}), secrets)
    assert isinstance(conn, FakeVcs)
    assert conn.secret == {"token": "t"} and conn.options == {"x": "1"}


def test_unknown_type_raises(monkeypatch):
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    with pytest.raises(ConfigError, match="unknown bed.vcs type: nope"):
        registry.load_connector("bed.vcs", ConnectorConfig(type="nope", auth_secret="s"), FakeSecrets({}))


@pytest.mark.parametrize("group, name, secret, options", [
    ("bed.vcs", "gitlab", {"token": "t"}, {}),
    ("bed.tracker", "jira", {"email": "e", "api_token": "t"}, {"url": "https://x.atlassian.net"}),
])
def test_new_connectors_registered(group, name, secret, options):
    conn = registry.load_connector(group, ConnectorConfig(type=name, auth_secret="s", options=options),
                                   FakeSecrets({"s": secret}))
    assert type(conn).__name__ in ("GitLabConnector", "JiraConnector")
