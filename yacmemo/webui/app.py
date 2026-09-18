"""WebUI: JSON API + Vue 3 frontend build for yacmemo-server.

Mounted under the same Starlette app as the MCP endpoints — one process, one
port. The API reuses each user's Store/Searcher/IndexDB directly (no second
data path). Route order matters: /api/* and /ui/* are registered BEFORE the
per-user MCP mounts so user ids can never shadow them (config also reserves
those ids).

Frontend: Vue 3 + Naive UI, built by scripts/build_webui.sh (npm) into
STATIC_DIR. Without a build the service still starts; /ui/ answers 503 with
build instructions while MCP/API remain fully functional.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
import tomllib
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..config import Config

logger = logging.getLogger(__name__)

# Vite 构建产物目录（scripts/build_webui.sh 生成；git 不跟踪，随部署同步）
STATIC_DIR = Path(__file__).parent / "dist"


def _ok(payload: dict) -> JSONResponse:
    return JSONResponse({"ok": True, **payload})


def _err(msg: str, status: int = 200) -> JSONResponse:
    # Guard refusals and user errors are normal outcomes → HTTP 200 with ok:false
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def create_webui_routes(config: Config, contexts: dict[str, dict]) -> list[Route]:
    """Build the WebUI routes. contexts: {user_id: {store, searcher, db, usage}}."""

    def _ctx(user_id: str) -> dict:
        ctx = contexts.get(user_id)
        if not ctx:
            raise KeyError(user_id)
        return ctx

    async def index(request: Request):
        return RedirectResponse("/ui/", status_code=307)

    async def ui_index(request: Request):
        index_file = STATIC_DIR / "index.html"
        if not index_file.is_file():
            return PlainTextResponse(
                "WebUI 前端未构建：请运行 scripts/build_webui.sh"
                "（或 cd frontend && npm run build）后重试",
                status_code=503,
            )
        return FileResponse(index_file)

    async def overview(request: Request):
        def _collect():
            users = []
            for uid, c in contexts.items():
                topics = c["store"].load_topics()
                active = [t for t in topics if not t.get("archived")]
                archived = [t for t in topics if t.get("archived")]
                curator_dir = c["store"].root / "curator"
                proposals = (
                    len(list(curator_dir.glob("提案-*.md")))
                    if curator_dir.is_dir() else 0)
                users.append({
                    "id": uid,
                    "note_count": len(c["store"].list_notes()),
                    "open_collisions": len(c["db"].list_collisions(status="open")),
                    "guard": c["db"].guard_stats(),
                    "topics": [{"title": t["title"], "card": t["card"],
                                "status": t["status"]} for t in active],
                    "archived_topics": [{"title": t["title"], "card": t["card"]}
                                        for t in archived],
                    "curator_proposals": proposals,
                    "git_status": c["store"].snapshots.status_line(),
                })
            return users

        users = await run_in_threadpool(_collect)
        emb = config.embedding
        return _ok({
            "users": users,
            "embedding": {"configured": bool(emb.base_url and emb.model),
                          "model": emb.model, "dimensions": emb.dimensions},
            "calls_today": usage_day_total(contexts),
        })

    def usage_db(config) -> object | None:
        for c in contexts.values():
            return c["usage"]
        return None

    def usage_day_total(contexts) -> int:
        u = usage_db(config)
        if not u:
            return 0
        days = u.day_counts(days=1)
        return days[0]["calls"] if days else 0

    async def usage_recent(request: Request):
        u = usage_db(config)
        if not u:
            return _ok({"rows": []})
        rows = await run_in_threadpool(
            u.recent,
            int(request.query_params.get("limit", 100)),
            request.query_params.get("user") or None,
            request.query_params.get("tool") or None,
        )
        return _ok({"rows": rows})

    async def usage_clients(request: Request):
        u = usage_db(config)
        if not u:
            return _ok({"rows": []})
        return _ok({"rows": await run_in_threadpool(u.client_summary)})

    async def usage_days(request: Request):
        u = usage_db(config)
        if not u:
            return _ok({"rows": []})
        return _ok({"rows": await run_in_threadpool(u.day_counts, 14)})

    async def notes_list(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        rows = await run_in_threadpool(c["store"].list_notes,
                                       request.query_params.get("path", ""),
                                       request.query_params.get("sort", "name"))
        items = []
        for rel in rows:
            p = c["store"].root / rel
            row = c["db"].get_note(rel)
            items.append({
                "path": rel,
                "title": row["title"] if row else rel.rsplit("/", 1)[-1].removesuffix(".md"),
                "mtime": int(p.stat().st_mtime) if p.is_file() else 0,
                "size": p.stat().st_size if p.is_file() else 0,
            })
        return _ok({"notes": items})

    async def note_get(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        path = request.query_params.get("path", "")
        try:
            r = await run_in_threadpool(c["store"].read, path)
        except Exception as e:
            return _err(str(e))
        return _ok({"path": r["path"], "title": r["title"], "content": r["content"]})

    async def note_save(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        body = await _body(request)
        try:
            r = await run_in_threadpool(c["store"].save,
                                        body.get("path", ""), body.get("content", ""))
        except Exception as e:
            return _err(str(e))
        return _ok(r)

    async def note_create(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        body = await _body(request)
        try:
            r = await run_in_threadpool(
                c["store"].write, body.get("title", ""), body.get("content", ""),
                force=bool(body.get("force")),
                force_confirm=bool(body.get("force_confirm", True)),
            )
        except Exception as e:
            return _err(str(e))
        return _ok(r)

    async def note_delete(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        path = request.query_params.get("path", "")
        try:
            r = await run_in_threadpool(c["store"].delete_note, path)
        except Exception as e:
            return _err(str(e))
        return _ok(r)

    async def search(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        q = request.query_params.get("q", "")
        if not q.strip():
            return _err("空的查询")
        try:
            results = await run_in_threadpool(
                c["searcher"].search, q,
                int(request.query_params.get("limit", 10)),
                request.query_params.get("kind", "hybrid"),
            )
        except Exception as e:
            return _err(str(e))
        return _ok({"results": [
            {k: r.get(k) for k in ("path", "title", "score", "channels", "warnings")}
            for r in results],
            # 向量通道降级 / 短查询提示（MCP/WebUI 同源，见 searcher.last_notice）
            "notice": c["searcher"].last_notice})

    async def audit(request: Request):
        try:
            uid = request.path_params["user"]
            c = _ctx(uid)
        except KeyError:
            return _err("未知用户", 404)
        try:
            r = await run_in_threadpool(c["store"].audit)
        except Exception as e:
            return _err(str(e))
        # store.audit() 已把结果写进 store.last_audit（MCP/WebUI 共享缓存）
        return _ok({"audit": r})

    async def audit_last(request: Request):
        """最近一次审计结果（内存缓存）：页面加载即显示待处置，不必重跑。
        缓存挂在 Store 上——MCP memory_audit 与本端点互通。"""
        try:
            uid = request.path_params["user"]
            c = _ctx(uid)
        except KeyError:
            return _err("未知用户", 404)
        return _ok(c["store"].last_audit or {"audit": None})

    async def audit_actions_list(request: Request):
        """处置历史全量（audit_actions 表——处置的持久化权威，快照内嵌节只是轨迹）。"""
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        return _ok({"actions": c["db"].list_audit_actions()})

    async def proposal_action(request: Request):
        """裁决 curator 提案条目：持久化 + 提案笔记留痕；执行仍由 agent 按留痕进行。"""
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        body = await _body(request)
        try:
            r = await run_in_threadpool(
                c["store"].record_proposal_action,
                body.get("file", ""), int(body.get("index", 0)),
                body.get("action", ""), body.get("type", ""),
                body.get("reason", ""), body.get("note", ""),
            )
        except Exception as e:
            return _err(str(e))
        try:
            note = await run_in_threadpool(c["store"].read, r["path"])
            r["content"] = note["content"]
        except Exception:
            pass
        return _ok(r)

    async def audit_runs(request: Request):
        """历史审计快照目录（journal/audit/*.md，按时间倒序）。"""
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        d = c["store"].root / "journal" / "audit"
        files = sorted(d.glob("*.md"), reverse=True) if d.is_dir() else []
        return _ok({"runs": [
            {"file": f.name, "path": f"journal/audit/{f.name}",
             "mtime": int(f.stat().st_mtime), "size": f.stat().st_size}
            for f in files]})

    async def audit_action(request: Request):
        """记录人类对审计问题的处置（追加进快照 + 持久化，D2 同步撞车状态）。"""
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        body = await _body(request)
        try:
            path = await run_in_threadpool(
                c["store"].record_audit_action,
                body.get("file", ""), body.get("id", ""),
                body.get("action", ""), body.get("label", ""),
                body.get("note", ""),
            )
        except Exception as e:
            return _err(str(e))
        try:
            r = await run_in_threadpool(c["store"].read, path)
        except Exception:
            return _ok({"path": path})
        return _ok({"path": path, "content": r["content"]})

    async def reindex(request: Request):
        """Full rebuild: wipe derived state, re-walk all files, re-detect D2."""
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        try:
            r = await run_in_threadpool(c["store"].reindex)
        except Exception as e:
            return _err(str(e))
        return _ok(r)

    async def collision_resolve(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        body = await _body(request)
        try:
            await run_in_threadpool(c["db"].resolve_collision,
                                    body.get("id", ""), body.get("status", ""))
        except Exception as e:
            return _err(str(e))
        return _ok({})

    # ---- curator（深度审查：LLM 提案，只提案不执行）----

    async def curator_run(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        if not (config.curator.base_url and config.curator.model):
            return _err("curator 未配置：请在 设置 页的 [curator] 节填写 base_url / model")

        def _run():
            from yacmemo.curator import run_check
            return run_check(config, c["user"], dry_run=False)

        try:
            report = await run_in_threadpool(_run)
        except Exception as e:
            return _err(f"深度审查失败: {e}")
        return _ok({"report": report})

    async def proposals_list(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        d = c["store"].root / "curator"
        files = sorted(d.glob("提案-*.md"), reverse=True) if d.is_dir() else []
        return _ok({"proposals": [
            {"file": f.name, "path": f"curator/{f.name}",
             "mtime": int(f.stat().st_mtime)} for f in files]})

    # ---- 主题 ----

    async def topics_list(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        topics = await run_in_threadpool(c["store"].load_topics)
        active = [{"title": t["title"], "card": t["card"],
                   "status": t["status"], "related": t["related"]}
                  for t in topics if not t.get("archived")]
        archived = [{"title": t["title"], "card": t["card"],
                     "status": t["status"]}
                    for t in topics if t.get("archived")]
        return _ok({"active": active, "archived": archived})

    async def topic_archive(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        body = await _body(request)
        try:
            r = await run_in_threadpool(c["store"].archive_topic,
                                        body.get("title", ""))
        except Exception as e:
            return _err(str(e))
        return _ok(r)

    # ---- 画像/偏好 ----

    async def profile_get(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        section = request.query_params.get("section", "")
        try:
            text = await run_in_threadpool(c["store"].get_preference, section)
        except Exception as e:
            return _err(str(e))
        return _ok({"content": text})

    async def profile_save(request: Request):
        try:
            c = _ctx(request.path_params["user"])
        except KeyError:
            return _err("未知用户", 404)
        body = await _body(request)
        try:
            r = await run_in_threadpool(c["store"].update_preference,
                                        body.get("section", ""),
                                        body.get("content", ""))
        except Exception as e:
            return _err(str(e))
        return _ok(r)

    # ---- 配置管理（config.toml 在线编辑：用户 / embedding / curator）----

    async def config_get(request: Request):
        if not config.config_path or not Path(config.config_path).is_file():
            return _err("服务未使用配置文件启动（全部为默认值），无可编辑内容")
        text = await run_in_threadpool(Path(config.config_path).read_text,
                                       encoding="utf-8")
        return _ok({"path": config.config_path, "content": text})

    async def config_save(request: Request):
        if not config.config_path:
            return _err("服务未使用配置文件启动，无法保存")
        body = await _body(request)
        content = body.get("content", "")
        target = Path(config.config_path)

        # 1) TOML 语法 + 结构校验（写到临时文件走完整 load_config）
        try:
            tomllib.loads(content)
        except Exception as e:
            return _err(f"TOML 语法错误: {e}")
        tmp = target.with_suffix(".toml.validating")
        tmp.write_text(content, encoding="utf-8")
        try:
            from ..config import load_config as _load
            _load(str(tmp))
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return _err(f"配置校验失败: {e}")
        tmp.unlink(missing_ok=True)

        # 2) 备份 + 原子落盘（保留 600 权限）
        backup = None
        if target.exists():
            backup = f"{target.name}.bak-{time.strftime('%Y%m%d%H%M%S')}"
            await run_in_threadpool(shutil.copy2, target, target.parent / backup)
        await run_in_threadpool(target.write_text, content, encoding="utf-8")
        import os as _os
        _os.chmod(target, 0o600)

        # 3) 可选重启（systemd Restart 由 unit 决定；延迟 1.5s 让响应先送达）
        restarting = bool(body.get("restart"))
        if restarting:
            def _restart():
                time.sleep(1.5)
                subprocess.run(["systemctl", "restart", "yacmemo"], check=False)
            threading.Thread(target=_restart, daemon=True).start()
        return _ok({"saved": True, "backup": backup, "restarting": restarting})

    return [
        Route("/", index, methods=["GET"]),
        Route("/ui", ui_index, methods=["GET"]),
        Route("/ui/", ui_index, methods=["GET"]),
        *((Mount("/ui/assets", app=StaticFiles(directory=STATIC_DIR / "assets"),
                 name="assets"),) if (STATIC_DIR / "assets").is_dir() else ()),
        Route("/api/overview", overview, methods=["GET"]),
        Route("/api/usage", usage_recent, methods=["GET"]),
        Route("/api/usage/clients", usage_clients, methods=["GET"]),
        Route("/api/usage/days", usage_days, methods=["GET"]),
        Route("/api/{user}/notes", notes_list, methods=["GET"]),
        Route("/api/{user}/notes", note_create, methods=["POST"]),
        Route("/api/{user}/note", note_get, methods=["GET"]),
        Route("/api/{user}/note", note_save, methods=["PUT"]),
        Route("/api/{user}/note", note_delete, methods=["DELETE"]),
        Route("/api/{user}/search", search, methods=["GET"]),
        Route("/api/{user}/audit", audit, methods=["POST"]),
        Route("/api/{user}/audit/last", audit_last, methods=["GET"]),
        Route("/api/{user}/audit/runs", audit_runs, methods=["GET"]),
        Route("/api/{user}/audit/actions", audit_actions_list, methods=["GET"]),
        Route("/api/{user}/audit/action", audit_action, methods=["POST"]),
        Route("/api/{user}/proposal/action", proposal_action, methods=["POST"]),
        Route("/api/{user}/reindex", reindex, methods=["POST"]),
        Route("/api/{user}/collision", collision_resolve, methods=["POST"]),
        Route("/api/{user}/curator", curator_run, methods=["POST"]),
        Route("/api/{user}/proposals", proposals_list, methods=["GET"]),
        Route("/api/{user}/topics", topics_list, methods=["GET"]),
        Route("/api/{user}/topics/archive", topic_archive, methods=["POST"]),
        Route("/api/{user}/profile", profile_get, methods=["GET"]),
        Route("/api/{user}/profile", profile_save, methods=["PUT"]),
        Route("/api/config", config_get, methods=["GET"]),
        Route("/api/config", config_save, methods=["POST"]),
    ]
