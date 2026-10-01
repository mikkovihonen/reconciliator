"""Singleton Vector Index IPC Daemon process and CLI entry point."""

from __future__ import annotations

import argparse
import atexit
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Optional

from reconciliator.config import AppConfig, load_config
from reconciliator.indexer.ipc.handler import VectorDaemonRequestHandler
from reconciliator.indexer.ipc.pool import (
    IndexPoolManager,
    ManagedIndex,
    get_process_rss_mb,
)
from reconciliator.indexer.ipc.protocol import get_default_socket_path
from reconciliator.indexer.ipc.server import VectorDaemonServer

logger = logging.getLogger("reconciliator.indexer.ipc.daemon")


def run_daemon(
    socket_path: Path | str,
    config_path: Optional[Path | str] = None,
) -> None:
    """Run the standalone vector daemon process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cfg_p: Optional[Path] = Path(config_path) if config_path else None
    if cfg_p is None or not cfg_p.exists():
        if Path("config.yaml").exists():
            cfg_p = Path("config.yaml")
        elif Path("config.example.yaml").exists():
            cfg_p = Path("config.example.yaml")

    cfg = load_config(cfg_p) if cfg_p and cfg_p.exists() else AppConfig()
    sock_p = Path(socket_path)

    logger.info("Initializing Reconciliator Vector Daemon on socket: %s", sock_p)
    server = VectorDaemonServer(sock_p, config=cfg)

    def _sig_handler(sig: int, frame: Any) -> None:
        logger.info("Received signal %d; shutting down daemon.", sig)
        server.shutdown_gracefully()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    atexit.register(server.shutdown_gracefully)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        sock_p.unlink(missing_ok=True)
        logger.info("Vector Daemon terminated cleanly.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconciliator Singleton Vector Index Daemon")
    parser.add_argument(
        "--socket-path",
        type=str,
        default=None,
        help="Path to the Unix domain socket",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to configuration YAML file",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=None,
        help="Idle shutdown timeout in seconds override",
    )
    args = parser.parse_args()

    cfg_arg = args.config
    if cfg_arg is None:
        if Path("config.yaml").exists():
            cfg_arg = "config.yaml"
        elif Path("config.example.yaml").exists():
            cfg_arg = "config.example.yaml"

    sock = Path(args.socket_path) if args.socket_path else get_default_socket_path(Path.cwd())
    run_daemon(sock, config_path=cfg_arg)


__all__ = [
    "IndexPoolManager",
    "ManagedIndex",
    "VectorDaemonRequestHandler",
    "VectorDaemonServer",
    "get_process_rss_mb",
    "main",
    "run_daemon",
]


if __name__ == "__main__":
    main()
