"""Profile layer tests: PROFILE.md get/update, context placement, stray exemption."""

from __future__ import annotations

import pytest

from yacmemo.store import Store, StoreError


def test_update_creates_profile_with_first_section(store: Store):
    r = store.update_preference("沟通风格", "- [偏好] 直接、重数据")
    assert r["created"] is True
    assert (store.root / "PROFILE.md").is_file()
    text = (store.root / "PROFILE.md").read_text(encoding="utf-8")
    assert text.startswith("# 用户画像与偏好")
    assert "## 沟通风格" in text
    # searchable like any other note
    assert store.db.fts_search("直接、重数据")


def test_update_replaces_and_appends_sections(store: Store):
    store.update_preference("沟通风格", "- [偏好] v1")
    r = store.update_preference("沟通风格", "- [偏好] v2 覆盖")
    assert r.get("created") is None  # replaced via edit_section
    assert "v2 覆盖" in store.get_preference("沟通风格")
    assert "v1" not in store.get_preference("沟通风格")

    store.update_preference("材料偏好", "- [偏好] 公文体")
    full = store.get_preference()
    assert "## 沟通风格" in full and "## 材料偏好" in full
    # both sections survive replacement of the other
    assert "公文体" in full


def test_get_preference_missing_section_errors(store: Store):
    store.update_preference("沟通风格", "- [偏好] x")
    with pytest.raises(StoreError, match="没有小节"):
        store.get_preference("不存在的节")


def test_get_preference_without_file(store: Store):
    assert "尚无" in store.get_preference()
    assert "尚无" in store.get_preference("任意节")


def test_profile_exempt_from_stray_and_context_first(tstore: Store):
    tstore.update_preference("身份", "- [身份] 测试用户")
    assert (tstore.root / "PROFILE.md").is_file()
    # never counted as stray
    assert tstore.audit()["stray"] == []
    # memory_context puts the profile FIRST, before the registry
    ctx = tstore.memory_context()
    assert ctx.index("用户画像与偏好") < ctx.index("主题记忆注册表")


def test_update_preference_empty_section_refused(store: Store):
    with pytest.raises(StoreError, match="小节名不能为空"):
        store.update_preference("  ", "内容")


def test_memory_context_hints_missing_profile(store: Store):
    """PROFILE.md 缺失时冷启动注入引导（与专属必读「尚未创建」同款），
    创建后引导消失。"""
    ctx = store.memory_context()
    assert "用户画像（尚未创建）" in ctx
    assert "update_user_preference" in ctx
    store.update_preference("身份", "- [身份] 测试用户")
    ctx2 = store.memory_context()
    assert "用户画像（尚未创建）" not in ctx2
    assert "用户画像与偏好" in ctx2
