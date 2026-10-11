"""MCP 工具冒烟：此前零执行覆盖的工具各 1-2 条真实往返用例。

只依赖既有公开语义（返回文案关键词），全部走 test_mcp_payload 的 _probe
在进程内做真实 MCP 往返；conftest 的 store fixture 已种子好「笔记主题」
（覆盖 notes/ 目录，卡 notes/a.md）。若因并行重构导致个别用例红，给该用例
加 `# leader 重构待对齐` 注释并 skip，不去改源码对齐。
"""

from __future__ import annotations

from yacmemo.store import Store
from yacmemo.search import Searcher

from test_mcp_payload import _probe


# ------------------------------------------------------------ memory_edit

def test_memory_edit_replaces_unique_anchor(store: Store, searcher: Searcher):
    """memory_edit 用唯一锚点就地替换 notes/a.md 并重新索引。"""
    _, r = _probe(store, searcher, "memory_edit",
                  {"path": "notes/a.md", "old_string": "内容A",
                   "new_string": "内容A已改"})
    text = r.content[0].text
    assert "已修改并重新索引: notes/a.md" in text, f"成功文案不符: {text!r}"

    _, r = _probe(store, searcher, "memory_read", {"path_or_title": "notes/a.md"})
    text = r.content[0].text
    assert "内容A已改" in text, f"替换未落盘: {text!r}"
    assert "内容A\n" not in text.replace("内容A已改", ""), "旧内容应已被替换"


def test_memory_edit_rejects_missing_anchor(store: Store, searcher: Searcher):
    """memory_edit 对不存在的锚点报「未找到」而不是静默成功。"""
    _, r = _probe(store, searcher, "memory_edit",
                  {"path": "notes/a.md", "old_string": "冒烟不存在的锚点XYZ",
                   "new_string": "x"})
    text = r.content[0].text
    assert "未找到" in text, f"缺锚点应报未找到: {text!r}"
    assert "notes/a.md" in text, f"报错应指明是哪篇笔记: {text!r}"


# ----------------------------------------------------- memory_edit_section

def test_memory_edit_section_replaces_body(store: Store, searcher: Searcher):
    """memory_edit_section 按 ## 标题整段替换小节体、保留标题行。"""
    _, r = _probe(store, searcher, "memory_write",
                  {"title": "notes/小节笔记.md",
                   "content": "# 小节笔记\n\n## 冒烟小节\n\n旧内容一\n"})
    assert "已写入并索引" in r.content[0].text, \
        f"前置写入失败: {r.content[0].text!r}"

    _, r = _probe(store, searcher, "memory_edit_section",
                  {"path": "notes/小节笔记.md", "heading": "冒烟小节",
                   "new_content": "新内容一\n新内容二"})
    text = r.content[0].text
    assert "已替换小节 '冒烟小节' 并重新索引" in text, f"成功文案不符: {text!r}"

    _, r = _probe(store, searcher, "memory_read",
                  {"path_or_title": "notes/小节笔记.md"})
    text = r.content[0].text
    assert "## 冒烟小节" in text, f"标题行应保留: {text!r}"
    assert "新内容二" in text and "旧内容一" not in text, \
        f"小节体应整段替换: {text!r}"


def test_memory_edit_section_missing_heading_lists_sections(
        store: Store, searcher: Searcher):
    """memory_edit_section 对不存在的小节报未找到并列出现有小节名。"""
    _, r = _probe(store, searcher, "memory_write",
                  {"title": "notes/小节笔记.md",
                   "content": "# 小节笔记\n\n## 冒烟小节\n\n旧内容\n"})
    assert "已写入并索引" in r.content[0].text, \
        f"前置写入失败: {r.content[0].text!r}"

    _, r = _probe(store, searcher, "memory_edit_section",
                  {"path": "notes/小节笔记.md", "heading": "冒烟不存在的小节",
                   "new_content": "x"})
    text = r.content[0].text
    assert "未找到小节标题" in text, f"应报未找到小节标题: {text!r}"
    assert "冒烟不存在的小节" in text, f"报错应回显标题名: {text!r}"
    assert "现有小节" in text and "冒烟小节" in text, \
        f"应列出已有小节名辅助纠错: {text!r}"


