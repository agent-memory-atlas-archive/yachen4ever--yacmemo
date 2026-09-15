"""Config tests: the shipped example must stay parseable (regression: broken by a bad patch)."""

from pathlib import Path

import pytest

from yacmemo.config import load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def test_example_config_parses():
    cfg = load_config(str(EXAMPLE))
    assert cfg.config_path == str(EXAMPLE)
    assert [u.id for u in cfg.users] == ["yachen", "user2"]
    assert cfg.curator.enabled is False
    assert cfg.guard.title_similarity_threshold == 0.85


def test_load_config_defaults_without_file():
    cfg = load_config(None)
    assert cfg.config_path is None
    assert cfg.users == []


@pytest.mark.parametrize("bad", [
    "[memory]\nroot='x'\n\n[[users]]\nid='bad id!'\nroot='r'\n",   # illegal id
    "[memory]\nroot='x'\n\n[[users]]\nid='api'\nroot='r'\n",        # reserved id
])
def test_load_config_rejects_bad_user_ids(tmp_path, bad):
    f = tmp_path / "bad.toml"
    f.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(f))
