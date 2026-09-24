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


def test_parse_proposal_invalid_raises_with_diagnosis():
    """解析失败必须可诊断：错误带原始返回开头 + 常见原因指引（2026-09-24 实测：
    [curator].model 配成 embedding 模型时回显垃圾内容，旧报错 'Expecting value'
    无从查案）。RuntimeError 不是 ValueError，调用方按宽异常兜底。"""
    with pytest.raises(RuntimeError) as e:
        parse_proposal("这不是 JSON")
    assert "这不是 JSON" in str(e.value)
    assert "embedding" in str(e.value)


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


def test_run_check_same_day_rerun_appends_review(tstore: Store):
    from yacmemo.config import UserEntry

    def llm_with_findings(system: str, user: str) -> str:
        return ('{"summary": "1 条建议。", "findings": ['
                '{"type": "stale-card", "severity": "low", '
                '"paths": ["notes/a.md"], "reason": "现状描述偏旧", '
                '"proposal": "更新主题卡现状"}]}')

    def llm_clean(system: str, user: str) -> str:
        return '{"summary": "无发现。", "findings": []}'

    user = UserEntry(id="tester", root=str(tstore.root))
    run_check(tstore.config, user, dry_run=False, llm_call=llm_with_findings)
    # 同日重跑（复审，无新发现）不得新建同标题笔记（D1 守卫不应被 curator 自身触发）
    report = run_check(tstore.config, user, dry_run=False, llm_call=llm_clean)

    proposals = list((tstore.root / "curator").glob("提案-*.md"))
    assert len(proposals) == 1, "同日重跑应追加复审小节，而不是新建提案文件"
    text = proposals[0].read_text(encoding="utf-8")
    assert text.count("# 记忆质量提案") == 1, "标题必须保持唯一"
    assert "## 复审" in text and "未发现新问题" in text
    assert "待裁决" in report  # 返回值仍是本次复审的完整报告文本


def test_cleanup_audit_snapshots_retention(tstore: Store):
    """curator 顺手清理过期审计快照（文件名日期判旧；处置在 DB、历史在 git）。"""
    from datetime import date

    from yacmemo.curator import cleanup_audit_snapshots

    audit_dir = tstore.root / "journal" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "20200101-000000.md").write_text("# 审计快照 20200101\n", encoding="utf-8")
    (audit_dir / "20200102.md").write_text("# 审计快照 20200102\n", encoding="utf-8")
    (audit_dir / "说明文档.md").write_text("# 无日期命名的文件不清理\n", encoding="utf-8")
    today = date.today().strftime("%Y%m%d")
    (audit_dir / f"{today}.md").write_text(f"# 审计快照 {today}\n", encoding="utf-8")

    n = cleanup_audit_snapshots(tstore, 7)
    assert n == 2
    assert not (audit_dir / "20200101-000000.md").exists()
    assert not (audit_dir / "20200102.md").exists()
    assert (audit_dir / f"{today}.md").exists()
    assert (audit_dir / "说明文档.md").exists()  # 无法判日期的文件不动

    assert cleanup_audit_snapshots(tstore, 0) == 0  # 0 = 永不清理
