import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "ci_terraform", Path(__file__).resolve().parents[1] / "scripts/ci/terraform.py"
)
terraform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(terraform)


def test_diagnostics_never_print_config_or_raw_errors(capsys):
    secret = "private-config-do-not-publish"
    events = [
        {"type": "outputs", "outputs": {"config": secret}},
        {"type": "diagnostic", "diagnostic": {
            "severity": "error", "summary": secret,
            "detail": f"{secret}: not authorized to perform: s3:GetBucketPolicy on secret ARN",
            "snippet": {"code": secret},
        }},
        {"type": "diagnostic", "diagnostic": {
            "severity": "error", "summary": "Invalid function argument", "detail": secret,
        }},
        {"type": "change_summary", "changes": {"add": 1, "change": 2, "remove": 0}},
    ]
    terraform.report("\n".join(json.dumps(event) for event in events) + "\n" + secret, secret)
    output = capsys.readouterr().out
    assert secret not in output
    assert "secret ARN" not in output
    assert "s3:GetBucketPolicy" in output
    assert "Invalid function argument" in output
    assert "add=1 change=2 remove=0" in output


def test_exit_status_and_json_mode_are_preserved(capsys):
    with patch.object(terraform.subprocess, "run", return_value=Mock(
        returncode=1, stdout="private config", stderr="private credentials"
    )) as run:
        assert terraform.main(["-chdir=infra", "plan", "-lock=false"]) == 1
    assert run.call_args.args[0] == ["terraform", "-chdir=infra", "plan", "-json", "-lock=false"]
    output = capsys.readouterr().out
    assert "private config" not in output
    assert "private credentials" not in output
