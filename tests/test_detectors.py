"""Detector tests: title normalization, D1 conflicts, D3 dangling links, parsing."""

from __future__ import annotations

from yacmemo.detectors import (
    d1_scan,
    d3_scan,
    find_title_conflicts,
    normalize_title,
    parse_links,
    parse_observations,
    title_similarity,
)


def test_normalize_strips_suffixes_dates_and_case():
    assert normalize_title("Yacmemo部署配置-2") == normalize_title("yacmemo部署配置")
    assert normalize_title("部署配置(新)") == normalize_title("部署配置")
    assert normalize_title("部署配置-更新") == normalize_title("部署配置")
    assert normalize_title("部署记录 2026-09-13") == normalize_title("部署记录")
    assert normalize_title("部署配置-v3") == normalize_title("部署配置")


def test_normalize_keeps_distinct_topics():
    assert title_similarity("端口配置", "备份策略") < 0.5


def test_find_title_conflicts_threshold():
    existing = [
        {"path": "a.md", "title": "yacmemo部署配置"},
        {"path": "b.md", "title": "备份策略"},
    ]
    conflicts = find_title_conflicts("yacmemo部署配置-2", existing, 0.85)
    assert len(conflicts) == 1
    assert conflicts[0]["path"] == "a.md"

    assert find_title_conflicts("完全不同的主题", existing, 0.85) == []


def test_find_title_conflicts_exclude_self():
    existing = [{"path": "a.md", "title": "端口配置"}]
    assert find_title_conflicts("端口配置", existing, 0.85, exclude_path="a.md") == []


def test_d1_scan_pairwise():
    titles = [
        {"path": "a.md", "title": "yacmemo部署配置"},
        {"path": "b.md", "title": "yacmemo部署配置-2"},
        {"path": "c.md", "title": "备份策略"},
    ]
    out = d1_scan(titles, 0.85)
    assert len(out) == 1
    assert {out[0]["a_path"], out[0]["b_path"]} == {"a.md", "b.md"}


def test_parse_observations():
    content = """# 标题

正文段落。

- [配置] 服务端口为 9721
- [运维] 每日备份 #重要
- 这不是 observation（没有 [类别]）
- [x] 这是 GFM 任务清单勾选项，不是 observation
- [ ] 待办同样不是
"""
    obs = parse_observations(content)
    assert len(obs) == 2
    assert obs[0] == {"category": "配置", "text": "服务端口为 9721", "line": 5}
    assert obs[1]["category"] == "运维"
    assert obs[1]["text"].endswith("#重要")


def test_parse_links_unique_in_order():
    content = "见 [[A]] 和 [[B]]，再见 [[A]]。"
    assert parse_links(content) == ["A", "B"]


def test_d3_scan_dangling_only():
    contents = {
        "a.md": "引用 [[b]] 和 [[ghost]]",
        "b.md": "自足笔记",
    }
    dangling = d3_scan(contents, {"b"})
    assert dangling == [{"path": "a.md", "link": "ghost"}]
