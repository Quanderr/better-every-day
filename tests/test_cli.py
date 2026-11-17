from pathlib import Path

from bed.cli import main

ROOT = Path(__file__).parents[1]


def test_validate_example_ok(capsys):
    assert main(["validate", str(ROOT / "config/example.yaml")]) == 0


def test_validate_broken_file_reports(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text((ROOT / "config/example.yaml").read_text().replace("vcs: main-github", "vcs: nope", 1))
    assert main(["validate", str(bad)]) == 1
    assert "unknown vcs connector" in capsys.readouterr().out
