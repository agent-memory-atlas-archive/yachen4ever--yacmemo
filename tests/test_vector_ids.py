"""Vector store: ids with quotes must not break the delete-then-add upsert.

2026-10-03 审查 H2：`_upsert` 把 id 直接插进 SQL 谓词，而同文件
`delete_by_path` 明明写了 `path.replace("'", "''")`——作者知道要转义，
漏了一处。文件名含单引号（`Bob's规则.md`，Debian 上完全合法）时 delete
抛 "Unterminated string literal"，又被 `contextlib.suppress` 吞掉 → 旧行
不删、add 照跑 → 同 id 重复累积。

后果不止是脏数据：search 的 RRF 逐条累加 `1/(k+rank)`，同一路径出现两次
得分就虚高。实测里重复路径 0.0325 压过了双通道命中的正常笔记 0.0323。
"""

from __future__ import annotations

import logging

import lancedb.table
import pytest

from yacmemo.vector import VectorStore

# 会打破朴素 f-string 谓词的三种 id：单引号、SQL 注入形状、引号+反引号混合
TRICKY = [
    "topics/Bob's规则.md",
    "topics/x' OR '1'='1",
    'topics/a"b`c.md',
]


@pytest.fixture
def vec(cfg) -> VectorStore:
    v = VectorStore(cfg.lancedb_path, dimensions=1024)
    return v


def _ids(v: VectorStore) -> list[str]:
    rows = v.search_note_vectors([0.0] * 1024, limit=50)
    return [r["id"] for r in rows]


@pytest.mark.parametrize("path", TRICKY)
def test_reupsert_does_not_accumulate_duplicate_rows(vec: VectorStore, path: str):
    """同一路径 upsert 两次，表里只能有一行。

    另一条不相关的行必须活着：`topics/x' OR '1'='1` 在修前的谓词里是
    **合法 SQL**（`id = 'topics/x' OR '1'='1'` 恒真 = 删全表），旧断言只数
    自己的 id，删表之后它照样是 0 行——这条用例在有洞的代码上是绿的。
    旁边放一个无关行，删表就无所遁形。
    """
    emb = [0.0] * 1024
    emb[0] = 1.0
    keep = "topics/无关笔记.md"
    vec.upsert_note_vector(keep, "不该被牵连", emb)
    vec.upsert_note_vector(path, "第一版", emb)
    vec.upsert_note_vector(path, "第二版", emb)
    ids = _ids(vec)
    assert ids.count(path) == 1, f"同 id 累积成 {ids.count(path)} 行: {ids}"
    assert ids.count(keep) == 1, f"无关行被连带删了（谓词恒真 = 删表）: {ids}"


def test_upsert_replaces_text_of_existing_row(vec: VectorStore):
    """不只是行数：内容也得换成新值（旧行没删就会拿到旧文本）。"""
    path = "topics/Bob's规则.md"
    emb = [0.0] * 1024
    emb[0] = 1.0
    vec.upsert_note_vector(path, "旧文本", emb)
    vec.upsert_note_vector(path, "新文本", emb)
    rows = vec.search_note_vectors(emb, limit=50)
    got = [r["text"] for r in rows if r["id"] == path]
    assert got == ["新文本"], f"旧行没被替换掉: {got}"


@pytest.mark.parametrize("path", TRICKY)
def test_delete_by_path_removes_quoted_id(vec: VectorStore, path: str):
    emb = [0.0] * 1024
    emb[0] = 1.0
    vec.upsert_note_vector(path, "内容", emb)
    vec.delete_by_path(path)
    assert path not in _ids(vec), "含引号的 id 没删掉"


def test_injection_shaped_id_deletes_only_itself(vec: VectorStore):
    """`x' OR '1'='1` 若被插值进谓词就是一次全表删除——只该删自己那行。"""
    emb = [0.0] * 1024
    emb[0] = 1.0
    keep = "topics/正常笔记.md"
    bad = "topics/x' OR '1'='1"
    vec.upsert_note_vector(keep, "正常内容", emb)
    vec.upsert_note_vector(bad, "坏内容", emb)
    assert len(_ids(vec)) == 2

    vec.delete_by_path(bad)
    ids = _ids(vec)
    assert bad not in ids, "注入形状的 id 没删掉"
    assert keep in ids, "同表其它行被误删了"


# ---- 日志注入：路径里的换行能伪造出第二行日志 ----------------------------


def test_newline_in_path_cannot_forge_a_second_log_line(vec: VectorStore,
                                                        caplog, monkeypatch):
    """`notes/a\nERROR 伪造的处置记录` 这样一个路径就能在日志里伪造出第二行。

    pre-delete 失败是这条日志唯一的入口（失败被吞掉、只留一条 warning），
    所以用 monkeypatch 让 delete 抛错来触发它；断言落在渲染后的日志文本上：
    一个值仍占一行，换行被折成字面量（可诊断性不丢）。
    """
    def _boom(self, predicate):
        raise RuntimeError("模拟 delete 失败")

    # LanceTable 覆写了 delete，得打在实际被调用的那个类上
    monkeypatch.setattr(lancedb.table.LanceTable, "delete", _boom)
    emb = [0.0] * 1024
    emb[0] = 1.0
    forged = "notes/a\nERROR 伪造的第二行.md"

    with caplog.at_level(logging.WARNING, logger="yacmemo.vector"):
        vec.upsert_note_vector(forged, "内容", emb)

    msgs = [r.getMessage() for r in caplog.records
            if r.name == "yacmemo.vector" and r.levelno >= logging.WARNING]
    assert msgs, "pre-delete 失败没有留痕，索引劣化将无从排查"
    for m in msgs:
        assert "\n" not in m, f"日志值里有裸换行，可伪造新行: {m!r}"
        assert "\\n" in m, f"换行没被折成字面量，丢掉了可诊断性: {m!r}"
    # caplog.text 是 handler 真正写出的东西——每条记录各占一行
    forged_lines = [ln for ln in caplog.text.splitlines()
                    if "ERROR 伪造的第二行" in ln]
    assert len(forged_lines) == 1, f"伪造出了额外的日志行: {caplog.text}"
    assert "Vector pre-delete" in forged_lines[0], forged_lines[0]
    # 吞掉失败也不能吞掉写入：add 照跑，行仍要在
    assert forged in _ids(vec), "失败分支把写入也丢了"
