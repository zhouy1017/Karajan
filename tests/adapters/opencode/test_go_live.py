import json

import pytest
from karajan.adapters.opencode.go_live import GoLiveProbe, main


def test_cli_requires_live_before_reading_credential_or_runtime(tmp_path, capsys):
    output = tmp_path / "new-output"
    assert (
        main(
            [
                "--runtime",
                str(tmp_path / "missing-runtime"),
                "--directory",
                str(output),
                "--credential-file",
                str(tmp_path / "missing-key"),
                "--scenario",
                "edit",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "LIVE_AUTHORIZATION_REQUIRED"
    assert report["profile_enabled"] is False
    assert not output.exists()


def test_unknown_scenario_cannot_read_missing_files(tmp_path):
    probe = GoLiveProbe(tmp_path / "missing-runtime", tmp_path / "output", tmp_path / "missing-key")
    with pytest.raises(ValueError, match="UNKNOWN_SCENARIO"):
        probe.run("anything", live=True)
    assert not (tmp_path / "output").exists()
