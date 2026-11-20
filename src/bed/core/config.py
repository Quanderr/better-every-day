from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import croniter
from pydantic import ValidationError

from bed.core.models import AppConfig, ConnectorConfig

CONNECTOR_KINDS = ("vcs", "tracker", "notifier")
TOP_LEVEL_KEYS = {"model", "connectors", "notifications", "defaults", "repos"}
DEFAULT_KEYS = {"prompt", "categories", "limits", "verify", "exclude_paths", "protected_paths", "base_branch"}


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def load_config(text: str) -> AppConfig:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError([f"invalid yaml: {e}"]) from e
    if not isinstance(raw, dict):
        raise ConfigError(["config must be a mapping"])
    errors: list[str] = []
    data = _normalize(raw, errors)
    try:
        cfg = AppConfig.model_validate(data)
    except ValidationError as e:
        errors.extend(_format(e))
        raise ConfigError(errors) from e
    errors.extend(_check(cfg))
    if errors:
        raise ConfigError(errors)
    return cfg


def _normalize(raw: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    for key in raw.keys() - TOP_LEVEL_KEYS:
        errors.append(f"{key}: extra key not permitted")
    defaults = dict(raw.get("defaults") or {})
    for key in defaults.keys() - DEFAULT_KEYS:
        errors.append(f"defaults.{key}: extra key not permitted")
    deployment_prompt = defaults.pop("prompt", "")
    connectors = raw.get("connectors") or {}
    data: dict[str, Any] = {
        "model": raw.get("model"),
        "notifications": raw.get("notifications"),
        "prompt": deployment_prompt,
        "repos": [_merge_repo(defaults, repo) for repo in raw.get("repos") or []],
    }
    for kind in CONNECTOR_KINDS:
        data[kind] = {name: _connector(c) for name, c in (connectors.get(kind) or {}).items()}
    return data


def _connector(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    known = {k: raw[k] for k in ("type", "auth_secret") if k in raw}
    options = {k: str(v) for k, v in raw.items() if k not in known}
    return {**known, "options": options}


def _merge_repo(defaults: dict[str, Any], repo: Any) -> Any:
    if not isinstance(repo, dict):
        return repo
    merged = {k: v for k, v in defaults.items() if k in DEFAULT_KEYS}
    merged.update(repo)
    if isinstance(defaults.get("limits"), dict) and isinstance(repo.get("limits"), dict):
        merged["limits"] = {**defaults["limits"], **repo["limits"]}
    if isinstance(defaults.get("protected_paths"), list) and isinstance(repo.get("protected_paths"), list):
        merged["protected_paths"] = [*defaults["protected_paths"], *repo["protected_paths"]]
    return merged


def _format(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = "extra key not permitted" if err["type"] == "extra_forbidden" else err["msg"]
        out.append(f"{loc}: {msg}")
    return out


def _check(cfg: AppConfig) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for repo in cfg.repos:
        where = f"repos.{repo.name}"
        if repo.limits.daily_usd_budget is not None and cfg.model.prices is None:
            errors.append(f"{where}: model.prices required with daily_usd_budget")
        if repo.name in seen:
            errors.append(f"{where}: duplicate repo")
        seen.add(repo.name)
        if repo.vcs not in cfg.vcs:
            errors.append(f"{where}: unknown vcs connector {repo.vcs!r}")
        if repo.tracker not in cfg.tracker:
            errors.append(f"{where}: unknown tracker connector {repo.tracker!r}")
        errors.extend(_check_schedule(where, repo.schedule, repo.timezone))
        if repo.verify.mode == "sandbox" and not repo.verify.commands:
            errors.append(f"{where}: verify.commands required in sandbox mode")
    if cfg.notifications:
        if cfg.notifications.notifier not in cfg.notifier:
            errors.append(f"notifications: unknown notifier {cfg.notifications.notifier!r}")
        errors.extend(
            _check_schedule("notifications", cfg.notifications.summary_schedule, cfg.notifications.timezone)
        )
    return errors


def _eventbridge_errors(schedule: str) -> list[str]:
    parts = schedule.split()
    if len(parts) != 5:
        return []
    dom, dow = parts[2], parts[4]
    errors = []
    if dom != "*" and dow != "*":
        errors.append("schedule cannot restrict both day-of-month and day-of-week")
    if "/" in dow:
        errors.append("day-of-week step values are not supported")
    for part in dow.split(","):
        bounds = part.split("-")
        if len(bounds) == 2 and all(b.isdigit() for b in bounds):
            start, end = (int(b) % 7 + 1 for b in bounds)
            if start > end:
                errors.append(f"day-of-week range {part} would wrap; use 0 for Sunday or split the range")
    return errors


def _check_schedule(where: str, schedule: str, timezone: str) -> list[str]:
    errors = []
    if len(schedule.split()) != 5 or not croniter.is_valid(schedule):
        errors.append(f"{where}: invalid schedule {schedule!r}")
    errors.extend(f"{where}: {e}" for e in _eventbridge_errors(schedule))
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"{where}: invalid timezone {timezone!r}")
    return errors


__all__ = ["ConfigError", "ConnectorConfig", "load_config"]
