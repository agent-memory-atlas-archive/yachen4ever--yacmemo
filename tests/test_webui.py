"""WebUI API tests: notes CRUD, search, audit, collision resolve, usage log."""

from __future__ import annotations

import asyncio
import sqlite3

import httpx


def test_webui_index_served(http_server):
    """已构建 → /ui/ 200；未构建 → 503 带构建指引（服务本身不崩）。"""
    from yacmemo.webui.app import STATIC_DIR

    r = httpx.get(f"http://127.0.0.1:{http_server}/ui/", timeout=5)
    if (STATIC_DIR / "index.html").is_file():
        assert r.status_code == 200
        assert "yacmemo" in r.text
    else:
        assert r.status_code == 503
        assert "build_webui" in r.text


def test_webui_api_notes_crud(http_server):
    base = f"http://127.0.0.1:{http_server}/api/alice"
    # create (via REST, same guard path as MCP)
    r = httpx.post(f"{base}/notes", json={
        "title": "notes/端口配置", "content": "# 端口配置\n\n服务端口为 9721\n"})
    assert r.json()["ok"] is True

    # near-duplicate refused
    r = httpx.post(f"{base}/notes", json={
        "title": "notes/端口配置-2", "content": "x", "force": False})
    body = r.json()
    assert body["ok"] is False and "近似标题" in body["error"]

    # force bypasses (human clicks in WebUI = human confirmation)
    r = httpx.post(f"{base}/notes", json={
        "title": "notes/端口配置-2", "content": "# 端口配置-2\nx", "force": True,
        "force_confirm": True})
    assert r.json()["ok"] is True

    # list
    r = httpx.get(f"{base}/notes")
    paths = [n["path"] for n in r.json()["notes"]]
    assert "notes/端口配置.md" in paths and "notes/端口配置-2.md" in paths

    # read
    r = httpx.get(f"{base}/note", params={"path": "端口配置"})
    assert "9721" in r.json()["content"]

    # save (full-content edit)——save 走确切路径，覆盖已有文件不受注册制拦截
    r = httpx.put(f"{base}/note", json={
        "path": "notes/端口配置", "content": "# 端口配置\n\n服务端口为 8080\n"})
    assert r.json()["ok"] is True
    r = httpx.get(f"{base}/note", params={"path": "端口配置"})
    assert "8080" in r.json()["content"]

    # search via API
    r = httpx.get(f"{base}/search", params={"q": "服务端口"})
    assert any(n["title"] == "端口配置" for n in r.json()["results"])

    # audit
    r = httpx.post(f"{base}/audit")
    a = r.json()["audit"]
    assert "title_duplicates" in a and "guard_stats" in a

    # delete
    r = httpx.delete(f"{base}/note", params={"path": "端口配置-2"})
    assert r.json()["ok"] is True
    r = httpx.get(f"{base}/notes")
    assert "端口配置-2.md" not in [n["path"] for n in r.json()["notes"]]


