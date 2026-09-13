"""yacmemo WebUI: admin panel for user management, memory browsing,
search, consistency dashboard, and system status.

Mounts on the enhancer's FastAPI app as a sub-application at /admin.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from yacmemo.config import Config, UserConfig
from yacmemo.db import MemoryDB
from yacmemo.vector import VectorStore
from yacmemo.embedding import EmbeddingClient
from yacmemo.webui.auth import AdminAuthMiddleware

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Use jinja2.Environment directly (avoids Starlette 1.6 Jinja2Templates cache bug)
from jinja2 import Environment, FileSystemLoader, select_autoescape
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def create_webui_app(config: Config) -> FastAPI:
    """Create the WebUI FastAPI sub-app."""
    app = FastAPI(title="yacmemo admin")

    # Auth middleware
    auth = AdminAuthMiddleware(app, config.webui.admin_token)
    app.middleware("http")(auth.dispatch)

    # ---- Helper: get user components ----

    def _get_db(user: UserConfig) -> MemoryDB:
        return MemoryDB(config.user_sqlite_abs(user))

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

    def _render(template_name: str, request: Request, **context) -> HTMLResponse:
        """Render a Jinja2 template and return HTMLResponse.

        Automatically injects 'users' (for nav sidebar) into all templates.
        """
        if "users" not in context:
            context["users"] = [{"id": u.id, "display_name": u.display_name}
                                 for u in config.users]
        tmpl = _jinja_env.get_template(template_name)
        html = tmpl.render(request=request, **context)
        return HTMLResponse(content=html)

    def _user_stats(user: UserConfig) -> dict:
        """Get stats for a user."""
        try:
            db = _get_db(user)
            memory_root = config.user_memory_root_abs(user)

            # Count .md files
            md_count = 0
            for dirpath, dirnames, filenames in os.walk(memory_root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                md_count += sum(1 for f in filenames if f.endswith(".md"))

            # DB counts
            valid_nodes = db.conn.execute("SELECT COUNT(*) FROM nodes WHERE valid=1").fetchone()[0]
            total_nodes = db.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            total_events = db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            processed = db.conn.execute("SELECT COUNT(*) FROM processed_files WHERE status='success'").fetchone()[0]
            pending = db.conn.execute("SELECT COUNT(*) FROM consistency_log WHERE status='pending'").fetchone()[0]
            db.close()

            return {
                "id": user.id,
                "display_name": user.display_name,
                "md_files": md_count,
                "valid_nodes": valid_nodes,
                "total_nodes": total_nodes,
                "events": total_events,
                "processed": processed,
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

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if not config.webui.admin_token:
            return RedirectResponse(url="/admin/", status_code=302)
        return _render("login.html", request)

    @app.post("/api/login")
    async def do_login(token: str = Form(...)):
        if token == config.webui.admin_token:
            session = auth.create_session()
            resp = RedirectResponse(url="/admin/", status_code=302)
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

    # ---- Dashboard / Home ----

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        stats = [_user_stats(u) for u in config.users]
        return _render("dashboard.html", request,
            users=stats,
            total_users=len(config.users),
            total_nodes=sum(s.get("valid_nodes", 0) for s in stats),
            total_events=sum(s.get("events", 0) for s in stats),
            total_pending=sum(s.get("pending_consistency", 0) for s in stats),
        )

    # ---- Module 1: User Management ----

    @app.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request):
        stats = [_user_stats(u) for u in config.users]
        return _render("users.html", request,
            users=stats,
        )

    @app.get("/api/users")
    async def api_list_users():
        return {"users": [_user_stats(u) for u in config.users]}

    @app.post("/api/users/{user_id}/trigger-scan")
    async def api_trigger_scan(user_id: str):
        """Trigger extraction scan for a user via webhook."""
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found")

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

    # ---- Module 2: Memory Browser ----

    @app.get("/memory/{user_id}", response_class=HTMLResponse)
    async def memory_browser(request: Request, user_id: str):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found")

        memory_root = config.user_memory_root_abs(user)
        file_tree = _build_file_tree(memory_root, memory_root)

        db = _get_db(user)
        nodes = db.conn.execute(
            "SELECT name, type, summary, source_path, valid, created_at FROM nodes ORDER BY name"
        ).fetchall()
        events = db.conn.execute(
            "SELECT date, type, summary, source_path FROM events ORDER BY date DESC LIMIT 100"
        ).fetchall()
        db.close()

        return _render("memory.html", request,
            user=user,
            file_tree=file_tree,
            nodes=[dict(n) for n in nodes],
            events=[dict(e) for e in events],
        )

    @app.get("/api/memory/{user_id}/file")
    async def api_read_file(user_id: str, path: str = ""):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found")

        memory_root = config.user_memory_root_abs(user)
        full_path = os.path.join(memory_root, path) if not os.path.isabs(path) else path

        if not os.path.isfile(full_path) or not os.path.realpath(full_path).startswith(memory_root):
            raise HTTPException(404, "File not found")

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                return {"content": f.read(), "path": path}
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.get("/api/memory/{user_id}/history/{entity_name}")
    async def api_entity_history(user_id: str, entity_name: str):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found")

        db = _get_db(user)
        history = db.get_node_history(entity_name)
        db.close()
        return {"history": [dict(h) for h in history]}

    # ---- Module 3: Search ----

    @app.get("/search/{user_id}", response_class=HTMLResponse)
    async def search_page(request: Request, user_id: str):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found")

        return _render("search.html", request,
            user=user,
        )

    @app.post("/api/search/{user_id}")
    async def api_search(user_id: str, query: str = Form(...), limit: int = 10):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found")

        try:
            emb = _get_emb()
            vector = _get_vector(user)
            query_emb = emb.embed_one(query)
            results = vector.search_all(query_emb, limit=limit)
            return {"results": results, "query": query}
        except Exception as e:
            return {"error": str(e), "results": []}

    @app.post("/api/grep/{user_id}")
    async def api_grep(user_id: str, pattern: str = Form(...)):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found")

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

    # ---- Module 4: Consistency Dashboard ----

    @app.get("/consistency/{user_id}", response_class=HTMLResponse)
    async def consistency_page(request: Request, user_id: str):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found")

        db = _get_db(user)
        pending = db.get_pending_consistency()

        # Also get auto-invalidated history
        auto_logs = db.conn.execute(
            "SELECT * FROM consistency_log WHERE auto_invalidated=1 ORDER BY checked_at DESC LIMIT 50"
        ).fetchall()

        # Invalidated nodes
        invalidated = db.conn.execute(
            "SELECT name, type, summary, invalid_at, invalid_reason, source_path "
            "FROM nodes WHERE valid=0 ORDER BY invalid_at DESC LIMIT 50"
        ).fetchall()
        db.close()

        return _render("consistency.html", request,
            user=user,
            pending=[dict(p) for p in pending],
            auto_logs=[dict(a) for a in auto_logs],
            invalidated=[dict(i) for i in invalidated],
        )

    @app.post("/api/consistency/{user_id}/resolve/{log_id}")
    async def api_resolve_consistency(user_id: str, log_id: str, action: str = Form(...)):
        try:
            user = config.get_user(user_id)
        except ValueError:
            raise HTTPException(404, "User not found")

        db = _get_db(user)

        # Support short ID
        if len(log_id) < 32:
            pending = db.get_pending_consistency()
            match = [p for p in pending if p["id"].startswith(log_id)]
            if len(match) != 1:
                raise HTTPException(400, f"Cannot uniquely match log_id: {log_id}")
            log_id = match[0]["id"]

        if action == "confirm":
            log = db.conn.execute(
                "SELECT old_node_id FROM consistency_log WHERE id=?", (log_id,)
            ).fetchone()
            if log:
                db.invalidate_node(log["old_node_id"], "manual_confirmed")
            db.resolve_consistency(log_id, "manual_confirmed")
            msg = "Old fact invalidated."
        elif action == "dismiss":
            db.resolve_consistency(log_id, "manual_dismissed")
            msg = "Contradiction dismissed."
        else:
            db.close()
            raise HTTPException(400, f"Unknown action: {action}")

        db.close()
        return {"status": "ok", "message": msg}

    # ---- Module 5: System Status ----

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        # Check LLM connectivity
        llm_ok = False
        llm_detail = ""
        try:
            import httpx
            resp = httpx.get(
                f"{config.llm.base_url}/models",
                headers={"Authorization": f"Bearer {config.llm.api_key}"},
                timeout=5,
            )
            llm_ok = resp.status_code == 200
            llm_detail = f"{resp.status_code}"
        except Exception as e:
            llm_detail = str(e)

        # Check embedding connectivity
        emb_ok = False
        emb_detail = ""
        try:
            import httpx
            resp = httpx.get(
                f"{config.embedding.base_url}/models",
                headers={"Authorization": f"Bearer {config.embedding.api_key}"},
                timeout=5,
            )
            emb_ok = resp.status_code == 200
            emb_detail = f"{resp.status_code}"
        except Exception as e:
            emb_detail = str(e)

        # Per-user index stats
        user_indexes = []
        for user in config.users:
            try:
                sqlite_path = config.user_sqlite_abs(user)
                lancedb_path = config.user_lancedb_abs(user)
                sqlite_size = os.path.getsize(sqlite_path) if os.path.exists(sqlite_path) else 0

                db = _get_db(user)
                node_count = db.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                event_count = db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                processed = db.conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0]
                failed = db.conn.execute("SELECT COUNT(*) FROM processed_files WHERE status='failed'").fetchone()[0]
                # Recent extractions
                recent = db.conn.execute(
                    "SELECT path, status, processed_at, split_file_count FROM processed_files "
                    "ORDER BY processed_at DESC LIMIT 5"
                ).fetchall()
                db.close()

                user_indexes.append({
                    "user_id": user.id,
                    "sqlite_size": f"{sqlite_size / 1024:.0f} KB",
                    "nodes": node_count,
                    "events": event_count,
                    "processed": processed,
                    "failed": failed,
                    "recent": [dict(r) for r in recent],
                })
            except Exception as e:
                user_indexes.append({"user_id": user.id, "error": str(e)})

        return _render("status.html", request,
            llm_ok=llm_ok,
            llm_detail=llm_detail,
            llm_base_url=config.llm.base_url,
            llm_model=config.llm.model,
            emb_ok=emb_ok,
            emb_detail=emb_detail,
            emb_base_url=config.embedding.base_url,
            emb_model=config.embedding.model,
            user_indexes=user_indexes,
            extract_cron=config.schedule.extract_cron,
            consistency_cron=config.schedule.consistency_cron,
        )

    @app.get("/api/status")
    async def api_status():
        """JSON API for health checks."""
        return {"status": "ok", "users": [u.id for u in config.users]}

    return app


def _build_file_tree(root: str, base: str) -> list[dict]:
    """Build a nested file tree structure for Jinja2 rendering."""
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
