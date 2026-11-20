import json
from datetime import UTC, datetime

import pytest
import respx
from helpers import make_finding

from bed.connectors.slack import create
from bed.connectors.slack.connector import NotifierError, render
from bed.core.models import RunRecord
from bed.core.ports import Summary

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def published(i, **kw):
    return make_finding(f"fp{i}", title=f"Fix {i}", pr_url=f"https://gh/pull/{i}", ticket_id=f"WEB-{i}",
                        ticket_url=f"https://lin/WEB-{i}", pr_number=i, **kw)


def text(blocks):
    return json.dumps(blocks, ensure_ascii=False)


def test_render_includes_links_and_omits_empty():
    blocks = render(Summary(opened=[published(1)]))
    t = text(blocks)
    assert "better-every-day — 1 new PRs" in t
    assert "<https://gh/pull/1|Fix 1> · acme/web · <https://lin/WEB-1|WEB-1>" in t
    assert "Waiting for review" not in t and "Failed" not in t and "Skipped" not in t


def test_render_skipped_and_failed():
    runs = [RunRecord(run_id="r", repo="acme/api", status="baseline_red", started_at=NOW)]
    skipped = [RunRecord(run_id="s", repo="acme/web", status="skipped_backpressure", started_at=NOW)]
    t = text(render(Summary(failed=runs, skipped=skipped, waiting=[published(2)])))
    assert "acme/api — baseline_red" in t and "Skipped (review backlog)" in t and "Waiting for review" in t
    assert "0 new PRs" in t


def test_section_capped_at_20():
    t = text(render(Summary(opened=[published(i) for i in range(25)])))
    assert "Fix 19" in t and "Fix 20" not in t and "+5 more" in t


def test_escapes_mrkdwn_in_titles():
    t = text(render(Summary(opened=[published(1).model_copy(update={"title": "a<b>&c|d"})])))
    assert "a&lt;b&gt;&amp;c" in t


def test_send_posts_channel_and_blocks():
    slack = create({"bot_token": "xoxb-1"}, {"channel": "#bed"})
    with respx.mock() as mock:
        route = mock.post("https://slack.com/api/chat.postMessage").respond(200, json={"ok": True})
        slack.send(Summary(opened=[published(1)]))
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer xoxb-1"
    payload = json.loads(req.content)
    assert payload["channel"] == "#bed" and payload["blocks"] and payload["text"]


def test_send_not_ok_raises():
    slack = create({"bot_token": "x"}, {"channel": "#bed"})
    with respx.mock() as mock, pytest.raises(NotifierError, match="channel_not_found"):
        mock.post("https://slack.com/api/chat.postMessage").respond(200, json={"ok": False,
                                                                               "error": "channel_not_found"})
        slack.send(Summary(opened=[published(1)]))


def test_channel_required():
    with pytest.raises(KeyError):
        create({"bot_token": "x"}, {})


def test_section_text_under_slack_limit():
    long = [published(i).model_copy(update={"title": "T" * 180}) for i in range(20)]
    blocks = render(Summary(opened=long))
    for b in blocks[1:]:
        assert len(b["text"]["text"]) <= 3000
    assert "more" in json.dumps(blocks)


def test_render_stuck_section():
    t = text(render(Summary(stuck=[make_finding("fp", status="publishing", title="Half done")])))
    assert "Stuck publishing" in t and "Half done" in t


def test_render_without_ticket_omits_ticket_part():
    finding = make_finding("fp9", title="Fix 9", pr_url="https://gh/pull/9", pr_number=9)
    t = text(render(Summary(opened=[finding])))
    assert "<https://gh/pull/9|Fix 9> · acme/web\"" in t