# ------------------------------------------------------------- memory_move

def test_memory_move_moves_and_lists(store: Store, searcher: Searcher):
    """memory_move 移动笔记到新路径，回执含新旧路径且列表立即可见。"""
    _, r = _probe(store, searcher, "memory_write",
                  {"title": "notes/x.md", "content": "# x\n内容X\n"})
    assert "已写入并索引: notes/x.md" in r.content[0].text, \
        f"前置写入失败: {r.content[0].text!r}"

    _, r = _probe(store, searcher, "memory_move",
                  {"path": "notes/x.md", "new_path": "notes/y.md"})
    text = r.content[0].text
    assert "已移动: notes/x.md → notes/y.md" in text, f"成功文案不符: {text!r}"

    _, r = _probe(store, searcher, "memory_list", {"path": ""})
    text = r.content[0].text
    assert "notes/y.md" in text, f"移动后新路径应在列表: {text!r}"
    assert "notes/x.md" not in text, f"旧路径不应再出现在列表: {text!r}"


def test_memory_move_rejects_existing_target(store: Store, searcher: Searcher):
    """memory_move 目标路径已存在时拒绝移动。"""
    _, r = _probe(store, searcher, "memory_write",
                  {"title": "notes/x.md", "content": "# x\n内容X\n"})
    assert "已写入并索引: notes/x.md" in r.content[0].text, \
        f"前置写入失败: {r.content[0].text!r}"

    _, r = _probe(store, searcher, "memory_move",
                  {"path": "notes/x.md", "new_path": "notes/b.md"})
    text = r.content[0].text
    assert "目标已存在" in text and "notes/b.md" in text, \
        f"目标已存在应拒绝: {text!r}"


# ----------------------------------------------------------- memory_delete

def test_memory_delete_reports_git_recoverable(store: Store, searcher: Searcher):
    """memory_delete 删除笔记并提示 git 历史可恢复。"""
    _, r = _probe(store, searcher, "memory_write",
                  {"title": "notes/deleteme.md", "content": "# deleteme\n内容D\n"})
    assert "已写入并索引: notes/deleteme.md" in r.content[0].text, \
        f"前置写入失败: {r.content[0].text!r}"

    _, r = _probe(store, searcher, "memory_delete", {"path": "notes/deleteme.md"})
    text = r.content[0].text
    assert "已删除: notes/deleteme.md" in text, f"成功文案不符: {text!r}"
    assert "git 历史可恢复" in text, f"应提示 git 可恢复: {text!r}"


def test_memory_read_after_delete_not_found(store: Store, searcher: Searcher):
    """memory_delete 之后按原标题读取应报「未找到笔记」。"""
    _, r = _probe(store, searcher, "memory_write",
                  {"title": "notes/deleteme.md", "content": "# deleteme\n内容D\n"})
    assert "已写入并索引: notes/deleteme.md" in r.content[0].text, \
        f"前置写入失败: {r.content[0].text!r}"
    _, r = _probe(store, searcher, "memory_delete", {"path": "notes/deleteme.md"})
    assert "已删除" in r.content[0].text, f"前置删除失败: {r.content[0].text!r}"

    _, r = _probe(store, searcher, "memory_read",
                  {"path_or_title": "notes/deleteme.md"})
    text = r.content[0].text
    assert "未找到笔记" in text, f"删除后读取应报未找到: {text!r}"


# ----------------------------------------------------- memory_audit_update

def test_memory_audit_update_rejects_invalid_issue_id(store: Store,
                                                      searcher: Searcher):
    """memory_audit_update 对非法 issue_id 报「issue_id 非法」而非落事件。"""
    _, r = _probe(store, searcher, "memory_audit_update",
                  {"issue_id": "冒烟-非法-ID", "event": "executing"})
    text = r.content[0].text
    assert "issue_id 非法" in text, f"非法 id 应被拒绝: {text!r}"
    assert "冒烟-非法-ID" in text, f"报错应回显传入 id: {text!r}"


