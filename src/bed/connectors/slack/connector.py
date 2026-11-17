from typing import Any

import httpx

from bed.core.models import Finding, RunRecord
from bed.core.ports import Summary

POST_URL = "https://slack.com/api/chat.postMessage"
SECTION_CAP = 20
SECTION_CHARS = 2900


class NotifierError(Exception):
    pass


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "¦")


def _link(url: str | None, label: str) -> str:
    return f"<{url}|{_escape(label)}>" if url else _escape(label)


def _finding_line(f: Finding) -> str:
    return f"{_link(f.pr_url, f.title)} · {f.repo} · {_link(f.ticket_url, f.ticket_id or '')}"


def _section(title: str, lines: list[str]) -> list[dict[str, Any]]:
    if not lines:
        return []
    head = f"*{title}*"
    shown: list[str] = []
    size = len(head)
    for line in lines[:SECTION_CAP]:
        if size + len(line) + 1 > SECTION_CHARS - 20:
            break
        shown.append(line)
        size += len(line) + 1
    if len(lines) > len(shown):
        shown.append(f"+{len(lines) - len(shown)} more")
    return [{"type": "section", "text": {"type": "mrkdwn", "text": head + "\n" + "\n".join(shown)}}]


def _run_line(r: RunRecord) -> str:
    return f"{r.repo} — {r.status}"


def render(summary: Summary) -> list[dict[str, Any]]:
    header = {"type": "header",
              "text": {"type": "plain_text", "text": f"better-every-day — {len(summary.opened)} new PRs"}}
    skipped = sorted({r.repo for r in summary.skipped})
    return [
        header,
        *_section("New", [_finding_line(f) for f in summary.opened]),
        *_section("Waiting for review", [_finding_line(f) for f in summary.waiting]),
        *_section("Skipped (review backlog)", skipped),
        *_section("Failed", [_run_line(r) for r in summary.failed]),
        *_section("Stuck publishing", [f"{_escape(f.title)} · {f.repo} · {f.ticket_id or 'no ticket'}"
                                       for f in summary.stuck]),
    ]


class SlackConnector:
    def __init__(self, bot_token: str, channel: str, client: httpx.Client | None = None):
        self.bot_token = bot_token
        self.channel = channel
        self.client = client or httpx.Client(timeout=30)

    def send(self, summary: Summary) -> None:
        blocks = render(summary)
        response = self.client.post(
            POST_URL,
            headers={"Authorization": f"Bearer {self.bot_token}"},
            json={"channel": self.channel, "blocks": blocks, "text": blocks[0]["text"]["text"]},
        )
        if response.status_code != 200:
            raise NotifierError(f"Slack API {response.status_code}")
        data = response.json()
        if not data.get("ok"):
            raise NotifierError(data.get("error", "unknown error"))


def create(secret: dict[str, str], options: dict[str, str]) -> SlackConnector:
    return SlackConnector(bot_token=secret["bot_token"], channel=options["channel"])
