"""yacmemo WebUI: admin panel — pure JSON API backend + SPA static file serving.

Mounts on the enhancer's FastAPI app as a sub-application at /admin.
Frontend is a Vue 3 + Naive UI SPA built from frontend/dist/.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from yacmemo.config import Config, UserConfig
from yacmemo.db import MemoryDB
from yacmemo.embedding import EmbeddingClient
from yacmemo.vector import VectorStore
from yacmemo.webui.auth import AdminAuthMiddleware

# Path to the built frontend (frontend/dist/)
_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


def create_webui_app(config: Config, db: MemoryDB) -> FastAPI:
    """Create the WebUI FastAPI sub-app (JSON API + SPA serving)."""
    app = FastAPI(title="yacmemo admin")

    # Auth middleware
    auth = AdminAuthMiddleware(app, config.webui.admin_token)
    app.middleware("http")(auth.dispatch)

    # ---- Helpers ----

    def _get_vector(user: UserConfig) -> VectorStore:
        return VectorStore(config.user_lancedb_abs(user), config.embedding.dimensions)

    def _get_emb() -> EmbeddingClient:
        return EmbeddingClient(
            base_url=config.embedding.base_url,
            api_key=config.embedding.api_key,
            model=config.embedding.model,
            dimensions=config.embedding.dimensions,
            timeout=config.embedding.timeout,
        )

    def _user_stats(user: UserConfig) -> dict:
        """Get stats for a user."""
        try:
            memory_root = config.user_memory_root_abs(user)

            md_count = 0
            for _dirpath, dirnames, filenames in os.walk(memory_root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                md_count += sum(1 for f in filenames if f.endswith(".md"))

            uid = user.id
            valid_nodes = db.conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE user_id=? AND valid=1", (uid,)
            ).fetchone()[0]
            total_nodes = db.conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE user_id=?", (uid,)
            ).fetchone()[0]
            total_events = db.conn.execute(
                "SELECT COUNT(*) FROM events WHERE user_id=?", (uid,)
            ).fetchone()[0]
            processed = db.conn.execute(
                "SELECT COUNT(*) FROM processed_files WHERE user_id=? "
                "AND status='success'", (uid,)
            ).fetchone()[0]
            failed = db.conn.execute(
                "SELECT COUNT(*) FROM processed_files WHERE user_id=? "
                "AND status='failed'", (uid,)
            ).fetchone()[0]
            pending = db.conn.execute(
                "SELECT COUNT(*) FROM consistency_log WHERE user_id=? "
                "AND status='pending'", (uid,)
            ).fetchone()[0]

            return {
                "id": user.id,
                "display_name": user.display_name,
                "md_files": md_count,
                "valid_nodes": valid_nodes,
                "total_nodes": total_nodes,
                "events": total_events,
                "processed": processed,
                "failed": failed,
                "pending_consistency": pending,
                "has_custom_key": bool(user.llm_api_key),
            }
        except Exception as e:
            return {
                "id": user.id,
                "display_name": user.display_name,
                "error": str(e),
            }

    # ---- Login ----

    @app.post("/api/login")
    async def do_login(token: str = Form(...)):
        if token == config.webui.admin_token:
            session = auth.create_session()
            resp = JSONResponse({"status": "ok"})
            resp.set_cookie("yacmemo_admin", session, httponly=True, max_age=86400)
            return resp
        return JSONResponse({"error": "invalid token"}, status_code=401)

    @app.get("/logout")
    async def logout(request: Request):
        cookie = request.cookies.get("yacmemo_admin")
        if cookie and cookie in auth._sessions:
            auth._sessions.discard(cookie)
        resp = RedirectResponse(url="/admin/login", status_code=302)
        resp.delete_cookie("yacmemo_admin")
        return resp

    # ---- API: Users ----

    @app.get("/api/users")
    async def api_list_users():
        return {"users": [_user_stats(u) for u in config.users]}

    @app.post("/api/users/{user_id}/trigger-scan")
    async def api_trigger_scan(user_id: str):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found") from None

        import httpx
        try:
            resp = httpx.post(
                f"http://{config.server.host}:{config.server.port}/trigger",
                json={"action": "consistency", "user_id": user.id},
                timeout=5,
            )
            return {"status": "ok", "detail": resp.json()}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/api/users")
    async def api_create_user(request: Request):
        """Create a new user. Accepts JSON body."""
        data = await request.json()
        uid = data.get("id", "")
        if not uid:
            raise HTTPException(400, "id is required")
        if db.get_user(uid):
            raise HTTPException(409, f"User already exists: {uid}")

        display_name = data.get("display_name") or uid
        memory_root = data.get("memory_root", "")
        if not memory_root:
            raise HTTPException(400, "memory_root is required")

        llm_api_key = data.get("llm_api_key", "")
        embedding_api_key = data.get("embedding_api_key", "")

        db.add_user(
            id=uid, display_name=display_name, memory_root=memory_root,
            llm_api_key=llm_api_key, embedding_api_key=embedding_api_key,
        )

        new_user = UserConfig(
            id=uid, display_name=display_name, memory_root=memory_root,
            llm_api_key=llm_api_key, embedding_api_key=embedding_api_key,
        )
        config.users.append(new_user)

        os.makedirs(config.user_memory_root_abs(new_user), exist_ok=True)
        os.makedirs(config.user_lancedb_abs(new_user), exist_ok=True)

        return {"status": "ok", "user": {"id": uid, "display_name": display_name}}

    @app.delete("/api/users/{user_id}")
    async def api_delete_user(user_id: str):
        if not db.get_user(user_id):
            raise HTTPException(404, f"User not found: {user_id}")

        db.remove_user(user_id)
        config.users = [u for u in config.users if u.id != user_id]

        return {"status": "ok", "deleted": user_id}

    # ---- API: Memory ----

    @app.get("/api/memory/{user_id}")
    async def api_memory(user_id: str):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found") from None

        memory_root = config.user_memory_root_abs(user)
        file_tree = _build_file_tree(memory_root, memory_root)

        nodes = db.conn.execute(
            "SELECT name, type, summary, source_path, valid, created_at "
            "FROM nodes WHERE user_id=? ORDER BY name",
            (user_id,)
        ).fetchall()
        events = db.conn.execute(
            "SELECT date, type, summary, source_path "
            "FROM events WHERE user_id=? ORDER BY date DESC LIMIT 100",
            (user_id,)
        ).fetchall()

        return {
            "file_tree": file_tree,
            "nodes": [dict(n) for n in nodes],
            "events": [dict(e) for e in events],
        }

    @app.get("/api/memory/{user_id}/file")
    async def api_read_file(user_id: str, path: str = ""):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found") from None

        memory_root = config.user_memory_root_abs(user)
        full_path = (
            os.path.join(memory_root, path) if not os.path.isabs(path) else path
        )

        if (not os.path.isfile(full_path)
                or not os.path.realpath(full_path).startswith(memory_root)):
            raise HTTPException(404, "File not found")

        try:
            with open(full_path, encoding="utf-8") as f:
                return {"content": f.read(), "path": path}
        except Exception as e:
            raise HTTPException(500, str(e)) from e

    @app.get("/api/memory/{user_id}/history/{entity_name}")
    async def api_entity_history(user_id: str, entity_name: str):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found") from None

        history = db.get_node_history(user.id, entity_name)
        return {"history": [dict(h) for h in history]}

    # ---- API: Search ----

    @app.post("/api/search/{user_id}")
    async def api_search(user_id: str, query: str = Form(...), limit: int = 10):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found") from None

        try:
            emb = _get_emb()
            vector = _get_vector(user)
            query_emb = emb.embed_one(query)
            results = vector.search_all(query_emb, limit=limit)
            # Strip large vector field from results
            for r in results:
                r.pop("vector", None)
            return {"results": results, "query": query}
        except Exception as e:
            return {"error": str(e), "results": []}

    @app.post("/api/grep/{user_id}")
    async def api_grep(user_id: str, pattern: str = Form(...)):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found") from None

        memory_root = config.user_memory_root_abs(user)
        try:
            result = subprocess.run(
                ["rg", "-n", "--no-heading", pattern, memory_root],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode not in (0, 1):
                return {"error": result.stderr, "matches": ""}
            return {"matches": result.stdout or "(no matches)"}
        except FileNotFoundError:
            return {"error": "ripgrep not installed", "matches": ""}
        except subprocess.TimeoutExpired:
            return {"error": "search timeout", "matches": ""}

    # ---- API: Consistency ----

    @app.get("/api/consistency/{user_id}")
    async def api_consistency(user_id: str):
        try:
            config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found") from None

        pending = db.get_pending_consistency(user_id)

        auto_logs = db.conn.execute(
            "SELECT * FROM consistency_log WHERE user_id=? "
            "AND auto_invalidated=1 ORDER BY checked_at DESC LIMIT 50",
            (user_id,)
        ).fetchall()

        invalidated = db.conn.execute(
            "SELECT name, type, summary, invalid_at, invalid_reason, source_path "
            "FROM nodes WHERE user_id=? AND valid=0 "
            "ORDER BY invalid_at DESC LIMIT 50",
            (user_id,)
        ).fetchall()

        return {
            "pending": [dict(p) for p in pending],
            "auto_logs": [dict(a) for a in auto_logs],
            "invalidated": [dict(i) for i in invalidated],
        }

    @app.post("/api/consistency/{user_id}/resolve/{log_id}")
    async def api_resolve_consistency(user_id: str, log_id: str, action: str = Form(...)):
        try:
            config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found") from None

        # Support short ID
        if len(log_id) < 32:
            pending = db.get_pending_consistency(user_id)
            match = [p for p in pending if p["id"].startswith(log_id)]
            if len(match) != 1:
                raise HTTPException(400, f"Cannot uniquely match log_id: {log_id}")
            log_id = match[0]["id"]

        if action == "confirm":
            log = db.conn.execute(
                "SELECT old_node_id FROM consistency_log "
                "WHERE id=? AND user_id=?",
                (log_id, user_id),
            ).fetchone()
            if log:
                db.invalidate_node(user_id, log["old_node_id"], "manual_confirmed")
            db.resolve_consistency(user_id, log_id, "manual_confirmed")
            msg = "Old fact invalidated."
        elif action == "dismiss":
            db.resolve_consistency(user_id, log_id, "manual_dismissed")
            msg = "Contradiction dismissed."
        else:
            raise HTTPException(400, f"Unknown action: {action}")

        return {"status": "ok", "message": msg}

    # ---- API: Split file integrity ----

    @app.get("/api/split-status/{user_id}")
    async def api_split_status(user_id: str):
        """Get split file integrity status for a user."""
        try:
            config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found") from None

        records = db.list_split_files(user_id)
        result = []
        for r in records:
            result.append({
                "path": r["path"],
                "status": r["status"],
                "updated_at": r["updated_at"],
            })

        # Also check for .conflict files on disk
        import glob as _glob
        user_obj = config.get_user(user_id)
        memory_root = config.user_memory_root_abs(user_obj)
        conflict_files = _glob.glob(
            os.path.join(memory_root, "**", "*.conflict.*"), recursive=True
        )
        conflicts = [os.path.relpath(cf, memory_root) for cf in conflict_files]

        return {"files": result, "conflicts": conflicts}

    # ---- API: Status ----

    @app.get("/api/status")
    async def api_status():
        """System status: LLM/embedding connectivity, cron, per-user index health."""
        llm_ok, llm_detail = _check_service(config.llm.base_url, config.llm.api_key)
        emb_ok, emb_detail = _check_service(
            config.embedding.base_url, config.embedding.api_key
        )

        user_indexes = []
        for user in config.users:
            try:
                uid = user.id
                sqlite_size = (
                    os.path.getsize(config.sqlite_abs)
                    if os.path.exists(config.sqlite_abs) else 0
                )
                node_count = db.conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE user_id=?", (uid,)
                ).fetchone()[0]
                event_count = db.conn.execute(
                    "SELECT COUNT(*) FROM events WHERE user_id=?", (uid,)
                ).fetchone()[0]
                processed = db.conn.execute(
                    "SELECT COUNT(*) FROM processed_files WHERE user_id=?", (uid,)
                ).fetchone()[0]
                failed = db.conn.execute(
                    "SELECT COUNT(*) FROM processed_files "
                    "WHERE user_id=? AND status='failed'", (uid,),
                ).fetchone()[0]
                recent = db.conn.execute(
                    "SELECT path, status, processed_at, split_file_count "
                    "FROM processed_files WHERE user_id=? "
                    "ORDER BY processed_at DESC LIMIT 5",
                    (uid,)
                ).fetchall()

                user_indexes.append({
                    "user_id": uid,
                    "sqlite_size": f"{sqlite_size / 1024:.0f} KB",
                    "nodes": node_count,
                    "events": event_count,
                    "processed": processed,
                    "failed": failed,
                    "recent": [dict(r) for r in recent],
                })
            except Exception as e:
                user_indexes.append({"user_id": uid, "error": str(e)})

        return {
            "llm_ok": llm_ok,
            "llm_detail": llm_detail,
            "llm_base_url": config.llm.base_url,
            "llm_model": config.llm.model,
            "emb_ok": emb_ok,
            "emb_detail": emb_detail,
            "emb_base_url": config.embedding.base_url,
            "emb_model": config.embedding.model,
            "user_indexes": user_indexes,
            "extract_cron": config.schedule.extract_cron,
            "consistency_cron": config.schedule.consistency_cron,
        }

    @app.get("/api/status/health")
    async def api_health():
        return {"status": "ok", "users": [u.id for u in config.users]}

    # ---- SPA static file serving ----

    if _FRONTEND_DIST.is_dir():
        # Mount static assets (js, css, images)
        assets_dir = _FRONTEND_DIST / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        # Fallback: serve index.html for all non-API routes (SPA routing)
        @app.get("/{path:path}")
        async def spa_fallback(path: str):
            # Try to serve a real file first
            file_path = _FRONTEND_DIST / path
            if file_path.is_file():
                return FileResponse(str(file_path))

            # Fallback to index.html for client-side routing
            index_path = _FRONTEND_DIST / "index.html"
            if index_path.is_file():
                return FileResponse(str(index_path))

            return JSONResponse({"error": "frontend not built"}, status_code=404)

    return app


def _check_service(base_url: str, api_key: str) -> tuple[bool, str]:
    """Check if a service endpoint is reachable."""
    try:
        import httpx
        resp = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        return resp.status_code == 200, f"{resp.status_code}"
    except Exception as e:
        return False, str(e)


def _build_file_tree(root: str, base: str) -> list[dict]:
    """Build a nested file tree structure for the API response."""
    result = []
    if not os.path.isdir(root):
        return result

    for entry in sorted(os.listdir(root)):
        if entry.startswith("."):
            continue
        full = os.path.join(root, entry)
        rel = os.path.relpath(full, base)

        if os.path.isdir(full):
            children = _build_file_tree(full, base)
            result.append({
                "name": entry,
                "path": rel,
                "type": "dir",
                "children": children,
            })
        elif entry.endswith(".md"):
            result.append({
                "name": entry,
                "path": rel,
                "type": "file",
            })

    return result