# -------------------------------------------------------------- topic_list

def test_topic_list_shows_topic_and_card(store: Store, searcher: Searcher):
    """topic_list 列出种子主题「笔记主题」并给出卡位置。"""
    _, r = _probe(store, searcher, "topic_list", {})
    text = r.content[0].text
    assert "笔记主题" in text, f"种子主题应在列表: {text!r}"
    assert "卡: notes/a.md" in text, f"应给出主题卡位置: {text!r}"
    assert "活跃主题" in text, f"应标明活跃分组: {text!r}"


def test_topic_list_tag_filter_empty(store: Store, searcher: Searcher):
    """topic_list 按不存在的标签过滤返回空结果文案。"""
    _, r = _probe(store, searcher, "topic_list", {"tag": "冒烟无此标签"})
    text = r.content[0].text
    assert "没有标签为「冒烟无此标签」的主题" in text, \
        f"空过滤结果应有明确文案: {text!r}"


# ------------------------------------------------------------ topic_status

def test_topic_status_updates_registry(store: Store, searcher: Searcher):
    """topic_status 更新注册表现状行并在回执中回显新现状。"""
    _, r = _probe(store, searcher, "topic_status",
                  {"title": "笔记主题", "status": "冒烟更新后的现状"})
    text = r.content[0].text
    assert "已更新" in text and "笔记主题" in text, f"成功文案不符: {text!r}"
    assert "冒烟更新后的现状" in text, f"回执应含新现状: {text!r}"


def test_topic_status_rejects_unknown_topic(store: Store, searcher: Searcher):
    """topic_status 对注册表中不存在的主题报错并列出现有主题。"""
    _, r = _probe(store, searcher, "topic_status",
                  {"title": "冒烟不存在的主题", "status": "x"})
    text = r.content[0].text
    assert "注册表中没有主题" in text, f"未知主题应拒绝: {text!r}"
    assert "笔记主题" in text, f"报错应列出现有主题: {text!r}"


# -------------------------------------------------------------- topic_tag

def test_topic_tag_adds_tag_and_lists_all_tags(store: Store, searcher: Searcher):
    """topic_tag 给主题加标签并在回执里给出全库标签清单。"""
    _, r = _probe(store, searcher, "topic_tag",
                  {"title": "笔记主题", "add": "开发"})
    text = r.content[0].text
    assert "已更新「笔记主题」标签" in text, f"成功文案不符: {text!r}"
    assert "全库现有标签" in text and "开发" in text, \
        f"回执应含全库标签清单: {text!r}"


# ------------------------------------------------- topic_register/unregister

def test_topic_register_then_unregister_keeps_notes(store: Store,
                                                    searcher: Searcher):
    """topic_register 注册临时主题后 topic_unregister 注销，笔记文件未动。"""
    _, r = _probe(store, searcher, "topic_register",
                  {"title": "冒烟临时主题", "description": "冒烟用临时主题"})
    text = r.content[0].text
    assert "已注册主题「冒烟临时主题」" in text, f"注册回执不符: {text!r}"
    assert "topics/冒烟临时主题/abstract.md" in text, \
        f"回执应给出卡位置: {text!r}"

    _, r = _probe(store, searcher, "topic_unregister", {"title": "冒烟临时主题"})
    text = r.content[0].text
    assert "已注销主题「冒烟临时主题」" in text, f"注销回执不符: {text!r}"
    assert "笔记文件未动" in text, f"应说明笔记文件未动: {text!r}"
    assert (store.root / "topics/冒烟临时主题/abstract.md").is_file(), \
        "注销后 abstract 文件必须仍在盘上（只是移出注册表）"


# ------------------------------------------- archive_note / unarchive_note

