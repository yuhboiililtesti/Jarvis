"""``python -m jarvis.nodes`` -- run the satellite node agent on the ThinkPad.

This is the entry point the systemd ``jarvis-node.service`` unit invokes. It
loads config, resolves the shared secret, prints the bind/health address and
runs the FastAPI app (via uvicorn) or the stdlib fallback server.

The shared token is never printed or logged.
"""

from __future__ import annotations

import argparse

from ..config import load_config
from ..logging_setup import get_logger, setup_logging
from ..version import VERSION
from .agent import run_agent
from .protocol import DEFAULT_NODE_PORT

log = get_logger("jarvis.nodes.__main__")

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis.nodes",
        description="Run the JARVIS satellite node agent (Core relays tasks here).",
    )
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"bind port (default: config node_port / {DEFAULT_NODE_PORT})")
    args = parser.parse_args(argv)

    cfg = load_config()
    try:
        setup_logging(cfg.log_level, cfg.data_path / "logs")
    except Exception as exc:  # pragma: no cover - logging must never block startup
        log.warning("could not set up file logging (%s); continuing", type(exc).__name__)

    port = int(args.port if args.port is not None else cfg.server.node_port)
    print(f"JARVIS node {VERSION} -> http://{args.host}:{port}/node/health")
    run_agent(cfg, host=args.host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
