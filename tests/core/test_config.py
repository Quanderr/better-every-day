from pathlib import Path

import pytest
import yaml

from bed.core.config import ConfigError, load_config

EXAMPLE = (Path(__file__).parents[2] / "config" / "example.yaml").read_text()


def _mutate(fn):
    def apply(text):
        data = yaml.safe_load(text)
        fn(data)
        return yaml.safe_dump(data)

    return apply


def with_repo_limits(**limits):
    return _mutate(lambda d: d["repos"][0].update(limits=limits))(EXAMPLE)


unknown_vcs_ref = _mutate(lambda d: d["repos"][0].update(vcs="nope"))
unknown_tracker_ref = _mutate(lambda d: d["repos"][0].update(tracker="nope"))
project_without_tracker = _mutate(lambda d: d["repos"][0].pop("tracker"))
tracker_without_project = _mutate(lambda d: d["repos"][0].pop("tracker_project"))
unknown_notifier_ref = _mutate(lambda d: d["notifications"].update(notifier="nope"))
bad_cron = _mutate(lambda d: d["repos"][0].update(schedule="not a cron"))
bad_timezone = _mutate(lambda d: d["repos"][0].update(timezone="Mars/Olympus"))
sandbox_without_commands = _mutate(lambda d: d["repos"][0].update(verify={"install": "x"}))
duplicate_repo = _mutate(lambda d: d["repos"].append(dict(d["repos"][0])))
unknown_key = _mutate(lambda d: d["repos"][0].update(surprise=1))


def test_defaults_merge_into_repo():
    cfg = load_config(EXAMPLE)
    web = cfg.repo("acme/web")
    assert web.limits.max_diff_lines == 150
    assert web.verify.mode == "sandbox"
    assert web.categories == ["bug", "error-handling", "test-gap", "dead-code", "perf", "docs"]


def test_repo_overrides_default_limit():
    web = load_config(with_repo_limits(max_prs_per_run=1)).repo("acme/web")
    assert web.limits.max_prs_per_run == 1
    assert web.limits.max_diff_lines == 150


def test_ci_mode_needs_no_commands():
    assert load_config(EXAMPLE).repo("acme/api").verify.mode == "ci"


def test_deployment_prompt_and_connector_options():
    cfg = load_config(EXAMPLE)
    assert cfg.prompt.startswith("Prefer correctness")
    assert cfg.notifier["team-slack"].options == {"channel": "#better-every-day"}
    assert cfg.notifications.notifier == "team-slack"


def test_unknown_repo_lookup_raises():
    with pytest.raises(KeyError):
        load_config(EXAMPLE).repo("acme/none")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (unknown_vcs_ref, "unknown vcs connector"),
        (unknown_tracker_ref, "unknown tracker connector"),
        (project_without_tracker, "tracker_project requires tracker"),
        (tracker_without_project, "tracker_project required with tracker"),
        (unknown_notifier_ref, "unknown notifier"),
        (bad_cron, "invalid schedule"),
        (bad_timezone, "invalid timezone"),
        (sandbox_without_commands, "verify.commands required"),
        (duplicate_repo, "duplicate repo"),
        (unknown_key, "extra"),
    ],
)
def test_invalid_config_rejected(mutate, message):
    with pytest.raises(ConfigError, match=message):
        load_config(mutate(EXAMPLE))


def test_all_errors_collected():
    text = bad_cron(unknown_vcs_ref(EXAMPLE))
    with pytest.raises(ConfigError) as err:
        load_config(text)
    assert "invalid schedule" in str(err.value) and "unknown vcs connector" in str(err.value)


def test_missing_verify_defaults_to_sandbox_and_requires_commands():
    text = _mutate(lambda d: d["repos"][0].pop("verify"))(EXAMPLE)
    with pytest.raises(ConfigError, match="verify.commands required"):
        load_config(text)


@pytest.mark.parametrize("schedule, message", [
    ("0 5 * * 5-7", "wrap"),
    ("0 5 1 * 1-5", "both day-of-month and day-of-week"),
    ("0 5 * * */2", "step"),
])
def test_schedule_must_convert_to_eventbridge(schedule, message):
    text = _mutate(lambda d: d["repos"][0].update(schedule=schedule))(EXAMPLE)
    with pytest.raises(ConfigError, match=message):
        load_config(text)


@pytest.mark.parametrize("schedule", ["0 5 * * 1-5", "0 5 * * 0,6", "0 5 * * MON-FRI", "0 5 1 * *", "30 2 * * 0-6"])
def test_eventbridge_compatible_schedules_accepted(schedule):
    load_config(_mutate(lambda d: d["repos"][0].update(schedule=schedule))(EXAMPLE))


def test_gitlab_jira_example_valid():
    cfg = load_config((Path(__file__).parents[2] / "config" / "example-gitlab-jira.yaml").read_text())
    assert cfg.vcs["main-gitlab"].type == "gitlab"
    assert cfg.tracker["main-jira"].options == {"url": "https://acme.atlassian.net", "issue_type": "Task"}
    assert cfg.repo("acme/platform/api").tracker_project == "API"


def test_protected_paths_merge_defaults_and_repo():
    def mutate(d):
        d["defaults"]["protected_paths"] = ["ops/**"]
        d["repos"][0]["protected_paths"] = ["scripts/**"]
    cfg = load_config(_mutate(mutate)(EXAMPLE))
    assert cfg.repo("acme/web").protected_paths == ["ops/**", "scripts/**"]
    assert cfg.repo("acme/api").protected_paths == ["ops/**"]


def test_daily_budget_defaults_to_fifteen_and_null_disables_it():
    text = _mutate(lambda d: d["defaults"]["limits"].pop("daily_usd_budget"))(EXAMPLE)
    assert load_config(text).repos[0].limits.daily_usd_budget == 15
    assert load_config(with_repo_limits(daily_usd_budget=None)).repos[0].limits.daily_usd_budget is None


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_daily_budget(value):
    with pytest.raises(ConfigError):
        load_config(with_repo_limits(daily_usd_budget=value))


def test_daily_budget_requires_prices_but_unlimited_does_not():
    text = _mutate(lambda d: d["model"].pop("prices"))(EXAMPLE)
    with pytest.raises(ConfigError, match="model.prices required"):
        load_config(text)
    text = _mutate(lambda d: d["defaults"]["limits"].update(daily_usd_budget=None))(text)
    load_config(text)


def test_no_tracker_example_valid():
    cfg = load_config((Path(__file__).parents[2] / "config" / "example-no-tracker.yaml").read_text())
    repo = cfg.repo("acme/web")
    assert cfg.tracker == {} and repo.tracker is None and repo.tracker_project is None


def test_tracker_optional_per_repo():
    def drop(d):
        d["repos"][0].pop("tracker")
        d["repos"][0].pop("tracker_project")

    cfg = load_config(_mutate(drop)(EXAMPLE))
    assert cfg.repo("acme/web").tracker is None and cfg.repo("acme/api").tracker == "main-linear"


def test_cache_defaults_on_and_can_be_disabled_in_defaults_or_repo():
    assert all(r.cache for r in load_config(EXAMPLE).repos)
    off = load_config(_mutate(lambda d: d["defaults"].update(cache=False))(EXAMPLE))
    assert not any(r.cache for r in off.repos)
    one = load_config(_mutate(lambda d: d["repos"][0].update(cache=False))(EXAMPLE))
    assert [r.cache for r in one.repos] == [False, True]
