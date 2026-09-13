"""yacmemo stdio MCP server (single user, same-box agents).

    yacmemo-mcp --root /srv/yacmemo/yachen/memory

For agents on other machines use the HTTP server instead (`yacmemo-server`),
which serves every user at /{user_id}/mcp — nothing to install client-side.
Both entries share the same tool surface (yacmemo.tools.register_tools).
"""

from __future__ import annotations

import argparse
import logging

from mcp.server.fastmcp import FastMCP

from yacmemo.config import load_config
from yacmemo.embedding import EmbeddingClient
from yacmemo.index_db import IndexDB
from yacmemo.search import Searcher
from yacmemo.store import Store
from yacmemo.tools import register_tools
from yacmemo.vector import VectorStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("yacmemo.mcp")


def main():
    parser = argparse.ArgumentParser(description="yacmemo stdio MCP server (single user)")
    parser.add_argument("--config", default=None, help="Path to config.toml")
    parser.add_argument("--root", default=None,
                        help="Memory root directory (overrides config)")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.root:
        config.memory.root = args.root

    db = IndexDB(config.sqlite_path)
    emb = None
    vectors = None
    if config.embedding.base_url and config.embedding.model:
        emb = EmbeddingClient(
            base_url=config.embedding.base_url, api_key=config.embedding.api_key,
            model=config.embedding.model, dimensions=config.embedding.dimensions,
            timeout=config.embedding.timeout,
        )
        vectors = VectorStore(config.lancedb_path, config.embedding.dimensions)
    else:
        logger.warning("Embedding endpoint not configured — running FTS-only.")

    store = Store(config, db, emb, vectors)
    searcher = Searcher(config, db, emb, vectors)

    mcp = FastMCP("yacmemo")
    register_tools(mcp, store, searcher)

    logger.info("yacmemo MCP server starting (root=%s, stdio transport)",
                config.root_abs)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
