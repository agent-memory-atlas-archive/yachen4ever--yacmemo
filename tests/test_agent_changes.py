"""Agent 接入契约数据源（agent_changes）的自洽性。"""

from __future__ import annotations

from yacmemo.agent_changes import (
    AGENT_CHANGELOG,
    AGENT_CONTRACT_DIGEST,
    AGENT_CONTRACT_VERSION,
    contract_version_key,
)


def test_changelog_head_matches_contract_version():
    """变更记录的最新条目必须是当前契约版本——改契约必追加条目并前进版本。"""
    assert AGENT_CHANGELOG
    newest = max(AGENT_CHANGELOG, key=contract_version_key)
    assert newest == AGENT_CONTRACT_VERSION


def test_contract_version_key_parses():
    assert contract_version_key("0.1.3") == (0, 1, 3)
    assert contract_version_key("v0.2.10") == (0, 2, 10)
    assert contract_version_key("0.2.10") > contract_version_key("0.2.9")
    assert contract_version_key("garbage") == (0,)
    assert contract_version_key("") == (0,)


def test_digest_carries_load_bearing_conventions():
    """速览必须含 topics/ 前缀与 abstract 摘要卡约定（integration_check 的更新载体）。"""
    assert "topics/<主题>/<笔记名>" in AGENT_CONTRACT_DIGEST
    assert "摘要卡" in AGENT_CONTRACT_DIGEST
