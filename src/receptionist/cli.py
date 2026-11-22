"""Console entry point: `receptionist serve` and `receptionist chat`."""

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import uvicorn

from receptionist.app import create_app
from receptionist.config import get_settings
from receptionist.deps import build_deps
from receptionist.session import CallSession

# psycopg's async driver can't run on the Windows default ProactorEventLoop.
_WINDOWS = sys.platform == "win32"
UVICORN_LOOP = "asyncio:SelectorEventLoop" if _WINDOWS else "auto"


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    return asyncio.SelectorEventLoop if _WINDOWS else None


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro, loop_factory=loop_factory())


def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(build_deps(settings)),
        host="0.0.0.0",
        port=settings.port,
        loop=UVICORN_LOOP,
        timeout_graceful_shutdown=840,
    )


async def run_chat(
    session: Any,
    read_line: Callable[[], Awaitable[str | None]],
    write: Callable[[str], None],
) -> None:
    """Reads caller lines until EOF or the call ends; prints speech, with fillers in
    brackets, `[switch → hi]` and `[end]`."""
    while True:
        write("you> ")
        line = await read_line()
        if line is None:
            write("\n")
            return
        if not line.strip():
            continue
        write("receptionist> ")
        ended = False
        async for speech in session.respond(line):
            if speech.kind == "token":
                write(speech.text)
            elif speech.kind == "filler":
                write(f"[{speech.text}] ")
            elif speech.kind == "switch_language":
                write(f"[switch → {speech.lang}] ")
            elif speech.kind == "end":
                write(" [end]")
                ended = True
        write("\n")
        if ended:
            return


async def _read_stdin() -> str | None:
    line = await asyncio.to_thread(sys.stdin.readline)
    return line or None


def _write_stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


async def chat(raw_from: str, call_sid: str) -> None:
    deps = build_deps(get_settings())
    await deps.startup()
    try:
        session = await CallSession.start(call_sid, raw_from, deps)
        await run_chat(session, _read_stdin, _write_stdout)
    finally:
        await deps.shutdown()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="receptionist")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the HTTP/WebSocket server")
    chat_parser = sub.add_parser("chat", help="talk to the agent in text mode (stdin/stdout)")
    chat_parser.add_argument(
        "--from", dest="raw_from", required=True, help="caller ID, e.g. +919876543210"
    )
    chat_parser.add_argument("--call-sid", help="call id (default: cli-<uuid>)")
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve()
    elif args.command == "chat":
        for stream in (sys.stdout, sys.stdin):
            if hasattr(stream, "reconfigure"):  # Devanagari on a Windows console
                stream.reconfigure(encoding="utf-8")
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        run(chat(args.raw_from, args.call_sid or f"cli-{uuid.uuid4()}"))


if __name__ == "__main__":
    main()
