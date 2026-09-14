"""Curator tests: material building, proposal parsing, report writing (no network)."""

from __future__ import annotations

import pytest

from yacmemo.curator import build_material, parse_proposal, run_check
from yacmemo.store import Store


def test_parse_proposal_plain_and_fenced():
    raw = '{"summary": "s", "findings": [{"type": "duplicate", "severity": "high"}]}'
    assert parse_proposal(raw)["findings"][0]["type"] == "duplicate"

    fenced = '```json\n{"summary": "s2", "findings": []}\n```'
    assert parse_proposal(fenced)["summary"] == "s2"


def test_parse_proposal_invalid_raises():
    with pytest.raises(ValueError):
        parse_proposal("这不是 JSON")


def test_build_material_contains_registry_cards_and_audit(tstore: Store):
    tstore.topic_register("测试主题", description="用于构建材料")
    material = build_material(tstore)
    assert "主题注册表" in material
    assert "测试主题" in material
    assert "dangling_links" in material  # 审计结果 JSON 段


def test_run_check_writes_proposal_report(tstore: Store):
    from yacmemo.config import UserEntry

    def fake_llm(system: str, user: str) -> str:
        assert "质量审查员" in system
        assert "测试主题" in user
        return ('{"summary": "整体健康，1 条建议。", "findings": ['
                '{"type": "stale-card", "severity": "low", '
                '"paths": ["notes/a.md"], "reason": "现状描述偏旧", '
                '"proposal": "更新主题卡现状"}]}')

    user = UserEntry(id="tester", root=str(tstore.root))
    report = run_check(tstore.config, user, dry_run=True, llm_call=fake_llm)
    assert "待裁决" in report and "整体健康" in report

    # 非 dry-run：报告落盘到 curator/
    report2 = run_check(tstore.config, user, dry_run=False, llm_call=fake_llm)
    assert "待裁决" in report2
    proposals = list((tstore.root / "curator").glob("提案-*.md"))
    assert proposals, "proposal report note should be saved"
    # 报告本身入库后可被检索
    assert tstore.db.fts_search("待裁决")