def test_archive_note_then_unarchive_roundtrip(store: Store, searcher: Searcher):
    """archive_note 归档主题内笔记（对客户端不可见），unarchive_note 可逆移回。"""
    _, r = _probe(store, searcher, "memory_write",
                  {"title": "notes/待归档.md", "content": "# 待归档\n内容X\n"})
    assert "已写入并索引: notes/待归档.md" in r.content[0].text, \
        f"前置写入失败: {r.content[0].text!r}"

    _, r = _probe(store, searcher, "archive_note",
                  {"path": "notes/待归档.md", "reason": "冒烟归档"})
    text = r.content[0].text
    assert "已归档" in text, f"归档回执不符: {text!r}"
    assert "不可见" in text, f"回执应说明归档后不可见: {text!r}"
    assert "archive/笔记主题/待归档.md" in text, \
        f"回执应给出归档路径: {text!r}"

    _, r = _probe(store, searcher, "memory_list", {"path": ""})
    assert "待归档.md" not in r.content[0].text, \
        f"归档后不得出现在列表: {r.content[0].text!r}"

    _, r = _probe(store, searcher, "unarchive_note",
                  {"path": "archive/笔记主题/待归档.md"})
    text = r.content[0].text
    assert "已取消归档" in text, f"取消归档回执不符: {text!r}"
    assert "notes/待归档.md" in text, f"回执应给出移回路径: {text!r}"


def test_archive_note_rejects_abstract_card(store: Store, searcher: Searcher):
    """archive_note 对主题卡（abstract）拒绝单独归档并指路 archive_topic。"""
    _, r = _probe(store, searcher, "topic_register",
                  {"title": "冒烟卡主题", "description": "冒烟用"})
    assert "已注册主题" in r.content[0].text, \
        f"前置注册失败: {r.content[0].text!r}"

    _, r = _probe(store, searcher, "archive_note",
                  {"path": "topics/冒烟卡主题/abstract.md"})
    text = r.content[0].text
    assert "主题卡" in text, f"abstract 应拒绝归档: {text!r}"
    assert "archive_topic" in text, f"应指路整主题归档: {text!r}"


# ----------------------------------------------------------- archive_topic

def test_archive_topic_hides_from_client(store: Store, searcher: Searcher):
    """archive_topic 归档整主题：卡移入 archive/ 且对客户端不可见。"""
    _, r = _probe(store, searcher, "topic_register",
                  {"title": "冒烟归档主题", "description": "冒烟用"})
    assert "已注册主题" in r.content[0].text, \
        f"前置注册失败: {r.content[0].text!r}"

    _, r = _probe(store, searcher, "archive_topic", {"title": "冒烟归档主题"})
    text = r.content[0].text
    assert "已归档主题「冒烟归档主题」" in text, f"归档回执不符: {text!r}"
    assert "对客户端不可见" in text, f"回执应说明不可见: {text!r}"


# ------------------------------------------------------------ profile 读写

def test_get_user_preference_without_profile(store: Store, searcher: Searcher):
    """get_user_preference 在尚无 PROFILE.md 时返回建立引导而非报错。"""
    _, r = _probe(store, searcher, "get_user_preference", {})
    text = r.content[0].text
    assert "尚无用户画像" in text, f"无 PROFILE 应返回建立引导: {text!r}"
    assert "update_user_preference" in text, f"引导应指路建卡工具: {text!r}"


def test_update_user_preference_creates_section(store: Store, searcher: Searcher):
    """update_user_preference 新建小节写 PROFILE.md，随后可按小节读回。"""
    _, r = _probe(store, searcher, "update_user_preference",
                  {"section": "冒烟偏好", "content": "- [冒烟] 偏好事实行"})
    text = r.content[0].text
    assert "已更新 PROFILE.md" in text, f"成功文案不符: {text!r}"
    assert "冒烟偏好" in text, f"回执应回显小节名: {text!r}"

    _, r = _probe(store, searcher, "get_user_preference", {"section": "冒烟偏好"})
    text = r.content[0].text
    assert "偏好事实行" in text, f"按小节读回应见写入内容: {text!r}"
