"""yacmemo enhancer: extraction + consistency service (cron + webhook).

Supports multiple users — each with isolated memory directory and index.
Cron scans all users; webhook triggers extraction for a specific user.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

from yacmemo.config import load_config, Config, UserConfig
from yacmemo.db import MemoryDB
from yacmemo.vector import VectorStore
from yacmemo.embedding import EmbeddingClient
from yacmemo.llm import LLMClient
from yacmemo.extractor import Extractor
from yacmemo.consistency import ConsistencyChecker
from yacmemo.fs_utils import list_md_files

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("yacmemo.enhancer")


class TriggerRequest(BaseModel):
    action: str              # "extract" or "consistency"
    path: str | None = None
    user_id: str | None = None


@dataclass
class UserResolvedConfig:
    """Per-user resolved config passed to Extractor/Checker.

    Extractor/Checker access these attributes: memory_root_abs, llm, embedding, consistency.
    """
    memory_root_abs: str
    sqlite_abs: str
    lancedb_abs: str
    llm: object
    embedding: object
    consistency: object


def _build_for_user(config: Config, user: UserConfig):
    """Create (extractor, checker) for a specific user with isolated data paths."""
    memory_root = config.user_memory_root_abs(user)
    sqlite_path = config.user_sqlite_abs(user)
    lancedb_path = config.user_lancedb_abs(user)

    Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    Path(lancedb_path).parent.mkdir(parents=True, exist_ok=True)

    db = MemoryDB(sqlite_path)
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
        sqlite_abs=sqlite_path,
        lancedb_abs=lancedb_path,
        llm=llm_cfg,
        embedding=emb_cfg,
        consistency=config.consistency,
    )

    extractor = Extractor(user_cfg, db, vector, llm, emb)
    checker = ConsistencyChecker(user_cfg, db, vector, llm, emb)
    return extractor, checker


def scan_user(config: Config, user: UserConfig):
    """Scan all .md files for one user."""
    try:
        extractor, checker = _build_for_user(config, user)
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
    except Exception as e:
        logger.error("[%s] Scan failed: %s", user.id, e)


def scan_all(config: Config):
    """Scan all users."""
    for user in config.users:
        scan_user(config, user)


def create_app(config: Config) -> FastAPI:
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
            md_path = req.path if os.path.isabs(req.path) else os.path.join(memory_root, req.path)

            if not os.path.isfile(md_path):
                return {"status": "error", "error": "File not found"}

            def _do():
                try:
                    extractor, checker = _build_for_user(config, user)
                    extractor.process_file(md_path)
                    checker.check_all()
                except Exception as e:
                    logger.error("[%s] Extract+check failed for %s: %s", user.id, md_path, e)

            threading.Thread(target=_do, daemon=True).start()
            return {"status": "accepted", "user": user.id}

        elif req.action == "consistency":
            def _do():
                try:
                    _, checker = _build_for_user(config, user)
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
                        help="Initialize: run full extraction + consistency for all users")
    parser.add_argument("--scan", action="store_true",
                        help="Run one-time scan and exit (no server)")
    parser.add_argument("--user", default=None,
                        help="Limit scan/init to a specific user")
    args = parser.parse_args()

    config = load_config(args.config)

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
            scan_user(config, user)
        logger.info("Done")
        return

    if args.init:
        logger.info("Initial full extraction for %d user(s)", len(users_to_process))
        for user in users_to_process:
            scan_user(config, user)
        logger.info("Initialization complete")

    app = create_app(config)

    scheduler = BackgroundScheduler()

    def _parse_cron(cron_str: str) -> dict:
        parts = cron_str.split()
        return {
            "minute": parts[0], "hour": parts[1],
            "day": parts[2], "month": parts[3], "day_of_week": parts[4],
        }

    scheduler.add_job(
        lambda: scan_all(config),
        "cron", **_parse_cron(config.schedule.extract_cron), id="extract_cron",
    )
    scheduler.add_job(
        lambda: scan_all(config),
        "cron", **_parse_cron(config.schedule.consistency_cron), id="consistency_cron",
    )
    scheduler.start()
    logger.info("Cron started: extract=%s, consistency=%s",
                config.schedule.extract_cron, config.schedule.consistency_cron)

    logger.info("Starting webhook server on %s:%d (%d users: %s)",
                config.server.host, config.server.port,
                len(config.users), [u.id for u in config.users])
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")


if __name__ == "__main__":
    main()
