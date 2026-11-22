"""Console entry point: `receptionist serve` (chat is added later)."""

import argparse

import uvicorn

from receptionist.app import create_app
from receptionist.config import get_settings


def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=settings.port,
        timeout_graceful_shutdown=840,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="receptionist")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the HTTP/WebSocket server")
    args = parser.parse_args()
    if args.command == "serve":
        serve()


if __name__ == "__main__":
    main()
