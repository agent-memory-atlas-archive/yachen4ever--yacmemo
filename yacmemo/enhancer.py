"""yacmemo enhancer: extraction + consistency service (cron + webhook)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

from yacmemo.config import load_config
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
    action: str  # "extract" or "consistency"
    path: str | None = None


def create_app(config, extractor: Extractor, checker: ConsistencyChecker) -> FastAPI:
    app = FastAPI(title="yacmemo enhancer")

    @app.post("/trigger")
    async def trigger(req: TriggerRequest):
        if req.action == "extract" and req.path:
            memory_root = config.memory_root_abs
            md_path = req.path if os.path.isabs(req.path) else os.path.join(memory_root, req.path)

            if not os.path.isfile(md_path):
                return {"status": "error", "error": "File not found"}

            def _do():
                try:
                    extractor.process_file(md_path)
                    checker.check_all()
                except Exception as e:
                    logger.error("Extract+check failed for %s: %s", md_path, e)

            threading.Thread(target=_do, daemon=True).start()
            return {"status": "accepted"}

        elif req.action == "consistency":
            def _do():
                checker.check_all()
            threading.Thread(target=_do, daemon=True).start()
            return {"status": "accepted"}

        return {"status": "unknown_action"}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


def scan_all(extractor: Extractor):
    """Scan all .md files and process changed ones."""
    memory_root = extractor.config.memory_root_abs
    md_files = list_md_files(memory_root)
    logger.info("Scanning %d .md files", len(md_files))

    for md_path in md_files:
        try:
            result = extractor.process_file(md_path)
            if result.status == "failed":
                logger.error("Failed: %s — %s", result.path, result.errors)
        except Exception as e:
            logger.error("Error processing %s: %s", md_path, e)


def main():
    parser = argparse.ArgumentParser(description="yacmemo enhancer service")
    parser.add_argument("--config", default=None, help="Path to config.toml")
    parser.add_argument("--init", action="store_true",
                        help="Initialize: run full extraction + consistency scan then start service")
    parser.add_argument("--scan", action="store_true",
                        help="Run one-time scan and exit (no server)")
    args = parser.parse_args()

    config = load_config(args.config)

    Path(config.sqlite_abs).parent.mkdir(parents=True, exist_ok=True)
    Path(config.lancedb_abs).parent.mkdir(parents=True, exist_ok=True)

    db = MemoryDB(config.sqlite_abs)
    vector = VectorStore(config.lancedb_abs, config.embedding.dimensions)
    emb = EmbeddingClient(
        base_url=config.embedding.base_url,
        api_key=config.embedding.api_key,
        model=config.embedding.model,
        dimensions=config.embedding.dimensions,
        timeout=config.embedding.timeout,
    )
    llm = LLMClient(
        base_url=config.llm.base_url,
        api_key=config.llm.api_key,
        model=config.llm.model,
        enable_thinking=config.llm.enable_thinking,
        response_format=config.llm.response_format,
        timeout=config.llm.extract_timeout,
    )
    extractor = Extractor(config, db, vector, llm, emb)
    checker = ConsistencyChecker(config, db, vector, llm, emb)

    if args.scan:
        logger.info("Running one-time scan")
        scan_all(extractor)
        checker.check_all()
        logger.info("Done")
        return

    if args.init:
        logger.info("Initial full extraction scan")
        scan_all(extractor)
        logger.info("Initial consistency check")
        checker.check_all()
        logger.info("Initialization complete")

    app = create_app(config, extractor, checker)

    scheduler = BackgroundScheduler()

    def _parse_cron(cron_str: str) -> dict:
        parts = cron_str.split()
        return {
            "minute": parts[0],
            "hour": parts[1],
            "day": parts[2],
            "month": parts[3],
            "day_of_week": parts[4],
        }

    scheduler.add_job(
        lambda: scan_all(extractor),
        "cron",
        **_parse_cron(config.schedule.extract_cron),
        id="extract_cron",
    )
    scheduler.add_job(
        lambda: checker.check_all(),
        "cron",
        **_parse_cron(config.schedule.consistency_cron),
        id="consistency_cron",
    )
    scheduler.start()
    logger.info("Cron started: extract=%s, consistency=%s",
                config.schedule.extract_cron, config.schedule.consistency_cron)

    logger.info("Starting webhook server on %s:%d", config.server.host, config.server.port)
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")


if __name__ == "__main__":
    main()
