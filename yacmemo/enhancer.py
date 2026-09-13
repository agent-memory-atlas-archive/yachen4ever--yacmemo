"""yacmemo enhancer: extraction + consistency service (cron + webhook).

System-level SQLite with user isolation. Users are synced from config.toml
into the database on startup; runtime user additions via WebUI are persistent.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
from dataclasses import dataclass

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from pydantic import BaseModel

from yacmemo.config import Config, UserConfig, load_config
from yacmemo.consistency import ConsistencyChecker
from yacmemo.db import MemoryDB
from yacmemo.embedding import EmbeddingClient
from yacmemo.extractor import Extractor
from yacmemo.fs_utils import is_in_split_dir, list_md_files
from yacmemo.llm import LLMClient
from yacmemo.vector import VectorStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("yacmemo.enhancer")


class TriggerRequest(BaseModel):
    action: str
    path: str | None = None
    user_id: str | None = None


@dataclass
class UserResolvedConfig:
    """Per-user resolved config passed to Extractor/Checker."""
    memory_root_abs: str
    sqlite_abs: str
    lancedb_abs: str
    llm: object
    embedding: object
    consistency: object


def _build_for_user(config: Config, user: UserConfig, db: MemoryDB):
    """Create (extractor, checker) for a specific user."""
    memory_root = config.user_memory_root_abs(user)
    lancedb_path = config.user_lancedb_abs(user)

    os.makedirs(lancedb_path, exist_ok=True)

    vector = VectorStore(lancedb_path, config.embedding.dimensions)

    llm_cfg = config.resolve_llm(user)
    emb_cfg = config.resolve_embedding(user)

    emb = EmbeddingClient(
        base_url=emb_cfg.base_url, api_key=emb_cfg.api_key,
        model=emb_cfg.model, dimensions=emb_cfg.dimensions, timeout=emb_cfg.timeout,
    )
    llm = LLMClient(
        base_url=llm_cfg.base_url, api_key=llm_cfg.api_key,
        model=llm_cfg.model, enable_thinking=llm_cfg.enable_thinking,
        response_format=llm_cfg.response_format, timeout=llm_cfg.extract_timeout,
    )

    user_cfg = UserResolvedConfig(
        memory_root_abs=memory_root,
        sqlite_abs=config.sqlite_abs,
        lancedb_abs=lancedb_path,
        llm=llm_cfg,
        embedding=emb_cfg,
        consistency=config.consistency,
    )

    # Patch db methods to bind user_id automatically
    extractor = _UserExtractor(user_cfg, user.id, db, vector, llm, emb)
    checker = _UserChecker(user_cfg, user.id, db, vector, llm, emb)
    return extractor, checker


class _UserExtractor:
    """Wraps Extractor with user_id bound to all db calls."""

    def __init__(self, cfg, user_id, db, vector, llm, emb):
        self._user_id = user_id
        self._db = db
        self._vector = vector
        self._llm = llm
        self._emb = emb
        self._inner = Extractor(cfg, db, vector, llm, emb)

    def process_file(self, md_path: str):
        return self._inner.process_file(self._user_id, md_path)


class _UserChecker:
    """Wraps ConsistencyChecker with user_id bound to all db calls."""

    def __init__(self, cfg, user_id, db, vector, llm, emb):
        self._user_id = user_id
        self._db = db
        self._vector = vector
        self._llm = llm
        self._emb = emb
        self._inner = ConsistencyChecker(cfg, db, vector, llm, emb)

    def check_new_node(self, node_id: str):
        self._inner.check_new_node(self._user_id, node_id)

    def check_all(self):
        self._inner.check_all(self._user_id)


def check_split_integrity(config: Config, user: UserConfig, db: MemoryDB):
    """Check split file integrity for a user during cron scan."""
    memory_root = config.user_memory_root_abs(user)
    split_records = db.list_split_files(user.id)
    issues = []

    for rec in split_records:
        path = rec["path"]
        full_path = os.path.join(memory_root, path)

        if not os.path.isfile(full_path):
            # File was deleted by user
            if rec["status"] != "stale":
                db.update_split_file_status(user.id, path, "stale")
                issues.append({"path": path, "status": "stale",
                               "message": "拆分文件被删除"})
            continue

        # File exists — check hash
        from yacmemo.fs_utils import file_hash
        disk_hash = file_hash(full_path)
        if disk_hash != rec["content_hash"] and rec["status"] == "active":
            db.update_split_file_status(user.id, path, "user_edited")
            issues.append({"path": path, "status": "user_edited",
                           "message": "检测到用户手动修改"})

    return issues


def scan_user(config: Config, user: UserConfig, db: MemoryDB):
    """Scan all .md files for one user."""
    try:
        extractor, checker = _build_for_user(config, user, db)
        memory_root = config.user_memory_root_abs(user)
        md_files = list_md_files(memory_root)
        logger.info("[%s] Scanning %d .md files", user.id, len(md_files))

        for md_path in md_files:
            try:
                result = extractor.process_file(md_path)
                if result.status == "failed":
                    logger.error("[%s] Failed: %s — %s", user.id, result.path, result.errors)
            except Exception as e:
                logger.error("[%s] Error processing %s: %s", user.id, md_path, e)

        checker.check_all()

        # Check split file integrity
        issues = check_split_integrity(config, user, db)
        for issue in issues:
            logger.warning("[%s] Split file issue: %s — %s",
                           user.id, issue["path"], issue["message"])
    except Exception as e:
        logger.error("[%s] Scan failed: %s", user.id, e)


def scan_all(config: Config, db: MemoryDB):
    """Scan all users."""
    for user in config.users:
        scan_user(config, user, db)


def create_app(config: Config, db: MemoryDB) -> FastAPI:
    app = FastAPI(title="yacmemo enhancer")

    @app.post("/trigger")
    async def trigger(req: TriggerRequest):
        if not req.user_id:
            return {"status": "error", "error": "user_id required"}

        try:
            user = config.get_user(req.user_id)
        except ValueError:
            return {"status": "error", "error": f"Unknown user: {req.user_id}"}

        if req.action == "extract" and req.path:
            memory_root = config.user_memory_root_abs(user)
            md_path = (
                req.path if os.path.isabs(req.path)
                else os.path.join(memory_root, req.path)
            )

            if not os.path.isfile(md_path):
                return {"status": "error", "error": "File not found"}

            if is_in_split_dir(md_path, memory_root):
                return {"status": "error",
                        "error": "Cannot process split directory file"}

            def _do():
                try:
                    extractor, checker = _build_for_user(config, user, db)
                    extractor.process_file(md_path)
                    checker.check_all()
                except Exception as e:
                    logger.error("[%s] Extract+check failed for %s: %s", user.id, md_path, e)

            threading.Thread(target=_do, daemon=True).start()
            return {"status": "accepted", "user": user.id}

        elif req.action == "consistency":
            def _do():
                try:
                    _, checker = _build_for_user(config, user, db)
                    checker.check_all()
                except Exception as e:
                    logger.error("[%s] Consistency check failed: %s", user.id, e)

            threading.Thread(target=_do, daemon=True).start()
            return {"status": "accepted", "user": user.id}

        return {"status": "unknown_action"}

    @app.get("/health")
    async def health():
        return {"status": "ok", "users": [u.id for u in config.users]}

    return app


def main():
    parser = argparse.ArgumentParser(description="yacmemo enhancer service")
    parser.add_argument("--config", default=None, help="Path to config.toml")
    parser.add_argument("--init", action="store_true",
                        help="Initialize: sync users to DB, run full extraction for all users")
    parser.add_argument("--scan", action="store_true",
                        help="Run one-time scan and exit (no server)")
    parser.add_argument("--user", default=None,
                        help="Limit scan/init to a specific user")
    args = parser.parse_args()

    config = load_config(args.config)

    # System-level SQLite
    db = MemoryDB(config.sqlite_abs)

    # Sync users from config.toml to database
    config.users = config.sync_users_to_db(db)
    logger.info("Loaded %d users from DB: %s", len(config.users), [u.id for u in config.users])

    if args.user:
        try:
            users_to_process = [config.get_user(args.user)]
        except ValueError as e:
            logger.error(str(e))
            return
    else:
        users_to_process = config.users

    if args.scan:
        logger.info("Running one-time scan for %d user(s)", len(users_to_process))
        for user in users_to_process:
            scan_user(config, user, db)
        logger.info("Done")
        return

    if args.init:
        logger.info("Initial full extraction for %d user(s)", len(users_to_process))
        for user in users_to_process:
            scan_user(config, user, db)
        logger.info("Initialization complete")

    app = create_app(config, db)

    # Mount WebUI if enabled
    if config.webui.enabled:
        from yacmemo.webui.app import create_webui_app
        webui_app = create_webui_app(config, db)
        app.mount("/admin", webui_app)
        logger.info("WebUI mounted at /admin (port %d)", config.server.port)

    scheduler = BackgroundScheduler()

    def _parse_cron(cron_str: str) -> dict:
        parts = cron_str.split()
        return {
            "minute": parts[0], "hour": parts[1],
            "day": parts[2], "month": parts[3], "day_of_week": parts[4],
        }

    scheduler.add_job(
        lambda: scan_all(config, db),
        "cron", **_parse_cron(config.schedule.extract_cron), id="extract_cron",
    )
    scheduler.add_job(
        lambda: scan_all(config, db),
        "cron", **_parse_cron(config.schedule.consistency_cron), id="consistency_cron",
    )
    scheduler.start()
    logger.info("Cron started: extract=%s, consistency=%s",
                config.schedule.extract_cron, config.schedule.consistency_cron)

    logger.info("Starting server on %s:%d (%d users: %s)",
                config.server.host, config.server.port,
                len(config.users), [u.id for u in config.users])
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")


if __name__ == "__main__":
    main()
