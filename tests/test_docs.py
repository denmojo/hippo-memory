from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_sections_present():
    text = (ROOT / "README.md").read_text()
    for heading in ("## Install", "## The nightly dream", "## Data and privacy",
                    "## Commands", "## Uninstall", "## Platforms"):
        assert heading in text
    assert "not affiliated with" in text.lower()


def test_install_doc_is_agent_runnable():
    text = (ROOT / "docs" / "INSTALL.md").read_text()
    for cmd in ("pipx install", "hippo init", "hippo doctor", "claude setup-token"):
        assert cmd in text


def test_no_owner_or_vault_in_docs():
    for p in (ROOT / "README.md", ROOT / "docs" / "INSTALL.md", ROOT / "CHANGELOG.md"):
        t = p.read_text()
        for needle in ("/Users/", "vault-law", "laurel"):
            assert needle not in t, f"{needle} in {p.name}"
