import html
import json
import re
from typing import Any

import httpx

from bed.connectors.http import HttpApi
from bed.core.models import BOT_LABEL
from bed.core.ports import TicketRef, TrackerError

FP_LABEL = "bed-fp-"
SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
HEADING = re.compile(r"(#{1,6}) (.*)")
BULLET = re.compile(r"[-*] (.*)")
SUMMARY_MAX = 250
FALLBACK_SUMMARY = "better-every-day finding"
ZERO_WIDTH = "\u200b"


def _error(status: int, body: str) -> TrackerError:
    try:
        data = json.loads(body)
        messages = list(data.get("errorMessages", [])) + [f"{k}: {v}" for k, v in data.get("errors", {}).items()]
        detail = "; ".join(messages) or body[:500]
    except (ValueError, AttributeError):
        detail = body[:500]
    return TrackerError(f"Jira API {status}: {detail}")


def _decode(text: str) -> str:
    return html.unescape(text.replace(ZERO_WIDTH, ""))


def _inline(line: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for i, part in enumerate(_decode(line).split("`")):
        if not part:
            continue
        node: dict[str, Any] = {"type": "text", "text": part}
        if i % 2 == 1:
            node["marks"] = [{"type": "code"}]
        nodes.append(node)
    return nodes


def _paragraph(lines: list[str]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if i:
            content.append({"type": "hardBreak"})
        content.extend(_inline(line))
    return {"type": "paragraph", "content": content}


def _closing_fence(lines: list[str], start: int) -> int | None:
    for j in range(start, len(lines)):
        if lines[j].startswith("```"):
            return j
    return None


def to_adf(markdown: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    paragraph: list[str] = []
    bullets: list[str] = []
    lines = markdown.split("\n")

    def flush() -> None:
        if paragraph:
            content.append(_paragraph(paragraph))
            paragraph.clear()
        if bullets:
            content.append({"type": "bulletList", "content": [
                {"type": "listItem", "content": [_paragraph([item])]} for item in bullets]})
            bullets.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        heading = HEADING.fullmatch(line)
        bullet = BULLET.fullmatch(line)
        end = _closing_fence(lines, i + 1) if line.startswith("```") else None
        if end is not None:
            flush()
            code = "\n".join(lines[i + 1:end]).replace("``" + ZERO_WIDTH + "`", "```")
            content.append({"type": "codeBlock", "content": [{"type": "text", "text": code}] if code else []})
            i = end
        elif heading:
            flush()
            content.append({"type": "heading", "attrs": {"level": len(heading.group(1))},
                            "content": _inline(heading.group(2).strip())})
        elif bullet:
            if paragraph:
                flush()
            bullets.append(bullet.group(1))
        elif not line.strip():
            flush()
        else:
            if bullets:
                flush()
            paragraph.append(line)
        i += 1
    flush()
    return {"type": "doc", "version": 1, "content": content}


def clean_summary(title: str) -> str:
    collapsed = " ".join(title.split())
    return (collapsed or FALLBACK_SUMMARY)[:SUMMARY_MAX]


class JiraConnector:
    def __init__(self, url: str, email: str, api_token: str, issue_type: str = "Task"):
        self.url = url.rstrip("/")
        self.issue_type = issue_type
        self.http = HttpApi(self.url, {"Accept": "application/json"}, _error, auth=httpx.BasicAuth(email, api_token))

    def _ref(self, key: str) -> TicketRef:
        return TicketRef(id=key, url=f"{self.url}/browse/{key}")

    def find_ticket(self, project: str, fingerprint: str) -> TicketRef | None:
        if not SAFE_TOKEN.fullmatch(project) or not SAFE_TOKEN.fullmatch(fingerprint):
            raise TrackerError("project key and fingerprint must be alphanumeric")
        data = self.http.send("POST", "/rest/api/3/search/jql", json={
            "jql": f'project = "{project}" AND labels = "{FP_LABEL}{fingerprint}"',
            "fields": ["key"],
            "maxResults": 1,
        }).json()
        issues = data.get("issues", [])
        return self._ref(issues[0]["key"]) if issues else None

    def create_ticket(self, project: str, title: str, body: str, fingerprint: str) -> TicketRef:
        data = self.http.send("POST", "/rest/api/3/issue", json={"fields": {
            "project": {"key": project},
            "summary": clean_summary(title),
            "issuetype": {"name": self.issue_type},
            "labels": [BOT_LABEL, f"{FP_LABEL}{fingerprint}"],
            "description": to_adf(body),
        }}).json()
        return self._ref(data["key"])

    def link_pr(self, ticket_id: str, pr_url: str, title: str) -> None:
        self.http.send("POST", f"/rest/api/3/issue/{ticket_id}/remotelink", json={
            "globalId": pr_url, "object": {"url": pr_url, "title": title},
        })


def create(secret: dict[str, str], options: dict[str, str]) -> JiraConnector:
    return JiraConnector(url=options["url"], email=secret["email"], api_token=secret["api_token"],
                         issue_type=options.get("issue_type", "Task"))
