"""yacmemo HTTP MCP server: one process, every user, every machine.

    yacmemo-server --config config.toml

Each user's memory mounts at `/{user_id}/mcp` (streamable HTTP, stateless).
Any MCP-capable agent on any machine connects with just a URL — nothing to
install client-side, no per-machine processes:

    http://debsvc.local:9721/yachen/mcp
    http://debsvc.local:9721/user2/mcp

Also serves `GET /health`. For a same-box stdio agent use `yacmemo-mcp`.
"""

from __future__ import annotations

import argparse
import contextlib
import logging

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from yacmemo.config import Config, UserEntry, load_config
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
logger = logging.getLogger("yacmemo.server")


def build_user_mcp(config: Config, user: UserEntry) -> FastMCP:
    """Build one FastMCP instance bound to a single user's memory root."""
    mcp = FastMCP(
        f"yacmemo-{user.id}",
        stateless_http=True,  # no session affinity — any client, any proxy
        host=config.server.host,
        port=config.server.port,
    )

    db = IndexDB(str(config.user_root_abs(user) / ".index" / "index.db"))
    emb = None
    vectors = None
    if config.embedding.base_url and config.embedding.model:
        emb = EmbeddingClient(
            base_url=config.embedding.base_url, api_key=config.embedding.api_key,
            model=config.embedding.model, dimensions=config.embedding.dimensions,
            timeout=config.embedding.timeout,
        )
        vectors = VectorStore(str(config.user_root_abs(user) / ".index" / "lancedb"),
                              config.embedding.dimensions)
    else:
        logger.warning("[%s] Embedding endpoint not configured — FTS-only.", user.id)

    store = Store(config, db, emb, vectors, root=config.user_root_abs(user))
    searcher = Searcher(config, db, emb, vectors)
    register_tools(mcp, store, searcher)
    return mcp


def create_app(config: Config) -> Starlette:
    servers = {u.id: build_user_mcp(config, u) for u in config.users}
    if not servers:
        raise SystemExit("config.toml 未定义任何 [[users]] — HTTP 服务至少需要一个用户")

    async def health(request):
        return JSONResponse({"status": "ok", "users": list(servers)})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # Sub-apps' lifespans don't run under Mount(); run each session
        # manager explicitly.
        async with contextlib.AsyncExitStack() as stack:
            for mcp in servers.values():
                await stack.enter_async_context(mcp.session_manager.run())
            yield

    routes = [Route("/health", health, methods=["GET"])]
    for uid, mcp in servers.items():
        routes.append(Mount(f"/{uid}", app=mcp.streamable_http_app()))

    logger.info("Mounted users: %s", [f"/{uid}/mcp" for uid in servers])
    return Starlette(routes=routes, lifespan=lifespan)


def main():
    parser = argparse.ArgumentParser(description="yacmemo HTTP MCP server")
    parser.add_argument("--config", default=None, help="Path to config.toml")
    args = parser.parse_args()

    config = load_config(args.config)
    app = create_app(config)
    logger.info("yacmemo HTTP server on %s:%d", config.server.host, config.server.port)
    uvicorn.run(app, host=config.server.host, port=config.server.port,
                log_level="info")


if __name__ == "__main__":
    main()
