import hippo_memory
from hippo_memory import cli


def test_version_constant():
    assert hippo_memory.__version__ == "0.1.0"


def test_cli_version_flag(capsys):
    try:
        cli.main(["--version"])
    except SystemExit as e:
        assert e.code == 0
    assert "hippo 0.1.0" in capsys.readouterr().out