def test_webui_collision_resolve(http_server, tmp_path):
    """Insert a collision row directly, then resolve it via the WebUI API."""
    db_path = tmp_path / "alice" / ".index" / "index.db"
    conn = sqlite3.connect(db_path)
    cid = "test-collision-1"
    conn.execute(
        "INSERT INTO collisions (id, kind, a_path, b_path, a_text, b_text, score, "
        "detected_at, status) VALUES (?,?,?,?,?,?,?,?, 'open')",
        (cid, "obs", "a.md", "b.md", "A 文本", "B 文本", 0.9, "2026-09-14T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    base = f"http://127.0.0.1:{http_server}/api/alice"
    r = httpx.post(f"{base}/audit")
    a = r.json()["audit"]
    # a.md / b.md notes don't exist → stale pruning removes it; insert again
    # against real notes instead
    if not any(c["id"] == cid for c in a["collisions"]):
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM collisions WHERE id=?", (cid,))
        conn.execute(
            "INSERT INTO notes (path, title, content_hash, updated_at) "
            "VALUES ('a.md','A','x','2026-09-14'), ('b.md','B','y','2026-09-14')")
        conn.execute(
            "INSERT INTO collisions (id, kind, a_path, b_path, a_text, b_text, score, "
            "detected_at, status) VALUES (?,?,?,?,?,?,?,?, 'open')",
            (cid, "obs", "a.md", "b.md", "A 文本", "B 文本", 0.9,
             "2026-09-14T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

    r = httpx.post(f"{base}/collision",
                   json={"id": cid, "status": "dismissed"})
    assert r.json()["ok"] is True

    r = httpx.post(f"{base}/audit")
    a = r.json()["audit"]
    row = next((c for c in a["collisions"] if c["id"] == cid), None)
    if row is not None:  # dismissed rows only show with status filter; open list must not contain it
        assert row["status"] != "open"


def test_usage_logged_for_mcp_calls(http_server, tmp_path):
    """MCP tool calls land in the server-level usage log with client info."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def call():
        async with (streamablehttp_client(
                f"http://127.0.0.1:{http_server}/alice/mcp") as (r, w, _),
                ClientSession(r, w) as s):
            await s.initialize()
            await s.call_tool("memory_write", {
                "title": "notes/调用日志测试", "content": "# 调用日志测试\n内容\n"})
            await s.call_tool("memory_edit", {
                "path": "调用日志测试", "old_string": "内容", "new_string": "内容改"})
            await s.call_tool("memory_search", {"query": "内容改"})

    asyncio.run(call())

    usage_db = tmp_path / "server-data" / "usage.db"
    assert usage_db.is_file()
    conn = sqlite3.connect(usage_db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM call_log WHERE user_id='alice' ORDER BY ts")]
    tools = [r["tool"] for r in rows]
    assert "memory_write" in tools and "memory_search" in tools
    write_row = next(r for r in rows if r["tool"] == "memory_write")
    assert "调用日志测试" in write_row["summary"]
    assert write_row["ok"] == 1
    # edit 的 before_hash 落库（变更前内容 hash，atlas 评审的 before-image 缺口）
    edit_row = next(r for r in rows if r["tool"] == "memory_edit")
    assert edit_row["before_hash"]
    conn.close()


def test_usage_logged_for_webui_mutations(http_server, tmp_path):
    """WebUI 控制台的保存/新建/删除/画像编辑同样落 call_log（client=webui），
    变更类操作带 before_hash——使用记录页不再只见 MCP 不见控制台。"""
    base = f"http://127.0.0.1:{http_server}/api/alice"
    r = httpx.post(f"{base}/notes", json={
        "title": "notes/留痕测试", "content": "# 留痕测试\n"})
    assert r.json()["ok"] is True
    r = httpx.put(f"{base}/note", json={
        "path": "notes/留痕测试", "content": "# 留痕测试\n改\n"})
    assert r.json()["ok"] is True
    r = httpx.delete(f"{base}/note", params={"path": "留痕测试"})
    assert r.json()["ok"] is True
    r = httpx.put(f"{base}/profile", json={
        "section": "留痕小节", "content": "- [测试] 值"})
    assert r.json()["ok"] is True

    usage_db = tmp_path / "server-data" / "usage.db"
    conn = sqlite3.connect(usage_db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM call_log WHERE tool LIKE 'webui:%' AND user_id='alice'")]
    conn.close()
    tools = {r["tool"] for r in rows}
    assert {"webui:note_create", "webui:note_save", "webui:note_delete",
            "webui:profile_save"} <= tools
    save_row = next(r for r in rows if r["tool"] == "webui:note_save")
    assert save_row["client"] == "webui" and save_row["ok"] == 1
    assert save_row["before_hash"]  # 覆盖已有文件：有变更前内容 hash
    del_row = next(r for r in rows if r["tool"] == "webui:note_delete")
    assert del_row["before_hash"]


def test_webui_overview(http_server):
    r = httpx.get(f"http://127.0.0.1:{http_server}/api/overview")
    data = r.json()
    assert data["ok"] is True
    ids = {u["id"] for u in data["users"]}
    assert ids == {"alice", "bob"}
    assert "embedding" in data and data["embedding"]["configured"] is False


def test_config_get_save_roundtrip(http_server, tmp_path):
    # GET: 内容含用户段，path 指向 tmp 配置
    r = httpx.get(f"http://127.0.0.1:{http_server}/api/config", timeout=5).json()
    assert r["ok"] is True and "[[users]]" in r["content"]
    assert r["path"].endswith("config.toml")

    # POST 合法修改 → 保存 + 备份
    new_content = r["content"] + "\n# edited-by-test\n"
    r = httpx.post(f"http://127.0.0.1:{http_server}/api/config",
                   json={"content": new_content}, timeout=5).json()
    assert r["ok"] is True and r["backup"]
    assert (tmp_path / "config.toml").read_text(encoding="utf-8").endswith("# edited-by-test\n")
    backups = list((tmp_path).glob("config.toml.bak-*"))
    assert backups, "backup file should exist"

    # POST 非法 TOML → 拒绝
    r = httpx.post(f"http://127.0.0.1:{http_server}/api/config",
                   json={"content": "not [ valid toml"}, timeout=5).json()
    assert r["ok"] is False and "TOML" in r["error"]

    # POST 合法 TOML 但非法用户 id → 结构校验拒绝
    bad = httpx.get(f"http://127.0.0.1:{http_server}/api/config", timeout=5).json()["content"]
    bad = bad.replace('id = "bob"', 'id = "bad id!"')
    r = httpx.post(f"http://127.0.0.1:{http_server}/api/config",
                   json={"content": bad}, timeout=5).json()
    assert r["ok"] is False and "校验失败" in r["error"]


def test_proposals_list_empty(http_server):
    r = httpx.get(f"http://127.0.0.1:{http_server}/api/alice/proposals", timeout=5).json()
    assert r["ok"] is True and r["proposals"] == []


def test_curator_run_requires_config(http_server):
    r = httpx.post(f"http://127.0.0.1:{http_server}/api/alice/curator", timeout=5).json()
    assert r["ok"] is False and "未配置" in r["error"]


def test_audit_snapshot_written_and_listed(http_server):
    """确定性审计落盘 journal/audit/ 快照，且历史目录可见。"""
    base = f"http://127.0.0.1:{http_server}/api/alice"
    r = httpx.post(f"{base}/notes", json={
        "title": "notes/审计快照测试", "content": "# 审计快照测试\n内容\n"})
    assert r.json()["ok"] is True

    r = httpx.post(f"{base}/audit", timeout=5).json()
    assert r["ok"] is True
    af = r["audit"]["audit_file"]
    assert af.startswith("journal/audit/") and af.endswith(".md")

    runs = httpx.get(f"{base}/audit/runs", timeout=5).json()["runs"]
    assert any(x["path"] == af for x in runs)
    note = httpx.get(f"{base}/note", params={"path": af}, timeout=5).json()
    assert "## 处置记录" in note["content"]


def test_audit_disposition_appends_and_syncs_d2(http_server, tmp_path):
    """处置记录追加进快照；D2 处置同步 index 状态，重跑不再 open。"""
    base = f"http://127.0.0.1:{http_server}/api/alice"
    for t in ("甲记录", "乙记录"):
        r = httpx.post(f"{base}/notes", json={"title": f"notes/{t}", "content": f"# {t}\n内容\n"})
        assert r.json()["ok"] is True
    db_path = tmp_path / "alice" / ".index" / "index.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO collisions (id, kind, a_path, b_path, a_text, b_text, score, "
        "detected_at, status) VALUES ('d2-1','obs','notes/甲记录.md','notes/乙记录.md','A','B',"
        "0.9,'2026-09-14T00:00:00+00:00','open')")
    conn.commit()
    conn.close()

    r = httpx.post(f"{base}/audit", timeout=5).json()
    af = r["audit"]["audit_file"]
    assert any(c["id"] == "d2-1" for c in r["audit"]["collisions"])

    # 处置：已处理 → 快照追加处置行 + D2 状态同步
    r = httpx.post(f"{base}/audit/action", timeout=5, json={
        "file": af, "id": "D2:d2-1", "action": "resolved",
        "label": "合并内容", "note": "测试备注"})
    body = r.json()
    assert body["ok"] is True
    assert "合并内容" in body["content"] and "测试备注" in body["content"]

    # 重跑审计 → D2 不再出现在 open 撞车
    r = httpx.post(f"{base}/audit", timeout=5).json()
    assert all(c["id"] != "d2-1" for c in r["audit"]["collisions"])


def test_audit_last_and_actions(http_server):
    """audit/last 缓存最近一次结果；audit/actions 读处置历史（表是权威源）。"""
    base = f"http://127.0.0.1:{http_server}/api/alice"
    httpx.post(f"{base}/notes", json={
        "title": "notes/快照测试甲", "content": "# 快照测试甲\n内容\n"})

    r = httpx.get(f"{base}/audit/last", timeout=5).json()
    assert r["ok"] is True and r["audit"] is None  # 未审计时为空

    httpx.post(f"{base}/audit", timeout=5)
    r = httpx.get(f"{base}/audit/last", timeout=5).json()
    assert r["ok"] is True and r["audit"] is not None
    assert r["audit"]["audit_file"].startswith("journal/audit/")
    assert r["ts"] > 0

    # 处置一条 → actions 表可读
    r = httpx.post(f"{base}/audit/action", timeout=5, json={
        "file": r["audit"]["audit_file"], "id": "D4:孤儿.md",
        "action": "resolved", "label": "测试归位"})
    assert r.json()["ok"] is True
    r = httpx.get(f"{base}/audit/actions", timeout=5).json()
    kinds = [(a["kind"], a["action"]) for a in r["actions"]]
    assert ("D4", "resolved") in kinds


def test_proposal_action_adjudication(http_server, tmp_path):
    """提案裁决：audit_actions 记 P 类 + 提案笔记追加裁决留痕。"""
    base = f"http://127.0.0.1:{http_server}/api/alice"
    r = httpx.post(f"{base}/notes", json={
        "title": "curator/提案-测试",
        "content": ("# 记忆质量提案（alice，2026-09-17）\n\n## 总评\n测试\n\n"
                    "## 提案（1 条）\n\n"
                    "1. **[high] duplicate** — 两篇疑似重复\n"
                    "   - 涉及: a.md、b.md\n"
                    "   - 建议: 合并两篇\n\n"
                    "> 裁决后在本行下追加执行记录。\n")})
    assert r.json()["ok"] is True

    r = httpx.post(f"{base}/proposal/action", timeout=5, json={
        "file": "curator/提案-测试.md", "index": 1, "action": "adopted",
        "type": "duplicate", "reason": "两篇疑似重复"})
    body = r.json()
    assert body["ok"] is True
    assert "## 裁决记录" in body["content"] and "已采纳 第1条 [duplicate]" in body["content"]

    r = httpx.get(f"{base}/audit/actions", timeout=5).json()
    assert any(a["kind"] == "P" and a["action"] == "adopted" for a in r["actions"])

    # 重裁决（幂等 upsert）：改为忽略
    r = httpx.post(f"{base}/proposal/action", timeout=5, json={
        "file": "curator/提案-测试.md", "index": 1, "action": "dismissed",
        "type": "duplicate", "reason": "误报"})
    body = r.json()
    assert "已忽略 第1条" in body["content"]
    r = httpx.get(f"{base}/audit/actions", timeout=5).json()
    p_rows = [a for a in r["actions"] if a["kind"] == "P"]
    assert len(p_rows) == 1 and p_rows[0]["action"] == "dismissed"


def test_webui_create_outside_topics_refused(http_server):
    """REST 建笔记与 MCP 同守卫：未覆盖路径在 HTTP 层也拒绝。"""
    base = f"http://127.0.0.1:{http_server}/api/alice"
    r = httpx.post(f"{base}/notes", json={"title": "test/散记", "content": "x"})
    body = r.json()
    assert body["ok"] is False and "不属于任何注册主题" in body["error"]


def test_deleting_last_snapshot_clears_audit_cache(http_server):
    """手动删除/过期清理最近一次审计快照后，audit/last 缓存必须联动清除，
    否则审计页对着已删除的文件报"未找到笔记"（2026-09-18 用户实测）。"""
    base = f"http://127.0.0.1:{http_server}/api/alice"
    httpx.post(f"{base}/notes", json={
        "title": "notes/快照删除测试", "content": "# 快照删除测试\n内容\n"})
    r = httpx.post(f"{base}/audit", timeout=5).json()
    af = r["audit"]["audit_file"]
    assert af.startswith("journal/audit/")

    # 缓存存在
    r = httpx.get(f"{base}/audit/last", timeout=5).json()
    assert r["audit"] is not None

    # 删除快照 → 缓存联动清除
    r = httpx.delete(f"{base}/note", params={"path": af}, timeout=5).json()
    assert r["ok"] is True
    r = httpx.get(f"{base}/audit/last", timeout=5).json()
    assert r["audit"] is None


def test_identity_api_list_and_create(http_server):
    """identity 页 API：确定性 token 生成 + agents/ 目录扫描。"""
    base = f"http://127.0.0.1:{http_server}/api/alice"

    # 人类经 WebUI（identity=None = 管理员）可直接写 agents/ 种子目录
    r = httpx.post(f"{base}/notes", json={
        "title": "agents/hermes/必读", "content": "# 必读\n- 指针与纪律\n"})
    assert r.json()["ok"] is True

    # 创建 identity：token = <device>_<agent> 确定性拼接
    r = httpx.post(f"{base}/identities",
                   json={"agent": "hermes", "device": "r9000x"})
    body = r.json()
    assert body["ok"] is True
    assert body["token"] == "r9000x_hermes"
    assert body["agent_dir"] == "agents/hermes/"
    assert body["device_dir"] == "agents/hermes/r9000x/"

    # 非法 slug 被拒（含下划线会被当成 token 分隔符）
    r = httpx.post(f"{base}/identities",
                   json={"agent": "her_agent", "device": "r9000x"})
    body = r.json()
    assert body["ok"] is False and "非法" in body["error"]

    # 列表：目录扫描 + 已创建登记合入——刚创建、尚无文件的 identity 也要可见
    r = httpx.post(f"{base}/identities",
                   json={"agent": "teleagent", "device": "m5air"})
    assert r.json()["ok"] is True
    r = httpx.get(f"{base}/identities")
    rows = r.json()["identities"]
    hermes = next(x for x in rows if x["agent"] == "hermes")
    assert hermes["shared_files"] == 1
    assert hermes["devices"] == [
        {"device": "r9000x", "notes": 0, "last_mtime": 0, "active": False}]
    tele = next(x for x in rows if x["agent"] == "teleagent")
    assert tele["devices"] == [
        {"device": "m5air", "notes": 0, "last_mtime": 0, "active": False}]


def test_webui_password_flow(http_server_auth):
    """[webui].password 启用后：API 401、ui 回登录页、登录后放行。"""
    base = f"http://127.0.0.1:{http_server_auth}"

    # 未登录：API 401，ui 返回登录页
    r = httpx.get(f"{base}/api/overview", timeout=5)
    assert r.status_code == 401 and "未登录" in r.json()["error"]
    r = httpx.get(f"{base}/ui/", timeout=5)
    assert r.status_code == 200 and "doLogin" in r.text

    # 错误密码
    r = httpx.post(f"{base}/api/login", json={"password": "wrong"}, timeout=5)
    assert r.status_code == 401

    # 正确密码 → cookie → 后续请求放行
    with httpx.Client() as c:
        r = c.post(f"{base}/api/login", json={"password": "secret"}, timeout=5)
        assert r.json()["ok"] is True
        r = c.get(f"{base}/api/overview", timeout=5)
        assert r.status_code == 200 and r.json()["ok"] is True
