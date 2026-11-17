from typing import Any

import httpx

from bed.core.models import BOT_LABEL
from bed.core.ports import TicketRef, TrackerError

API_URL = "https://api.linear.app/graphql"
MARKER = "bed-fingerprint: "

TEAM_QUERY = "query($key: String!) { teams(filter: {key: {eq: $key}}) { nodes { id } } }"
LABEL_QUERY = """query($name: String!, $teamId: ID!) {
  issueLabels(filter: {name: {eq: $name}, team: {id: {eq: $teamId}}}) { nodes { id } }
}"""
LABEL_CREATE = """mutation($input: IssueLabelCreateInput!) {
  issueLabelCreate(input: $input) { success issueLabel { id } }
}"""
ISSUE_CREATE = """mutation($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { identifier url } }
}"""
ISSUE_SEARCH = "query($filter: IssueFilter) { issues(filter: $filter, first: 1) { nodes { identifier url } } }"
LINK_URL = """mutation($issueId: String!, $url: String!, $title: String) {
  attachmentLinkURL(issueId: $issueId, url: $url, title: $title) { success }
}"""


class LinearConnector:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=30)
        self._teams: dict[str, str] = {}
        self._labels: dict[str, str] = {}

    def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(API_URL, json={"query": query, "variables": variables},
                                    headers={"Authorization": self.api_key})
        if response.status_code != 200:
            raise TrackerError(f"Linear API {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if payload.get("errors"):
            raise TrackerError("; ".join(e.get("message", "") for e in payload["errors"]))
        return payload["data"]

    def _team_id(self, key: str) -> str:
        if key not in self._teams:
            nodes = self._gql(TEAM_QUERY, {"key": key})["teams"]["nodes"]
            if not nodes:
                raise TrackerError(f"unknown Linear team: {key}")
            self._teams[key] = nodes[0]["id"]
        return self._teams[key]

    def _label_id(self, team_id: str) -> str:
        if team_id not in self._labels:
            nodes = self._gql(LABEL_QUERY, {"name": BOT_LABEL, "teamId": team_id})["issueLabels"]["nodes"]
            if nodes:
                self._labels[team_id] = nodes[0]["id"]
            else:
                created = self._gql(LABEL_CREATE, {"input": {"name": BOT_LABEL, "teamId": team_id}})
                self._labels[team_id] = created["issueLabelCreate"]["issueLabel"]["id"]
        return self._labels[team_id]

    def find_ticket(self, project: str, fingerprint: str) -> TicketRef | None:
        flt = {"team": {"key": {"eq": project}}, "description": {"contains": f"{MARKER}{fingerprint}"}}
        nodes = self._gql(ISSUE_SEARCH, {"filter": flt})["issues"]["nodes"]
        return TicketRef(id=nodes[0]["identifier"], url=nodes[0]["url"]) if nodes else None

    def create_ticket(self, project: str, title: str, body: str, fingerprint: str) -> TicketRef:
        team_id = self._team_id(project)
        data = self._gql(ISSUE_CREATE, {"input": {
            "teamId": team_id,
            "title": title,
            "description": f"{body.rstrip()}\n\n{MARKER}{fingerprint}",
            "labelIds": [self._label_id(team_id)],
        }})["issueCreate"]
        if not data.get("success"):
            raise TrackerError("issueCreate failed")
        return TicketRef(id=data["issue"]["identifier"], url=data["issue"]["url"])

    def link_pr(self, ticket_id: str, pr_url: str, title: str) -> None:
        self._gql(LINK_URL, {"issueId": ticket_id, "url": pr_url, "title": title})


def create(secret: dict[str, str], options: dict[str, str]) -> LinearConnector:
    return LinearConnector(api_key=secret["api_key"])
